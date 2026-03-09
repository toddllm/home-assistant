# Sump Pump Monitor Verification Gaps - March 8, 2026

This document records what was verified during the live stuck-float debugging session,
and what still remains unverified on real hardware.

## Live Verified

- `NORMAL` restart persistence: restarting during an active stuck-float run preserved the
  accumulated run timer and still triggered `POWER_CYCLE` at the 3-minute threshold.
- Safe tier startup: restarting from saved `TIER_1 / OFF` resumed without energizing the
  plug.
- Duplicate-process lock: a second monitor instance exited immediately when the PID file
  was already held.
- Runtime OFF-phase correction: if the plug was manually or externally turned on during
  a tier `OFF` phase, the running monitor turned it back off on the next active poll.
- `POWER_CYCLE` resume: restarting in `POWER_CYCLE / ON` resumed the settle phase,
  detected the still-running pump, and fell back to `TIER_1 / OFF`.
- `COOLDOWN -> NORMAL`: the monitor later observed a full 90-second stable idle window,
  confirmed the float was unstuck, and returned to `NORMAL` on real hardware.
- `COOLDOWN -> previous tier`: seeding `COOLDOWN` and restarting caused the monitor to
  turn the plug on, detect the pump running again, and return to `TIER_1 / OFF`.
- Notification title sanitization: the ntfy `Title` header no longer fails on em dashes.

## Verified By Unit Test Only

- Dry-pulse exit from a tier back to `NORMAL`.
- Tier escalation from `TIER_1 -> TIER_2` and `TIER_2 -> TIER_3`.
- `POWER_CYCLE -> NORMAL` when the pump actually stays idle after power is restored.
- State-duration restore for long-lived states like `COOLDOWN` and `LOCKOUT`.

## Not Live-Tested On Purpose

- Overtemperature warning and lockout paths.
  Triggering them intentionally would require overheating the plug.
- Shelly-loss warning and critical lockout paths.
  Intentionally losing contact with the plug would remove active protection.
- Weather-adaptive rest periods.
  The AI analyzer was disabled during the session, so default timings were used.
- `TIER_3` repeated cycle alerting.
  Reaching `TIER_3` live would require a much longer stuck-float run than was safe to
  manufacture under low-water conditions.
- Shelly reboot-detection alerting.
  Rebooting the live plug on purpose was not justified during an active stuck-float
  incident.

## Recommended Next Live Checks

- During the next higher-water event, verify either dry-pulse exit or genuine tier
  escalation under normal unattended control rather than state seeding.
- When the sump is not under risk, test one controlled Shelly reboot and verify the
  reboot alert plus post-restart state recovery.
