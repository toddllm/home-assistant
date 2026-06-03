#!/usr/bin/env python3
"""
sump_assess.py — first-principles "is the sump situation OK?" assessor.

This is the SMART WATCHER substrate. It does not control the relay (the
monitor + guardian own control). It OBSERVES the real signals, classifies
the situation from first principles, prints a structured report, and can
alert on genuine concern (with de-dup to avoid crying wolf).

Why this exists
---------------
The power-only Shelly cannot directly sense water height. But the Zoeller's
internal float is wired in series with the motor, so:
  * plug ON + ~500W draw  => float CLOSED => water present, pump is pumping
  * plug ON + ~0W draw     => float OPEN   => pit low / empty
  * temp climbing while running => less water cooling => approaching DRY run

From those, plus run frequency / energy / float-open events / temperature,
we can tell apart the situations that actually matter:

  FLOOD_RISK     pump hasn't run in a long time but pit may have water
                 (stuck-OFF float / dead pump) -- the catastrophic case
  DRY_DANGER     pump running hot / low-power => running dry => burnout risk
  HIGH_INFLOW    frequent healthy ~500W wet cycles, temps fine, float opens
                 => LOTS of pumping but pump is fine. With no rain this is
                 almost always a failed/missing check valve recycling water.
                 NOT a fault. Do not try to "fix" it by pumping less.
  UNREACHABLE    cannot reach the Shelly right now
  MONITOR_DOWN   primary monitor heartbeat is stale
  CALM           normal low-activity float cycling

Usage:
  sump_assess.py            # print report + verdict
  sump_assess.py --alert    # also send ntfy/email on WARN/CRITICAL (de-duped)
  sump_assess.py --quiet    # just the VERDICT line (for cron/Claude parsing)
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
MON_LOG = HERE / "sump_pump_monitor.log"
GUARD_LOG = HERE / "sump_pump_guardian.log"
ASSESS_LOG = HERE / "sump_assess.log"
HEARTBEAT = Path("/var/tmp/sump_pump_monitor.heartbeat")
SHELLY_IP_CACHE = Path("/var/tmp/shelly_last_ip")
STATE_FILE = Path("/var/tmp/sump_assess_state.json")
DISCOVERY = HERE / "shelly_discovery.py"

# ---- config (env overridable) ----
def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

SHELLY_IP = os.environ.get("SHELLY_IP", "")
SHELLY_MAC = os.environ.get("SHELLY_MAC", "841FE8F85BBC").upper().replace(":", "")
SUBNET = os.environ.get("SHELLY_SUBNET", "192.168.68")
WET_WATTS = float(os.environ.get("WET_WATTS", "350"))        # >= this while ON = pumping water
HOT_C = float(os.environ.get("ASSESS_HOT_C", "58"))          # approaching overtemp
NO_RUN_HOURS = float(os.environ.get("NO_RUN_ALERT_HOURS", "12"))
HEARTBEAT_STALE_S = float(os.environ.get("ASSESS_HEARTBEAT_STALE_S", "600"))
# "excessive runtime" flag: minutes of pumping per hour above which we note
# likely check-valve recycle / high inflow.
BUSY_MIN_PER_HR = float(os.environ.get("ASSESS_BUSY_MIN_PER_HR", "12"))
# de-dup window per alert class (hours)
ALERT_DEDUP_H = float(os.environ.get("ASSESS_ALERT_DEDUP_H", "3"))

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")


def now_s():
    return time.time()


def http_get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def resolve_shelly():
    """Return a reachable Shelly IP or None. Handles DHCP drift."""
    candidates = []
    if SHELLY_IP:
        candidates.append(SHELLY_IP)
    if SHELLY_IP_CACHE.exists():
        c = SHELLY_IP_CACHE.read_text().strip()
        if c and c not in candidates:
            candidates.append(c)
    for ip in candidates:
        try:
            info = http_get_json(f"http://{ip}/rpc/Shelly.GetDeviceInfo", timeout=4)
            if info.get("mac", "").upper().replace(":", "") == SHELLY_MAC:
                _cache_ip(ip)
                return ip
        except Exception:
            pass
    # last resort: discovery script (ARP/scan by MAC)
    if DISCOVERY.exists():
        try:
            out = subprocess.run(
                [sys.executable, str(DISCOVERY)],
                capture_output=True, text=True, timeout=60,
            )
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out.stdout)
            if m:
                ip = m.group(1)
                _cache_ip(ip)
                return ip
        except Exception:
            pass
    return None


def _cache_ip(ip):
    try:
        SHELLY_IP_CACHE.write_text(ip)
    except Exception:
        pass


def shelly_status(ip):
    try:
        d = http_get_json(f"http://{ip}/rpc/Shelly.GetStatus", timeout=5)
        sw = d.get("switch:0", {})
        sysd = d.get("sys", {})
        return {
            "output": bool(sw.get("output", False)),
            "power": float(sw.get("apower", 0.0) or 0.0),
            "voltage": float(sw.get("voltage", 0.0) or 0.0),
            "temp_c": float(sw.get("temperature", {}).get("tC", 0.0) or 0.0),
            "energy_wh": float(sw.get("aenergy", {}).get("total", 0.0) or 0.0),
            "uptime_h": float(sysd.get("uptime", 0) or 0) / 3600.0,
        }
    except Exception:
        return None


TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def tail(path, n):
    if not path.exists():
        return []
    try:
        data = path.read_text(errors="replace").splitlines()
        return data[-n:]
    except Exception:
        return []


def parse_monitor(lines):
    """Extract recent signals from the monitor log tail."""
    run_powers, run_temps = [], []
    float_opens = 0
    dry_pulses = 0
    unreachable = 0
    state = "unknown"
    last_run_dt = None
    for ln in lines:
        m = TS_RE.match(ln)
        ts = m.group(1) if m else None
        mm = re.search(r"cycle \d+: pump ran \(([\d.]+)W, ([\d.]+)C\)", ln)
        if mm:
            run_powers.append(float(mm.group(1)))
            run_temps.append(float(mm.group(2)))
            if ts:
                last_run_dt = ts
        if "stopped on its own" in ln or re.search(r"Pump stopped after [\d.]+ min", ln):
            float_opens += 1
        if "DRY (" in ln:
            dry_pulses += 1
        if "Could not reach Shelly" in ln:
            unreachable += 1
        sm = re.search(r"STATE: \w+ -> (\w+)", ln)
        if sm:
            state = sm.group(1)
        sm2 = re.search(r"cycle \d+: turning ON", ln)
        # track current tier from "TIERX cycle"
        tm = re.search(r"(TIER_\d|NORMAL|COOLDOWN|LOCKOUT|POWER_CYCLE) cycle", ln)
        if tm:
            state = tm.group(1)
        if "Starting in NORMAL" in ln:
            state = "NORMAL"
    return {
        "run_powers": run_powers,
        "run_temps": run_temps,
        "float_opens": float_opens,
        "dry_pulses": dry_pulses,
        "unreachable": unreachable,
        "state": state,
        "last_run_dt": last_run_dt,
    }


def heartbeat_age():
    if HEARTBEAT.exists():
        return now_s() - HEARTBEAT.stat().st_mtime
    return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(s):
    try:
        STATE_FILE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


def send_ntfy(title, body, priority="default"):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": title, "Priority": priority, "Tags": "ocean,droplet"},
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def send_email(subject, body):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_EMAIL):
        return
    import smtplib
    from email.mime.text import MIMEText
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = NOTIFY_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [a.strip() for a in NOTIFY_EMAIL.split(",")], msg.as_string())
    except Exception:
        pass


def classify(st, mon, hb_age, prev):
    """Return (severity, klass, headline, detail, action)."""
    # severity: 0 OK, 1 INFO, 2 WARN, 3 CRITICAL
    if st is None:
        return (2, "UNREACHABLE",
                "Shelly not reachable right now",
                "Could not read the Shelly on the LAN. Watchdog/guardian failsafe "
                "should cover prolonged loss; check Wi-Fi / DHCP if this persists.",
                "If persistent >15 min, check the plug power and router.")

    temp = st["temp_c"]
    running = st["output"] and st["power"] >= WET_WATTS
    med_run = median(mon["run_powers"])

    # energy delta since last assessment -> minutes of pumping
    busy_note = ""
    pump_min_per_hr = None
    if prev.get("energy_wh") and prev.get("ts"):
        d_wh = st["energy_wh"] - float(prev["energy_wh"])
        d_h = max(1e-6, (now_s() - float(prev["ts"])) / 3600.0)
        if d_wh >= 0:
            # ~500W while pumping => 8.33 Wh per pumping-minute
            pump_min = d_wh / 8.33
            pump_min_per_hr = pump_min / d_h

    # CRITICAL: overheating / dry-run danger
    if temp >= 60 or (running and temp >= HOT_C):
        return (3, "DRY_DANGER",
                f"Pump may be running dry (relay temp {temp:.1f}C)",
                f"Relay temperature {temp:.1f}C with output={'ON' if st['output'] else 'OFF'}, "
                f"power={st['power']:.0f}W. High temp + power usually means the pump is "
                f"moving little/no water (running dry) -> burnout risk. The guardian should "
                f"force a cooling rest at 60C/70C.",
                "Verify guardian forced a rest; if temp keeps climbing, pull the plug and "
                "check the float / pit by hand.")

    # FLOOD_RISK: no pumping for a long time (can't confirm water w/o sensor)
    no_run_h = None
    if mon["last_run_dt"]:
        try:
            last = datetime.strptime(mon["last_run_dt"], "%Y-%m-%d %H:%M:%S")
            no_run_h = (datetime.now() - last).total_seconds() / 3600.0
        except Exception:
            pass
    if no_run_h is not None and no_run_h >= NO_RUN_HOURS:
        return (2, "FLOOD_RISK",
                f"No pump run seen in {no_run_h:.1f}h",
                f"The pump hasn't drawn power in {no_run_h:.1f}h (threshold {NO_RUN_HOURS:.0f}h). "
                f"This is fine if the pit is genuinely dry, but with a power-only sensor we "
                f"cannot prove there's no water. If the float is stuck OFF, this is the "
                f"flood case.",
                "Eyeball the pit. This is exactly what the planned pit water sensor would "
                "resolve automatically.")

    # MONITOR_DOWN
    if hb_age is not None and hb_age >= HEARTBEAT_STALE_S:
        return (2, "MONITOR_DOWN",
                f"Primary monitor heartbeat stale ({hb_age/60:.0f}m)",
                f"sump_pump_monitor heartbeat is {hb_age/60:.0f} min old. launchd should "
                f"respawn it; the guardian still enforces safety cutoffs.",
                "Check `launchctl list | grep com.sump` and the monitor log.")

    # HIGH_INFLOW: lots of healthy wet pumping
    busy = pump_min_per_hr is not None and pump_min_per_hr >= BUSY_MIN_PER_HR
    wet_cycles = med_run is not None and med_run >= WET_WATTS
    if busy or (wet_cycles and len(mon["run_powers"]) >= 6 and temp < HOT_C):
        rate = f"{pump_min_per_hr:.0f} min/hr" if pump_min_per_hr is not None else "high"
        return (1, "HIGH_INFLOW",
                f"Pumping a lot but pump is healthy ({rate})",
                f"Recent runs draw a healthy ~{med_run:.0f}W (wet pumping) at {temp:.1f}C, "
                f"and the float has opened on its own {mon['float_opens']}x in the window "
                f"(so it is NOT stuck). The pump is fine; there is simply real water coming "
                f"in. With no rain, the usual cause is a failed/missing check valve recycling "
                f"the same water. Monitor state: {mon['state']}.",
                "This is not a fault and should not be 'fixed' by pumping less. Real fix is "
                "a check-valve inspection (~$15). Until then, leave it pumping.")

    # CALM
    return (0, "CALM",
            f"Normal (relay {('ON' if st['output'] else 'OFF')}, {temp:.1f}C)",
            f"Output={'ON' if st['output'] else 'OFF'} power={st['power']:.0f}W temp={temp:.1f}C "
            f"uptime={st['uptime_h']:.0f}h. Monitor state: {mon['state']}. "
            f"Float opened {mon['float_opens']}x, {mon['dry_pulses']} dry pulses in window.",
            "No action needed.")


def main():
    args = set(sys.argv[1:])
    quiet = "--quiet" in args
    do_alert = "--alert" in args

    ip = resolve_shelly()
    st = shelly_status(ip) if ip else None
    mon = parse_monitor(tail(MON_LOG, 400))
    hb_age = heartbeat_age()
    prev = load_state()

    sev, klass, head, detail, action = classify(st, mon, hb_age, prev)

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    sev_name = {0: "OK", 1: "INFO", 2: "WARN", 3: "CRITICAL"}[sev]
    verdict = f"VERDICT: {klass} [{sev_name}] — {head}"

    if not quiet:
        print(f"=== sump assessment {stamp} ===")
        print(f"shelly: {ip or 'UNREACHABLE'}", end="")
        if st:
            print(f"  output={'ON' if st['output'] else 'OFF'} {st['power']:.0f}W "
                  f"{st['temp_c']:.1f}C {st['voltage']:.0f}V uptime={st['uptime_h']:.0f}h "
                  f"energy={st['energy_wh']:.0f}Wh")
        else:
            print()
        print(f"monitor: state={mon['state']} runs={len(mon['run_powers'])} "
              f"(median {median(mon['run_powers']) or 0:.0f}W) "
              f"float_opens={mon['float_opens']} dry_pulses={mon['dry_pulses']} "
              f"unreachable_warns={mon['unreachable']} "
              f"heartbeat_age={'%.0fs' % hb_age if hb_age is not None else 'n/a'}")
        print()
        print(verdict)
        print(f"  {detail}")
        print(f"  ACTION: {action}")
    else:
        print(verdict)

    # log every run (one line)
    try:
        with open(ASSESS_LOG, "a") as f:
            f.write(f"[{stamp}] {klass} {sev_name} | "
                    f"shelly={ip or 'down'} "
                    f"{('%.0fW %.1fC %s' % (st['power'], st['temp_c'], 'ON' if st['output'] else 'OFF')) if st else 'no-status'} "
                    f"state={mon['state']} | {head}\n")
    except Exception:
        pass

    # alert policy: WARN/CRITICAL only, de-duped per class
    if do_alert and sev >= 2:
        last = prev.get("alerts", {}).get(klass, 0)
        if now_s() - float(last) >= ALERT_DEDUP_H * 3600:
            prio = "urgent" if sev >= 3 else "high"
            subj = f"{'URGENT: ' if sev >= 3 else ''}SUMP {klass}: {head}"
            body = f"{head}\n\n{detail}\n\nSuggested: {action}\n\n({stamp})"
            send_ntfy(subj, body, priority=prio)
            send_email(subj, body)
            prev.setdefault("alerts", {})[klass] = now_s()

    # persist energy + ts for next delta, and last verdict
    if st:
        prev["energy_wh"] = st["energy_wh"]
    prev["ts"] = now_s()
    prev["last_klass"] = klass
    prev["last_sev"] = sev
    save_state(prev)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
