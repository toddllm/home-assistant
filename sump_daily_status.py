#!/usr/bin/env python3
"""
Once-a-day sump system status digest, sent from the always-on primary (toddllm).

Rolls up: live Shelly/pump state, primary-host service health (monitor +
guardian + smart-assess), the latest assess verdict, and a single best-effort
reachability check of the Mac cold standby.

Deliberately low-key about the Mac: the Mac is a laptop that sleeps and leaves
the LAN. We do ONE best-effort ping and report it as information only -- we
never alarm or retry on "Mac offline". Per the operator's instruction: do not
poll the standby, just include it in the daily note.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from shelly_discovery import find_shelly_ip

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
MONITOR_STATE = Path("/var/tmp/sump_pump_state.json")
MONITOR_HEARTBEAT = Path("/var/tmp/sump_pump_monitor.heartbeat")
ASSESS_LOG = ROOT / "sump_assess.log"
GUARDIAN_LOG = ROOT / "sump_pump_guardian.log"

STANDBY_HOST = os.environ.get("SUMP_STANDBY_HOST", "Todds-MacBook-Pro.local")


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env()

SHELLY_MAC = os.environ.get("SHELLY_MAC", "841FE8F85BBC")
SHELLY_IP = os.environ.get("SHELLY_IP", "")
SHELLY_SCAN_CIDR = os.environ.get("SHELLY_SCAN_CIDR", "192.168.68.0/24")
SHELLY_LAST_IP_CACHE = os.environ.get("SHELLY_LAST_IP_CACHE", "/var/tmp/shelly_last_ip")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAILS = [e.strip() for e in os.environ.get("NOTIFY_EMAIL", GMAIL_USER).split(",") if e.strip()]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


def rpc(host: str, method: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"http://{host}/rpc/{method}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def svc_active(unit: str) -> str:
    try:
        out = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or out.stderr.strip() or "unknown"
    except Exception:
        return "unknown"


def last_log_line(path: Path) -> str:
    try:
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        return lines[-1] if lines else "(empty)"
    except OSError:
        return "(no log)"


def ping(host: str) -> bool:
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True, timeout=6,
        ).returncode == 0
    except Exception:
        return False


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds/60:.0f}m"
    return f"{seconds/3600:.1f}h"


def gather() -> dict:
    now = time.time()
    info: dict = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    host = find_shelly_ip(
        expected_mac=SHELLY_MAC,
        candidates=[SHELLY_IP] if SHELLY_IP else None,
        cidr=SHELLY_SCAN_CIDR,
        cache_path=SHELLY_LAST_IP_CACHE,
    )
    info["shelly_ip"] = host
    if host:
        try:
            sw = rpc(host, "Switch.GetStatus?id=0")
            info["output"] = sw.get("output")
            info["power"] = float(sw.get("apower", 0) or 0)
            info["temp_c"] = float(sw.get("temperature", {}).get("tC", 0) or 0)
            info["voltage"] = float(sw.get("voltage", 0) or 0)
        except Exception as e:
            info["shelly_error"] = str(e)

    try:
        st = json.loads(MONITOR_STATE.read_text())
        info["state"] = st.get("state")
        info["cycles"] = st.get("cycle_count")
        lr = st.get("last_pump_run_wall")
        info["last_run_h"] = round((now - lr) / 3600, 1) if lr else None
    except OSError:
        info["state"] = "(no state file)"

    info["hb_age"] = (now - MONITOR_HEARTBEAT.stat().st_mtime) if MONITOR_HEARTBEAT.exists() else None
    info["svc_monitor"] = svc_active("sump-pump-monitor.service")
    info["svc_guardian"] = svc_active("sump-pump-guardian.timer")
    info["svc_assess"] = svc_active("sump-smart-assess.timer")
    info["assess_last"] = last_log_line(ASSESS_LOG)
    info["guardian_last"] = last_log_line(GUARDIAN_LOG)
    info["standby_reachable"] = ping(STANDBY_HOST)
    return info


def render(info: dict) -> tuple[str, str, str]:
    healthy = (
        info.get("svc_monitor") == "active"
        and info.get("hb_age") is not None and info["hb_age"] < 300
        and info.get("shelly_ip")
    )
    tag = "OK" if healthy else "ATTENTION"
    out = info.get("output")
    out_s = "ON" if out else ("OFF" if out is not None else "?")
    subject = (
        f"Sump daily [{tag}]: {out_s} {info.get('power', 0):.0f}W "
        f"{info.get('temp_c', 0):.0f}C, state {info.get('state')}"
    )

    lines = [
        f"Sump system daily status — {info['ts']} (from toddllm, primary)",
        "",
        f"OVERALL: {tag}",
        "",
        "PUMP / SHELLY",
        f"  resolved IP : {info.get('shelly_ip')}",
        f"  relay       : {out_s}   power {info.get('power', 0):.0f}W   "
        f"temp {info.get('temp_c', 0):.0f}C   {info.get('voltage', 0):.0f}V",
        f"  monitor     : state={info.get('state')}  cycles={info.get('cycles')}  "
        f"last_run={info.get('last_run_h')}h ago",
        "",
        "PRIMARY (toddllm) SERVICES",
        f"  monitor svc : {info.get('svc_monitor')}   (heartbeat {fmt_age(info.get('hb_age'))} ago)",
        f"  guardian    : {info.get('svc_guardian')}",
        f"  smart-assess: {info.get('svc_assess')}",
        f"  last assess : {info.get('assess_last')}",
        f"  last guard  : {info.get('guardian_last')}",
        "",
        "STANDBY (Mac, cold)",
        f"  {STANDBY_HOST}: {'reachable' if info.get('standby_reachable') else 'offline (informational — standby sleeps/leaves the LAN, not an alert)'}",
        "  fail back   : stop toddllm monitor, then on the Mac `launchctl enable gui/$(id -u)/com.sump.pump-monitor && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sump.pump-monitor.plist`",
        "",
        "(Daily digest. Pump anomalies are alerted separately by the monitor, guardian, and smart-assess.)",
    ]
    body = "\n".join(lines)
    return subject, body, tag


def send_email(subject: str, body: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_EMAILS):
        print("daily: email skipped (missing Gmail config)", flush=True)
        return
    escaped = html.escape(body).replace("\n", "<br>\n")
    msg = MIMEText(f"<div style='font-family:monospace'>{escaped}</div>", "html")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(NOTIFY_EMAILS)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_USER, NOTIFY_EMAILS, msg.as_string())
    print(f"daily: email sent to {', '.join(NOTIFY_EMAILS)}", flush=True)


def send_ntfy(subject: str, body: str) -> None:
    if not NTFY_TOPIC:
        print("daily: ntfy skipped (missing NTFY_TOPIC)", flush=True)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": subject, "Priority": "default", "Tags": "droplet"},
    )
    urllib.request.urlopen(req, timeout=10)
    print("daily: ntfy sent", flush=True)


def main() -> int:
    info = gather()
    subject, body, _ = render(info)
    print(body, flush=True)
    try:
        send_email(subject, body)
    except Exception as e:
        print(f"daily: email error: {e}", flush=True)
    try:
        send_ntfy(subject, body)
    except Exception as e:
        print(f"daily: ntfy error: {e}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
