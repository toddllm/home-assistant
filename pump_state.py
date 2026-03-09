"""
Pump State Machine - Tiered Escalation for Stuck Float

States:
  NORMAL       - Plug ON, float controls pump. Monitor watches.
  POWER_CYCLE  - Attempting power cycle to unstick float.
  TIER_1       - 60s ON / 15 min OFF. Gentle duty cycle, auto-exits on 3 dry pulses.
  TIER_2       - 90s ON / 10 min OFF. Moderate duty cycle.
  TIER_3       - 120s ON / 10 min OFF. Max duty, alerts every cycle.
  COOLDOWN     - Float may have unstuck. Confirm stable idle before NORMAL.
  LOCKOUT      - Overtemp or prolonged Shelly loss. Pump OFF, manual intervention needed.

Transitions:
  NORMAL → stuck 3 min → POWER_CYCLE
  POWER_CYCLE → success → NORMAL
  POWER_CYCLE → fail → TIER_1
  TIER_1 → 3 cycles still stuck → TIER_2
  TIER_2 → 6 cycles still stuck → TIER_3
  Any tier → pump stops → COOLDOWN
  Any tier → 3 consecutive dry pulses → NORMAL
  COOLDOWN → stable idle 90s → NORMAL
  COOLDOWN → pump restarts → resume previous tier
  Any state → overtemp → LOCKOUT
  Any state → Shelly lost 30 min → LOCKOUT
  LOCKOUT → manual restart only
"""

import json
import os
import time
from pathlib import Path

# Use /var/tmp for persistence across host reboots (not cleared on reboot like /tmp)
STATE_FILE = Path(os.environ.get("STATE_FILE", "/var/tmp/sump_pump_state.json"))
PID_FILE = Path(os.environ.get("PID_FILE", "/var/tmp/sump_pump_monitor.pid"))

# States
NORMAL = "NORMAL"
POWER_CYCLE = "POWER_CYCLE"
TIER_1 = "TIER_1"
TIER_2 = "TIER_2"
TIER_3 = "TIER_3"
COOLDOWN = "COOLDOWN"
LOCKOUT = "LOCKOUT"

# Duty cycle phases within a tier
PHASE_ON = "ON"
PHASE_OFF = "OFF"

# Tier configuration can be tuned via env vars. TIER_1 also falls back to the
# legacy SAFE_* names so existing deployments do not silently change behavior.
def _int_env(name, default):
    value = os.environ.get(name)
    if value is None:
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def _tier_env(name, default, legacy_name=None):
    if os.environ.get(name) is not None:
        return _int_env(name, default)
    if legacy_name and os.environ.get(legacy_name) is not None:
        return _int_env(legacy_name, default)
    return int(default)


# Tier configuration: run_s, rest_s, escalate_after cycles, next tier
TIER_CONFIG = {
    TIER_1: {
        "run_s": _tier_env("TIER_1_RUN_SECONDS", 60, legacy_name="SAFE_RUN_SECONDS"),
        "rest_s": _tier_env("TIER_1_REST_SECONDS", 900, legacy_name="SAFE_REST_SECONDS"),
        "escalate_after": _int_env("TIER_1_ESCALATE_AFTER", 3),
        "next": TIER_2,
    },
    TIER_2: {
        "run_s": _tier_env("TIER_2_RUN_SECONDS", 90),
        "rest_s": _tier_env("TIER_2_REST_SECONDS", 600),
        "escalate_after": _int_env("TIER_2_ESCALATE_AFTER", 6),
        "next": TIER_3,
    },
    TIER_3: {
        "run_s": _tier_env("TIER_3_RUN_SECONDS", 120),
        "rest_s": _tier_env("TIER_3_REST_SECONDS", 600),
        "escalate_after": None,
        "next": None,
    },
}

# How many consecutive dry pulses before returning to NORMAL
DRY_EXIT_THRESHOLD = 3

# Cooldown: seconds of stable idle required before returning to NORMAL
COOLDOWN_STABLE_SECONDS = 90

# Shelly unreachable thresholds (seconds)
SHELLY_WARN_SECONDS = 300       # 5 minutes
SHELLY_CRITICAL_SECONDS = 1800  # 30 minutes


