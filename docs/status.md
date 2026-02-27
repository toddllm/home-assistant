# Sump Pump System — Running Status

**Last updated:** 2026-02-27
**Location:** Fort Hunter, NY 12069 (Schoharie Creek / Mohawk River confluence)

---

## System Health

| Component | Host | Status | Since | Notes |
|-----------|------|--------|-------|-------|
| Sump pump monitor | toddllm (Linux) | **Running** | Feb 24 19:36 | systemd service, polling every 30s |
| AI analyzer | Mac (M3 Max) | **Running** | Feb 26 15:33 | launchd service, port 8078 |
| Dashboard | toddllm | **Running** | Feb 23 | port 8077 |
| Shelly Plug | 192.168.68.147 | **Up (unstable firmware)** | Feb 26 17:51 | Last crash: reboot #4, reset reason 4 |
| AI telemetry | toddllm → Mac | **Flowing** | Feb 27 00:29 | First real analysis at 05:29 UTC |

## Shelly Firmware Investigation (Issue #1)

**Current phase:** Phase 2 — Firmware update to stable 1.7.4
**Firmware:** 1.7.99-plugusg4prod1 (factory pre-release, 2025-10-21)
**Target:** 1.7.4 (stable, 2026-01-27)

### Crash History

| # | Date | Uptime | Reset | Phase | Notes |
|---|------|--------|-------|-------|-------|
| 1 | Feb 23 20:14 | 13.7h | 4 (watchdog) | Pre-fix | Matter+BLE enabled |
| 2 | Feb 24 07:31 | 11.3h | 3 (sw reset) | Pre-fix | Matter+BLE enabled |
| 3 | Feb 24 15:24 | 7.9h | 4 (watchdog) | Pre-fix | Matter+BLE enabled, shortest interval |
| 4 | **Feb 26 17:51** | **40.4h** | 4 (watchdog) | **Phase 1** | Matter+BLE disabled. Preceded by uptime glitch |

### Phase 1 Conclusion

Disabling Matter + BLE improved mean time between crashes from ~11h to 40h but did not eliminate them. Root cause is in firmware 1.7.99 itself, not just memory pressure from wireless stacks.

### Phase 2 Plan (firmware update)

**Status:** Ready to execute
**Method:** Temporarily re-enable Shelly Cloud (~5 min), trigger OTA update to stable 1.7.4, re-disable cloud
**Risk:** Low — brief cloud exposure, ~2 min downtime during flash. Pump stays powered (plug default is ON after reboot).
**Timing:** Should be done before spring melt (late March). Earlier is better — each day on unstable firmware is unnecessary risk.
**Detailed procedure:** See `docs/shelly-reboot-investigation.md`, Phase 2 section

---

## Weather Intelligence (Epics 1-2)

### Data Collection Status

| Source | Table | Interval | First Data | Count | Status |
|--------|-------|----------|-----------|-------|--------|
| Open-Meteo weather | weather | 1h | Feb 26 | ~10 | Collecting (soil moisture NULL — frozen ground) |
| Open-Meteo forecast | forecast | 6h | Feb 26 | ~336 rows | Collecting (168 hourly rows/fetch) |
| USGS Mohawk@Fonda | stream_gauge | 30m | Feb 26 | ~20 | Collecting (10.4ft, discharge ice-affected) |
| USGS Mohawk@Amsterdam | stream_gauge | 30m | Feb 26 | ~20 | Collecting (10.6ft, discharge ice-affected) |
| USGS Schoharie@Burtonsville | stream_gauge | 30m | Feb 26 | ~20 | Collecting (1.9ft, discharge ice-affected) |
| NWS flood alerts | nws_alerts | 15m | Feb 26 | 0 alerts | Collecting (no active alerts — correct) |
| Pump telemetry | readings | 30s | Feb 27 | ~2 | Just started flowing |
| AI analysis | analyses | 5m | Feb 27 | 1 | First analysis completed |
| Correlations | correlations | daily | — | 0 | Need more data (minimum 3 days) |

### Current Conditions (Feb 27)

- **Temperature:** -6.4C (well below freezing)
- **Precipitation:** 0.0mm
- **Snow depth:** 0.04m (thin cover)
- **Soil moisture:** NULL (ground frozen — Open-Meteo limitation)
- **Mohawk River:** 10.4-10.6ft, stable, ice-affected
- **Schoharie Creek:** 1.9ft, stable, ice-affected
- **Weather risk score:** 0.024 (low)
- **Pump activity:** ~1-2 cycles/day, 30s each (winter baseline)

