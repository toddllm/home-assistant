#!/usr/bin/env python3
"""
Sump Pump Monitor - Shelly Plug US Gen4
Monitors power draw to detect a stuck float switch.

Uses a tiered escalation state machine:
  NORMAL       - Plug ON, float controls pump. Monitor watches.
  POWER_CYCLE  - Attempting power cycle to unstick float.
  TIER_1       - 60s ON / 15 min OFF. Gentle duty cycle.
  TIER_2       - 90s ON / 10 min OFF. Moderate duty cycle.
  TIER_3       - 120s ON / 10 min OFF. Max duty, alerts every cycle.
  COOLDOWN     - Confirming float unstuck before returning to NORMAL.
  LOCKOUT      - Overtemp or Shelly lost. Overtemp is manual-only; Shelly loss can auto-recover.
"""

import json
import os
import time
import smtplib
import signal
import subprocess
import sys
import urllib.request
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path

import requests
from shelly_discovery import find_shelly_ip
from weather_check import assess as weather_assess


def load_env():
    """Load config from .env file in the same directory as this script."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(f"ERROR: {env_path} not found. Copy .env.example and fill in values.", flush=True)
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env()

from pump_state import (
    PumpStateMachine, check_single_instance, cleanup_pid, validate_config,
    NORMAL, POWER_CYCLE, TIER_1, TIER_2, TIER_3, COOLDOWN, LOCKOUT,
    PHASE_ON, PHASE_OFF, TIER_CONFIG, DRY_EXIT_THRESHOLD,
    COOLDOWN_STABLE_SECONDS, SHELLY_WARN_SECONDS, SHELLY_CRITICAL_SECONDS,
)

# Shelly config
SHELLY_IP = os.environ["SHELLY_IP"]
SHELLY_USER = os.environ.get("SHELLY_USER", "")
SHELLY_PASSWORD = os.environ.get("SHELLY_PASSWORD", "")
SHELLY_MAC = os.environ.get("SHELLY_MAC", "841FE8F85BBC")
SHELLY_SCAN_CIDR = os.environ.get("SHELLY_SCAN_CIDR", "192.168.68.0/24")
SHELLY_LAST_IP_CACHE = os.environ.get("SHELLY_LAST_IP_CACHE", "/var/tmp/shelly_last_ip")
MONITOR_HEARTBEAT_FILE = Path(os.environ.get("MONITOR_HEARTBEAT_FILE", "/var/tmp/sump_pump_monitor.heartbeat"))

# Thresholds
POWER_THRESHOLD_WATTS = float(os.environ.get("POWER_THRESHOLD_WATTS", "100.0"))
MAX_RUN_MINUTES = float(os.environ.get("MAX_RUN_MINUTES", "3"))
MAX_RUN_MINUTES_WET = float(os.environ.get("MAX_RUN_MINUTES_WET", "6"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
ACTIVE_POLL_SECONDS = float(os.environ.get("ACTIVE_POLL_SECONDS", "5"))
OUTPUT_RECOVERY_GRACE_SECONDS = float(os.environ.get("OUTPUT_RECOVERY_GRACE_SECONDS", "5"))
SHELLY_RECOVERY_STABLE_SECONDS = int(os.environ.get("SHELLY_RECOVERY_STABLE_SECONDS", "300"))

# Power cycle config
POWER_CYCLE_OFF_SECONDS = int(os.environ.get("POWER_CYCLE_OFF_SECONDS", "10"))
POWER_CYCLE_SETTLE_SECONDS = int(os.environ.get("POWER_CYCLE_SETTLE_SECONDS", "60"))

# Temperature safety
MAX_TEMP_C = float(os.environ.get("MAX_TEMP_C", "60.0"))
TEMP_WARN_C = float(os.environ.get("TEMP_WARN_C", "50.0"))
SHELLY_HARD_TEMP_C = float(os.environ.get("SHELLY_HARD_TEMP_C", "70.0"))

# Voltage safety (normal US is ~120V)
VOLTAGE_LOW = float(os.environ.get("VOLTAGE_LOW", "110.0"))
VOLTAGE_HIGH = float(os.environ.get("VOLTAGE_HIGH", "130.0"))

# WiFi signal threshold
RSSI_WARN = int(os.environ.get("RSSI_WARN", "-80"))

# Expected pump power range when running (motor health)
PUMP_POWER_LOW = float(os.environ.get("PUMP_POWER_LOW", "350.0"))
PUMP_POWER_HIGH = float(os.environ.get("PUMP_POWER_HIGH", "700.0"))

# No-run alert: hours without pump running before alerting
NO_RUN_ALERT_HOURS = float(os.environ.get("NO_RUN_ALERT_HOURS", "24"))

# Status digest: periodic "alive" notification (seconds, 0 = disabled)
DIGEST_INTERVAL_SECONDS = int(os.environ.get("DIGEST_INTERVAL_SECONDS", "7200"))

# Float unstick: interval between automatic attempts in TIER_3 (seconds)
UNSTICK_INTERVAL_SECONDS = int(os.environ.get("UNSTICK_INTERVAL_SECONDS", "14400"))  # 4 hours
UNSTICK_CYCLES = int(os.environ.get("UNSTICK_CYCLES", "6"))  # rapid ON/OFF cycles
UNSTICK_TRIGGER = Path("/var/tmp/sump_pump_unstick_trigger")

# AI Analyzer (optional — runs on Mac with Ollama)
AI_ENABLED = os.environ.get("AI_ENABLED", "false").lower() == "true"
AI_ANALYZER_URL = os.environ.get("AI_ANALYZER_URL", "http://localhost:8078")

# Notification config
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAILS = [e.strip() for e in os.environ.get("NOTIFY_EMAIL", GMAIL_USER).split(",") if e.strip()]
NOTIFY_EMAILS_URGENT = [e.strip() for e in os.environ.get("NOTIFY_EMAIL_URGENT", GMAIL_USER).split(",") if e.strip()]
NOTIFY_EMAILS_LOG = [e.strip() for e in os.environ.get("NOTIFY_EMAIL_LOG", "smart-home-monitor@agentmail.to").split(",") if e.strip()]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]


LOG_FILE = Path(__file__).parent / "sump_pump_monitor.log"


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    if sys.stdout.isatty():
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def write_monitor_heartbeat():
    try:
        MONITOR_HEARTBEAT_FILE.write_text(str(int(time.time())) + "\n")
    except OSError as e:
        log(f"WARNING: could not write monitor heartbeat: {e}")


def rediscover_shelly_ip(reason):
    global SHELLY_IP
    found = find_shelly_ip(
        expected_mac=SHELLY_MAC,
        candidates=[SHELLY_IP],
        cidr=SHELLY_SCAN_CIDR,
        cache_path=SHELLY_LAST_IP_CACHE,
        logger=log,
    )
    if found and found != SHELLY_IP:
        old_ip = SHELLY_IP
        SHELLY_IP = found
        log(f"Shelly IP rediscovered after {reason}: {old_ip} -> {SHELLY_IP}")
    return bool(found)


def shelly_rpc(method, params=None, *, _retried=False):
    """Call a Shelly RPC method via HTTP with digest auth (uses curl for SHA-256 support)."""
    url = f"http://{SHELLY_IP}/rpc/{method}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{query}"
    try:
        cmd = ["curl", "-s", "--connect-timeout", "5", "--max-time", "10",
               "-w", "\n%{http_code}"]
        if SHELLY_PASSWORD:
            cmd.extend(["-u", f"{SHELLY_USER}:{SHELLY_PASSWORD}", "--digest"])
        cmd.append(url)
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=15,
        )
        lines = result.stdout.rsplit("\n", 1)
        body = lines[0] if len(lines) > 1 else result.stdout
        http_code = lines[-1].strip() if len(lines) > 1 else "0"
        if result.returncode != 0 or http_code != "200" or not body.strip():
            log(f"ERROR: Shelly RPC '{method}' failed at {SHELLY_IP}: HTTP {http_code}")
            if not _retried and (http_code in ("0", "000") or result.returncode != 0):
                if rediscover_shelly_ip(f"{method} HTTP {http_code}"):
                    return shelly_rpc(method, params, _retried=True)
            return None
        return json.loads(body)
    except Exception as e:
        log(f"ERROR: Shelly RPC '{method}' failed at {SHELLY_IP}: {e}")
        if not _retried and rediscover_shelly_ip(f"{method} exception"):
            return shelly_rpc(method, params, _retried=True)
        return None


def get_power_status():
    """Get current power readings from the Shelly plug."""
    data = shelly_rpc("Shelly.GetStatus")
    if not data:
        return None
    sw = data.get("switch:0", {})
    illum = data.get("illuminance:0", {})
    wifi = data.get("wifi", {})
    sys_info = data.get("sys", {})
    return {
        "output": sw.get("output", False),
        "power": sw.get("apower", 0.0),
        "voltage": sw.get("voltage", 0.0),
        "freq": sw.get("freq", 0.0),
        "current": sw.get("current", 0.0),
        "temp_c": sw.get("temperature", {}).get("tC", 0.0),
        "illumination": illum.get("illumination", "unknown"),
        "rssi": wifi.get("rssi", 0),
        "ssid": wifi.get("ssid", "unknown"),
        "bssid": wifi.get("bssid", "unknown"),
        "uptime": sys_info.get("uptime", 0),
        "reset_reason": sys_info.get("reset_reason", -1),
        "ram_free": sys_info.get("ram_free", 0),
    }


def turn_on():
    shelly_rpc("Switch.Set", {"id": 0, "on": "true"})
    log("Plug turned ON")


def turn_off():
    shelly_rpc("Switch.Set", {"id": 0, "on": "false"})
    log("Plug turned OFF")


def is_pump_running(status=None):
    if status is None:
        status = get_power_status()
    return status is not None and status["power"] > POWER_THRESHOLD_WATTS


def is_shelly_connectivity_lockout(sm):
    """Return True when LOCKOUT was caused by Shelly reachability only."""
    return sm.state == LOCKOUT and sm.lockout_reason.startswith("Shelly unreachable")


def maybe_recover_shelly_lockout(sm, status, *, startup=False):
    """Auto-recover a connectivity lockout after stable Shelly reachability."""
    if not is_shelly_connectivity_lockout(sm) or not status:
        return False

    prefix = "Startup: " if startup else ""
    if sm.shelly_recovered_at is None:
        sm.shelly_recovered_at = time.monotonic()
        log(
            f"{prefix}Shelly reachable during connectivity lockout, "
            f"waiting {SHELLY_RECOVERY_STABLE_SECONDS}s before auto-recovery"
        )
        return False

    recovered_for = time.monotonic() - sm.shelly_recovered_at
    if recovered_for < SHELLY_RECOVERY_STABLE_SECONDS:
        return False

    if not status["output"]:
        log(f"{prefix}Connectivity lockout recovered: restoring plug ON for float control")
        turn_on()
        sm.output_recovery_until = time.monotonic() + OUTPUT_RECOVERY_GRACE_SECONDS
        sm.running_since = None
    elif is_pump_running(status):
        if sm.running_since is None:
            sm.running_since = time.monotonic()
            log(f"{prefix}Connectivity lockout recovered while pump is already running")
    else:
        sm.running_since = None

    sm.shelly_warn_alerted = False
    sm.shelly_critical_alerted = False
    send_notification(
        "Sump pump: Shelly connectivity restored",
        f"The Shelly plug has been reachable for {recovered_for / 60:.1f} minutes after a "
        "connectivity lockout.\n\n"
        "Returning to NORMAL monitoring and restoring automatic protection.",
    )
    sm.transition(NORMAL, f"Shelly recovered after {recovered_for / 60:.1f} min stable")
    return True


def send_notification(subject, body, urgent=False):
    """Send email (+ ntfy if urgent) per the routing matrix.

    urgent=True  -> NOTIFY_EMAIL_URGENT (todd), subject prefix "URGENT: ", fires ntfy.
    urgent=False -> NOTIFY_EMAIL_LOG (agentmail), no prefix, skips ntfy.

    See docs/2026-05-26-monitor-redesign.md and the Sump Pump Email Routing
    Policy Google Doc for the per-call classification.
    """
    recipients = NOTIFY_EMAILS_URGENT if urgent else NOTIFY_EMAILS_LOG
    final_subject = f"URGENT: {subject}" if urgent else subject
    try:
        msg = MIMEText(body)
        msg["Subject"] = final_subject
        msg["From"] = GMAIL_USER
        msg["To"] = ", ".join(recipients)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, recipients, msg.as_string())
        log(f"{'URGENT' if urgent else 'LOG'} email sent to {', '.join(recipients)}: {subject}")
    except Exception as e:
        log(f"ERROR: Failed to send email ({'URGENT' if urgent else 'LOG'}): {e}")

    if not urgent:
        return  # ntfy is for URGENT only

    try:
        ntfy_title = final_subject.replace("—", "-").replace("–", "-")
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": ntfy_title, "Priority": "urgent", "Tags": "warning"},
        )
        urllib.request.urlopen(req, timeout=10)
        log(f"URGENT ntfy sent to {NTFY_TOPIC}")
    except Exception as e:
        log(f"ERROR: Failed to send ntfy alert: {e}")


def send_digest_notification(subject, body):
    """Send status digest — same as send_notification but lower ntfy priority."""
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = ", ".join(NOTIFY_EMAILS)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, NOTIFY_EMAILS, msg.as_string())
        log(f"Digest sent via email to {', '.join(NOTIFY_EMAILS)}")
    except Exception as e:
        log(f"ERROR: Failed to send digest email: {e}")
    try:
        ntfy_title = subject.replace("—", "-").replace("–", "-")
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": ntfy_title, "Priority": "default", "Tags": "clipboard"},
        )
        urllib.request.urlopen(req, timeout=10)
        log(f"Digest sent via ntfy to {NTFY_TOPIC}")
    except Exception as e:
        log(f"ERROR: Failed to send digest ntfy: {e}")


def send_status_digest(sm, status):
    """Send periodic status digest so silence = problem."""
    if status is None:
        send_digest_notification(
            "SUMP PUMP DIGEST: Shelly UNREACHABLE",
            "The Shelly plug is currently unreachable.\n\n"
            f"State: {sm.state}\n"
            f"Monitor PID: {os.getpid()}\n"
            f"Time in state: {sm.time_in_state() / 60:.0f} min",
        )
        return

    power = status["power"]
    temp = status["temp_c"]
    voltage = status["voltage"]
    rssi = status.get("rssi", 0)
    uptime_h = status.get("uptime", 0) / 3600
    hours_since_run = (time.monotonic() - sm.last_pump_run) / 3600

    state_detail = ""
    if sm.is_tier():
        config = sm.tier_config()
        rest = sm.adjusted_rest()
        state_detail = (
            f"Phase: {sm.duty_phase}\n"
            f"Cycle: {sm.cycle_count}\n"
            f"Dry streak: {sm.consecutive_dry}/{DRY_EXIT_THRESHOLD}\n"
            f"Timing: {config['run_s']}s ON / {rest:.0f}s OFF "
            f"(weather mult: {sm.weather_multiplier():.1f}x)\n"
        )
        if sm.last_unstick_at > 0:
            unstick_ago = (time.monotonic() - sm.last_unstick_at) / 3600
            state_detail += f"Last unstick attempt: {unstick_ago:.1f}h ago\n"
    elif sm.state == LOCKOUT:
        state_detail = f"Reason: {sm.lockout_reason}\n"

    subject = (
        f"SUMP PUMP DIGEST: {sm.state} | {power:.0f}W | {temp:.0f}C"
    )
    body = (
        f"State: {sm.state} (for {sm.time_in_state() / 60:.0f} min)\n"
        f"{state_detail}\n"
        f"READINGS:\n"
        f"  Power:   {power:.1f}W\n"
        f"  Temp:    {temp:.1f}C\n"
        f"  Voltage: {voltage:.1f}V\n"
        f"  RSSI:    {rssi} dBm\n"
        f"  Uptime:  {uptime_h:.1f}h\n\n"
        f"Last pump run: {hours_since_run:.1f}h ago\n"
        f"Weather risk: {sm.cached_weather_risk:.2f}\n"
        f"Monitor PID: {os.getpid()}\n\n"
        f"Next digest in {DIGEST_INTERVAL_SECONDS // 60} min. "
        f"If you stop receiving these, the monitor may be down."
    )
    send_digest_notification(subject, body)


def ship_to_analyzer(reading):
    """Fire-and-forget: send telemetry to AI analyzer. Silent on failure."""
    if not AI_ENABLED:
        return
    try:
        requests.post(f"{AI_ANALYZER_URL}/ingest", json=reading, timeout=2)
    except Exception:
        pass


def fetch_weather_risk(sm):
    """Query AI analyzer for weather risk score. Updates sm.cached_weather_risk."""
    if not AI_ENABLED:
        return
    if (time.monotonic() - sm.last_weather_check) < 1800:
        return
    try:
        resp = requests.get(f"{AI_ANALYZER_URL}/api/weather-risk", timeout=5)
        risk = resp.json().get("risk_score", 0.3)
        sm.cached_weather_risk = risk
        sm.last_weather_check = time.monotonic()
        log(f"Weather risk updated: {risk:.2f} (rest multiplier: {sm.weather_multiplier():.1f}x)")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Float unstick routine — blocking (~2.5 min), actively manages relay
# ---------------------------------------------------------------------------

def attempt_float_unstick(sm, reason="manual"):
    """Rapid power cycling to dislodge stuck float. Blocking (~2.5 min).

    Sequence: OFF 10s, then N x (ON 5s / OFF 5s), then ON 60s and check.
    Returns True if pump stopped (may have worked), False if still stuck.
    """
    log(f"UNSTICK ATTEMPT: starting rapid cycle sequence ({reason})")
    send_notification(
        f"SUMP PUMP: Attempting float unstick ({reason})",
        f"Running rapid power cycle sequence to try to dislodge sediment.\n\n"
        f"Reason: {reason}\n"
        f"State: {sm.state}\n"
        f"Sequence: OFF 10s, then {UNSTICK_CYCLES}x (ON 5s / OFF 5s), then ON 60s check",
    )

    sm.last_unstick_at = time.monotonic()
    sm.save_state()

    # Phase 1: Full OFF — let water settle, float rest under gravity
    turn_off()
    time.sleep(10)

    # Phase 2: Rapid cycles — motor startup jolts + water turbulence
    for i in range(UNSTICK_CYCLES):
        turn_on()
        time.sleep(5)
        # Quick temp check mid-sequence
        if i == UNSTICK_CYCLES // 2:
            mid_status = get_power_status()
            if mid_status and mid_status["temp_c"] > TEMP_WARN_C:
                log(f"UNSTICK: aborting — temp {mid_status['temp_c']:.1f}C too high")
                turn_off()
                return False
        turn_off()
        time.sleep(5)
        log(f"UNSTICK: rapid cycle {i + 1}/{UNSTICK_CYCLES} complete")

    # Phase 3: Leave ON for 60s, let float settle, check if pump stops
    log("UNSTICK: holding ON for 60s to check if float cleared...")
    turn_on()
    time.sleep(60)

    status = get_power_status()
    if status and not is_pump_running(status):
        log(f"UNSTICK SUCCESS: pump idle ({status['power']:.1f}W) after sequence!")
        send_notification(
            "SUMP PUMP: Float unstick MAY HAVE WORKED!",
            f"After rapid cycling, pump is idle ({status['power']:.1f}W).\n"
            f"Entering COOLDOWN to confirm float is truly unstuck.\n\n"
            f"Temp: {status['temp_c']:.1f}C, Voltage: {status['voltage']:.1f}V",
        )
        sm.pre_cooldown_state = sm.state
        sm.transition(COOLDOWN, "unstick attempt succeeded")
        return True
    else:
        power = status["power"] if status else "unknown"
        log(f"UNSTICK FAILED: pump still running ({power}W)")
        send_notification(
            "Sump pump: float unstick attempt failed (steady state, will retry)",
            f"Pump still running ({power}W) after rapid cycling.\n"
            f"Resuming {sm.state} duty cycle. This is part of normal escalation handling.",
        )
        turn_off()  # return to OFF phase of current tier
        sm.duty_phase = PHASE_OFF
        sm.phase_started_at = time.monotonic()
        sm.save_state()
        return False


# ---------------------------------------------------------------------------
# State handlers — called once per poll cycle, must NOT block
# ---------------------------------------------------------------------------

def handle_normal(sm, status):
    """NORMAL: plug ON, float controls pump. Watch for stuck float."""
    power = status["power"]
    pump_running = is_pump_running(status)

    # Plug should be ON in normal mode
    if not status["output"]:
        if sm.running_since is not None:
            sm.output_recovery_until = max(
                sm.output_recovery_until,
                time.monotonic() + OUTPUT_RECOVERY_GRACE_SECONDS,
            )
        log("WARNING: Plug output is OFF unexpectedly! Turning back ON.")
        send_notification(
            "Sump pump: plug was turned off, monitor restored ON",
            "The Shelly plug output was found OFF during normal monitoring.\n"
            "This was not done by the monitor script.\n\n"
            "The plug has been turned back ON to protect against flooding.",
        )
        turn_on()
        return

    if pump_running:
        sm.last_pump_run = time.monotonic()
        sm.no_run_alerted = False
        sm.output_recovery_until = 0.0

        if sm.running_since is None:
            sm.running_since = time.monotonic()
            sm.power_anomaly_alerted = False
            log(f"Pump started running ({power:.1f}W, {status['temp_c']:.1f}C, "
                f"light={status.get('illumination', '?')})")

        # Power anomaly check (skip first 30s for startup transients)
        if (not sm.power_anomaly_alerted
                and sm.running_since
                and (time.monotonic() - sm.running_since) > 30
                and (power < PUMP_POWER_LOW or power > PUMP_POWER_HIGH)):
            log(f"POWER ANOMALY: {power:.1f}W outside expected range "
                f"({PUMP_POWER_LOW}-{PUMP_POWER_HIGH}W)")
            send_notification(
                "Sump pump: abnormal power draw",
                f"Pump is drawing {power:.1f}W, outside expected range "
                f"({PUMP_POWER_LOW:.0f}-{PUMP_POWER_HIGH:.0f}W).\n\n"
                f"{'Low power could indicate a failing motor or partial blockage.' if power < PUMP_POWER_LOW else 'High power could indicate a seized impeller or electrical fault.'}\n"
                f"Current: {status['current']:.2f}A, Voltage: {status['voltage']:.1f}V, "
                f"Temp: {status['temp_c']:.1f}C",
            )
            sm.power_anomaly_alerted = True

        # Stuck float detection — weather-aware threshold.
        # Dry weather: stay strict (3 min) because any sustained run is suspicious.
        # Wet weather: relax (default 6 min) because legitimate heavy-rain
        # cycles can pump for longer without being stuck.
        run_minutes = (time.monotonic() - sm.running_since) / 60
        try:
            w = weather_assess()
        except Exception:
            w = {"ok": False, "is_wet": False}
        threshold = MAX_RUN_MINUTES_WET if w.get("is_wet") else MAX_RUN_MINUTES
        if run_minutes >= threshold:
            tag = "wet" if w.get("is_wet") else "dry"
            start_power_cycle(sm, f"pump ran {run_minutes:.1f} min ({tag} threshold {threshold} min)")

    else:
        if sm.running_since is not None:
            if time.monotonic() < sm.output_recovery_until:
                return
            run_duration = (time.monotonic() - sm.running_since) / 60
            log(f"Pump stopped after {run_duration:.1f} min ({power:.1f}W, "
                f"{status['temp_c']:.1f}C, light={status.get('illumination', '?')})")
            sm.running_since = None
            sm.output_recovery_until = 0.0

        # No-run alert
        hours_since_run = (time.monotonic() - sm.last_pump_run) / 3600
        if hours_since_run >= NO_RUN_ALERT_HOURS and not sm.no_run_alerted:
            log(f"NO-RUN WARNING: Pump hasn't run in {hours_since_run:.1f} hours")
            try:
                w = weather_assess()
            except Exception:
                w = {"ok": False, "is_wet": False}
            wet = bool(w.get("is_wet"))
            weather_line = (
                f"Weather: recent_rain={w.get('recent_rain_mm')}mm, "
                f"current={w.get('current_rain_mm_h')}mm/h, "
                f"forecast_24h={w.get('forecast_24h_mm')}mm, is_wet={wet}."
            )
            send_notification(
                f"Sump pump: hasn't run in {hours_since_run:.0f} hours",
                f"The pump has not drawn power in {hours_since_run:.1f} hours "
                f"(alert threshold: {NO_RUN_ALERT_HOURS:.0f} hours).\n\n"
                "This could mean:\n"
                "- Float switch stuck DOWN (won't trigger when water rises)\n"
                "- Very low water table (normal in dry weather)\n"
                "- Pump disconnected from plug\n\n"
                f"{weather_line}\n"
                + ("Wet conditions: flood risk if float is stuck. Investigate."
                   if wet else
                   "Dry conditions: pump idling is likely expected, no action needed."),
                urgent=wet,
            )
            sm.no_run_alerted = True


def start_power_cycle(sm, reason):
    """Begin a power cycle. The main loop will advance it non-blockingly."""
    log(f"Pump stuck — starting power cycle ({reason})")
    sm.transition(POWER_CYCLE, reason)
    sm.duty_phase = PHASE_OFF
    sm.phase_started_at = time.monotonic()
    sm.running_since = None
    turn_off()
    log(f"POWER CYCLE: holding OFF for {POWER_CYCLE_OFF_SECONDS} seconds...")
    sm.save_state()


def handle_power_cycle(sm, status):
    """POWER_CYCLE: non-blocking off/settle sequence. Called every poll cycle."""
    elapsed = sm.time_in_phase()

    if sm.duty_phase == PHASE_OFF:
        if status and status["output"]:
            log("POWER CYCLE: plug drifted ON during OFF phase, turning OFF and restarting timer")
            turn_off()
            sm.phase_started_at = time.monotonic()
            sm.save_state()
            return
        if elapsed >= POWER_CYCLE_OFF_SECONDS:
            log(f"POWER CYCLE: OFF period complete after {elapsed:.0f}s, turning back ON")
            turn_on()
            sm.duty_phase = PHASE_ON
            sm.phase_started_at = time.monotonic()
            sm.save_state()
        return

    if sm.duty_phase != PHASE_ON:
        log(f"POWER CYCLE: invalid phase '{sm.duty_phase}', resetting to OFF")
        sm.duty_phase = PHASE_OFF
        sm.phase_started_at = time.monotonic()
        turn_off()
        sm.save_state()
        return

    if status and not status["output"]:
        log("POWER CYCLE: plug drifted OFF during settle phase, turning ON and restarting timer")
        turn_on()
        sm.phase_started_at = time.monotonic()
        sm.save_state()
        return

    if elapsed < POWER_CYCLE_SETTLE_SECONDS:
        return

    if status and is_pump_running(status):
        log(f"Pump STILL running after power cycle ({status['power']:.1f}W)")
        # Weather-aware: on dry days a stuck-closed float just pumps dry, so
        # skip the TIER_1/TIER_2 staircase and go straight to TIER_3's longest
        # rest period. Wet days still ramp through TIER_1->TIER_2->TIER_3 in
        # case there is real water and shorter rests are needed.
        try:
            w = weather_assess()
        except Exception:
            w = {"ok": False, "is_wet": False}
        target_tier = TIER_1 if w.get("is_wet") else TIER_3
        config = TIER_CONFIG[target_tier]
        sm.transition(target_tier, f"power cycle failed ({'wet -> TIER_1 staircase' if w.get('is_wet') else 'dry -> jump TIER_3'})")
        send_notification(
            f"Sump pump: stuck float — entering {target_tier} duty cycle",
            f"Float switch is stuck after power cycle attempt.\n\n"
            f"Weather: is_wet={w.get('is_wet')}, recent_rain={w.get('recent_rain_mm')}mm, "
            f"forecast_24h={w.get('forecast_24h_mm')}mm.\n"
            f"On dry days we skip directly to TIER_3 (longest rest) to minimize "
            f"dry-pump wear. On wet days we start at TIER_1.\n\n"
            f"{target_tier}: {config['run_s']}s ON / {config['rest_s'] // 60} min OFF.\n\n"
            f"This is normal escalation behavior; no immediate action required.",
        )
        # Start with plug OFF, first rest period
        turn_off()
    else:
        power = status["power"] if status else "unknown"
        log(f"Power cycle WORKED! Pump is idle ({power}W)")
        send_notification(
            "Sump pump: power cycle fixed stuck float",
            f"A power cycle was performed and the pump stopped.\n"
            "Float switch appears unstuck. Back to normal monitoring.",
        )
        sm.running_since = None
        sm.transition(NORMAL, "power cycle succeeded")


def reconcile_startup_state(sm, status):
    """Reconcile plug output and timers with the restored state on startup."""
    if status is None:
        return

    if sm.state == NORMAL:
        if sm.running_since is not None and sm.output_recovery_until <= time.monotonic():
            sm.output_recovery_until = time.monotonic() + OUTPUT_RECOVERY_GRACE_SECONDS
        if not status["output"]:
            log("Startup: NORMAL mode requires plug ON, turning ON...")
            turn_on()
            if sm.running_since is not None:
                log("Startup: preserving stuck-float timer while plug output recovers")
                return
            refreshed_status = get_power_status()
            if refreshed_status is not None:
                status = refreshed_status
        if is_pump_running(status):
            sm.output_recovery_until = 0.0
            if sm.running_since is None:
                sm.running_since = time.monotonic()
                log(f"Startup: pump already running ({status['power']:.1f}W), starting stuck-float timer")
            else:
                run_minutes = (time.monotonic() - sm.running_since) / 60
                log(f"Startup: resuming stuck-float timer at {run_minutes:.1f} min "
                    f"({status['power']:.1f}W)")
        elif sm.running_since is not None:
            if time.monotonic() < sm.output_recovery_until:
                log("Startup: waiting briefly before clearing restored running timer")
                return
            log("Startup: clearing stale running timer because pump is idle")
            sm.running_since = None
            sm.output_recovery_until = 0.0
        return

    if sm.state == POWER_CYCLE:
        if sm.duty_phase == PHASE_OFF and status["output"]:
            log("Startup: resuming power cycle OFF phase, turning plug OFF")
            turn_off()
        elif sm.duty_phase == PHASE_ON and not status["output"]:
            log("Startup: resuming power cycle settle phase, turning plug ON")
            turn_on()
        return

    if sm.is_tier():
        should_be_on = sm.duty_phase == PHASE_ON
        if should_be_on and not status["output"]:
            log(f"Startup: resuming {sm.state} ON phase, turning plug ON")
            turn_on()
        elif not should_be_on and status["output"]:
            log(f"Startup: resuming {sm.state} OFF phase, turning plug OFF")
            turn_off()
        return

    if sm.state == COOLDOWN:
        if not status["output"]:
            log("Startup: COOLDOWN keeps the plug ON, turning ON")
            turn_on()
        return

    if sm.state == LOCKOUT:
        if maybe_recover_shelly_lockout(sm, status, startup=True):
            return
        if is_shelly_connectivity_lockout(sm):
            return
        if status["output"]:
            log("Startup: LOCKOUT requires plug OFF, turning OFF")
            turn_off()


def next_sleep_seconds(sm):
    """Wake more frequently during active timing-sensitive phases."""
    default_sleep = float(POLL_INTERVAL_SECONDS)

    if sm.state == NORMAL and sm.running_since is not None:
        return max(1.0, min(default_sleep, ACTIVE_POLL_SECONDS))

    if sm.state == POWER_CYCLE:
        if sm.duty_phase == PHASE_OFF:
            remaining = max(0.0, POWER_CYCLE_OFF_SECONDS - sm.time_in_phase())
            return max(1.0, min(default_sleep, ACTIVE_POLL_SECONDS, remaining or 1.0))
        if sm.duty_phase == PHASE_ON:
            remaining = max(0.0, POWER_CYCLE_SETTLE_SECONDS - sm.time_in_phase())
            return max(1.0, min(default_sleep, ACTIVE_POLL_SECONDS, remaining or 1.0))
        return 1.0

    if sm.is_tier():
        config = sm.tier_config()
        if not config:
            return default_sleep
        if sm.duty_phase == PHASE_ON:
            remaining = max(0.0, config["run_s"] - sm.time_in_phase())
            return max(1.0, min(default_sleep, ACTIVE_POLL_SECONDS, remaining or 1.0))
        if sm.duty_phase == PHASE_OFF:
            remaining = max(0.0, sm.adjusted_rest() - sm.time_in_phase())
            return max(1.0, min(default_sleep, remaining or 1.0))

    if sm.state == COOLDOWN:
        remaining = max(0.0, COOLDOWN_STABLE_SECONDS - (time.monotonic() - sm.cooldown_started_at))
        return max(1.0, min(default_sleep, ACTIVE_POLL_SECONDS, remaining or 1.0))

    return default_sleep


def handle_duty_cycle(sm, status):
    """TIER_1/2/3: non-blocking duty cycle. Called every poll cycle."""
    config = sm.tier_config()
    if not config:
        sm.transition(NORMAL, "invalid tier config")
        return

    elapsed = sm.time_in_phase()

    if sm.duty_phase == PHASE_OFF:
        if status["output"]:
            log(f"{sm.state}: plug is ON during OFF phase, turning OFF and restarting rest timer")
            turn_off()
            sm.phase_started_at = time.monotonic()
            return
        adjusted_rest = sm.adjusted_rest()
        if elapsed >= adjusted_rest:
            # Rest period is over — turn ON
            sm.cycle_count += 1
            log(f"{sm.state} cycle {sm.cycle_count}: turning ON for {config['run_s']}s")
            turn_on()
            sm.duty_phase = PHASE_ON
            sm.phase_started_at = time.monotonic()
        # else: still resting, nothing to do

    elif sm.duty_phase == PHASE_ON:
        if not status["output"]:
            log(f"{sm.state}: plug is OFF during ON phase, turning ON and restarting pulse timer")
            turn_on()
            sm.phase_started_at = time.monotonic()
            return
        pump_running = is_pump_running(status)

        if elapsed >= config["run_s"]:
            # ON period is over — evaluate and turn OFF
            if pump_running:
                sm.consecutive_dry = 0
                log(f"{sm.state} cycle {sm.cycle_count}: pump ran "
                    f"({status['power']:.1f}W, {status['temp_c']:.1f}C)")
            else:
                sm.consecutive_dry += 1
                log(f"{sm.state} cycle {sm.cycle_count}: DRY "
                    f"({status['power']:.1f}W) — streak {sm.consecutive_dry}/{DRY_EXIT_THRESHOLD}")

                if sm.consecutive_dry >= DRY_EXIT_THRESHOLD:
                    log(f"{sm.state}: {DRY_EXIT_THRESHOLD} consecutive dry pulses — "
                        "water table is low")
                    turn_on()  # leave plug ON for normal float operation
                    send_notification(
                        f"Sump pump: {sm.state} → NORMAL (water table low)",
                        f"{sm.state} ran {sm.cycle_count} cycles. "
                        f"Last {DRY_EXIT_THRESHOLD} pulses drew no power.\n\n"
                        "Switching to normal monitoring (plug ON, float controls pump).",
                    )
                    sm.running_since = None
                    sm.transition(NORMAL, "dry pulses")
                    return

            turn_off()
            sm.duty_phase = PHASE_OFF
            sm.phase_started_at = time.monotonic()

            # Check escalation
            if (config["escalate_after"]
                    and sm.cycle_count >= config["escalate_after"]
                    and sm.consecutive_dry < DRY_EXIT_THRESHOLD):
                next_tier = config["next"]
                next_config = TIER_CONFIG[next_tier]
                # Try unstick before escalating
                log(f"Pre-escalation unstick attempt before {sm.state} -> {next_tier}")
                if attempt_float_unstick(sm, f"pre-escalation {sm.state}->{next_tier}"):
                    return  # unstick worked, now in COOLDOWN
                log(f"Escalating: {sm.state} -> {next_tier} "
                    f"(after {sm.cycle_count} cycles)")
                send_notification(
                    f"Sump pump: escalating to {next_tier} (steady state)",
                    f"Float still stuck after {sm.cycle_count} cycles of {sm.state}.\n\n"
                    f"Escalating to {next_tier}: {next_config['run_s']}s ON / "
                    f"{next_config['rest_s'] // 60} min OFF.\n\n"
                    f"This is designed escalation behavior, not urgent.",
                )
                sm.transition(next_tier, f"escalation from {sm.state}")
                turn_off()

            # TIER_3: alert every cycle + periodic unstick attempts
            elif sm.state == TIER_3:
                # Try periodic unstick every UNSTICK_INTERVAL_SECONDS
                since_unstick = time.monotonic() - sm.last_unstick_at
                if since_unstick >= UNSTICK_INTERVAL_SECONDS:
                    log(f"TIER_3: periodic unstick attempt (last was {since_unstick / 3600:.1f}h ago)")
                    if attempt_float_unstick(sm, "periodic TIER_3"):
                        return  # unstick worked
                else:
                    send_notification(
                        f"Sump pump: TIER 3 cycle {sm.cycle_count} (steady state max duty)",
                        f"Float is still stuck. TIER 3 is the maximum duty cycle.\n\n"
                        f"Pump: {status['power']:.1f}W, Temp: {status['temp_c']:.1f}C\n"
                        f"Cycle {sm.cycle_count}, running {config['run_s']}s ON / "
                        f"{config['rest_s'] // 60} min OFF.\n\n"
                        f"Next unstick attempt in {(UNSTICK_INTERVAL_SECONDS - since_unstick) / 60:.0f} min.\n"
                        f"Manual trigger: touch /var/tmp/sump_pump_unstick_trigger",
                    )

            # Periodic updates for TIER_1 and TIER_2
            elif sm.cycle_count > 0 and sm.cycle_count % 3 == 0:
                total_min = sm.time_in_state() / 60
                send_notification(
                    f"Sump pump: {sm.state} update (cycle {sm.cycle_count})",
                    f"{sm.state} has been running for {total_min:.0f} min "
                    f"({sm.cycle_count} cycles).\n\n"
                    f"Pump: {status['power']:.1f}W, Temp: {status['temp_c']:.1f}C\n"
                    f"Dry streak: {sm.consecutive_dry}/{DRY_EXIT_THRESHOLD}\n\n"
                    f"Please manually investigate when possible.",
                )

        else:
            # Still in ON phase — check if pump stopped (float may have unstuck)
            if elapsed > 10 and not pump_running:
                log(f"{sm.state}: Pump stopped on its own ({status['power']:.1f}W) — "
                    "float may be unstuck!")
                sm.pre_cooldown_state = sm.state
                sm.transition(COOLDOWN, "pump stopped during ON pulse")
                # Leave plug ON during cooldown to see if it stays idle


def handle_cooldown(sm, status):
    """COOLDOWN: confirm pump stays idle before returning to NORMAL."""
    pump_running = is_pump_running(status)

    if pump_running:
        # Pump restarted — float re-stuck or still has water
        prev = sm.pre_cooldown_state or TIER_1
        log(f"COOLDOWN: Pump running again ({status['power']:.1f}W) — "
            f"returning to {prev}")
        sm.transition(prev, "pump restarted during cooldown")
        turn_off()
        return

    elapsed = time.monotonic() - sm.cooldown_started_at
    if elapsed >= COOLDOWN_STABLE_SECONDS:
        log(f"COOLDOWN: Pump idle for {elapsed:.0f}s — confirmed unstuck!")
        send_notification(
            "Sump pump: float unstuck, back to normal",
            f"Pump stayed idle for {COOLDOWN_STABLE_SECONDS}s during cooldown.\n"
            "Float switch appears to have unstuck.\n\n"
            "Returning to normal monitoring mode.",
        )
        turn_on()  # ensure plug is ON for normal float operation
        sm.running_since = None
        sm.transition(NORMAL, "cooldown confirmed")


def handle_lockout(sm, status):
    """LOCKOUT: pump is OFF, manual intervention required. Just log periodically."""
    # Alert every 30 minutes while in lockout
    elapsed = sm.time_in_state()
    if elapsed > 0 and int(elapsed) % 1800 < POLL_INTERVAL_SECONDS:
        log(f"LOCKOUT: still locked out ({sm.lockout_reason}), {elapsed / 60:.0f} min")


def check_overtemp(sm, status):
    """Check temperature. Returns True if lockout triggered."""
    if not status:
        return False

    temp = status["temp_c"]

    # Hard Shelly electronics cutoff. This is terminal until manual restart.
    if temp >= SHELLY_HARD_TEMP_C:
        log(f"TEMP HARD SAFETY: {temp:.1f}C exceeds {SHELLY_HARD_TEMP_C}C! Forcing OFF.")
        turn_off()
        send_notification(
            "SUMP PUMP: HARD OVERTEMP LOCKOUT",
            f"Plug temperature reached {temp:.1f}C (hard limit: {SHELLY_HARD_TEMP_C}C).\n"
            f"Pump has been forced OFF.\n\n"
            f"State was: {sm.state}\n"
            f"Manual restart required after temperature drops.\n\n"
            f"Please investigate immediately.",
            urgent=True,
        )
        sm.transition(LOCKOUT, f"hard overtemp {temp:.1f}C")
        return True

    # Soft thermal threshold. Keep existing LOCKOUT behavior as a cooling duty
    # threshold, but do not confuse it with the hard electronics cutoff.
    if temp > MAX_TEMP_C:
        log(f"TEMP SAFETY: {temp:.1f}C exceeds soft threshold {MAX_TEMP_C}C. Forcing OFF.")
        turn_off()
        send_notification(
            "SUMP PUMP: thermal cooling lockout",
            f"Plug temperature reached {temp:.1f}C (soft threshold: {MAX_TEMP_C}C).\n"
            f"Pump has been forced OFF for cooling.\n\n"
            f"State was: {sm.state}\n"
            f"Hard cutoff remains {SHELLY_HARD_TEMP_C}C.\n\n"
            f"Please investigate if water is still entering the pit.",
        )
        sm.transition(LOCKOUT, f"overtemp {temp:.1f}C")
        return True

    # Warning
    if temp > TEMP_WARN_C and not sm.temp_warned:
        log(f"TEMP WARNING: {temp:.1f}C approaching limit ({MAX_TEMP_C}C)")
        send_notification(
            "Sump pump: temperature rising",
            f"Plug temperature is {temp:.1f}C (warning: {TEMP_WARN_C}C, "
            f"cutoff: {MAX_TEMP_C}C).\n\n"
            f"Pump power: {status['power']:.1f}W. Temperature is rising but not yet critical.\n"
            f"Emergency shutoff will trigger at {MAX_TEMP_C}C.",
        )
        sm.temp_warned = True
    elif temp <= TEMP_WARN_C:
        sm.temp_warned = False

    return False


def check_shelly_lost(sm):
    """Check if Shelly has been unreachable too long. Returns True if lockout triggered."""
    elapsed = time.monotonic() - sm.last_shelly_contact

    if elapsed >= SHELLY_CRITICAL_SECONDS:
        if not sm.shelly_critical_alerted:
            log(f"SHELLY CRITICAL: unreachable for {elapsed / 60:.0f} min")
            send_notification(
                "SUMP PUMP: CRITICAL - Shelly offline 30+ minutes",
                f"The Shelly plug has been unreachable for {elapsed / 60:.0f} minutes.\n"
                "The pump is completely unprotected.\n\n"
                "Possible causes:\n"
                "- Extended firmware crash loop\n"
                "- WiFi outage\n"
                "- Power outage to the plug\n\n"
                "Please physically check the sump pump and Shelly plug.",
                urgent=True,
            )
            sm.shelly_critical_alerted = True
            sm.transition(LOCKOUT, f"Shelly unreachable {elapsed / 60:.0f} min")
            return True

    elif elapsed >= SHELLY_WARN_SECONDS:
        if not sm.shelly_warn_alerted:
            log(f"SHELLY LOST: unreachable for {elapsed:.0f}s")
            send_notification(
                "SUMP PUMP: Shelly plug unreachable",
                f"Cannot reach Shelly at {SHELLY_IP} for {elapsed / 60:.0f} minutes.\n\n"
                "Possible causes:\n"
                "- Firmware crash (check for reboot when it comes back)\n"
                "- WiFi outage\n"
                "- Power outage to the plug\n\n"
                "The pump is UNMONITORED until connectivity returns.",
            )
            sm.shelly_warn_alerted = True

    return False


def detect_reboot(sm, status):
    """Detect Shelly plug reboots via uptime counter."""
    uptime = status.get("uptime", 0)
    reset_reason = status.get("reset_reason", -1)

    if sm.last_uptime is not None and uptime < sm.last_uptime:
        if uptime < 300:
            # Real reboot
            reset_names = {
                1: "POWERON (power-on)", 3: "SW_RESET (software/panic restart)",
                4: "PANIC (CPU exception)", 5: "INT_WDT (interrupt watchdog)",
                6: "TASK_WDT (task watchdog)", 9: "BROWNOUT (voltage drop)",
            }
            reset_name = reset_names.get(reset_reason, f"unknown ({reset_reason})")
            was_down = sm.last_uptime - uptime + POLL_INTERVAL_SECONDS
            log(f"PLUG REBOOTED: uptime {sm.last_uptime}s -> {uptime}s — "
                f"reset_reason={reset_reason} ({reset_name})")
            send_notification(
                f"Sump pump: plug rebooted ({reset_name})",
                f"The Shelly plug rebooted.\n\n"
                f"RESET REASON: {reset_name}\n"
                f"  1 = power-on, 3 = software/panic restart, 4 = CPU exception\n"
                f"  5 = interrupt watchdog, 6 = task watchdog, 9 = brownout\n\n"
                f"TIMING:\n"
                f"  Previous uptime: {sm.last_uptime:,}s ({sm.last_uptime / 3600:.1f}h)\n"
                f"  Current uptime:  {uptime}s (down ~{was_down}s)\n\n"
                f"DEVICE STATE:\n"
                f"  Output:   {'ON' if status['output'] else 'OFF'}\n"
                f"  Power:    {status['power']:.1f}W\n"
                f"  Voltage:  {status['voltage']:.1f}V\n"
                f"  Temp:     {status['temp_c']:.1f}C\n"
                f"  RAM free: {status.get('ram_free', 0):,} bytes\n"
                f"  WiFi:     {status.get('ssid', '?')} (RSSI {status.get('rssi', '?')} dBm)\n\n"
                f"Monitor state: {sm.state}\n"
                f"See: https://github.com/toddllm/home-assistant/issues/1",
            )

            # Post-reboot: if in NORMAL and pump is running, start stuck-float timer
            if sm.state == NORMAL and is_pump_running(status) and sm.running_since is None:
                sm.running_since = time.monotonic()
                log(f"Post-reboot: pump already running ({status['power']:.1f}W), "
                    "starting stuck-float timer")

            # Post-reboot: if in a tier, plug may be OFF — turn ON if we're in ON phase
            if sm.is_tier() and sm.duty_phase == PHASE_ON and not status["output"]:
                log(f"Post-reboot: restoring plug ON (was in {sm.state} ON phase)")
                turn_on()

        else:
            log(f"Uptime counter glitch: {sm.last_uptime}s -> {uptime}s "
                f"(delta={sm.last_uptime - uptime}s, ignoring)")

    sm.last_uptime = uptime


def check_voltage(sm, status):
    """Check voltage and alert if out of range."""
    voltage = status["voltage"]
    if voltage < VOLTAGE_LOW or voltage > VOLTAGE_HIGH:
        if not sm.voltage_alerted:
            level = "LOW (brownout)" if voltage < VOLTAGE_LOW else "HIGH (overvoltage)"
            log(f"VOLTAGE WARNING: {voltage:.1f}V — {level}")
            send_notification(
                f"Sump pump: voltage {level}",
                f"Grid voltage is {voltage:.1f}V (normal range: {VOLTAGE_LOW}-{VOLTAGE_HIGH}V).\n\n"
                f"{'Low voltage can damage the pump motor and cause overheating.' if voltage < VOLTAGE_LOW else 'High voltage can damage electronics and the pump motor.'}\n"
                f"Power: {status['power']:.1f}W, Temp: {status['temp_c']:.1f}C",
                urgent=True,
            )
            sm.voltage_alerted = True
    else:
        sm.voltage_alerted = False


def check_wifi(sm, status):
    """Check WiFi signal strength."""
    rssi = status.get("rssi", 0)
    if rssi < RSSI_WARN:
        if not sm.rssi_alerted:
            log(f"WIFI WARNING: RSSI {rssi} dBm is below {RSSI_WARN} dBm")
            send_notification(
                "Sump pump: weak WiFi signal",
                f"WiFi signal strength dropped to {rssi} dBm "
                f"(warn threshold: {RSSI_WARN} dBm).\n\n"
                "If signal degrades further, the monitor may lose connectivity to the plug.\n"
                "Consider moving the router closer or adding a WiFi extender.",
            )
            sm.rssi_alerted = True
    else:
        sm.rssi_alerted = False


def check_illumination(sm, status):
    """Track illumination changes."""
    illumination = status.get("illumination", "unknown")
    if sm.last_illumination is not None and illumination != sm.last_illumination:
        log(f"Light changed: {sm.last_illumination} -> {illumination}")
        if sm.last_illumination == "dark" and illumination != "dark":
            send_notification(
                "Sump pump: light level changed",
                f"Illumination changed from '{sm.last_illumination}' to '{illumination}'.\n\n"
                "This could mean someone is in the basement or water is reflecting light.\n"
                f"Pump power: {status['power']:.1f}W, Temp: {status['temp_c']:.1f}C",
            )
    sm.last_illumination = illumination


def shutdown(signum, frame):
    log("Shutting down monitor. Leaving plug in current state.")
    cleanup_pid()
    sys.exit(0)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


if __name__ == "__main__":
    log("=== Sump Pump Monitor Started ===")

    # Single instance check
    if not check_single_instance(log):
        sys.exit(1)

    # Config validation
    validate_config(log, send_notification, {
        "POLL_INTERVAL_SECONDS": POLL_INTERVAL_SECONDS,
        "ACTIVE_POLL_SECONDS": ACTIVE_POLL_SECONDS,
        "OUTPUT_RECOVERY_GRACE_SECONDS": OUTPUT_RECOVERY_GRACE_SECONDS,
        "SHELLY_RECOVERY_STABLE_SECONDS": SHELLY_RECOVERY_STABLE_SECONDS,
        "MAX_RUN_MINUTES": MAX_RUN_MINUTES,
        "POWER_CYCLE_OFF_SECONDS": POWER_CYCLE_OFF_SECONDS,
        "POWER_CYCLE_SETTLE_SECONDS": POWER_CYCLE_SETTLE_SECONDS,
        "TIER_1_RUN_SECONDS": TIER_CONFIG[TIER_1]["run_s"],
        "TIER_1_REST_SECONDS": TIER_CONFIG[TIER_1]["rest_s"],
        "TIER_2_RUN_SECONDS": TIER_CONFIG[TIER_2]["run_s"],
        "TIER_2_REST_SECONDS": TIER_CONFIG[TIER_2]["rest_s"],
        "TIER_3_RUN_SECONDS": TIER_CONFIG[TIER_3]["run_s"],
        "TIER_3_REST_SECONDS": TIER_CONFIG[TIER_3]["rest_s"],
    })

    # Log config
    log(f"Shelly IP: {SHELLY_IP}")
    log(f"Power threshold: {POWER_THRESHOLD_WATTS}W")
    log(f"Max continuous run: {MAX_RUN_MINUTES} min dry / {MAX_RUN_MINUTES_WET} min wet before intervention")
    log(f"Power cycle: {POWER_CYCLE_OFF_SECONDS}s OFF / {POWER_CYCLE_SETTLE_SECONDS}s settle")
    log(f"Temp warning: {TEMP_WARN_C}C, cutoff: {MAX_TEMP_C}C")
    log(f"Shelly hard temp cutoff: {SHELLY_HARD_TEMP_C}C")
    log(f"Voltage range: {VOLTAGE_LOW}-{VOLTAGE_HIGH}V")
    log(f"Expected pump power: {PUMP_POWER_LOW}-{PUMP_POWER_HIGH}W")
    log(f"WiFi RSSI warning: {RSSI_WARN} dBm")
    log(f"No-run alert after: {NO_RUN_ALERT_HOURS}h")
    log(f"Poll interval: {POLL_INTERVAL_SECONDS}s")
    log(f"Active poll interval: {ACTIVE_POLL_SECONDS:.0f}s")
    log(f"Output recovery grace: {OUTPUT_RECOVERY_GRACE_SECONDS:.0f}s")
    log(f"Shelly lockout recovery: {SHELLY_RECOVERY_STABLE_SECONDS:.0f}s stable connectivity")
    log(f"AI analyzer: {'enabled -> ' + AI_ANALYZER_URL if AI_ENABLED else 'disabled'}")
    log(f"Status digest: every {DIGEST_INTERVAL_SECONDS // 60} min" if DIGEST_INTERVAL_SECONDS > 0 else "Status digest: disabled")
    log(f"Float unstick: {UNSTICK_CYCLES} cycles, interval {UNSTICK_INTERVAL_SECONDS // 3600}h")
    for tier, cfg in TIER_CONFIG.items():
        esc = f", escalates after {cfg['escalate_after']} cycles" if cfg["escalate_after"] else ", max tier"
        log(f"  {tier}: {cfg['run_s']}s ON / {cfg['rest_s'] // 60} min OFF{esc}")

    # Initialize state machine
    sm = PumpStateMachine(log)
    if not sm.load_state():
        log("Starting in NORMAL mode (no saved state)")

    startup_status = get_power_status()
    reconcile_startup_state(sm, startup_status)
    sm.save_state()

    log(f"Monitor ready: {sm.format_status()}")

    # Send startup digest immediately
    if DIGEST_INTERVAL_SECONDS > 0:
        log(f"Status digest enabled: every {DIGEST_INTERVAL_SECONDS // 60} min")
        startup_digest_status = startup_status or get_power_status()
        send_status_digest(sm, startup_digest_status)

    # Heartbeat: log a status line every N poll cycles (default ~10 min)
    HEARTBEAT_SECONDS = 600
    last_heartbeat_at = time.monotonic()
    last_digest_at = time.monotonic()

    # -----------------------------------------------------------------------
    # Main loop — non-blocking, runs every POLL_INTERVAL_SECONDS
    # -----------------------------------------------------------------------
    while True:
        try:
            write_monitor_heartbeat()
            status = get_power_status()

            if status is None:
                log("WARNING: Could not reach Shelly, retrying next cycle")
                sm.shelly_recovered_at = None
                if check_shelly_lost(sm):
                    pass  # transitioned to LOCKOUT
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Shelly is reachable — reset connectivity tracking
            sm.last_shelly_contact = time.monotonic()
            if sm.shelly_recovered_at is None:
                sm.shelly_recovered_at = time.monotonic()
            if sm.shelly_warn_alerted or sm.shelly_critical_alerted:
                log("Shelly connectivity restored")
                sm.shelly_warn_alerted = False
                sm.shelly_critical_alerted = False

            # Ship telemetry
            ship_to_analyzer(status)

            # Fetch weather risk periodically
            fetch_weather_risk(sm)

            # Safety checks (always active, all states)
            if check_overtemp(sm, status):
                sm.save_state()
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            if maybe_recover_shelly_lockout(sm, status):
                sm.save_state()
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Informational checks
            detect_reboot(sm, status)
            check_voltage(sm, status)
            check_wifi(sm, status)
            check_illumination(sm, status)

            # State-specific handling
            if sm.state == NORMAL:
                handle_normal(sm, status)
            elif sm.state == POWER_CYCLE:
                handle_power_cycle(sm, status)
            elif sm.is_tier():
                handle_duty_cycle(sm, status)
            elif sm.state == COOLDOWN:
                handle_cooldown(sm, status)
            elif sm.state == LOCKOUT:
                handle_lockout(sm, status)

            # Heartbeat log
            if (time.monotonic() - last_heartbeat_at) >= HEARTBEAT_SECONDS:
                power = status["power"]
                temp = status["temp_c"]
                uptime_h = status.get("uptime", 0) / 3600
                rssi = status.get("rssi", 0)
                log(f"HEARTBEAT: {sm.state} | {power:.1f}W | {temp:.1f}C | "
                    f"{status['voltage']:.1f}V | RSSI {rssi} | "
                    f"uptime {uptime_h:.1f}h")
                last_heartbeat_at = time.monotonic()

            # Remote unstick trigger (touch /var/tmp/sump_pump_unstick_trigger)
            if UNSTICK_TRIGGER.exists():
                try:
                    UNSTICK_TRIGGER.unlink()
                except OSError:
                    pass
                if sm.is_tier() or sm.state == NORMAL:
                    attempt_float_unstick(sm, "remote trigger")

            # Status digest notification
            if DIGEST_INTERVAL_SECONDS > 0:
                if (time.monotonic() - last_digest_at) >= DIGEST_INTERVAL_SECONDS:
                    send_status_digest(sm, status)
                    last_digest_at = time.monotonic()

            # Persist state every cycle
            sm.save_state()

        except Exception as e:
            log(f"ERROR in main loop: {e}")

        time.sleep(next_sleep_seconds(sm))
