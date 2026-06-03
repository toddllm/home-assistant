# Sump monitoring moved back to toddllm (primary), Mac → cold standby

Date: 2026-06-02

## Why

`toddllm` (the always-on Linux box) came back after being down since early May.
The Mac had been carrying sump monitoring as a hardened fallback since 2026-05-12
(see 2026-05-12-mac-fallback-hardening.md). Per operator decision, control moves
back to toddllm as the **sole active controller**, the Mac becomes a **cold
standby** for manual fail-back, and a **daily digest** replaces any continuous
polling of the standby.

## What was on toddllm when it came back (landmines found + fixed)

- systemd `sump-pump-monitor.service` auto-started **month-old code** (`bab8559`)
  with `SHELLY_IP=192.168.68.151` (stale; device had DHCP-drifted to `.134`) and
  no MAC-discovery — i.e. blind. Stopped, then redeployed with current code.
- A **cron watchdog** `*/5 * * * * watchdog_monitor.sh` was independently
  respawning the monitor (it had already launched two duplicate copies). It does
  not coordinate with systemd, which already has `Restart=always`. **Disabled**
  (commented in tdeshane's crontab, backed up to `~/crontab.bak.PRE-CUTOVER.*`).
  systemd `Restart=always` is now the sole restart mechanism.
- `sump-pump-dashboard.service` was crash-looping (`status=203/EXEC`). Stopped +
  disabled (not part of this migration; revisit separately).

## Cutover sequence (no two active controllers, no uncontrolled gap)

1. Stop toddllm's stale monitor + the cron watchdog + kill stray copies. Mac
   stayed sole controller throughout.
2. Sync current code to toddllm over the LAN (`git reset --hard origin/main` to a
   clean base, then scp the working-tree files — GitHub push to `main` was
   blocked by policy; deploy did not need it). Align `.env` Shelly section to the
   known-good Gen2 config (`.134`, blank auth).
3. Read-only verify: `import sump_pump_monitor` (validates env + deps), resolve
   Shelly by MAC, read live status.
4. Start under systemd (single supervised instance), fresh NORMAL state.
5. `launchctl bootout` all 5 Mac jobs (relay-safe: monitor's SIGTERM handler
   "leaves plug in current state"). Mac → cold standby (plists remain on disk).

The monitor's own `shutdown()` never touches the relay, so stop/start never
drops pumping.

## toddllm services now (systemd, not launchd/cron)

| Unit | Cadence | Role |
|------|---------|------|
| `sump-pump-monitor.service` | always (`Restart=always`) | control: float + tiered safety |
| `sump-pump-guardian.timer` | 120s | independent hard safety: 70C force-OFF, 60C+running cooling rest, >4min run rest, no-run/unreachable/IP-drift alerts |
| `sump-smart-assess.timer` | 15min | first-principles classifier + de-duped WARN/CRIT alerts |
| `sump-daily-status.timer` | daily 08:00 | one digest: pump + primary-service health + best-effort Mac-standby reachability (informational, never alarms on Mac offline) |
| `sump-claude-check.timer` | 00/06/12/18:05 | **installed but DISABLED** — needs `claude` CLI + Anthropic auth on toddllm |

Unit files are checked into `deploy/`. Install pattern:
`sudo install -m 644 deploy/<unit> /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now <timer>`.

Portability fixes made so the same scripts run on Linux: `sump_pump_guardian.py`
`ROOT` now derives from `__file__`; `sump_claude_check.sh` resolves its own dir
and uses `$HOME`. `sump_assess.py`, `shelly_discovery.py`, `weather_check.py`
were already portable.

## Known follow-ups

- **claude-check auth**: install `claude` (no node/npm on toddllm; stale
  `~/.claude/.credentials.json` from Jan 24) and authenticate, then
  `systemctl enable --now sump-claude-check.timer`.
- **smart-assess run-recency is brittle**: it parses the monitor *log* for
  `cycle N: pump ran (...)` lines (TIER-only). On a fresh start / NORMAL float
  control it can read a stale "last run" (caused a one-off false `FLOOD_RISK` at
  cutover; self-heals once TIER_3 logs a cycle). Fix: read `last_pump_run_wall`
  from `/var/tmp/sump_pump_state.json` (as the guardian already does).
- **Device-local failsafe** (Shelly on-device heartbeat script) remains the right
  answer for "primary host dies" — still unbuilt (see 2026-05-12 doc §1a).
- DHCP reservation for the Shelly (MAC `841FE8F85BBC`) to stop IP drift.
- `sump-pump-dashboard.service` is disabled/broken — fix or remove.

## Fail back to the Mac (manual)

The Mac jobs are persistently **disabled** (`launchctl disable`), so they will NOT
auto-load on reboot/login. To fail back:

1. Stand toddllm down first (so only one controller):
   `ssh toddllm 'sudo systemctl stop sump-pump-monitor.service sump-pump-guardian.timer sump-smart-assess.timer'`
2. On the Mac, re-enable + load each service:
   `launchctl enable gui/$(id -u)/com.sump.pump-monitor`
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sump.pump-monitor.plist`
   (repeat for `com.sump.pump-guardian`, `com.sump.pump-watchdog`, `com.sump.smart-assess`, `com.sump.claude-check`).

To re-disable the Mac after failing back to toddllm: `launchctl bootout` then
`launchctl disable` each `gui/$(id -u)/com.sump.*`.
