# Shelly Plug US Gen4 — Reboot/Crash Investigation

**Device:** Shelly Plug US Gen4 (S4PL-00116US)
**Firmware:** 1.7.99-plugusg4prod1 (pre-production build)
**GitHub Issue:** [#1](https://github.com/toddllm/home-assistant/issues/1)
**Status:** Monitoring after disabling Matter + BLE (2026-02-24)

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

**Memory pressure from concurrent wireless stacks.** The Shelly Plug US Gen4 runs on an ESP32 which shares a single radio between WiFi, BLE, and Matter. Running all three simultaneously:

1. **Matter protocol stack** is heavy — includes mDNS/DNSSD, IPv6, fabric management
2. **BLE scanning** contends with WiFi for radio time
3. **WiFi mesh roaming** (BSSID changes between APs) adds transient memory/CPU spikes
4. Combined load can cause the watchdog timer to expire (OWDT_RESET) or trigger a software crash (SW_RESET)

### Evidence

#### From Shelly Firmware Changelog
- **"Matter: Fix crash by DNSSD resolving only non-LL IPv6 addresses"** — a known crash in the Matter stack related to DNS service discovery. This may be the exact bug hitting our device.
- Multiple BLE-related crash fixes in recent firmware versions
- Ongoing optimization for concurrent wireless component handling

#### From Community Reports
- **ESP32 Forum:** BLE + WiFi causes out-of-memory crashes ([esp32.com/viewtopic.php?t=5549](https://www.esp32.com/viewtopic.php?t=5549))
- **ESPHome Issues:** BLE makes Shelly Plus 1 unresponsive, OTA crashes when BLE tracker enabled ([github.com/esphome/issues/issues/3485](https://github.com/esphome/issues/issues/3485))
- **Home Assistant Community:** Shelly Gen4 devices going unavailable, connection issues
- **Shelly Community:** Gen4 Power Strip users report "unexpected switch off"
- **Arduino-ESP32 Issues:** Memory fail with BLE/WiFi on latest ESP update ([github.com/espressif/arduino-esp32/issues/8741](https://github.com/espressif/arduino-esp32/issues/8741))

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

## Monitoring Plan

### Success Criteria
- **No watchdog resets (reason 4) for 72+ hours** → Matter/BLE was the cause
- **No software resets (reason 3) for 72+ hours** → Confirmed fix

### If Crashes Continue

| Step | Action | Why |
|------|--------|-----|
| 1 | Enable debug logging via WebSocket | Stream logs to capture crash context |
| 2 | Check core dump at `http://SHELLY_IP/debug/core` | Get stack trace from crash |
| 3 | Set static BSSID (disable WiFi roaming) | Eliminate mesh roaming as trigger |
| 4 | Check for firmware updates | 1.7.99 is pre-production; stable may fix this |
| 5 | File bug with Shelly support | Include core dump + timeline |
| 6 | Consider firmware downgrade | Use Shelly archive tool to find last stable |

### Debug Logging (If Needed)

```bash
# Enable debug logging
curl -X POST "http://SHELLY_IP/rpc/Sys.SetConfig" \
  --digest -u admin:PASSWORD \
  -H "Content-Type: application/json" \
  -d '{"config": {"debug": {"websocket": {"enable": true}}}}'

# Stream logs in real-time
websocat ws://SHELLY_IP/debug/log

# Check for crash dumps
curl http://SHELLY_IP/debug/core --digest -u admin:PASSWORD
```

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

- [Shelly Gen2+ API — System Component](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Sys/)
- [Shelly Gen2+ Firmware Changelog](https://shelly-api-docs.shelly.cloud/gen2/changelog/)
- [Shelly Gen2+ Debug Logs](https://shelly-api-docs.shelly.cloud/gen2/General/DebugLogs/)
- [ESP32 BLE + WiFi Memory Issues](https://www.esp32.com/viewtopic.php?t=5549)
- [ESPHome: BLE Makes Shelly Plus 1 Unresponsive](https://github.com/esphome/issues/issues/3485)
- [Arduino-ESP32: Memory Fail with BLE/WiFi](https://github.com/espressif/arduino-esp32/issues/8741)
- [Shelly Firmware Archive Tool](http://archive.shelly-tools.de/)
- [Shelly Troubleshooting Guide](https://support.shelly.cloud/en/support/solutions/articles/103000280420-troubleshooting-shelly-devices)

---

## Updates

**2026-02-24:** Disabled Matter and BLE. RAM free improved 48%. Monitoring for 72h to confirm fix.
