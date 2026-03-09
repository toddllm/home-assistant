import unittest
from unittest.mock import MagicMock

import pump_state


class PumpStateConfigTests(unittest.TestCase):
    def test_validate_config_warns_and_notifies_for_risky_values(self):
        log = MagicMock()
        notify = MagicMock()

        warnings = pump_state.validate_config(
            log,
            notify,
            {
                "POLL_INTERVAL_SECONDS": 90,
                "ACTIVE_POLL_SECONDS": 15,
                "MAX_RUN_MINUTES": 0.5,
                "POWER_CYCLE_OFF_SECONDS": 1,
                "POWER_CYCLE_SETTLE_SECONDS": 2,
                "TIER_1_RUN_SECONDS": 5,
                "TIER_1_REST_SECONDS": 500,
                "TIER_2_RUN_SECONDS": 90,
                "TIER_2_REST_SECONDS": 8000,
            },
        )

        self.assertGreaterEqual(len(warnings), 6)
        self.assertTrue(any("POLL_INTERVAL_SECONDS=90" in warning for warning in warnings))
        self.assertTrue(any("ACTIVE_POLL_SECONDS=15" in warning for warning in warnings))
        self.assertTrue(any("POWER_CYCLE_OFF_SECONDS=1" in warning for warning in warnings))
        notify.assert_called_once()
        self.assertGreater(log.call_count, 0)

    def test_validate_config_is_quiet_for_reasonable_values(self):
        log = MagicMock()
        notify = MagicMock()

        warnings = pump_state.validate_config(
            log,
            notify,
            {
                "POLL_INTERVAL_SECONDS": 30,
                "ACTIVE_POLL_SECONDS": 5,
                "MAX_RUN_MINUTES": 3,
                "POWER_CYCLE_OFF_SECONDS": 10,
                "POWER_CYCLE_SETTLE_SECONDS": 60,
                "TIER_1_RUN_SECONDS": 60,
                "TIER_1_REST_SECONDS": 900,
                "TIER_2_RUN_SECONDS": 90,
                "TIER_2_REST_SECONDS": 600,
                "TIER_3_RUN_SECONDS": 120,
                "TIER_3_REST_SECONDS": 600,
            },
        )

        self.assertEqual(warnings, [])
        log.assert_not_called()
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
