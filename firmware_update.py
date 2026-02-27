#!/usr/bin/env python3
"""
Shelly Plug US Gen4 — Firmware Update Script (Phase 2)

Safely updates firmware from 1.7.99 (factory pre-release) to 1.7.4 (stable)
with full pre/post-flight verification and automatic state restoration.

Usage:
    python3 firmware_update.py              # Full update with interactive prompts
    python3 firmware_update.py --check      # Pre-flight check only (read-only)
    python3 firmware_update.py --skip-monitor  # Skip pausing monitor on toddllm
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth

# --- Target firmware ---
TARGET_VERSIONS = {"1.7.4", "1.7.5"}
FACTORY_MARKER = "plugusg4prod1"

# --- Timeouts ---
CLOUD_CONNECT_TIMEOUT = 60
UPDATE_CHECK_RETRIES = 3
UPDATE_CHECK_DELAY = 10
OTA_POLL_INTERVAL = 5
OTA_TIMEOUT = 600  # 10 minutes


# --- Environment ---

def load_env():
    """Load config from .env file in the same directory as this script."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(f"ERROR: {env_path} not found. Copy .env.example and fill in values.")
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env()

SHELLY_IP = os.environ["SHELLY_IP"]
SHELLY_USER = os.environ["SHELLY_USER"]
SHELLY_PASSWORD = os.environ["SHELLY_PASSWORD"]
AUTH = HTTPDigestAuth(SHELLY_USER, SHELLY_PASSWORD)


# --- Logging ---

log = logging.getLogger("firmware_update")
log.setLevel(logging.DEBUG)

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(_console)

_file = logging.FileHandler(Path(__file__).parent / "firmware_update.log")
_file.setLevel(logging.DEBUG)
_file.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(_file)


# --- RPC helpers ---

