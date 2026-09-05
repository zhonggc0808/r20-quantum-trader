"""Offline isolated test for final order quote verification in submit_protected_limit_order.
Zero real CLI, zero network, zero real exchange operations.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure scripts directory is in sys.path so okx_runtime can be imported
scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

import scripts.ai_factor_trader as aft


class SubmitProtectedLimitOrderTests(unittest.TestCase):
    @patch("scripts.ai_factor_trader.run_cmd_result")
    @patch("scripts.ai_factor_trader.selected_environment")
    def test_submit_protected_limit_order_core_rejection(self, mock_env, mock_run):
        # Environment is real / not simulated to test raw effective prices
        env_obj = MagicMock()
        env_obj.simulated = False
        mock_env.return_value = env_obj

        # 1. Invalid Geometry (Long: sl > px)
        ok, reason = aft.submit_protected_limit_order("BTC-USDT-SWAP", "buy", "long", 1, 100.0, 120.0, 105.0)
        self.assertFalse(ok)
        self.assertIn("买多几何不合法", reason)
        mock_run.assert_not_called()

        # 2. Insufficient RR (Long: RR = 1.0 < 2.0)
        ok, reason = aft.submit_protected_limit_order("BTC-USDT-SWAP", "buy", "long", 1, 100.0, 110.0, 90.0)
        self.assertFalse(ok)
        self.assertIn("盈亏比不足 2.0", reason)
        mock_run.assert_not_called()

        # 3. Non-finite value (NaN / Inf)
        ok, reason = aft.submit_protected_limit_order("BTC-USDT-SWAP", "buy", "long", 1, float("nan"), 130.0, 90.0)
        self.assertFalse(ok)
        self.assertIn("有限数值", reason)
        mock_run.assert_not_called()

        # 4. Valid Quote passes core verification and proceeds to exchange command
        mock_run.return_value = {"ok": True, "data": {"ordId": "ord_mock_12345"}}
        ok, order_id = aft.submit_protected_limit_order("BTC-USDT-SWAP", "buy", "long", 1, 100.0, 125.0, 90.0)
        self.assertTrue(ok)
        self.assertEqual(order_id, "ord_mock_12345")
        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
