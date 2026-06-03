#!/usr/bin/env python3
"""
Independent high-level sump pump guardian.

Runs every 2 minutes via launchd. This does not replace sump_pump_monitor.py.
It is a last-line guardrail for IP drift, unreachable Shelly, no-run silence,
long pump runs, and Shelly relay overheating.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from shelly_discovery import find_shelly_ip
from weather_check import assess as weather_assess


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
LOG_FILE = ROOT / "sump_pump_guardian.log"
STATE_FILE = Path("/var/tmp/sump_pump_guardian_state.json")
MONITOR_STATE_FILE = Path("/var/tmp/sump_pump_state.json")
MONITOR_HEARTBEAT = Path("/var/tmp/sump_pump_monitor.heartbeat")


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env()

SHELLY_IP = os.environ.get("SHELLY_IP", "192.168.68.120")
SHELLY_MAC = os.environ.get("SHELLY_MAC", "841FE8F85BBC")
SHELLY_SCAN_CIDR = os.environ.get("SHELLY_SCAN_CIDR", "192.168.68.0/24")
SHELLY_LAST_IP_CACHE = os.environ.get("SHELLY_LAST_IP_CACHE", "/var/tmp/shelly_last_ip")

POWER_THRESHOLD_WATTS = float(os.environ.get("POWER_THRESHOLD_WATTS", "100.0"))
NO_RUN_ALERT_HOURS = float(os.environ.get("NO_RUN_ALERT_HOURS", "12"))
GUARDIAN_MAX_RUN_MIN = float(os.environ.get("GUARDIAN_MAX_RUN_MIN", "4"))
GUARDIAN_MAX_UNREACHABLE_MIN = float(os.environ.get("GUARDIAN_MAX_UNREACHABLE_MIN", "15"))
GUARDIAN_FORCED_REST_SECONDS = int(os.environ.get("GUARDIAN_FORCED_REST_SECONDS", "300"))
GUARDIAN_ALERT_REPEAT_SECONDS = int(os.environ.get("GUARDIAN_ALERT_REPEAT_SECONDS", "3600"))
GUARDIAN_STUCK_OFF_HOURS = float(os.environ.get("GUARDIAN_STUCK_OFF_HOURS", str(NO_RUN_ALERT_HOURS)))

MAX_TEMP_C = float(os.environ.get("MAX_TEMP_C", "60.0"))
SHELLY_HARD_TEMP_C = float(os.environ.get("SHELLY_HARD_TEMP_C", "70.0"))
SHELLY_TEMP_RESUME_C = float(os.environ.get("SHELLY_TEMP_RESUME_C", "55.0"))

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAILS = [e.strip() for e in os.environ.get("NOTIFY_EMAIL", GMAIL_USER).split(",") if e.strip()]
NOTIFY_EMAILS_URGENT = [e.strip() for e in os.environ.get("NOTIFY_EMAIL_URGENT", GMAIL_USER).split(",") if e.strip()]
NOTIFY_EMAILS_LOG = [e.strip() for e in os.environ.get("NOTIFY_EMAIL_LOG", "smart-home-monitor@agentmail.to").split(",") if e.strip()]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


def now_s() -> float:
    return time.time()


def log_status(message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    state["saved_at"] = now_s()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.rename(STATE_FILE)


def read_monitor_state() -> dict[str, Any]:
    if not MONITOR_STATE_FILE.exists():
        return {}
    try:
        return json.loads(MONITOR_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def alert_allowed(state: dict[str, Any], key: str) -> bool:
    alerts = state.setdefault("alerts", {})
    last = float(alerts.get(key, 0) or 0)
    if now_s() - last < GUARDIAN_ALERT_REPEAT_SECONDS:
        return False
    alerts[key] = now_s()
    return True


def send_email_alert(subject: str, body: str, urgent: bool = False) -> None:
    recipients = NOTIFY_EMAILS_URGENT if urgent else NOTIFY_EMAILS_LOG
    final_subject = f"URGENT SUMP GUARDIAN: {subject}" if urgent else f"SUMP GUARDIAN: {subject}"
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not recipients:
        log_status("email skipped: missing Gmail config")
        return

    escaped = html.escape(body).replace("\n", "<br>\n")
    msg = MIMEText(f"<div>{escaped}</div>", "html")
    msg["Subject"] = final_subject
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())


def send_ntfy_alert(subject: str, body: str, priority: str = "urgent") -> None:
    if not NTFY_TOPIC:
        log_status("ntfy skipped: missing NTFY_TOPIC")
        return

    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": subject, "Priority": priority, "Tags": "warning"},
    )
    urllib.request.urlopen(req, timeout=10)


# URGENT keys: things that require immediate human attention regardless of weather.
URGENT_ALERT_KEYS = {
    "hard_overtemp",
    "shelly_unreachable",
    "shelly_status_failed",
    "wet_but_no_pump",
    "high_water_flood",
    "high_water_level",
}

# Weather-conditional URGENT: these are LOG by default, but escalate to URGENT
# when the weather model says rain is recent or imminent (flooding risk).
WEATHER_CONDITIONAL_URGENT_KEYS = {
    "no_run",
    "never_saw_power",
    "max_run",  # max-run while wet means we may actually be losing the flood battle
}


def alert(state: dict[str, Any], key: str, subject: str, body: str, priority: str = "urgent") -> None:
    if not alert_allowed(state, key):
        return

    urgent = key in URGENT_ALERT_KEYS
    if not urgent and key in WEATHER_CONDITIONAL_URGENT_KEYS:
        try:
            w = weather_assess()
            if w.get("is_wet"):
                urgent = True
                body = (
                    body
                    + f"\n\nWeather: recent_rain={w.get('recent_rain_mm')}mm, "
                    f"current={w.get('current_rain_mm_h')}mm/h, "
                    f"forecast_24h={w.get('forecast_24h_mm')}mm — wet, escalating to URGENT."
                )
            else:
                body = (
                    body
                    + f"\n\nWeather: dry (recent={w.get('recent_rain_mm')}mm, "
                    f"forecast_24h={w.get('forecast_24h_mm')}mm). Keeping LOG-only."
                )
        except Exception as exc:
            log_status(f"weather check failed: {exc}")

    try:
        send_email_alert(subject, body, urgent=urgent)
        log_status(f"{'URGENT' if urgent else 'LOG'} email sent: {subject}")
    except Exception as exc:
        log_status(f"ERROR email alert failed: {exc}")

    if not urgent:
        return  # ntfy is for URGENT only

    try:
        send_ntfy_alert(subject, body, priority=priority)
        log_status(f"URGENT ntfy sent: {subject}")
    except Exception as exc:
        log_status(f"ERROR ntfy alert failed: {exc}")


def rpc(host: str, method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    url = f"http://{host}/rpc/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_shelly_status(host: str) -> dict[str, Any]:
    data = rpc(host, "Shelly.GetStatus")
    sw = data.get("switch:0", {})
    sys_info = data.get("sys", {})
    wifi = data.get("wifi", {})
    return {
        "output": bool(sw.get("output", False)),
        "power": float(sw.get("apower", 0.0) or 0.0),
        "voltage": float(sw.get("voltage", 0.0) or 0.0),
        "current": float(sw.get("current", 0.0) or 0.0),
        "temp_c": float(sw.get("temperature", {}).get("tC", 0.0) or 0.0),
        "uptime": int(sys_info.get("uptime", 0) or 0),
        "rssi": int(wifi.get("rssi", 0) or 0),
    }


def set_relay(host: str, on: bool, reason: str) -> None:
    rpc(host, "Switch.Set", {"id": 0, "on": "true" if on else "false"})
    log_status(f"relay {'ON' if on else 'OFF'}: {reason}")


def monitor_heartbeat_age_s() -> float | None:
    if not MONITOR_HEARTBEAT.exists():
        return None
    try:
        return max(0.0, now_s() - MONITOR_HEARTBEAT.stat().st_mtime)
    except OSError:
        return None


def seed_from_monitor_state(state: dict[str, Any], monitor_state: dict[str, Any]) -> None:
    last = monitor_state.get("last_pump_run_wall")
    if isinstance(last, (int, float)) and last > float(state.get("last_pump_cycle_wall", 0) or 0):
        state["last_pump_cycle_wall"] = float(last)


def update_run_tracking(state: dict[str, Any], status: dict[str, Any]) -> bool:
    running = status["output"] and status["power"] > POWER_THRESHOLD_WATTS
    current = now_s()

    if running:
        if not state.get("running_since_wall"):
            state["running_since_wall"] = current
        state["last_pump_cycle_wall"] = current
        state["ever_saw_pump_power"] = True
    else:
        state["running_since_wall"] = None

    return running


def unreachable_started_age_min(state: dict[str, Any]) -> float:
    started = float(state.get("unreachable_since_wall", now_s()) or now_s())
    return max(0.0, (now_s() - started) / 60.0)


def force_cooling_rest(state: dict[str, Any], host: str, reason: str) -> None:
    set_relay(host, False, reason)
    state["forced_rest_until_wall"] = now_s() + GUARDIAN_FORCED_REST_SECONDS
    state["last_action_taken"] = reason


def forced_rest_remaining_s(state: dict[str, Any]) -> float:
    return max(0.0, float(state.get("forced_rest_until_wall", 0) or 0) - now_s())


def maybe_resolve_shelly(state: dict[str, Any]) -> str | None:
    host = find_shelly_ip(
        expected_mac=SHELLY_MAC,
        candidates=[SHELLY_IP],
        cidr=SHELLY_SCAN_CIDR,
        cache_path=SHELLY_LAST_IP_CACHE,
        logger=log_status,
    )
    if host:
        if state.get("unreachable_since_wall"):
            state["unreachable_since_wall"] = None
        if host != SHELLY_IP:
            alert(
                state,
                "ip_changed",
                "SUMP GUARDIAN: Shelly IP changed",
                f"Shelly MAC {SHELLY_MAC} is reachable at {host}. Configured SHELLY_IP is {SHELLY_IP}.",
                priority="high",
            )
        state["last_shelly_ip"] = host
        return host

    if not state.get("unreachable_since_wall"):
        state["unreachable_since_wall"] = now_s()

    age_min = unreachable_started_age_min(state)
    if age_min >= GUARDIAN_MAX_UNREACHABLE_MIN:
        alert(
            state,
            "shelly_unreachable",
            "SUMP GUARDIAN: Shelly unreachable",
            f"Shelly has been unreachable for {age_min:.1f} minutes. Expected MAC {SHELLY_MAC}, configured IP {SHELLY_IP}, scan range {SHELLY_SCAN_CIDR}.",
        )

    return None


def fmt(value, spec, fallback="unknown"):
    if value is None:
        return fallback
    return format(value, spec)


def main() -> int:
    state = load_state()
    monitor_state = read_monitor_state()
    seed_from_monitor_state(state, monitor_state)

    host = maybe_resolve_shelly(state)
    if not host:
        log_status(
            f"status shelly=unreachable unreachable_min={unreachable_started_age_min(state):.1f} "
            f"last_action={state.get('last_action_taken', 'none')}"
        )
        save_state(state)
        return 2

    try:
        status = get_shelly_status(host)
    except Exception as exc:
        if not state.get("unreachable_since_wall"):
            state["unreachable_since_wall"] = now_s()
        age_min = unreachable_started_age_min(state)
        if age_min >= GUARDIAN_MAX_UNREACHABLE_MIN:
            alert(
                state,
                "shelly_status_failed",
                "SUMP GUARDIAN: Shelly status failed",
                f"Shelly at {host} failed Shelly.GetStatus for {age_min:.1f} minutes: {exc}",
            )
        log_status(f"status shelly=status_failed host={host} unreachable_min={age_min:.1f} error={exc}")
        save_state(state)
        return 2

    state["unreachable_since_wall"] = None
    running = update_run_tracking(state, status)
    current = now_s()

    last_cycle = float(state.get("last_pump_cycle_wall", 0) or 0)
    hours_since_cycle = (current - last_cycle) / 3600.0 if last_cycle else None
    hb_age = monitor_heartbeat_age_s()
    rest_remaining = forced_rest_remaining_s(state)

    if status["temp_c"] >= SHELLY_HARD_TEMP_C:
        force_cooling_rest(state, host, "hard Shelly temperature cutoff")
        alert(
            state,
            "hard_overtemp",
            "SUMP GUARDIAN: hard overtemp cutoff",
            f"Shelly relay temperature is {status['temp_c']:.1f}C, hard limit {SHELLY_HARD_TEMP_C:.1f}C. Relay forced OFF.",
        )
        log_status(
            f"status host={host} action=force_off_hard_temp output={status['output']} "
            f"power={status['power']:.1f}W temp={status['temp_c']:.1f}C"
        )
        save_state(state)
        return 1

    if running and status["temp_c"] >= MAX_TEMP_C:
        force_cooling_rest(state, host, "soft Shelly temperature cooling rest")
        alert(
            state,
            "soft_overtemp",
            "SUMP GUARDIAN: thermal cooling rest",
            f"Shelly relay temperature is {status['temp_c']:.1f}C, soft threshold {MAX_TEMP_C:.1f}C. Relay forced OFF for {GUARDIAN_FORCED_REST_SECONDS} seconds, then normal float control can resume.",
            priority="high",
        )
        log_status(
            f"status host={host} action=force_off_soft_temp output={status['output']} "
            f"power={status['power']:.1f}W temp={status['temp_c']:.1f}C"
        )
        save_state(state)
        return 1

    running_since = state.get("running_since_wall")
    if running and running_since and (current - float(running_since)) >= GUARDIAN_MAX_RUN_MIN * 60:
        run_min = (current - float(running_since)) / 60.0
        force_cooling_rest(state, host, "pump exceeded guardian max continuous run")
        alert(
            state,
            "max_run",
            "SUMP GUARDIAN: max run cutoff",
            f"Pump has drawn >{POWER_THRESHOLD_WATTS:.0f}W for {run_min:.1f} minutes, limit {GUARDIAN_MAX_RUN_MIN:.1f} minutes. Relay forced OFF for rest.",
        )
        log_status(
            f"status host={host} action=force_off_max_run run_min={run_min:.1f} "
            f"power={status['power']:.1f}W temp={status['temp_c']:.1f}C"
        )
        save_state(state)
        return 1

    if rest_remaining > 0:
        log_status(
            f"status host={host} action=forced_rest_remaining rest_s={rest_remaining:.0f} "
            f"output={status['output']} power={status['power']:.1f}W temp={status['temp_c']:.1f}C"
        )
        save_state(state)
        return 0

    if state.get("forced_rest_until_wall") and rest_remaining <= 0:
        state["forced_rest_until_wall"] = 0
        state["last_action_taken"] = "cooling rest expired"
        if not status["output"] and status["temp_c"] <= SHELLY_TEMP_RESUME_C:
            set_relay(host, True, "guardian cooling rest expired")
            alert(
                state,
                "rest_expired",
                "SUMP GUARDIAN: cooling rest expired",
                f"Cooling rest expired and Shelly temperature is {status['temp_c']:.1f}C. Relay turned ON to restore float-controlled pumping.",
                priority="high",
            )

    if hours_since_cycle is not None and hours_since_cycle >= NO_RUN_ALERT_HOURS:
        alert(
            state,
            "no_run",
            "SUMP GUARDIAN: no pump cycle observed",
            f"No pump power draw has been observed for {hours_since_cycle:.1f} hours, threshold {NO_RUN_ALERT_HOURS:.1f} hours. This may be a dry spell, but if the pit has water the float may be stuck OFF or the pump may have failed.\n\nRelay output: {'ON' if status['output'] else 'OFF'}\nPower: {status['power']:.1f}W\nShelly temperature: {status['temp_c']:.1f}C",
            priority="high",
        )

    if not state.get("ever_saw_pump_power") and hours_since_cycle is not None and hours_since_cycle >= GUARDIAN_STUCK_OFF_HOURS:
        alert(
            state,
            "never_saw_power",
            "SUMP GUARDIAN: pump has never drawn power",
            f"Guardian has not recorded any pump power draw for {hours_since_cycle:.1f} hours. Relay output is {'ON' if status['output'] else 'OFF'} and current power is {status['power']:.1f}W. Check for stuck-OFF float, disconnected pump, failed pump, or a normal dry pit.",
        )

    log_status(
        f"status host={host} output={'ON' if status['output'] else 'OFF'} "
        f"power={status['power']:.1f}W temp={status['temp_c']:.1f}C "
        f"running={'yes' if running else 'no'} "
        f"hours_since_cycle={fmt(hours_since_cycle, '.1f')} "
        f"monitor_heartbeat_age_s={fmt(hb_age, '.0f')} "
        f"last_action={state.get('last_action_taken', 'none')}"
    )

    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log_status(f"FATAL guardian error: {exc}")
        raise