class PumpStateMachine:
    def __init__(self, log_fn):
        self.log = log_fn

        # Current state
        self.state = NORMAL
        self.state_entered_at = time.monotonic()
        self.state_entered_wall = time.time()

        # Duty cycle tracking
        self.duty_phase = PHASE_OFF
        self.phase_started_at = time.monotonic()
        self.cycle_count = 0
        self.consecutive_dry = 0

        # Cooldown tracking
        self.cooldown_started_at = 0.0
        self.pre_cooldown_state = None

        # Lockout tracking
        self.lockout_reason = ""

        # Pump run tracking
        self.running_since = None       # monotonic timestamp, None if idle
        self.last_pump_run = time.monotonic()
        self.no_run_alerted = False
        self.output_recovery_until = 0.0

        # Shelly connectivity
        self.last_shelly_contact = time.monotonic()
        self.shelly_warn_alerted = False
        self.shelly_critical_alerted = False

        # Reboot detection
        self.last_uptime = None

        # Alert dedup
        self.last_illumination = None
        self.voltage_alerted = False
        self.rssi_alerted = False
        self.power_anomaly_alerted = False
        self.temp_warned = False

        # Weather risk (from AI analyzer)
        self.cached_weather_risk = 0.3
        self.last_weather_check = 0.0

    def _wall_from_monotonic(self, monotonic_ts):
        """Convert a monotonic timestamp to a best-effort wall clock."""
        if monotonic_ts is None:
            return None
        return time.time() - (time.monotonic() - monotonic_ts)

    def _monotonic_from_wall(self, wall_ts, fallback=None):
        """Reconstruct a monotonic timestamp from a saved wall clock."""
        if wall_ts in (None, 0):
            return fallback
        age = max(0.0, time.time() - wall_ts)
        return time.monotonic() - age

    def transition(self, new_state, reason=""):
        """Transition to a new state. Saves state to disk."""
        old = self.state
        self.state = new_state
        self.state_entered_at = time.monotonic()
        self.state_entered_wall = time.time()

        if new_state in TIER_CONFIG:
            self.cycle_count = 0
            self.consecutive_dry = 0
            self.duty_phase = PHASE_OFF
            self.phase_started_at = time.monotonic()

        if new_state == COOLDOWN:
            self.cooldown_started_at = time.monotonic()

        if new_state == LOCKOUT:
            self.lockout_reason = reason

        desc = f" ({reason})" if reason else ""
        self.log(f"STATE: {old} -> {new_state}{desc}")
        self.save_state()

    def time_in_state(self):
        return time.monotonic() - self.state_entered_at

    def time_in_phase(self):
        return time.monotonic() - self.phase_started_at

    def is_tier(self):
        return self.state in TIER_CONFIG

    def tier_config(self):
        return TIER_CONFIG.get(self.state)

    def weather_multiplier(self):
        """Adjust rest period based on weather risk. <1 = shorter rest = more pumping."""
        risk = self.cached_weather_risk
        if risk >= 0.7:
            return 0.5
        elif risk >= 0.5:
            return 0.7
        elif risk < 0.2:
            return 1.5
        return 1.0

    def adjusted_rest(self):
        """Get weather-adjusted rest period for current tier."""
        config = self.tier_config()
        if not config:
            return 0
        return config["rest_s"] * self.weather_multiplier()

    def save_state(self):
        """Persist state to disk for crash recovery. Atomic write."""
        data = {
            "state": self.state,
            "state_entered_wall": self.state_entered_wall,
            "phase_started_wall": self._wall_from_monotonic(self.phase_started_at),
            "cycle_count": self.cycle_count,
            "consecutive_dry": self.consecutive_dry,
            "duty_phase": self.duty_phase,
            "running_since_wall": self._wall_from_monotonic(self.running_since),
            "last_pump_run_wall": self._wall_from_monotonic(self.last_pump_run),
            "lockout_reason": self.lockout_reason,
            "pre_cooldown_state": self.pre_cooldown_state,
            "saved_at": time.time(),
            "pid": os.getpid(),
        }
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(STATE_FILE)
        except Exception as e:
            self.log(f"WARNING: Could not save state: {e}")

    def load_state(self):
        """Resume from saved state after restart. Returns True if state was loaded."""
        if not STATE_FILE.exists():
            return False
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            self.log(f"WARNING: Could not load state file: {e}")
            return False

        age = time.time() - data.get("saved_at", 0)
        saved_state = data.get("state", NORMAL)

        # Stale state (> 2 hours) — start fresh
        if age > 7200:
            self.log(f"State file is {age / 60:.0f} min old, starting fresh")
            return False

        self.state = saved_state
        self.state_entered_wall = data.get("state_entered_wall", time.time() - age)
        self.state_entered_at = self._monotonic_from_wall(
            self.state_entered_wall,
            fallback=time.monotonic() - age,
        )
        self.phase_started_at = self._monotonic_from_wall(
            data.get("phase_started_wall"),
            fallback=self.state_entered_at,
        )
        self.cycle_count = data.get("cycle_count", 0)
        self.consecutive_dry = data.get("consecutive_dry", 0)
        self.duty_phase = data.get("duty_phase", PHASE_OFF)
        self.lockout_reason = data.get("lockout_reason", "")
        self.pre_cooldown_state = data.get("pre_cooldown_state")
        self.running_since = self._monotonic_from_wall(data.get("running_since_wall"))
        self.cooldown_started_at = self.state_entered_at if self.state == COOLDOWN else 0.0

        # Restore last_pump_run from wall clock
        last_run_wall = data.get("last_pump_run_wall", 0)
        self.last_pump_run = self._monotonic_from_wall(last_run_wall, fallback=time.monotonic())

        details = [f"saved {age:.0f}s ago"]
        if self.is_tier() or self.state == POWER_CYCLE:
            details.append(f"phase {self.duty_phase}")
            details.append(f"cycle {self.cycle_count}")
        if self.running_since is not None:
            details.append(
                f"pump running {(time.monotonic() - self.running_since):.0f}s at save/load boundary"
            )
        self.log(f"Resumed from saved state: {saved_state} ({', '.join(details)})")

        return True

    def format_status(self, status=None):
        """Format current state for logging/notifications."""
        parts = [f"State: {self.state}"]
        if self.is_tier():
            config = self.tier_config()
            parts.append(f"Phase: {self.duty_phase}")
            parts.append(f"Cycle: {self.cycle_count}")
            parts.append(f"Dry streak: {self.consecutive_dry}/{DRY_EXIT_THRESHOLD}")
            parts.append(f"Timing: {config['run_s']}s ON / {self.adjusted_rest():.0f}s OFF")
        if self.state == LOCKOUT:
            parts.append(f"Reason: {self.lockout_reason}")
        elapsed = self.time_in_state()
        if elapsed > 60:
            parts.append(f"In state: {elapsed / 60:.0f} min")
        if status:
            parts.append(f"Power: {status.get('power', 0):.1f}W")
            parts.append(f"Temp: {status.get('temp_c', 0):.1f}C")
        return " | ".join(parts)


