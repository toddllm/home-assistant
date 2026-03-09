#!/usr/bin/env python3
"""
Sump Pump Monitor - Shelly Plug US Gen4
Monitors power draw to detect a stuck float switch.

Modes:
  NORMAL  - Plug ON, pump runs/stops on its own via float. Monitor power.
  STUCK   - Float stuck detected. Power cycle attempted. If unstuck -> NORMAL.
  SAFE    - Float still stuck after power cycle. Duty cycle:
            run 5 min (clear water), rest 10 min (prevent overheating).
            Keeps pumping to prevent flooding while protecting the pump.
            Returns to NORMAL if pump stops on its own during a run cycle.
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

# Shelly config
SHELLY_IP = os.environ["SHELLY_IP"]
SHELLY_USER = os.environ["SHELLY_USER"]
SHELLY_PASSWORD = os.environ["SHELLY_PASSWORD"]


# Thresholds
POWER_THRESHOLD_WATTS = float(os.environ.get("POWER_THRESHOLD_WATTS", "100.0"))
MAX_RUN_MINUTES = float(os.environ.get("MAX_RUN_MINUTES", "3"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

# Power cycle config
POWER_CYCLE_OFF_SECONDS = 10
POWER_CYCLE_SETTLE_SECONDS = 60

# Safe mode duty cycle
SAFE_RUN_SECONDS = int(os.environ.get("SAFE_RUN_SECONDS", "120"))
SAFE_REST_SECONDS = int(os.environ.get("SAFE_REST_SECONDS", "600"))
SAFE_UPDATE_INTERVAL = 3

# Conservative mode: plug OFF by default, brief ON pulses, no auto-exit
CONSERVATIVE_MODE = os.environ.get("CONSERVATIVE_MODE", "false").lower() == "true"
CONSERVATIVE_RUN_SECONDS = int(os.environ.get("CONSERVATIVE_RUN_SECONDS", "30"))
CONSERVATIVE_REST_SECONDS = int(os.environ.get("CONSERVATIVE_REST_SECONDS", "7200"))

# Spring mode: plug OFF by default, regular ON pulses to clear water
# Auto-exits to NORMAL if pump draws no power during a pulse (water table low)
SPRING_MODE = os.environ.get("SPRING_MODE", "false").lower() == "true"
SPRING_RUN_SECONDS = int(os.environ.get("SPRING_RUN_SECONDS", "60"))
SPRING_REST_SECONDS = int(os.environ.get("SPRING_REST_SECONDS", "1800"))

# Temperature safety
MAX_TEMP_C = float(os.environ.get("MAX_TEMP_C", "60.0"))
TEMP_WARN_C = float(os.environ.get("TEMP_WARN_C", "50.0"))

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

# AI Analyzer (optional — runs on Mac with Ollama)
AI_ENABLED = os.environ.get("AI_ENABLED", "false").lower() == "true"
AI_ANALYZER_URL = os.environ.get("AI_ANALYZER_URL", "http://localhost:8078")

# Notification config
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAILS = [e.strip() for e in os.environ.get("NOTIFY_EMAIL", GMAIL_USER).split(",") if e.strip()]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

# Modes
MODE_NORMAL = "NORMAL"
MODE_SAFE = "SAFE"
MODE_CONSERVATIVE = "CONSERVATIVE"
MODE_SPRING = "SPRING"


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def shelly_rpc(method, params=None):
    """Call a Shelly RPC method via HTTP with digest auth (uses curl for SHA-256 support)."""
    url = f"http://{SHELLY_IP}/rpc/{method}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{query}"
    try:
        result = subprocess.run(
            ["curl", "-s", "-u", f"{SHELLY_USER}:{SHELLY_PASSWORD}",
             "--digest", "--connect-timeout", "5", "--max-time", "10",
             "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=15,
        )
        lines = result.stdout.rsplit("\n", 1)
        body = lines[0] if len(lines) > 1 else result.stdout
        http_code = lines[-1].strip() if len(lines) > 1 else "0"
        if result.returncode != 0 or http_code != "200" or not body.strip():
            log(f"ERROR: Shelly RPC '{method}' failed: HTTP {http_code}")
            return None
        return json.loads(body)
    except Exception as e:
        log(f"ERROR: Shelly RPC '{method}' failed: {e}")
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


def check_temp_safety(status):
    """Force off if plug temperature is dangerously high."""
    if status and status["temp_c"] > MAX_TEMP_C:
        log(f"TEMP SAFETY: {status['temp_c']:.1f}C exceeds {MAX_TEMP_C}C! Forcing OFF.")
        turn_off()
        send_notification(
            "SUMP PUMP: OVERTEMP SAFETY SHUTOFF",
            f"Plug temperature reached {status['temp_c']:.1f}C (limit: {MAX_TEMP_C}C).\n"
            f"Pump has been forced OFF to prevent damage.\n\n"
            f"Please investigate immediately."
        )
        return True
    return False


def power_cycle():
    """Turn off, wait, turn back on. Returns True if pump is still running (stuck)."""
    log("POWER CYCLE: turning off for 10 seconds...")
    turn_off()
    time.sleep(POWER_CYCLE_OFF_SECONDS)

    log("POWER CYCLE: turning back on, waiting 60s to settle...")
    turn_on()
    time.sleep(POWER_CYCLE_SETTLE_SECONDS)

    status = get_power_status()
    if is_pump_running(status):
        log(f"Pump STILL running after power cycle ({status['power']:.1f}W)")
        return True
    else:
        power = status["power"] if status else "unknown"
        log(f"Power cycle WORKED! Pump is idle ({power}W)")
        return False


def send_notification(subject, body):
    """Send email and ntfy push notifications."""
    # Email (supports multiple comma-separated recipients)
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = ", ".join(NOTIFY_EMAILS)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, NOTIFY_EMAILS, msg.as_string())
        log(f"Alert sent via email to {', '.join(NOTIFY_EMAILS)}")
    except Exception as e:
        log(f"ERROR: Failed to send email alert: {e}")

    # ntfy push notification
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": subject, "Priority": "high", "Tags": "warning"},
        )
        urllib.request.urlopen(req, timeout=10)
        log(f"Alert sent via ntfy to {NTFY_TOPIC}")
    except Exception as e:
        log(f"ERROR: Failed to send ntfy alert: {e}")


def ship_to_analyzer(reading):
    """Fire-and-forget: send telemetry to AI analyzer. Silent on failure."""
    if not AI_ENABLED:
        return
    try:
        requests.post(
            f"{AI_ANALYZER_URL}/ingest",
            json=reading,
            timeout=2,
        )
    except Exception:
        pass


def run_safe_mode():
    """
    Duty cycle to protect pump while still clearing water.
    Returns to caller when float unsticks (pump stops on its own).
    """
    run_min = SAFE_RUN_SECONDS // 60
    rest_min = SAFE_REST_SECONDS // 60
    log("=== ENTERING SAFE MODE (duty cycle) ===")
    send_notification(
        "SUMP PUMP: Entering safe mode (float stuck)",
        f"Float switch is stuck after power cycle attempt.\n\n"
        f"Safe mode activated: pump will run {run_min} min ON / {rest_min} min OFF\n"
        f"to keep pumping water without overheating.\n\n"
        f"Please manually unstick the float when you can."
    )

    cycle_count = 0

    while True:
        cycle_count += 1
        log(f"SAFE MODE cycle {cycle_count}: turning ON for {SAFE_RUN_SECONDS // 60} min")
        turn_on()

        # Monitor during run period — check every 30s if pump stops on its own
        run_start = time.time()
        pump_stopped_naturally = False

        while time.time() - run_start < SAFE_RUN_SECONDS:
            time.sleep(POLL_INTERVAL_SECONDS)
            status = get_power_status()

            if status is None:
                continue

            # Temperature safety check
            if check_temp_safety(status):
                log("SAFE MODE: overtemp, resting early")
                break

            # If pump is no longer drawing power, float may have unstuck
            if not is_pump_running(status):
                log(f"SAFE MODE: Pump stopped on its own ({status['power']:.1f}W) - float may be unstuck!")
                pump_stopped_naturally = True
                break

        if pump_stopped_naturally:
            # Wait a bit and confirm it stays off
            time.sleep(60)
            status = get_power_status()
            if status and not is_pump_running(status):
                log("SAFE MODE: Confirmed pump is idle. Float unstuck! Returning to NORMAL mode.")
                send_notification(
                    "Sump pump: float unstuck, back to normal",
                    f"During safe mode cycle {cycle_count}, the pump stopped on its own.\n"
                    "Float switch appears to have unstuck.\n\n"
                    "Returning to normal monitoring mode."
                )
                return
            else:
                log("SAFE MODE: Pump started again — still has water or float re-stuck. Continuing.")

        # Rest period
        log(f"SAFE MODE cycle {cycle_count}: turning OFF for {SAFE_REST_SECONDS // 60} min rest")
        turn_off()

        # Send periodic updates
        if cycle_count % SAFE_UPDATE_INTERVAL == 0:
            send_notification(
                f"Sump pump: still in safe mode (cycle {cycle_count})",
                f"Safe mode has been running for {cycle_count} duty cycles "
                f"({cycle_count * (SAFE_RUN_SECONDS + SAFE_REST_SECONDS) // 60} minutes total).\n\n"
                f"Pump is being cycled: {SAFE_RUN_SECONDS // 60} min ON / "
                f"{SAFE_REST_SECONDS // 60} min OFF.\n\n"
                "Please manually investigate when possible."
            )

        time.sleep(SAFE_REST_SECONDS)


def run_conservative_mode():
    """
    Conservative mode: plug stays OFF, brief ON pulses to clear water.
    Does NOT auto-exit — requires manual restart with CONSERVATIVE_MODE=false.
    """
    run_s = CONSERVATIVE_RUN_SECONDS
    rest_s = CONSERVATIVE_REST_SECONDS
    log(f"=== ENTERING CONSERVATIVE MODE ({run_s}s ON / {rest_s // 3600}h OFF) ===")
    log("Plug will stay OFF by default. Will NOT auto-exit. Restart monitor with CONSERVATIVE_MODE=false to resume normal operation.")
    send_notification(
        "SUMP PUMP: Conservative mode activated",
        f"Float switch is stuck. Conservative mode activated.\n\n"
        f"Pump will run {run_s}s every {rest_s // 3600}h to clear water.\n"
        f"Plug stays OFF between pulses.\n\n"
        f"This mode will NOT auto-exit.\n"
        f"Restart monitor with CONSERVATIVE_MODE=false to resume normal operation."
    )

    # Start with plug OFF
    turn_off()
    cycle_count = 0

    while True:
        # Rest first (plug OFF)
        log(f"CONSERVATIVE: resting for {rest_s // 60} min (plug OFF)")
        time.sleep(rest_s)

        # Brief ON pulse
        cycle_count += 1
        log(f"CONSERVATIVE cycle {cycle_count}: turning ON for {run_s}s")
        turn_on()
        time.sleep(run_s)

        # Check what happened during the pulse
        status = get_power_status()
        power = status["power"] if status else 0
        temp_c = status["temp_c"] if status else 0
        log(f"CONSERVATIVE cycle {cycle_count}: pulse done — {power:.1f}W, {temp_c:.1f}C")

        # Turn OFF again
        turn_off()
        log(f"CONSERVATIVE cycle {cycle_count}: plug OFF")

        # Periodic update every 3 cycles
        if cycle_count % 3 == 0:
            send_notification(
                f"Sump pump: conservative mode update (cycle {cycle_count})",
                f"Conservative mode running for {cycle_count} cycles "
                f"({cycle_count * (run_s + rest_s) // 3600}h total).\n\n"
                f"Last pulse: {power:.1f}W, {temp_c:.1f}C\n\n"
                f"Restart monitor with CONSERVATIVE_MODE=false to resume normal operation."
            )


def run_spring_mode():
    """
    Spring mode: plug OFF by default, regular ON pulses to clear water.
    More frequent than conservative (60s ON / 30 min OFF).
    Auto-exits to NORMAL if pump draws no power during a pulse (water table low).
    """
    run_s = SPRING_RUN_SECONDS
    rest_s = SPRING_REST_SECONDS
    dry_streak = 0
    DRY_EXIT_THRESHOLD = 3  # exit after 3 consecutive dry pulses

    log(f"=== ENTERING SPRING MODE ({run_s}s ON / {rest_s // 60} min OFF) ===")
    log(f"Auto-exits to NORMAL after {DRY_EXIT_THRESHOLD} consecutive dry pulses.")
    send_notification(
        "SUMP PUMP: Spring mode activated",
        f"Spring mode: pump will run {run_s}s every {rest_s // 60} min to clear water.\n\n"
        f"Plug stays OFF between pulses.\n"
        f"Will auto-exit to normal monitoring after {DRY_EXIT_THRESHOLD} consecutive "
        f"dry pulses (no power draw = water table low).\n\n"
        f"Set SPRING_MODE=false and restart to exit manually."
    )

    # Start with plug OFF
    turn_off()
    cycle_count = 0

    while True:
        # Rest first (plug OFF)
        log(f"SPRING: resting for {rest_s // 60} min (plug OFF)")
        time.sleep(rest_s)

        # ON pulse
        cycle_count += 1
        log(f"SPRING cycle {cycle_count}: turning ON for {run_s}s")
        turn_on()
        time.sleep(run_s)

        # Check what happened during the pulse
        status = get_power_status()
        if status is None:
            log(f"SPRING cycle {cycle_count}: couldn't reach Shelly, turning OFF")
            turn_off()
            continue

        power = status["power"]
        temp_c = status["temp_c"]
        pump_ran = power > POWER_THRESHOLD_WATTS

        # Temperature safety
        if check_temp_safety(status):
            log("SPRING: overtemp, skipping to rest")
            dry_streak = 0
            time.sleep(300)
            continue

        log(f"SPRING cycle {cycle_count}: pulse done — {power:.1f}W, {temp_c:.1f}C — {'pump ran' if pump_ran else 'DRY'}")

        # Turn OFF again
        turn_off()

        if pump_ran:
            dry_streak = 0
        else:
            dry_streak += 1
            log(f"SPRING: dry streak {dry_streak}/{DRY_EXIT_THRESHOLD}")

            if dry_streak >= DRY_EXIT_THRESHOLD:
                log(f"SPRING: {DRY_EXIT_THRESHOLD} consecutive dry pulses — water table is low, exiting to NORMAL")
                send_notification(
                    "Sump pump: spring mode → normal (water table low)",
                    f"Spring mode ran {cycle_count} cycles. Last {DRY_EXIT_THRESHOLD} pulses "
                    f"drew no power — water table is low.\n\n"
                    f"Switching to normal monitoring (plug ON, float controls pump).\n"
                    f"Restart with SPRING_MODE=true if water returns."
                )
                turn_on()
                return

        # Periodic update every 6 cycles (~3 hours)
        if cycle_count % 6 == 0:
            send_notification(
                f"Sump pump: spring mode update (cycle {cycle_count})",
                f"Spring mode running for {cycle_count} cycles "
                f"({cycle_count * (run_s + rest_s) // 60} min total).\n\n"
                f"Last pulse: {power:.1f}W, {temp_c:.1f}C\n"
                f"Dry streak: {dry_streak}/{DRY_EXIT_THRESHOLD}\n\n"
                f"Set SPRING_MODE=false and restart to exit manually."
            )


def shutdown(signum, frame):
    log("Shutting down monitor. Leaving plug in current state.")
    sys.exit(0)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


if __name__ == "__main__":
    log("=== Sump Pump Monitor Started ===")
    log(f"Shelly IP: {SHELLY_IP}")
    log(f"Power threshold: {POWER_THRESHOLD_WATTS}W")
    log(f"Max continuous run: {MAX_RUN_MINUTES} min before intervention")
    log(f"Safe mode duty cycle: {SAFE_RUN_SECONDS // 60} min ON / {SAFE_REST_SECONDS // 60} min OFF")
    log(f"Temp warning: {TEMP_WARN_C}C, cutoff: {MAX_TEMP_C}C")
    log(f"Voltage range: {VOLTAGE_LOW}-{VOLTAGE_HIGH}V")
    log(f"Expected pump power: {PUMP_POWER_LOW}-{PUMP_POWER_HIGH}W")
    log(f"WiFi RSSI warning: {RSSI_WARN} dBm")
    log(f"No-run alert after: {NO_RUN_ALERT_HOURS}h")
    log(f"Poll interval: {POLL_INTERVAL_SECONDS}s")
    log(f"AI analyzer: {'enabled -> ' + AI_ANALYZER_URL if AI_ENABLED else 'disabled'}")
    log(f"Conservative mode: {'ACTIVE (' + str(CONSERVATIVE_RUN_SECONDS) + 's ON / ' + str(CONSERVATIVE_REST_SECONDS // 3600) + 'h OFF)' if CONSERVATIVE_MODE else 'off'}")
    log(f"Spring mode: {'ACTIVE (' + str(SPRING_RUN_SECONDS) + 's ON / ' + str(SPRING_REST_SECONDS // 60) + 'm OFF)' if SPRING_MODE else 'off'}")

    # Spring mode: regular pulses, auto-exits when water table drops
    if SPRING_MODE:
        run_spring_mode()
        # Returned from spring mode = water table low, fall through to normal monitoring
        log("Spring mode exited, continuing to normal monitoring")

    # Conservative mode: skip normal startup, go straight to conservative loop
    if CONSERVATIVE_MODE:
        run_conservative_mode()
        sys.exit(0)  # conservative mode never returns, but just in case

    # Make sure plug is on
    status = get_power_status()
    if status and not status["output"]:
        log("Plug is OFF, turning ON...")
        turn_on()
        time.sleep(2)

    mode = MODE_NORMAL
    running_since = None
    last_illumination = None
    last_uptime = None
    last_pump_run = time.time()  # assume it ran recently at startup
    no_run_alerted = False
    voltage_alerted = False
    rssi_alerted = False
    power_anomaly_alerted = False
    temp_warned = False

    while True:
        try:
            status = get_power_status()

            if status is None:
                log("WARNING: Could not reach Shelly, retrying next cycle")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Ship telemetry to AI analyzer (fire-and-forget)
            ship_to_analyzer(status)

            # Temperature safety always active
            if check_temp_safety(status):
                log("Waiting 5 minutes after overtemp shutoff...")
                time.sleep(300)
                turn_on()
                running_since = None
                continue

            power = status["power"]
            pump_running = is_pump_running(status)

            # Track illumination changes
            illumination = status.get("illumination", "unknown")
            if last_illumination is not None and illumination != last_illumination:
                log(f"Light changed: {last_illumination} -> {illumination}")
                if last_illumination == "dark" and illumination != "dark":
                    send_notification(
                        "Sump pump: light level changed",
                        f"Illumination changed from '{last_illumination}' to '{illumination}'.\n\n"
                        "This could mean someone is in the basement or water is reflecting light.\n"
                        f"Pump power: {power:.1f}W, Temp: {status['temp_c']:.1f}C"
                    )
            last_illumination = illumination

            # Plug rebooted detection (uptime decreased)
            uptime = status.get("uptime", 0)
            reset_reason = status.get("reset_reason", -1)
            if last_uptime is not None and uptime < last_uptime:
                # Distinguish real reboot (uptime near zero) from counter glitch
                if uptime < 300:
                    # Real reboot: uptime reset to near-zero
                    reset_names = {
                        1: "POWERON (power-on)", 3: "SW_RESET (software/panic restart)",
                        4: "PANIC (CPU exception)", 5: "INT_WDT (interrupt watchdog)",
                        6: "TASK_WDT (task watchdog)", 9: "BROWNOUT (voltage drop)",
                    }
                    reset_name = reset_names.get(reset_reason, f"unknown ({reset_reason})")
                    was_down_seconds = last_uptime - uptime + POLL_INTERVAL_SECONDS
                    log(f"PLUG REBOOTED: uptime reset from {last_uptime}s to {uptime}s — reset_reason={reset_reason} ({reset_name})")
                    send_notification(
                        f"Sump pump: plug rebooted ({reset_name})",
                        f"The Shelly plug rebooted.\n\n"
                        f"RESET REASON: {reset_name}\n"
                        f"  1 = power-on, 3 = software/panic restart, 4 = CPU exception\n"
                        f"  5 = interrupt watchdog, 6 = task watchdog, 9 = brownout\n\n"
                        f"TIMING:\n"
                        f"  Previous uptime: {last_uptime:,}s ({last_uptime/3600:.1f}h)\n"
                        f"  Current uptime:  {uptime}s (device was down ~{was_down_seconds}s)\n\n"
                        f"DEVICE STATE:\n"
                        f"  Output:   {'ON' if status['output'] else 'OFF'}\n"
                        f"  Power:    {power:.1f}W\n"
                        f"  Voltage:  {status['voltage']:.1f}V\n"
                        f"  Temp:     {status['temp_c']:.1f}C\n"
                        f"  RAM free: {status.get('ram_free', 0):,} bytes\n"
                        f"  WiFi:     {status.get('ssid', '?')} (RSSI {status.get('rssi', '?')} dBm)\n"
                        f"  BSSID:    {status.get('bssid', '?')}\n\n"
                        f"ACTION: {'Monitor will turn plug ON if needed.' if not status['output'] else 'Plug is ON, monitoring continues.'}\n\n"
                        f"If reset_reason is 3 or 4, this is a firmware crash, not a power outage.\n"
                        f"See: https://github.com/toddllm/home-assistant/issues/1"
                    )
                    # Post-reboot: if pump is already running, start stuck-float timer immediately
                    if pump_running and running_since is None:
                        running_since = time.time()
                        log(f"Post-reboot: pump already running ({power:.1f}W), starting stuck-float timer immediately")
                else:
                    # Minor uptime decrease (counter glitch, NTP adjustment, etc.)
                    log(f"Uptime counter glitch: {last_uptime}s -> {uptime}s (delta={last_uptime - uptime}s, ignoring)")

            last_uptime = uptime

            # Output turned off unexpectedly
            if not status["output"] and mode == MODE_NORMAL:
                log(f"WARNING: Plug output is OFF unexpectedly! Turning back ON.")
                send_notification(
                    "Sump pump: plug was turned off",
                    "The Shelly plug output was found OFF during normal monitoring.\n"
                    "This was not done by the monitor script.\n\n"
                    "The plug has been turned back ON to protect against flooding."
                )
                turn_on()
                time.sleep(2)
                continue

            # Voltage out of range
            voltage = status["voltage"]
            if voltage < VOLTAGE_LOW or voltage > VOLTAGE_HIGH:
                if not voltage_alerted:
                    level = "LOW (brownout)" if voltage < VOLTAGE_LOW else "HIGH (overvoltage)"
                    log(f"VOLTAGE WARNING: {voltage:.1f}V — {level}")
                    send_notification(
                        f"Sump pump: voltage {level}",
                        f"Grid voltage is {voltage:.1f}V (normal range: {VOLTAGE_LOW}-{VOLTAGE_HIGH}V).\n\n"
                        f"{'Low voltage can damage the pump motor and cause overheating.' if voltage < VOLTAGE_LOW else 'High voltage can damage electronics and the pump motor.'}\n"
                        f"Power: {power:.1f}W, Temp: {status['temp_c']:.1f}C"
                    )
                    voltage_alerted = True
            else:
                voltage_alerted = False

            # WiFi signal degradation
            rssi = status.get("rssi", 0)
            if rssi < RSSI_WARN:
                if not rssi_alerted:
                    log(f"WIFI WARNING: RSSI {rssi} dBm is below {RSSI_WARN} dBm")
                    send_notification(
                        "Sump pump: weak WiFi signal",
                        f"WiFi signal strength dropped to {rssi} dBm (warn threshold: {RSSI_WARN} dBm).\n\n"
                        "If signal degrades further, the monitor may lose connectivity to the plug.\n"
                        "Consider moving the router closer or adding a WiFi extender."
                    )
                    rssi_alerted = True
            else:
                rssi_alerted = False

            # Temperature early warning (before emergency cutoff)
            if status["temp_c"] > TEMP_WARN_C and status["temp_c"] <= MAX_TEMP_C:
                if not temp_warned:
                    log(f"TEMP WARNING: {status['temp_c']:.1f}C approaching limit ({MAX_TEMP_C}C)")
                    send_notification(
                        "Sump pump: temperature rising",
                        f"Plug temperature is {status['temp_c']:.1f}C (warning: {TEMP_WARN_C}C, cutoff: {MAX_TEMP_C}C).\n\n"
                        f"Pump power: {power:.1f}W. Temperature is rising but not yet critical.\n"
                        "Emergency shutoff will trigger at {MAX_TEMP_C}C."
                    )
                    temp_warned = True
            elif status["temp_c"] <= TEMP_WARN_C:
                temp_warned = False

            if pump_running:
                last_pump_run = time.time()
                no_run_alerted = False

                if running_since is None:
                    running_since = time.time()
                    power_anomaly_alerted = False
                    log(f"Pump started running ({power:.1f}W, {status['temp_c']:.1f}C, light={illumination})")

                # Abnormal power draw while running (motor health)
                if not power_anomaly_alerted and (power < PUMP_POWER_LOW or power > PUMP_POWER_HIGH):
                    # Only alert after pump has been running > 30s (skip startup transients)
                    if running_since and (time.time() - running_since) > 30:
                        log(f"POWER ANOMALY: {power:.1f}W outside expected range ({PUMP_POWER_LOW}-{PUMP_POWER_HIGH}W)")
                        send_notification(
                            "Sump pump: abnormal power draw",
                            f"Pump is drawing {power:.1f}W, outside expected range "
                            f"({PUMP_POWER_LOW:.0f}-{PUMP_POWER_HIGH:.0f}W).\n\n"
                            f"{'Low power could indicate a failing motor or partial blockage.' if power < PUMP_POWER_LOW else 'High power could indicate a seized impeller or electrical fault.'}\n"
                            f"Current: {status['current']:.2f}A, Voltage: {voltage:.1f}V, Temp: {status['temp_c']:.1f}C"
                        )
                        power_anomaly_alerted = True

                run_minutes = (time.time() - running_since) / 60

                if run_minutes >= MAX_RUN_MINUTES:
                    log(f"Pump running {run_minutes:.1f} min — attempting power cycle...")

                    still_stuck = power_cycle()

                    if still_stuck:
                        # Enter safe mode — handles its own notifications
                        run_safe_mode()
                        # Returned from safe mode = float unstuck
                        running_since = None
                        mode = MODE_NORMAL
                        log("Back in NORMAL monitoring mode")
                    else:
                        send_notification(
                            "Sump pump: power cycle fixed stuck float",
                            f"Pump had been running for {run_minutes:.1f} minutes.\n\n"
                            "A power cycle was performed and the pump stopped.\n"
                            "Float switch appears unstuck. Back to normal monitoring."
                        )
                        running_since = None

            else:
                if running_since is not None:
                    run_duration = (time.time() - running_since) / 60
                    log(f"Pump stopped after {run_duration:.1f} min ({power:.1f}W, {status['temp_c']:.1f}C, light={illumination})")
                    running_since = None

                # No-run alert: pump hasn't run in a long time (float stuck DOWN = flood risk)
                hours_since_run = (time.time() - last_pump_run) / 3600
                if hours_since_run >= NO_RUN_ALERT_HOURS and not no_run_alerted:
                    log(f"NO-RUN WARNING: Pump hasn't run in {hours_since_run:.1f} hours")
                    send_notification(
                        f"Sump pump: hasn't run in {hours_since_run:.0f} hours",
                        f"The pump has not drawn power in {hours_since_run:.1f} hours "
                        f"(alert threshold: {NO_RUN_ALERT_HOURS:.0f} hours).\n\n"
                        "This could mean:\n"
                        "- Float switch stuck DOWN (won't trigger when water rises)\n"
                        "- Very low water table (normal in dry weather)\n"
                        "- Pump disconnected from plug\n\n"
                        "If you're not in a dry spell, check the sump pit."
                    )
                    no_run_alerted = True

            time.sleep(POLL_INTERVAL_SECONDS)

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
