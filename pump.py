#!/usr/bin/env python3
"""
Sump Pump Manual Control - Quick CLI for Shelly Plug US Gen4

Usage:
  ./pump.py status      Show power, temp, switch state
  ./pump.py on          Turn pump on
  ./pump.py off         Turn pump off
  ./pump.py cycle       Power cycle (off 10s, on)
  ./pump.py watch       Live monitoring (Ctrl+C to stop)
"""

import os
import sys
import time
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth


def load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


load_env()

SHELLY_IP = os.environ["SHELLY_IP"]
AUTH = HTTPDigestAuth(
    os.environ["SHELLY_USER"],
    os.environ["SHELLY_PASSWORD"],
)


def rpc(method, params=None):
    url = f"http://{SHELLY_IP}/rpc/{method}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        r = requests.get(url, auth=AUTH, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        print(f"ERROR: Cannot reach Shelly at {SHELLY_IP}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def status():
    data = rpc("Shelly.GetStatus")
    sw = data.get("switch:0", {})
    temp = sw.get("temperature", {})
    energy = sw.get("aenergy", {})

    output = "ON" if sw.get("output") else "OFF"
    power = sw.get("apower", 0)
    voltage = sw.get("voltage", 0)
    current = sw.get("current", 0)
    temp_c = temp.get("tC", 0)
    temp_f = temp.get("tF", 0)
    total_kwh = energy.get("total", 0) / 1000

    state = "RUNNING" if power > 100 else "IDLE"

    print(f"Switch:  {output}")
    print(f"Pump:    {state}")
    print(f"Power:   {power:.1f}W")
    print(f"Current: {current:.2f}A")
    print(f"Voltage: {voltage:.1f}V")
    print(f"Temp:    {temp_c:.1f}C / {temp_f:.1f}F")
    print(f"Energy:  {total_kwh:.3f} kWh total")

    illum = data.get("illuminance:0", {})
    print(f"Light:   {illum.get('illumination', '?')}")

    wifi = data.get("wifi", {})
    print(f"WiFi:    {wifi.get('ssid', '?')} (RSSI {wifi.get('rssi', '?')})")
    print(f"IP:      {wifi.get('sta_ip', '?')}")

    if power > 100 and temp_c > 50:
        print(f"\nWARNING: Pump running and temp is high ({temp_c:.1f}C)")


def on():
    result = rpc("Switch.Set", {"id": 0, "on": "true"})
    was = "ON" if result.get("was_on") else "OFF"
    print(f"Pump ON (was {was})")


def off():
    result = rpc("Switch.Set", {"id": 0, "on": "false"})
    was = "ON" if result.get("was_on") else "OFF"
    print(f"Pump OFF (was {was})")


def cycle():
    print("Power cycling: OFF...")
    rpc("Switch.Set", {"id": 0, "on": "false"})
    for i in range(10, 0, -1):
        print(f"  waiting {i}s...", end="\r")
        time.sleep(1)
    print("Power cycling: ON... ")
    rpc("Switch.Set", {"id": 0, "on": "true"})
    time.sleep(3)
    data = rpc("Shelly.GetStatus")
    power = data.get("switch:0", {}).get("apower", 0)
    if power > 100:
        print(f"Pump is RUNNING ({power:.1f}W) — float may still be stuck")
    else:
        print(f"Pump is IDLE ({power:.1f}W)")


def watch():
    print("Live monitoring (Ctrl+C to stop)\n")
    print(f"{'Time':<10} {'Switch':<8} {'Power':>8} {'Current':>9} {'Voltage':>9} {'Temp':>7}")
    print("-" * 55)
    try:
        while True:
            data = rpc("Shelly.GetStatus")
            sw = data.get("switch:0", {})
            t = time.strftime("%H:%M:%S")
            output = "ON" if sw.get("output") else "OFF"
            power = sw.get("apower", 0)
            current = sw.get("current", 0)
            voltage = sw.get("voltage", 0)
            temp_c = sw.get("temperature", {}).get("tC", 0)

            marker = " <<<" if power > 100 else ""
            print(f"{t:<10} {output:<8} {power:>7.1f}W {current:>8.2f}A {voltage:>8.1f}V {temp_c:>5.1f}C{marker}")
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopped.")


COMMANDS = {
    "status": status,
    "on": on,
    "off": off,
    "cycle": cycle,
    "watch": watch,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__.strip())
        sys.exit(0)
    COMMANDS[sys.argv[1]]()
