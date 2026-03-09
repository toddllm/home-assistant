import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pump_state
import sump_pump_monitor as monitor


class PumpStateResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmpdir.name) / "state.json"
        self.pid_file = Path(self.tmpdir.name) / "pid.txt"
        self.orig_state_file = pump_state.STATE_FILE
        self.orig_pid_file = pump_state.PID_FILE
        pump_state.STATE_FILE = self.state_file
        pump_state.PID_FILE = self.pid_file

    def tearDown(self):
        pump_state.STATE_FILE = self.orig_state_file
        pump_state.PID_FILE = self.orig_pid_file
        self.tmpdir.cleanup()

    def make_sm(self):
        return pump_state.PumpStateMachine(lambda _msg: None)

    def test_running_since_persists_across_restart(self):
        sm = self.make_sm()
        sm.running_since = time.monotonic() - 125
        sm.last_pump_run = time.monotonic() - 10
        sm.save_state()

        restored = self.make_sm()
        self.assertTrue(restored.load_state())
        self.assertIsNotNone(restored.running_since)
        self.assertAlmostEqual(time.monotonic() - restored.running_since, 125, delta=2.0)

    def test_phase_timing_persists_for_tier_states(self):
        sm = self.make_sm()
        sm.transition(pump_state.TIER_1, "test")
        sm.cycle_count = 2
        sm.duty_phase = pump_state.PHASE_OFF
        sm.phase_started_at = time.monotonic() - 42
        sm.save_state()

        restored = self.make_sm()
        self.assertTrue(restored.load_state())
        self.assertEqual(restored.state, pump_state.TIER_1)
        self.assertEqual(restored.duty_phase, pump_state.PHASE_OFF)
        self.assertEqual(restored.cycle_count, 2)
        self.assertAlmostEqual(restored.time_in_phase(), 42, delta=2.0)

    def test_state_duration_persists_from_state_entered_wall(self):
        sm = self.make_sm()
        sm.transition(pump_state.COOLDOWN, "test")
        sm.state_entered_at = time.monotonic() - 75
        sm.state_entered_wall = time.time() - 75
        sm.cooldown_started_at = sm.state_entered_at
        sm.save_state()

        restored = self.make_sm()
        self.assertTrue(restored.load_state())
        self.assertEqual(restored.state, pump_state.COOLDOWN)
        self.assertAlmostEqual(restored.time_in_state(), 75, delta=2.0)
        self.assertAlmostEqual(time.monotonic() - restored.cooldown_started_at, 75, delta=2.0)

    def test_output_recovery_deadline_persists_across_restart(self):
        sm = self.make_sm()
        sm.output_recovery_until = time.monotonic() + 8
        sm.save_state()

        restored = self.make_sm()
        self.assertTrue(restored.load_state())
        self.assertGreater(restored.output_recovery_until, time.monotonic())
        self.assertAlmostEqual(restored.output_recovery_until - time.monotonic(), 8, delta=2.0)

    def test_last_uptime_persists_across_restart(self):
        sm = self.make_sm()
        sm.last_uptime = 12345
        sm.save_state()

        restored = self.make_sm()
        self.assertTrue(restored.load_state())
        self.assertEqual(restored.last_uptime, 12345)

    def test_startup_reconcile_keeps_existing_running_timer(self):
        sm = self.make_sm()
        sm.running_since = time.monotonic() - 150
        status = {"output": True, "power": 520.0, "temp_c": 33.0}

        with patch.object(monitor, "log"), patch.object(monitor, "turn_on") as turn_on:
            monitor.reconcile_startup_state(sm, status)

        turn_on.assert_not_called()
        self.assertAlmostEqual(time.monotonic() - sm.running_since, 150, delta=2.0)

    def test_startup_reconcile_preserves_timer_when_restoring_output(self):
        sm = self.make_sm()
        sm.running_since = time.monotonic() - 150
        status = {"output": False, "power": 0.0, "temp_c": 33.0}

        with patch.object(monitor, "log"), patch.object(monitor, "turn_on") as turn_on:
            monitor.reconcile_startup_state(sm, status)

        turn_on.assert_called_once()
        self.assertAlmostEqual(time.monotonic() - sm.running_since, 150, delta=2.0)

    def test_startup_reconcile_preserves_existing_output_recovery_deadline(self):
        sm = self.make_sm()
        sm.running_since = time.monotonic() - 150
        original_deadline = time.monotonic() + 3
        sm.output_recovery_until = original_deadline
        status = {"output": True, "power": 0.0, "temp_c": 33.0}

        with patch.object(monitor, "log"):
            monitor.reconcile_startup_state(sm, status)

        self.assertAlmostEqual(sm.output_recovery_until, original_deadline, delta=0.5)

    def test_startup_reconcile_keeps_plug_off_for_off_phase(self):
        sm = self.make_sm()
        sm.transition(pump_state.TIER_1, "test")
        sm.duty_phase = pump_state.PHASE_OFF
        status = {"output": False, "power": 0.0, "temp_c": 33.0}

        with patch.object(monitor, "log"), patch.object(monitor, "turn_on") as turn_on:
            monitor.reconcile_startup_state(sm, status)

        turn_on.assert_not_called()

    def test_startup_reconcile_power_cycle_off_turns_output_off(self):
        sm = self.make_sm()
        sm.transition(pump_state.POWER_CYCLE, "test")
        sm.duty_phase = pump_state.PHASE_OFF
        status = {"output": True, "power": 0.0, "temp_c": 33.0}

        with patch.object(monitor, "log"), patch.object(monitor, "turn_off") as turn_off:
            monitor.reconcile_startup_state(sm, status)

        turn_off.assert_called_once()

    def test_startup_reconcile_power_cycle_on_turns_output_on(self):
        sm = self.make_sm()
        sm.transition(pump_state.POWER_CYCLE, "test")
        sm.duty_phase = pump_state.PHASE_ON
        status = {"output": False, "power": 0.0, "temp_c": 33.0}

        with patch.object(monitor, "log"), patch.object(monitor, "turn_on") as turn_on:
            monitor.reconcile_startup_state(sm, status)

        turn_on.assert_called_once()

    def test_power_cycle_resumes_non_blocking(self):
        sm = self.make_sm()
        sm.transition(pump_state.POWER_CYCLE, "test")
        sm.duty_phase = pump_state.PHASE_OFF
        sm.phase_started_at = time.monotonic() - (monitor.POWER_CYCLE_OFF_SECONDS + 1)

        with patch.object(monitor, "log"), patch.object(monitor, "turn_on") as turn_on:
            monitor.handle_power_cycle(sm, {"output": False, "power": 0.0, "temp_c": 33.0})

        turn_on.assert_called_once()
        self.assertEqual(sm.state, pump_state.POWER_CYCLE)
        self.assertEqual(sm.duty_phase, pump_state.PHASE_ON)

    def test_power_cycle_off_phase_reasserts_output_off(self):
        sm = self.make_sm()
        sm.transition(pump_state.POWER_CYCLE, "test")
        sm.duty_phase = pump_state.PHASE_OFF
        original_phase_started = time.monotonic() - 30
        sm.phase_started_at = original_phase_started

        with patch.object(monitor, "log"), patch.object(monitor, "turn_off") as turn_off:
            monitor.handle_power_cycle(sm, {"output": True, "power": 450.0, "temp_c": 33.0})

        turn_off.assert_called_once()
        self.assertGreater(sm.phase_started_at, original_phase_started)

    def test_power_cycle_success_returns_to_normal(self):
        sm = self.make_sm()
        sm.transition(pump_state.POWER_CYCLE, "test")
        sm.duty_phase = pump_state.PHASE_ON
        sm.phase_started_at = time.monotonic() - (monitor.POWER_CYCLE_SETTLE_SECONDS + 1)

        with (
            patch.object(monitor, "log"),
            patch.object(monitor, "send_notification"),
        ):
            monitor.handle_power_cycle(sm, {"output": True, "power": 0.0, "temp_c": 33.0})

        self.assertEqual(sm.state, pump_state.NORMAL)

    def test_handle_normal_honors_output_recovery_grace(self):
        sm = self.make_sm()
        sm.running_since = time.monotonic() - 180
        sm.output_recovery_until = time.monotonic() + 5
        status = {"output": True, "power": 0.0, "temp_c": 33.0, "illumination": "dark"}

        with patch.object(monitor, "log"), patch.object(monitor, "send_notification"):
            monitor.handle_normal(sm, status)

        self.assertIsNotNone(sm.running_since)

    def test_handle_normal_sets_output_recovery_grace_when_restoring_output(self):
        sm = self.make_sm()
        sm.running_since = time.monotonic() - 45
        status = {"output": False, "power": 0.0, "temp_c": 33.0, "illumination": "dark"}

        with (
            patch.object(monitor, "log"),
            patch.object(monitor, "send_notification"),
            patch.object(monitor, "turn_on") as turn_on,
        ):
            monitor.handle_normal(sm, status)

        turn_on.assert_called_once()
        self.assertGreater(sm.output_recovery_until, time.monotonic())

    def test_next_sleep_seconds_uses_active_poll_during_on_phase(self):
        sm = self.make_sm()
        sm.transition(pump_state.TIER_1, "test")
        sm.duty_phase = pump_state.PHASE_ON
        sm.phase_started_at = time.monotonic() - 4

        with patch.object(monitor, "ACTIVE_POLL_SECONDS", 5):
            sleep_s = monitor.next_sleep_seconds(sm)

        self.assertLessEqual(sleep_s, 5.0)
        self.assertGreaterEqual(sleep_s, 1.0)

    def test_next_sleep_seconds_uses_active_poll_during_normal_run(self):
        sm = self.make_sm()
        sm.running_since = time.monotonic() - 12

        with patch.object(monitor, "ACTIVE_POLL_SECONDS", 5):
            sleep_s = monitor.next_sleep_seconds(sm)

        self.assertLessEqual(sleep_s, 5.0)
        self.assertGreaterEqual(sleep_s, 1.0)

    def test_duty_cycle_off_phase_reasserts_output_off(self):
        sm = self.make_sm()
        sm.transition(pump_state.TIER_1, "test")
        sm.duty_phase = pump_state.PHASE_OFF
        original_phase_started = time.monotonic() - 30
        sm.phase_started_at = original_phase_started

        with patch.object(monitor, "log"), patch.object(monitor, "turn_off") as turn_off:
            monitor.handle_duty_cycle(sm, {"output": True, "power": 480.0, "temp_c": 33.0})

        turn_off.assert_called_once()
        self.assertGreater(sm.phase_started_at, original_phase_started)
        self.assertEqual(sm.cycle_count, 0)

    def test_duty_cycle_on_phase_reasserts_output_on(self):
        sm = self.make_sm()
        sm.transition(pump_state.TIER_1, "test")
        sm.duty_phase = pump_state.PHASE_ON
        sm.cycle_count = 1
        original_phase_started = time.monotonic() - 10
        sm.phase_started_at = original_phase_started

        with patch.object(monitor, "log"), patch.object(monitor, "turn_on") as turn_on:
            monitor.handle_duty_cycle(sm, {"output": False, "power": 0.0, "temp_c": 33.0})

        turn_on.assert_called_once()
        self.assertGreater(sm.phase_started_at, original_phase_started)
        self.assertEqual(sm.cycle_count, 1)

    def test_duty_cycle_dry_exit_returns_to_normal(self):
        sm = self.make_sm()
        sm.transition(pump_state.TIER_1, "test")
        sm.duty_phase = pump_state.PHASE_ON
        sm.phase_started_at = time.monotonic() - (pump_state.TIER_CONFIG[pump_state.TIER_1]["run_s"] + 1)
        sm.cycle_count = 3
        sm.consecutive_dry = 2

        with (
            patch.object(monitor, "log"),
            patch.object(monitor, "turn_on") as turn_on,
            patch.object(monitor, "send_notification"),
        ):
            monitor.handle_duty_cycle(sm, {"output": True, "power": 0.0, "temp_c": 33.0})

        turn_on.assert_called_once()
        self.assertEqual(sm.state, pump_state.NORMAL)

    def test_duty_cycle_escalates_to_next_tier(self):
        sm = self.make_sm()
        sm.transition(pump_state.TIER_1, "test")
        sm.duty_phase = pump_state.PHASE_ON
        sm.phase_started_at = time.monotonic() - (pump_state.TIER_CONFIG[pump_state.TIER_1]["run_s"] + 1)
        sm.cycle_count = pump_state.TIER_CONFIG[pump_state.TIER_1]["escalate_after"]

        with (
            patch.object(monitor, "log"),
            patch.object(monitor, "turn_off") as turn_off,
            patch.object(monitor, "send_notification"),
        ):
            monitor.handle_duty_cycle(sm, {"output": True, "power": 500.0, "temp_c": 33.0})

        self.assertGreaterEqual(turn_off.call_count, 1)
        self.assertEqual(sm.state, pump_state.TIER_2)
        self.assertEqual(sm.duty_phase, pump_state.PHASE_OFF)

    def test_cooldown_restart_reenters_tier_and_turns_output_off(self):
        sm = self.make_sm()
        sm.state = pump_state.COOLDOWN
        sm.pre_cooldown_state = pump_state.TIER_2

        with patch.object(monitor, "log"), patch.object(monitor, "turn_off") as turn_off:
            monitor.handle_cooldown(sm, {"output": True, "power": 500.0, "temp_c": 33.0})

        turn_off.assert_called_once()
        self.assertEqual(sm.state, pump_state.TIER_2)
        self.assertEqual(sm.duty_phase, pump_state.PHASE_OFF)

    def test_cooldown_completion_returns_to_normal(self):
        sm = self.make_sm()
        sm.state = pump_state.COOLDOWN
        sm.cooldown_started_at = time.monotonic() - pump_state.COOLDOWN_STABLE_SECONDS - 1

        with (
            patch.object(monitor, "log"),
            patch.object(monitor, "turn_on") as turn_on,
            patch.object(monitor, "send_notification"),
        ):
            monitor.handle_cooldown(sm, {"output": True, "power": 0.0, "temp_c": 33.0})

        turn_on.assert_called_once()
        self.assertEqual(sm.state, pump_state.NORMAL)

    def test_check_single_instance_rejects_existing_pid(self):
        self.assertTrue(pump_state.check_single_instance(lambda _msg: None))
        self.assertFalse(pump_state.check_single_instance(lambda _msg: None))

    def test_send_notification_sanitizes_ntfy_title(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["title"] = request.get_header("Title")
            return MagicMock()

        smtp_ctx = MagicMock()
        smtp_ctx.__enter__.return_value = smtp_ctx

        with (
            patch.object(monitor.smtplib, "SMTP_SSL", return_value=smtp_ctx),
            patch.object(monitor.urllib.request, "urlopen", side_effect=fake_urlopen),
            patch.object(monitor, "log"),
        ):
            monitor.send_notification("SUMP PUMP: Stuck float — entering TIER 1", "body")

        self.assertEqual(captured["title"], "SUMP PUMP: Stuck float - entering TIER 1")


if __name__ == "__main__":
    unittest.main()
