# Stuck Float Incident - March 8, 2026

## Timeline

### Phase 1: Normal Operation (midnight - 6:35 AM)
- Pump running healthy short cycles (~30s each), normal power (571-783W)
- 5 normal pump cycles between midnight and 5:14 AM

### Phase 2: Float Gets Stuck (6:35 AM)
- **06:35** - Pump starts running continuously at 577W
- **06:38** - 3 min limit hit. Power cycle attempted. **Worked** - pump stopped.
- **07:01-07:08** - Two more short runs (1 min each), power lower than usual (469W, 316W)
- **07:17** - Pump stuck again (484W). Power cycle tried. **Failed** - still running at 493W.
- **07:21** - Enters SAFE MODE (1 min ON / 5 min OFF due to misconfigured .env)

### Phase 3: Safe Mode Struggles (7:21 AM - 1:00 PM)
Safe mode ran but had multiple problems:
- **Misconfigured .env**: `SAFE_RUN_SECONDS=30` and `SAFE_REST_SECONDS=7200` were set to conservative values, overriding safe mode defaults. Log showed "0 min ON / 120 min OFF" but the actual safe mode code used the env values (30s ON / 7200s rest — but the code was still using the safe mode loop timing, not these values in the first runs before the restart).
- **Shelly firmware crashes**: 4 PANIC crashes (reset_reason=4) at 09:35, 11:25, 11:56, 12:45
  - Each crash turned the plug OFF
  - Monitor detected reboot, turned plug back ON
  - Pump immediately stuck again, triggering another power cycle + safe mode entry
  - Pattern: crash → reboot → plug OFF → monitor turns ON → pump stuck → 3 min → power cycle fails → safe mode again
- **Duplicate monitor processes**: At 12:41-12:42, multiple monitor instances were running simultaneously, causing duplicate alerts and conflicting commands
- **Temp rising**: Plug temperature climbed from 32C to 45.9C during the repeated stuck-float cycles

### Phase 4: Manual Intervention + Conservative Mode (1:00 PM)
- User manually restarted monitor with `CONSERVATIVE_MODE=true`
- Conservative mode set to 30s ON / 2h OFF

### Phase 5: Conservative Mode Fails (~3:00 PM - 8:45 PM)
**Conservative mode crashed every single time it tried to run a pulse.**

Bug: `status["temp"]` should have been `status["temp_c"]` — the status dict uses `temp_c` as the key.

```python
# Bug (line 370):
temp = status["temp"] if status else 0    # KeyError: 'temp'

# Fix:
temp_c = status["temp_c"] if status else 0
```

Timeline of conservative crashes:
- **14:59** - Cycle 1 attempt → KeyError: 'temp' → crash → auto-restart
- **15:01** - Restart, 2h rest begins
- **17:01** - Cycle 1 attempt → KeyError: 'temp' → crash → auto-restart
- **17:01** - Restart, 2h rest begins
- **19:01** - Cycle 1 attempt → KeyError: 'temp' → crash → auto-restart
- **19:02** - Restart, 2h rest begins

**Result: From 1:00 PM to 8:45 PM (7h 45m), the pump was OFF the entire time.** Conservative mode never completed a single pulse. It would wait 2 hours, turn the plug ON for 30 seconds, then crash trying to read temperature, leaving the plug in an undefined state. The auto-restart service would kick in and start another 2h wait.

### Phase 6: Fix + Spring Mode (8:45 PM)
- Fixed `status["temp"]` → `status["temp_c"]` bug
- Fixed .env: restored SAFE_RUN_SECONDS=120, SAFE_REST_SECONDS=600
- Added SPRING_MODE (60s ON / 30 min OFF, auto-exits after 3 dry pulses)
- Deployed and confirmed running

## Root Causes

### 1. The `status["temp"]` bug
Conservative mode was never tested with a live Shelly plug. The status dict key is `temp_c`, not `temp`. This was introduced when conservative mode was first written and never caught because the mode was never activated until today.

### 2. Misconfigured .env overrides
When conservative mode was enabled, someone also set `SAFE_RUN_SECONDS=30` and `SAFE_REST_SECONDS=7200` — clobbering the safe mode defaults. This meant if safe mode was entered (before conservative was enabled), it would run 30s ON / 120 min OFF, which is far too little pumping for a stuck float with active water inflow.

### 3. Shelly firmware crashes compounding the problem
The Shelly Plug US Gen4 crashed 4 times (reset_reason=4, PANIC) during the incident. Each crash:
- Turned the plug OFF (default state after reboot)
- Made the monitor lose track of pump state
- Led to duplicate processes and conflicting commands

### 4. No validation of env var overrides
The safe mode env vars were silently overridden without any sanity check. `SAFE_RUN_SECONDS=30` with `SAFE_REST_SECONDS=7200` is nonsensical for safe mode (30s pumping per 2 hours won't prevent flooding).

## Lessons Learned

1. **Test all code paths with real hardware.** The conservative mode `status["temp"]` bug sat latent until the first real activation.
2. **Don't share env var namespaces.** SAFE_* and CONSERVATIVE_* should never be confused. The .env was edited hastily and clobbered safe mode settings.
3. **Add startup validation.** Sanity-check that SAFE_RUN_SECONDS > SAFE_REST_SECONDS / 10 or similar, and warn if safe mode is neutered.
4. **Conservative mode's 2h gap is too long** for active spring water inflow. The new SPRING mode (60s ON / 30 min OFF) fills this gap.
5. **The Shelly firmware crash issue (GitHub #1) is still actively causing problems.** It's not just a nuisance — it knocks the plug offline during critical pumping.

## Changes Made
- Fixed `status["temp"]` → `status["temp_c"]` in conservative mode
- Fixed `status["temp"]` → `status["temp_c"]` in conservative mode notification
- Restored .env: SAFE_RUN_SECONDS=120, SAFE_REST_SECONDS=600
- Added SPRING_MODE: 60s ON / 30 min OFF, auto-exits to NORMAL after 3 consecutive dry pulses
