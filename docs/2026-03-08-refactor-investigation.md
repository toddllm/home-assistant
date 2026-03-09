# Refactor Investigation - March 8, 2026 Evening

Note: this preserves the initial investigation notes from the first failed rollout.
Those notes reference `systemd` because that was the assumed service-manager context at
the time. The current local deployment and live validation in this repo use `launchd`
on macOS; see `docs/2026-03-08-verification-gaps.md` for the current verified status.

## Situation
- Monitor stopped, plug OFF, manually pulsing pump
- Float is stuck (pump draws ~500W any time plug is ON)
- Water level is low (pump sucking air at 60s)
- Manually pulsing 15-20s every 15-20 min to keep pit clear

## What Happened During the Refactor Deployment

### Timeline of the Refactored Code (20:45 - 21:20)

| Time | Event | Problem |
|------|-------|---------|
| 20:45 | Deployed spring mode code, started via nohup | Created process outside systemd |
| 20:53 | Old code (spring mode) caught pump run: 659W, stopped after 0.5 min | **Working correctly** |
| 21:04 | Killed spring mode, deployed refactored state machine code | |
| 21:05 | Old process caught pump run: 491W, stopped after 2.0 min | **Working correctly** (old code still running) |
| 21:07 | Killed process, restarted | |
| 21:08 | First refactored code start (nohup) | Two instances started |
| 21:09 | Second instance started | PID check not yet active (race) |
| 21:10 | Killed both, restarted | systemd auto-restart began hammering |
| 21:10-21:13 | **17 restart attempts in 3 minutes** | systemd Restart=always + PID lock = spam loop |
| 21:13 | Manual systemctl restart, clean start | |
| 21:14 | Another restart to fix buffering | Timer reset |
| 21:16 | **Pump starts running (504W)** | Caught in log |
| 21:17 | **Restart to fix log buffering** | **Timer reset after 1 min — never reached 3 min** |
| 21:17 | Restart detects pump running (491W), starts timer | |
| 21:18 | **Another restart for log fix** | **Timer reset again** |
| 21:18 | Detects pump running (502W), starts timer | |
| 21:20 | **Manual stop** | Pump had been running 4+ min unprotected |
| 21:20 | Manually turned plug OFF | Temp was 42.5C |

### Root Cause: `running_since` Not Persisted

The state file saves:
```json
{
  "state": "NORMAL",
  "cycle_count": 0,
  "duty_phase": "OFF",
  "last_pump_run_wall": 1773019205.53
}
```

**`running_since` is NOT in the state file.** On every restart:
1. `load_state()` restores state=NORMAL
2. Startup code checks if pump is running → yes → sets `running_since = time.monotonic()` (NOW)
3. Timer starts from zero
4. Next restart 1-2 min later → timer resets to zero again
5. **3-minute threshold is never reached**