### Hypotheses (all active, 0 evidence points yet)

1. Soil moisture (28-100cm) > 0.35 predicts pump cycling within 6h
2. Stream gauge > 4.0ft correlates with pump frequency > 4/hour
3. SWE decrease > 10mm/day + temps > 2C predicts increased pump activity
4. Cycling with no precip + low soil moisture = mechanical issue
5. 48h precipitation > 25mm predicts pump frequency > 6/hour within 12h

---

## Decision Log

### 2026-02-27: Enable AI telemetry

**Decision:** Deploy latest monitor code to toddllm, set AI_ENABLED=true, restart service.
**Reasoning:** Zero-risk change — fire-and-forget POST with graceful failure handling. Every day without telemetry is lost baseline data. Need dry-weather baseline before spring.
**Result:** Telemetry flowing. First AI analysis completed at 05:29 UTC. Status "normal", confidence 0.85.

### 2026-02-27: Proceed to Phase 2 firmware update

**Decision:** Update Shelly firmware from 1.7.99 to stable 1.7.4.
**Reasoning:** Phase 1 showed Matter/BLE was not the root cause (crash #4 at 40h). Firmware 1.7.99 is a pre-release development build. Stable 1.7.4 includes the DNSSD crash fix that likely addresses our watchdog timeouts. Spring melt increases pump activity and crash risk. Update risk (~5 min downtime) is negligible vs ongoing instability.
**Status:** Ready to execute. Waiting for operator confirmation.

### 2026-02-27: Add Mohawk River gauges

**Decision:** Track three USGS gauges instead of one (Mohawk upstream + downstream, Schoharie Creek).
**Reasoning:** Fort Hunter sits at the Schoharie/Mohawk confluence. Water table is driven by both waterways. When both rise simultaneously, backwater effects at the confluence compound the impact on the water table. Single-gauge monitoring would miss this critical interaction.
**Result:** All three gauges reporting. Risk score includes confluence-aware compounding factor.

### 2026-02-27: Correct location coordinates

**Decision:** Updated from 42.938/-74.185 to 42.942/-74.282.
**Reasoning:** Original coordinates were ~5 miles east (near Amsterdam). Correct location is Fort Hunter at the Schoharie/Mohawk confluence. Affects weather data accuracy and NWS alert zone.

---

## Upcoming Work

### This Week
- [ ] **Execute Phase 2 firmware update** (requires brief cloud re-enable)
- [x] ~~Enable AI telemetry on toddllm~~
- [ ] Verify 48h of clean AI analysis (Issue #3)

### Next 2 Weeks (Early March)
- [ ] Accumulate 7+ days of weather data with soil moisture (Issue #4, pending thaw)
- [ ] Accumulate 14+ days of stream gauge data (Issue #6)
- [ ] First correlation snapshots (need 3+ days of concurrent pump + weather data)
- [ ] Monitor for power anomalies (718-731W readings on Feb 24 need investigation)

### Before Spring Melt (Mid-March)
- [ ] All data sources collecting reliably for 2+ weeks
- [ ] Hypothesis engine has initial evidence points
- [ ] Firmware stable on 1.7.4 (or escalate to Phase 3)
- [ ] Establish dry-weather pump baseline (Issue #15)
- [ ] Physical pump inspection: check discharge pipe for ice, verify check valve, test float

---

## Power Reading Anomaly (needs investigation)

On Feb 24 20:50-21:01, the pump ran 7 cycles in 10 minutes with power ranging from 244W to 731W:

```
20:50:17  244.5W   ← low (partial startup capture?)
20:51:17  718.5W   ← above 700W threshold
20:52:17  729.7W   ← above 700W threshold
20:53:17  730.5W   ← above 700W threshold
20:55:18  482.8W   ← normal range
20:57:18  715.4W   ← borderline
21:00:48  372.3W   ← low
```

Questions:
- Is 718-731W within normal startup transient range, or is steady-state power elevated?
- Are the low readings (244W, 372W) partial captures from the 30s polling window?
- Was this a stuck-float event that self-resolved, or normal high-water cycling?

This will become clearer once we have 1-2 weeks of telemetry with the AI analyzer tracking running power stats separately from idle readings.
