# Shelly Plug US Gen4 — Reboot/Crash Investigation

**Device:** Shelly Plug US Gen4 (S4PL-00116US)
**Firmware:** 1.7.99-plugusg4prod1 (factory build, 2025-10-21)
**GitHub Issue:** [#1](https://github.com/toddllm/home-assistant/issues/1)
**Status:** Phase 1 — observing stability after disabling Matter + BLE

---

## Problem

The Shelly Plug US Gen4 controlling the sump pump rebooted 3 times in ~20 hours. These are **not** power outages — they are firmware crashes (watchdog resets).

### Timeline

| # | When | Uptime Before | Reset Reason | Notes |
|---|------|---------------|-------------|-------|
| 1 | 2026-02-23 ~20:14 | ~49,460s (~13.7h) | 4 (OWDT_RESET) | Watchdog timeout |
| 2 | 2026-02-24 ~07:31 | ~40,620s (~11.3h) | 3 (SW_RESET) | Software reset |
| 3 | 2026-02-24 ~15:24 | ~28,380s (~7.9h) | 4 (OWDT_RESET) | Watchdog timeout |

### Reset Reason Codes (ESP32)

| Code | Name | Meaning |
|------|------|---------|
| 1 | POWERON_RESET | Actual power loss |
| 3 | SW_RESET | Software-initiated reset (firmware crash → restart) |
| 4 | OWDT_RESET | Watchdog timer expired (firmware hung/stuck) |

Reset reasons 3 and 4 indicate firmware bugs, not electrical issues.

### Impact

Each reboot:
- Causes a brief connectivity gap (~30-60s)
- Resets the plug's switch state (mitigated by `initial_state: restore_last`)
- Triggers our monitoring alerts (reboot detection + unexpected OFF)
- The monitor auto-recovers by turning the switch back ON

Without our monitoring system, the plug would have silently gone offline and (before we set `initial_state`) stayed OFF — leaving the sump pump unprotected.

---

## Investigation

### Pre-Crash Device State

| Setting | Value | Concern |
|---------|-------|---------|
| Matter | **Enabled** (1 fabric) | Heavy protocol stack, known crash source |
| BLE | **Enabled** | Shares radio with WiFi on ESP32 |
| Free RAM | **147 KB** | Low for a device running WiFi + Matter + BLE |
| Firmware | 1.7.99-plugusg4prod1 | Pre-production build — not a stable release |
| WiFi | Mesh network (multiple APs) | BSSID changes observed — roaming events |

### Root Cause Hypothesis

A combination of three factors:

1. **Pre-release firmware (1.7.99).** This is a development build — the latest stable is **1.7.4** (released 2026-01-27). The stable 1.7.4 includes the DNSSD crash fix, DNS-SD answer parsing fix, and Matter subscription improvements. Our pre-release build may not contain all of these fixes.

2. **Memory pressure from concurrent wireless stacks.** The Shelly Plug US Gen4 runs on an ESP32 which shares a single radio between WiFi, BLE, and Matter. Running all three simultaneously:
   - **Matter protocol stack** is heavy — includes mDNS/DNSSD, IPv6, fabric management
   - **BLE scanning** contends with WiFi for radio time
   - Combined load can cause the watchdog timer to expire (OWDT_RESET) or trigger a software crash (SW_RESET)

3. **WiFi mesh roaming.** When a mesh AP hands off the device to another AP (BSSID change), the WiFi stack can hang during reconnection — triggering watchdog resets. 802.11r/k/v fast roaming protocols are known to cause instability with simple IoT WiFi stacks.

### Evidence

#### From Shelly Firmware Changelog
- **"Matter: Fix crash by DNSSD resolving only non-LL IPv6 addresses"** — a known crash in the Matter stack related to DNS service discovery. This may be the exact bug hitting our device.
- Multiple BLE-related crash fixes in recent firmware versions
- Ongoing optimization for concurrent wireless component handling

#### Firmware Version Analysis
- **1.7.99 is NOT a stable release.** The "x.y.99" numbering indicates a development/nightly build between stable releases.
- **1.7.4 (stable, 2026-01-27)** includes: DNS-SD answer parsing fix, Matter subscription resumption speedup, DAK fix
- **1.7.0** fixed Wi-Fi 6 support on Gen4 devices and 2PM Gen4 profile switching crashes
- **1.2.1** resolved "troublesome Wi-Fi reconnects"

#### From Community Reports
- **ESP32 Forum:** BLE + WiFi causes out-of-memory crashes ([esp32.com/viewtopic.php?t=5549](https://www.esp32.com/viewtopic.php?t=5549))
- **ESPHome Issues:** BLE makes Shelly Plus 1 unresponsive, OTA crashes when BLE tracker enabled ([github.com/esphome/issues/issues/3485](https://github.com/esphome/issues/issues/3485))
- **Arduino-ESP32 Issues:** WiFi + BLE causes task watchdog timeouts ([github.com/espressif/arduino-esp32/issues/1754](https://github.com/espressif/arduino-esp32/issues/1754))
- **Arduino-ESP32 Issues:** Memory fail with BLE/WiFi ([github.com/espressif/arduino-esp32/issues/8741](https://github.com/espressif/arduino-esp32/issues/8741))
- **Shelly Community:** "Firmware super unreliable" on 1 Mini Gen4 ([community.shelly.cloud/topic/9455](https://community.shelly.cloud/topic/9455-firmware-super-unreliable/))
- **Shelly Community:** "Unexpected switch off" on Power Strip 4 Gen4 ([community.shelly.cloud/topic/11999](https://community.shelly.cloud/topic/11999-unexpected-switch-off/))
- **Shelly Community:** WiFi disconnects with multiple APs, no reconnect ([community.shelly.cloud/topic/9561](https://community.shelly.cloud/topic/9561-wifi-disconnects-no-reconnect-since-15/))
- **Home Assistant Community:** Gen4 devices not connecting after network restart ([community.home-assistant.io](https://community.home-assistant.io/t/shelly-gen4-devices-not-connecting-after-ha-restart-or-network-restart/983126))

#### From Our Device
- RAM free jumped from **147 KB → 217 KB** (+48%) after disabling Matter and BLE
- BSSID changes in logs suggest WiFi roaming between mesh APs near crash times
- No correlation with pump activity (crashes happen during idle periods too)

---

## Actions Taken

### 2026-02-24: Disable Matter and BLE

```bash
# Disable Matter
curl -X POST "http://SHELLY_IP/rpc/Matter.SetConfig" \
  --digest -u admin:PASSWORD \
  -H "Content-Type: application/json" \
  -d '{"config": {"enable": false}}'

# Disable BLE
curl -X POST "http://SHELLY_IP/rpc/BLE.SetConfig" \
  --digest -u admin:PASSWORD \
  -H "Content-Type: application/json" \
  -d '{"config": {"enable": false}}'

# Reboot to apply
curl -X POST "http://SHELLY_IP/rpc/Shelly.Reboot" \
  --digest -u admin:PASSWORD
```

**Result:** RAM free increased from 147 KB to 217 KB (48% improvement). Matter num_fabrics dropped to 0.

### Verification After Reboot

```
RAM free:      217,288 bytes (was 147,496)
Matter:        disabled, num_fabrics=0
BLE:           disabled
Uptime:        counting from controlled reboot
Reset reason:  3 (SW_RESET — expected from manual reboot)
```

---

## Remediation Plan

Three phases, each testing one variable. Move to the next phase only if crashes continue.

### Phase 1: Observe After Disabling Matter + BLE (current)

**Started:** 2026-02-24 ~15:35 EST
**Duration:** 72 hours (until ~2026-02-27 15:35 EST)
**What changed:** Disabled Matter and BLE protocols

**Success criteria:**
- No watchdog resets (reason 4) for 72+ hours
- No software resets (reason 3) for 72+ hours

**Baseline metrics after fix:**

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| RAM free | 147,496 bytes | 213,452 bytes | +45% |
| RAM min (low watermark) | unknown | 203,564 bytes | — |
| Matter | enabled (1 fabric) | disabled (0 fabrics) | — |
| BLE | enabled | disabled | — |

**If crashes stop** → Root cause confirmed as Matter/BLE memory pressure. Leave them disabled (we don't use them). Proceed to Phase 2 anyway for general hardening.

**If crashes continue** → Matter/BLE wasn't the only cause. Proceed to Phase 2 immediately.

**If a crash occurs during Phase 1:**
1. Immediately check for core dump: `http://SHELLY_IP/debug/core`
2. Save the dump before the monitor auto-recovers and reboots the device
3. Record the reset_reason, uptime before crash, BSSID at time of crash

### Phase 2: Firmware Update (after Phase 1)

Update from factory firmware 1.7.99 (built 2025-10-21) to stable **1.7.4** (released 2026-01-27).

**Why this matters:** Firmware 1.7.99 is the factory build that shipped with the device. The version number looks newer than 1.7.4, but it's actually a pre-release development snapshot from 3 months before 1.7.4 stable was cut. The stable release includes:
- Fixed DNS-SD answer parsing (Matter)
- Fixed crash by DNSSD resolving only non-LL IPv6 addresses (Matter)
- Sped up Matter subscription resumption retries
- Fixed Wi-Fi 6 support on Gen4 devices
- Fixed DAKs with leading zeroes

**Complication:** Cloud is disabled (`"connected": false`) for security (local-only control). The device can't check Shelly's update servers, so `Shelly.CheckForUpdate` returns `{}`.

**Update options (pick one):**

**Option A — Temporarily re-enable Shelly Cloud (easiest):**
```python
import requests
from requests.auth import HTTPDigestAuth
AUTH = HTTPDigestAuth('admin', 'PASSWORD')
IP = 'SHELLY_IP'

# 1. Enable cloud
requests.post(f'http://{IP}/rpc/Cloud.SetConfig', auth=AUTH,
              json={'config': {'enable': True}})

# 2. Wait ~30 seconds for cloud to connect, then check for updates
r = requests.get(f'http://{IP}/rpc/Shelly.CheckForUpdate', auth=AUTH)
print(r.json())

# 3. If update available, apply it
requests.post(f'http://{IP}/rpc/Shelly.Update', auth=AUTH,
              json={'stage': 'stable'})

# 4. After device reboots with new firmware, disable cloud again
requests.post(f'http://{IP}/rpc/Cloud.SetConfig', auth=AUTH,
              json={'config': {'enable': False}})
```

**Option B — Shelly mobile app:**
Open the Shelly app → find the device → Settings → Firmware Update. The app communicates over LAN and can trigger the update even with cloud disabled.

**Option C — Manual OTA URL (if we can find the firmware URL):**
```
http://SHELLY_IP/ota?url=<firmware-download-url>
```
Firmware URLs can be found at the [Shelly Firmware Archive](http://archive.shelly-tools.de/). We need the URL for model `PlugUSG4`.

**After update:**
- Verify firmware version via `Shelly.GetDeviceInfo`
- Check RAM free (may change with new firmware)
- Re-disable cloud if Option A was used
- Monitor for another 72 hours

### Phase 3: WiFi Tuning (if crashes persist after Phase 2)

The device has active WiFi roaming configured:
```json
{
  "roam": {
    "rssi_thr": -80,
    "interval": 60
  }
}
```

This means the device checks every 60 seconds if it should switch to a different mesh AP. Roaming events can cause the ESP32 WiFi stack to hang.

**Options (try in order):**

**3a. Reduce roaming aggressiveness:**
```python
# Raise threshold so device only roams when signal is very weak
requests.post(f'http://{IP}/rpc/WiFi.SetConfig', auth=AUTH,
              json={'config': {'roam': {'rssi_thr': -70, 'interval': 120}}})
```

**3b. Assign static IP via DHCP reservation:**
On your router, assign a fixed IP to MAC `58:E6:C5:36:E7:54` so the device never needs to re-negotiate DHCP during roaming.

**3c. Disable 802.11r/k/v on mesh IoT SSID:**
In your Google/Nest WiFi settings, check if fast roaming can be disabled for IoT devices. This is the most commonly cited fix for Shelly WiFi instability.

**3d. Nuclear option — set static BSSID:**
Force the device to use only one specific AP. Eliminates roaming entirely but reduces coverage if that AP goes down.

### Phase 4: Escalation (if all else fails)

If the device continues crashing after Phases 1-3:

1. **Enable debug logging** to capture crash context:
```python
# Enable WebSocket debug logging
requests.post(f'http://{IP}/rpc/Sys.SetConfig', auth=AUTH,
              json={'config': {'debug': {'websocket': {'enable': True}}}})

# Stream logs (use websocat or similar WebSocket client)
# websocat ws://SHELLY_IP/debug/log
```

2. **Capture core dump** after next crash at `http://SHELLY_IP/debug/core`

3. **File bug with Shelly support** — include:
   - Device model: S4PL-00116US
   - Firmware version (current at time of report)
   - Core dump file
   - Crash timeline with reset_reason codes
   - Network environment (mesh WiFi, AP count)
   - Steps already taken

4. **Consider alternative firmware** (ESPHome) — last resort only. Gen4 support is experimental, and flashing requires physical UART access via a 1.27mm pitch serial header. High risk for a device protecting critical infrastructure.

### Decision Tree

```
Crashes stopped after Phase 1?
├── YES → Phase 1 fixed it. Proceed to Phase 2 for hardening.
│         └── Still stable after Phase 2? → Done. Monitor ongoing.
│         └── Crashes returned? → Phase 3 (WiFi tuning).
└── NO  → Phase 2 immediately (firmware update).
          └── Still crashing? → Phase 3 (WiFi tuning).
                └── Still crashing? → Phase 4 (debug logging + Shelly support).
```

### What NOT to Do

- **Don't flash alternative firmware** — too risky for a device protecting the sump pump. Gen4 ESPHome support is experimental, requires physical disassembly, and could brick the device.
- **Don't re-enable Matter or BLE** — we don't use them and they consume ~65 KB of RAM.
- **Don't rush** — test one variable at a time so we know which fix actually worked.

---

## Log Entries

### Reboot #1 — 2026-02-23 ~20:14
```
PLUG REBOOTED: uptime reset from 49460s to 42s (power outage?)
WARNING: Plug output is OFF unexpectedly! Turning back ON.
Plug turned back ON successfully
```

### Reboot #2 — 2026-02-24 ~07:31
```
PLUG REBOOTED: uptime reset from 40620s to 38s (power outage?)
WARNING: Plug output is OFF unexpectedly! Turning back ON.
Plug turned back ON successfully
```

### Reboot #3 — 2026-02-24 ~15:24
```
PLUG REBOOTED: uptime reset from 28380s to 45s (power outage?)
WARNING: Plug output is OFF unexpectedly! Turning back ON.
Plug turned back ON successfully
```

---

## References

### Shelly Documentation
- [Shelly Gen2+ API — System Component](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Sys/)
- [Shelly Gen2+ Firmware Changelog](https://shelly-api-docs.shelly.cloud/gen2/changelog/)
- [Shelly Gen2+ Debug Logs](https://shelly-api-docs.shelly.cloud/gen2/General/DebugLogs/)
- [Shelly Plug US Gen4 Documentation](https://us.shelly.com/blogs/documentation/shelly-plug-us-gen4)
- [Shelly Firmware Archive Tool](http://archive.shelly-tools.de/)
- [Shelly Troubleshooting Guide](https://support.shelly.cloud/en/support/solutions/articles/103000280420-troubleshooting-shelly-devices)
- [Firmware 1.7.4 Release Announcement](https://community.shelly.cloud/announcement/33-firmware-version-174-for-gen2-shelly-devices-released-firmware-version-174-f%C3%BCr-gen2-shelly-ger%C3%A4te-ver%C3%B6ffentlicht/)

### ESP32 / BLE / WiFi Issues
- [ESP32 BLE + WiFi Memory Issues](https://www.esp32.com/viewtopic.php?t=5549)
- [ESPHome: BLE Makes Shelly Plus 1 Unresponsive](https://github.com/esphome/issues/issues/3485)
- [Arduino-ESP32: WiFi Task Watchdog Timeout with BLE](https://github.com/espressif/arduino-esp32/issues/1754)
- [Arduino-ESP32: Memory Fail with BLE/WiFi](https://github.com/espressif/arduino-esp32/issues/8741)
- [ESP32 Reset Reason Documentation](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/reset_reason.html)
- [ESP-IDF Watchdog Timer Documentation](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/wdts.html)

### Community Reports
- [Shelly: "Firmware super unreliable" (1 Mini Gen4)](https://community.shelly.cloud/topic/9455-firmware-super-unreliable/)
- [Shelly: "Unexpected switch off" (Power Strip 4 Gen4)](https://community.shelly.cloud/topic/11999-unexpected-switch-off/)
- [Shelly: WiFi Disconnects with Multiple APs](https://community.shelly.cloud/topic/9561-wifi-disconnects-no-reconnect-since-15/)
- [Shelly: WiFi Disconnects with Multiple Access Points (Forum)](https://shelly-forum.com/thread/21982-shelly-wifi-disconnects-with-multiple-access-points/)
- [HA: Gen4 Devices Not Connecting After Network Restart](https://community.home-assistant.io/t/shelly-gen4-devices-not-connecting-after-ha-restart-or-network-restart/983126)
- [IoT WiFi Mesh Stability Guide](https://selorahomes.com/docs/how-to/troubleshooting/shelly-sonoff-device-reliability-crisis/)

---

## Updates

**2026-02-25 (afternoon) — Phase 1 checkpoint (28.5h):** No crashes since disabling Matter + BLE. Uptime steady at 102,613s (28.5h). RAM free holding at 217,004 bytes (vs 147K before fix). RAM min watermark is 173,924 bytes — healthy. Reset reason 3 is from the controlled reboot on Feb 24, not a new crash. The 19:32 event on Feb 24 was a counter glitch (uptime 14227→13183), not an actual reboot — the improved monitor code now distinguishes these. Phase 1 observation continues through ~2026-02-27 15:35 EST (~43.5h remaining). Early signs are very positive.

**2026-02-24 (evening):** Documented full 4-phase remediation plan. Key discovery: cloud is disabled so `Shelly.CheckForUpdate` returns empty — firmware update will require temporarily re-enabling cloud or using the Shelly app. Core dump returned 404 (cleared by controlled reboot). WiFi roaming is active (`rssi_thr: -80, interval: 60`).

**2026-02-24 (afternoon):** Disabled Matter and BLE. RAM free improved 48% (147 KB → 213 KB). Began Phase 1 observation. Research confirmed: firmware 1.7.99 is the factory build (2025-10-21), stable 1.7.4 (2026-01-27) includes the DNSSD crash fix. Multiple community reports of Gen4 instability, ESP32 BLE+WiFi memory contention, and WiFi mesh roaming crashes.