def check_single_instance(log_fn):
    """Prevent duplicate monitor processes."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # check if running
            log_fn(f"ERROR: Another instance (PID {old_pid}) is already running. Exiting.")
            return False
        except (ProcessLookupError, ValueError):
            pass  # stale PID file, OK to proceed
        except PermissionError:
            log_fn(f"WARNING: Cannot check PID {PID_FILE.read_text().strip()}, proceeding anyway")
    try:
        PID_FILE.write_text(str(os.getpid()))
    except OSError as e:
        log_fn(f"WARNING: Could not write PID file: {e}")
    return True


def cleanup_pid():
    """Remove PID file on clean shutdown."""
    try:
        if PID_FILE.exists() and int(PID_FILE.read_text().strip()) == os.getpid():
            PID_FILE.unlink()
    except (OSError, ValueError):
        pass


def validate_config(log_fn, send_notification_fn, config):
    """Sanity-check configuration at startup."""
    warnings = []

    poll = config.get("POLL_INTERVAL_SECONDS", 30)
    if poll > 60:
        warnings.append(f"POLL_INTERVAL_SECONDS={poll} is > 60s; slow stuck-float detection")

    active_poll = config.get("ACTIVE_POLL_SECONDS", 5)
    if active_poll > 10:
        warnings.append(
            f"ACTIVE_POLL_SECONDS={active_poll} is > 10s; active phase timing will be coarse"
        )

    max_run = config.get("MAX_RUN_MINUTES", 3)
    if max_run < 1:
        warnings.append(f"MAX_RUN_MINUTES={max_run} is < 1 min; may false-trigger on normal runs")
    if max_run > 10:
        warnings.append(f"MAX_RUN_MINUTES={max_run} is > 10 min; slow stuck-float detection")

    power_cycle_off = config.get("POWER_CYCLE_OFF_SECONDS", 10)
    power_cycle_settle = config.get("POWER_CYCLE_SETTLE_SECONDS", 60)
    if power_cycle_off < 3:
        warnings.append(
            f"POWER_CYCLE_OFF_SECONDS={power_cycle_off} is very short; relay may not fully reset"
        )
    if power_cycle_settle < 5:
        warnings.append(
            f"POWER_CYCLE_SETTLE_SECONDS={power_cycle_settle} is very short; stuck-float check may be noisy"
        )

    for tier_name in (TIER_1, TIER_2, TIER_3):
        tier_run = config.get(f"{tier_name}_RUN_SECONDS")
        tier_rest = config.get(f"{tier_name}_REST_SECONDS")
        if tier_run is None or tier_rest is None:
            continue
        if tier_run < 10:
            warnings.append(f"{tier_name}_RUN_SECONDS={tier_run} is very short (< 10s)")
        if tier_rest > 7200:
            warnings.append(f"{tier_name}_REST_SECONDS={tier_rest} is > 2h; pump may not run enough")
        if tier_run > 0 and tier_rest / tier_run > 60:
            warnings.append(
                f"{tier_name} ratio is {tier_rest / tier_run:.0f}:1 (rest:run) — duty cycle may be too weak"
            )

    if warnings:
        for w in warnings:
            log_fn(f"CONFIG WARNING: {w}")
        send_notification_fn(
            "SUMP PUMP: Configuration warnings at startup",
            "The following configuration issues were detected:\n\n"
            + "\n".join(f"- {w}" for w in warnings),
        )

    return warnings