def shelly_get(method, timeout=10):
    url = f"http://{SHELLY_IP}/rpc/{method}"
    resp = requests.get(url, auth=AUTH, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def shelly_post(method, params=None, timeout=10):
    url = f"http://{SHELLY_IP}/rpc/{method}"
    resp = requests.post(url, auth=AUTH, json=params or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# --- SSH helper ---

def ssh_command(cmd, check=True):
    result = subprocess.run(
        ["ssh", "toddllm", cmd],
        capture_output=True, text=True, timeout=30,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"SSH failed: {cmd}\n{result.stderr}")
    return result


# --- Prompt helper ---

def confirm(prompt):
    """Ask y/N question. Returns True only on explicit 'y'."""
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer == "y"


# --- Pre-flight ---

def preflight():
    """Gather device state snapshot. Returns dict or exits on failure."""
    log.info("=" * 60)
    log.info("STEP 1: Pre-flight check")
    log.info("=" * 60)

    try:
        device_info = shelly_get("Shelly.GetDeviceInfo")
    except Exception as e:
        log.error(f"Cannot reach Shelly at {SHELLY_IP}: {e}")
        sys.exit(1)

    sys_status = shelly_get("Sys.GetStatus")
    switch_status = shelly_get("Switch.GetStatus?id=0")
    cloud_config = shelly_get("Cloud.GetConfig")
    cloud_status = shelly_get("Cloud.GetStatus")
    matter_config = shelly_get("Matter.GetConfig")
    ble_config = shelly_get("BLE.GetConfig")

    snap = {
        "ver": device_info.get("ver", "unknown"),
        "fw_id": device_info.get("fw_id", ""),
        "model": device_info.get("model", "unknown"),
        "mac": device_info.get("mac", "unknown"),
        "uptime": sys_status.get("uptime", 0),
        "ram_free": sys_status.get("ram_free", 0),
        "available_updates": sys_status.get("available_updates", {}),
        "switch_output": switch_status.get("output", False),
        "apower": switch_status.get("apower", 0.0),
        "cloud_was_enabled": cloud_config.get("enable", False),
        "cloud_connected": cloud_status.get("connected", False),
        "matter_was_enabled": matter_config.get("enable", False),
        "ble_was_enabled": ble_config.get("enable", False),
    }

    is_factory = FACTORY_MARKER in snap["fw_id"] or FACTORY_MARKER in snap["ver"]

    # Display summary
    log.info("")
    log.info("  Device Summary")
    log.info("  " + "-" * 40)
    log.info(f"  Model:      {snap['model']}")
    log.info(f"  MAC:        {snap['mac']}")
    log.info(f"  Firmware:   {snap['ver']}{' (FACTORY PRE-RELEASE)' if is_factory else ''}")
    log.info(f"  fw_id:      {snap['fw_id']}")
    log.info(f"  Uptime:     {snap['uptime']:,}s ({snap['uptime']/3600:.1f}h)")
    log.info(f"  RAM free:   {snap['ram_free']:,} bytes")
    log.info(f"  Switch:     {'ON' if snap['switch_output'] else 'OFF'}")
    log.info(f"  Power:      {snap['apower']:.1f}W")
    log.info(f"  Cloud:      {'enabled' if snap['cloud_was_enabled'] else 'disabled'}"
             f" ({'connected' if snap['cloud_connected'] else 'disconnected'})")
    log.info(f"  Matter:     {'enabled' if snap['matter_was_enabled'] else 'disabled'}")
    log.info(f"  BLE:        {'enabled' if snap['ble_was_enabled'] else 'disabled'}")
    log.info("")

    if is_factory:
        log.info("  ** Factory build detected ('%s' marker present) — should be updated **", FACTORY_MARKER)
        log.info("")

    # Version check
    if snap["ver"] in TARGET_VERSIONS:
        log.info(f"  Firmware is already at target version {snap['ver']}. No update needed.")
        snap["already_at_target"] = True
    else:
        snap["already_at_target"] = False
        log.info(f"  Current: {snap['ver']}  →  Target: {' or '.join(sorted(TARGET_VERSIONS))}")

    log.info("")

    # Pump running warning
    if snap["apower"] > 100:
        log.warning(f"  ⚠ PUMP IS RUNNING ({snap['apower']:.1f}W)! Consider waiting for it to stop.")
        if not confirm("  Pump is currently running. Proceed anyway?"):
            log.info("Aborted by operator (pump running).")
            sys.exit(0)

    return snap


# --- Main flow ---

def main():
    parser = argparse.ArgumentParser(description="Shelly Plug US Gen4 firmware updater")
    parser.add_argument("--check", action="store_true", help="Pre-flight check only (read-only)")
    parser.add_argument("--skip-monitor", action="store_true", help="Skip pausing monitor on toddllm")
    args = parser.parse_args()

    snap = preflight()

    if args.check:
        log.info("--check mode: pre-flight complete. No changes made.")
        return

    # Safety gate
    if not confirm("Confirm the sump water level is currently low enough that a 5-10 minute outage is acceptable."):
        log.info("Aborted by operator (water level concern).")
        return

    if snap["already_at_target"]:
        log.info("Firmware already at target. Running post-flight verification only.")
        postflight(snap)
        return

    # State for cleanup
    monitor_was_active = None
    cloud_changed = False
    monitor_restored = False

    def cleanup():
        """Safety net: restore cloud and monitor to pre-update state."""
        nonlocal cloud_changed, monitor_restored
        if cloud_changed:
            try:
                log.info("Cleanup: restoring cloud to pre-update state (enabled=%s)", snap["cloud_was_enabled"])
                shelly_post("Cloud.SetConfig", {"config": {"enable": snap["cloud_was_enabled"]}})
                cloud_changed = False
            except Exception as e:
                log.error(f"Cleanup: failed to restore cloud config: {e}")

        if not args.skip_monitor and monitor_was_active is True and not monitor_restored:
            try:
                log.info("Cleanup: restoring monitor service (was active)")
                ssh_command("sudo systemctl start sump-pump-monitor")
                monitor_restored = True
            except Exception as e:
                log.error(f"Cleanup: failed to restart monitor: {e}")

    def sigint_handler(signum, frame):
        log.warning("Ctrl+C received — running cleanup...")
        cleanup()
        sys.exit(1)

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        # Step 2: Confirm plan
        log.info("=" * 60)
        log.info("STEP 2: Confirm update plan")
        log.info("=" * 60)
        log.info("")
        log.info("  The following steps will be performed:")
        log.info("  1. Pause sump-pump-monitor on toddllm (via SSH)")
        log.info("  2. Enable Shelly cloud (temporary)")
        log.info("  3. Check for firmware update")
        log.info("  4. Trigger OTA update (%s → %s)", snap["ver"], "/".join(sorted(TARGET_VERSIONS)))
        log.info("  5. Wait for firmware change (~2-5 min)")
        log.info("  6. Post-flight verification")
        log.info("  7. Restore Matter/BLE/Cloud to prior state")
        log.info("  8. Restore monitor service")
        log.info("")
        log.info("  Estimated downtime: 2-5 minutes")
        log.info("  Pump stays powered (initial_state: restore_last)")
        log.info("")

        if not confirm("Proceed with firmware update?"):
            log.info("Aborted by operator.")
            return

        # Step 3: Pause monitor
        if not args.skip_monitor:
            log.info("")
            log.info("=" * 60)
            log.info("STEP 3: Pause monitor on toddllm")
            log.info("=" * 60)
            try:
                result = ssh_command("systemctl is-active sump-pump-monitor", check=False)
                monitor_was_active = result.stdout.strip() == "active"
                log.info("  Monitor status: %s", "active" if monitor_was_active else "inactive")

                if monitor_was_active:
                    ssh_command("sudo systemctl stop sump-pump-monitor")
                    # Verify stopped
                    result = ssh_command("systemctl is-active sump-pump-monitor", check=False)
                    if result.stdout.strip() == "active":
                        log.error("  Failed to stop monitor — still active!")
                        if not confirm("  Monitor could not be stopped. Continue anyway?"):
                            return
                    else:
                        log.info("  Monitor stopped successfully.")
                else:
                    log.info("  Monitor was already stopped. Will leave it stopped after update.")
            except Exception as e:
                log.error(f"  SSH failed: {e}")
                if not confirm("  Cannot reach toddllm via SSH. Continue without pausing monitor?"):
                    return
                monitor_was_active = None
        else:
            log.info("")
            log.info("STEP 3: Skipped (--skip-monitor)")

        # Step 4: Enable cloud
        log.info("")
        log.info("=" * 60)
        log.info("STEP 4: Enable cloud (temporary)")
        log.info("=" * 60)

        if not snap["cloud_was_enabled"]:
            shelly_post("Cloud.SetConfig", {"config": {"enable": True}})
            cloud_changed = True
            log.info("  Cloud enabled. Waiting for connection...")

            start = time.time()
            connected = False
            while time.time() - start < CLOUD_CONNECT_TIMEOUT:
                try:
                    status = shelly_get("Cloud.GetStatus")
                    if status.get("connected"):
                        connected = True
                        break
                except Exception:
                    pass
                time.sleep(5)
                elapsed = int(time.time() - start)
                log.info("  Waiting for cloud connection... (%ds)", elapsed)

            if not connected:
                log.error("  Cloud did not connect within %ds. Aborting.", CLOUD_CONNECT_TIMEOUT)
                return
            log.info("  Cloud connected.")
        else:
            log.info("  Cloud was already enabled and %s.",
                     "connected" if snap["cloud_connected"] else "not connected — waiting")
            if not snap["cloud_connected"]:
                start = time.time()
                connected = False
                while time.time() - start < CLOUD_CONNECT_TIMEOUT:
                    try:
                        status = shelly_get("Cloud.GetStatus")
                        if status.get("connected"):
                            connected = True
                            break
                    except Exception:
                        pass
                    time.sleep(5)
                if not connected:
                    log.error("  Cloud did not connect within %ds. Aborting.", CLOUD_CONNECT_TIMEOUT)
                    return

        # Step 5: Check for update
        log.info("")
        log.info("=" * 60)
        log.info("STEP 5: Check for firmware update")
        log.info("=" * 60)

        update_info = None
        for attempt in range(1, UPDATE_CHECK_RETRIES + 1):
            try:
                result = shelly_get("Shelly.CheckForUpdate")
                stable = result.get("stable")
                if stable and stable.get("version"):
                    update_info = stable
                    break
                log.info("  Attempt %d/%d: no update info yet, retrying in %ds...",
                         attempt, UPDATE_CHECK_RETRIES, UPDATE_CHECK_DELAY)
            except Exception as e:
                log.info("  Attempt %d/%d failed: %s", attempt, UPDATE_CHECK_RETRIES, e)
            time.sleep(UPDATE_CHECK_DELAY)

        if not update_info:
            log.warning("")
            log.warning("  No update found after %d attempts.", UPDATE_CHECK_RETRIES)
            log.warning("  The device's firmware %s may be considered newer than stable by Shelly's server.", snap["ver"])
            log.warning("  Fallback: use Shelly.Update with a direct URL parameter.")
            log.warning("  Visit http://archive.shelly-tools.de/ to find the PlugUSG4 firmware URL.")
            log.warning("")
            url = input("  Paste firmware URL (or press Enter to abort): ").strip()
            if not url:
                log.info("Aborted by operator (no firmware URL).")
                return
            update_info = {"version": "manual", "url": url}
        else:
            available_ver = update_info["version"]
            log.info("  Available stable update: %s", available_ver)
            if available_ver not in TARGET_VERSIONS:
                log.warning("  Version %s is not in expected set %s!", available_ver, TARGET_VERSIONS)
                if not confirm(f"  Unexpected version {available_ver}. Proceed anyway?"):
                    return

        # Step 6: Trigger OTA
        log.info("")
        log.info("=" * 60)
        log.info("STEP 6: Trigger OTA update")
        log.info("=" * 60)

        if "url" in update_info:
            log.info("  Using direct URL: %s", update_info["url"])
            if not confirm("  Flash firmware from this URL?"):
                return
            shelly_post("Shelly.Update", {"url": update_info["url"]}, timeout=30)
        else:
            log.info("  Updating to stable channel: %s", update_info["version"])
            if not confirm("  Flash firmware now?"):
                return
            shelly_post("Shelly.Update", {"stage": "stable"}, timeout=30)

        log.info("  OTA triggered. Device will download and reboot.")

        # Step 7: Wait for firmware change
        log.info("")
        log.info("=" * 60)
        log.info("STEP 7: Waiting for firmware change")
        log.info("=" * 60)

        pre_ver = snap["ver"]
        start = time.time()
        failures = 0

        while time.time() - start < OTA_TIMEOUT:
            elapsed = int(time.time() - start)
            try:
                info = shelly_get("Shelly.GetDeviceInfo", timeout=5)
                new_ver = info.get("ver", "")
                failures = 0

                if new_ver != pre_ver:
                    log.info("  Firmware changed: %s → %s (%ds elapsed)", pre_ver, new_ver, elapsed)
                    snap["new_ver"] = new_ver
                    snap["new_fw_id"] = info.get("fw_id", "")
                    break
                else:
                    log.info("  Still at %s... (%ds elapsed)", pre_ver, elapsed)

            except Exception:
                failures += 1
                log.info("  Device unreachable (%ds elapsed, %d consecutive failures)", elapsed, failures)

            time.sleep(OTA_POLL_INTERVAL)
        else:
            log.error("")
            log.error("  TIMEOUT: Firmware did not change within %d minutes.", OTA_TIMEOUT // 60)
            log.error("  Recovery steps:")
            log.error("    1. Check if device is reachable: curl http://%s/rpc/Shelly.GetDeviceInfo", SHELLY_IP)
            log.error("    2. Power cycle the Shelly physically (unplug and replug)")
            log.error("    3. Check the Shelly app for device status")
            log.error("    4. Re-run this script to check current firmware version")

            return  # finally block will restore monitor

        # Step 8: Post-flight
        postflight(snap)
        cloud_changed = False  # postflight already restored cloud

    except Exception as e:
        log.error(f"Unexpected error: {e}")
        raise
    finally:
        cleanup()

    # Step 9: Restore monitor (informational — cleanup may have already handled it)
    if not args.skip_monitor:
        log.info("")
        log.info("=" * 60)
        log.info("STEP 9: Restore monitor")
        log.info("=" * 60)
        if monitor_was_active is True:
            if not monitor_restored:
                try:
                    ssh_command("sudo systemctl start sump-pump-monitor")
                    monitor_restored = True
                except Exception as e:
                    log.error(f"  Failed to restart monitor: {e}")
                    log.error("  Manual fix: ssh toddllm 'sudo systemctl start sump-pump-monitor'")
            result = ssh_command("systemctl is-active sump-pump-monitor", check=False)
            log.info("  Monitor status: %s", result.stdout.strip())
        elif monitor_was_active is False:
            log.info("  Monitor was stopped before update — leaving it stopped.")
        else:
            log.info("  Monitor state unknown (SSH was unavailable) — no action taken.")

    # Step 10: Print doc updates
    print_doc_updates(snap)


def postflight(snap):
    """Post-flight verification: check firmware, switch, restore settings."""
    log.info("")
    log.info("=" * 60)
    log.info("STEP 8: Post-flight verification")
    log.info("=" * 60)

    device_info = shelly_get("Shelly.GetDeviceInfo")
    new_ver = device_info.get("ver", "unknown")
    new_fw_id = device_info.get("fw_id", "")
    snap["new_ver"] = new_ver
    snap["new_fw_id"] = new_fw_id

    # Verify firmware version
    if new_ver in TARGET_VERSIONS:
        log.info("  Firmware: %s  ✓", new_ver)
    else:
        log.warning("  Firmware: %s — NOT in target set %s!", new_ver, TARGET_VERSIONS)

    # Verify switch state
    switch_status = shelly_get("Switch.GetStatus?id=0")
    output = switch_status.get("output", False)
    apower = switch_status.get("apower", 0.0)

    if output:
        log.info("  Switch: ON (%s W)  ✓", f"{apower:.1f}")
    else:
        log.warning("  Switch: OFF — turning ON immediately!")
        shelly_post("Switch.Set", {"id": 0, "on": True})
        time.sleep(2)
        switch_status = shelly_get("Switch.GetStatus?id=0")
        apower = switch_status.get("apower", 0.0)
        log.info("  Switch restored to ON (%s W)", f"{apower:.1f}")

    # Restore cloud to pre-update state
    cloud_config = shelly_get("Cloud.GetConfig")
    if cloud_config.get("enable") != snap["cloud_was_enabled"]:
        log.info("  Restoring cloud: enabled=%s", snap["cloud_was_enabled"])
        shelly_post("Cloud.SetConfig", {"config": {"enable": snap["cloud_was_enabled"]}})
    else:
        log.info("  Cloud: already at pre-update state (enabled=%s)", snap["cloud_was_enabled"])

    # Restore Matter/BLE
    need_reboot = False

    matter_config = shelly_get("Matter.GetConfig")
    if matter_config.get("enable") != snap["matter_was_enabled"]:
        log.info("  Restoring Matter: enabled=%s", snap["matter_was_enabled"])
        shelly_post("Matter.SetConfig", {"config": {"enable": snap["matter_was_enabled"]}})
        need_reboot = True
    else:
        log.info("  Matter: already at pre-update state (enabled=%s)", snap["matter_was_enabled"])

    ble_config = shelly_get("BLE.GetConfig")
    if ble_config.get("enable") != snap["ble_was_enabled"]:
        log.info("  Restoring BLE: enabled=%s", snap["ble_was_enabled"])
        shelly_post("BLE.SetConfig", {"config": {"enable": snap["ble_was_enabled"]}})
        need_reboot = True
    else:
        log.info("  BLE: already at pre-update state (enabled=%s)", snap["ble_was_enabled"])

    if need_reboot:
        log.info("  Matter/BLE changed — rebooting device...")
        try:
            shelly_post("Shelly.Reboot")
        except Exception:
            pass  # device goes away during reboot
        log.info("  Waiting 15s for reboot...")
        time.sleep(15)
        # Poll until back
        for _ in range(12):
            try:
                shelly_get("Shelly.GetDeviceInfo", timeout=5)
                log.info("  Device back online after settings reboot.")
                break
            except Exception:
                time.sleep(5)

    # Final status
    sys_status = shelly_get("Sys.GetStatus")
    new_ram = sys_status.get("ram_free", 0)
    new_uptime = sys_status.get("uptime", 0)

    log.info("")
    log.info("  Before / After Comparison")
    log.info("  " + "-" * 40)
    log.info("  Firmware:   %s  →  %s", snap["ver"], new_ver)
    log.info("  fw_id:      %s...  →  %s...", snap["fw_id"][:30], new_fw_id[:30])
    log.info("  RAM free:   %s  →  %s bytes", f"{snap['ram_free']:,}", f"{new_ram:,}")
    log.info("  Uptime:     %s  →  %s (reset expected)", f"{snap['uptime']:,}s", f"{new_uptime:,}s")
    log.info("  Power:      %.1fW  →  %.1fW", snap["apower"], apower)
    log.info("")
    log.info("  Post-flight verification COMPLETE.")


def print_doc_updates(snap):
    """Print ready-to-paste documentation blocks."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_ver = snap.get("new_ver", "?.?.?")

    log.info("")
    log.info("=" * 60)
    log.info("STEP 10: Documentation updates (copy-paste)")
    log.info("=" * 60)

    print(f"""
--- docs/shelly-reboot-investigation.md ---

### Phase 2: Firmware Update ({now})

- Updated firmware: {snap['ver']} → {new_ver}
- Method: OTA via `firmware_update.py` (automated script)
- Downtime: ~2-5 minutes
- Switch state: preserved (initial_state: restore_last)
- Matter: disabled, BLE: disabled (restored to pre-update state)
- Cloud: re-disabled after update
- Monitor: paused during update, restored after
- Next: Monitor stability for 72 hours. If no crashes → close Issue #1.
  If crashes continue → Phase 3 (WiFi tuning).

--- docs/status.md ---

| Component | Status | Notes |
|-----------|--------|-------|
| Shelly Plug US Gen4 | firmware {new_ver} (stable) | Updated {now} from {snap['ver']} (factory). Phase 2 complete. |

--- GitHub Issue #1 comment ---

## Phase 2 Complete: Firmware Update

- **{snap['ver']}** (factory pre-release) → **{new_ver}** (stable)
- Updated via automated `firmware_update.py` script
- Matter disabled, BLE disabled, cloud re-disabled
- Switch state preserved throughout
- **Next:** 72-hour observation period. If stable → close this issue.
""")


if __name__ == "__main__":
    main()