This is the critical bug. The old code didn't have this problem because:
- It wasn't being restarted repeatedly
- It used blocking `time.sleep()` inside safe mode loops — once it entered safe mode, it stayed there through restarts (systemd would restart and it would re-enter from scratch, but the blocking loop meant it didn't matter)

### Secondary Issue: Log Output Not Visible

The refactored code's main loop ran and updated the state file every 30s, proving it was polling. But **zero log lines appeared** after "Monitor ready" until we added direct file writes.

**Cause**: systemd `StandardOutput=append:/path/to/file` buffers differently than expected. Even with `flush=True` in Python's `print()`, the output was block-buffered at the OS level. Adding `python3 -u` partially helped but we kept restarting before logs could flush.

The fix of writing directly to the log file via `open()` works but creates **double lines** because systemd's `StandardOutput=append:` also writes to the same file.

### Tertiary Issue: systemd vs nohup Conflict

We started the process with `nohup` while `sump-pump-monitor.service` had `Restart=always`. This meant:
- nohup process held the PID file
- systemd kept trying to start new instances every 10s
- Each attempt hit the PID lock and exited with code 1
- systemd logged it as a failure and scheduled another restart
- **17 restart-attempt log lines in 3 minutes**

## Bugs Found in Refactored Code

### Bug 1: `running_since` Not Persisted (CRITICAL)
- **Severity**: Critical — defeats stuck-float detection across restarts
- **Fix**: Add `running_since_wall` to state file. On load, compute `running_since = monotonic() - (time.time() - running_since_wall)` to reconstruct elapsed time.

### Bug 2: Double Log Lines
- **Severity**: Cosmetic but confusing
- **Fix**: Either remove `StandardOutput=append:` from the systemd service (let the script handle its own logging), OR remove the direct file write from `log()`. Not both.

### Bug 3: Startup Turns Plug ON Before Checking State
- **Severity**: Medium — if restarting during a tier's OFF phase, startup turns the plug ON
- **Location**: `sump_pump_monitor.py` line 714-719
- **Fix**: Only turn plug ON if state is NORMAL. Tier states manage their own plug state.

### Bug 4: `handle_power_cycle` is Blocking
- **Severity**: Design issue — the power cycle function blocks for 70s (10s off + 60s settle)
- **Impact**: During those 70s, no heartbeat, no state save, no crash detection
- **Fix**: Make power cycle non-blocking using elapsed-time checks like the tier handlers. For now, acceptable since it's a brief operation.

### Bug 5: No Protection Against Timer Reset on Restart
- **Severity**: Critical — the entire escalation path depends on `running_since` accumulating
- **Impact**: If systemd restarts the process (firmware crash, code bug, manual restart), the stuck-float timer resets and the pump runs indefinitely in NORMAL mode
- **Fix**: Persist `running_since_wall` and restore it. Also: if pump is running at startup and state was NORMAL, check how long the pump has been running from historical data (state file, Shelly energy counters) rather than starting the timer from zero.

## What the Old Code Got Right

1. **Blocking safe mode loop**: Once triggered, it stayed in safe mode through process lifecycle. Restarts didn't matter because it would re-detect the stuck float and re-enter.
2. **No dependency on state persistence for safety**: The critical path (stuck float → power cycle → safe mode) was entirely within a single process lifetime.
3. **Simplicity**: One file, no imports, no state machine. Easy to reason about.

## What the Old Code Got Wrong

1. **Blocking loops**: Couldn't detect Shelly crashes or other events during rest periods
2. **No state persistence**: Restart = lose all context
3. **Multiple env-var modes with shared config**: Led to the `status["temp"]` bug and env var confusion
4. **No PID lock**: Duplicate processes possible

## Recommendations

### For Tonight
1. Keep monitor stopped
2. Manually pulse pump 15-20s every 15-20 min
3. Float is stuck — user to manually fix when possible

### For the Fix
1. **Persist `running_since_wall`** in state file — this is the #1 fix
2. **On startup with pump running + state=NORMAL**: calculate elapsed run time from state file's `running_since_wall`. If already past 3 min, skip straight to power cycle.
3. **Remove `StandardOutput=append:` from systemd service** — let `log()` handle file writes directly
4. **Test the restart scenario**: start monitor, simulate stuck float (set MAX_RUN_MINUTES=0.5), restart mid-run, verify timer persists
5. **Do NOT deploy to production until the restart-timer test passes**

### Testing Checklist Before Next Deploy
- [ ] Start monitor, pump idle → NORMAL, heartbeat logs appear
- [ ] Pump starts running → "Pump started" logged
- [ ] Pump stops → "Pump stopped" logged
- [ ] Pump runs > MAX_RUN_MINUTES → power cycle triggered
- [ ] Power cycle fails → TIER_1 entered
- [ ] TIER_1 escalates → TIER_2
- [ ] Restart during stuck float → timer persists, not reset
- [ ] Restart during TIER_1 → resumes TIER_1, not NORMAL
- [ ] PID lock prevents duplicates
- [ ] systemd Restart=always works without spam
