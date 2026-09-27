"""Offline isolated unit tests for core safety floor, interceptor pipeline and final quote verification.
Strictly local, temporary mocked directory, zero network, zero real exchange calls.
"""
from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.order_risk import (
    validate_quote_geometry_and_rr, validate_quote_geometry_and_rr_detailed,
)
import astra_backend.interceptor_manager as im
from scripts.trader.momentum_gate import evaluate_directional_momentum_gate


class CoreRiskAndInterceptorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.mock_root = Path(self.temp_dir.name)
        self.mock_plugins = self.mock_root / "plugins" / "interceptors"
        self.mock_plugins.mkdir(parents=True, exist_ok=True)
        self.mock_config = self.mock_root / "data" / "interceptor_plugins.json"

        # Patch paths in interceptor_manager
        self.patch_plugins_dir = patch.object(im, "PLUGINS_DIR", self.mock_plugins)
        self.patch_config_file = patch.object(im, "CONFIG_FILE", self.mock_config)
        self.patch_plugins_dir.start()
        self.patch_config_file.start()
        self.addCleanup(self.patch_plugins_dir.stop)
        self.addCleanup(self.patch_config_file.stop)

    def test_quote_geometry_and_rr_valid(self):
        # Long valid 2.5R
        ok, reason, rr = validate_quote_geometry_and_rr("BUY_LONG", 100.0, 125.0, 90.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertAlmostEqual(rr, 2.5)

        # Short valid 2.0R
        ok, reason, rr = validate_quote_geometry_and_rr("SELL_SHORT", 100.0, 80.0, 110.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertAlmostEqual(rr, 2.0)

    def test_quote_geometry_and_rr_invalid_geometry(self):
        # Long: sl >= entry
        ok, reason, _ = validate_quote_geometry_and_rr("BUY_LONG", 100.0, 120.0, 105.0)
        self.assertFalse(ok)
        self.assertIn("买多几何不合法", reason)

        # Short: tp >= entry
        ok, reason, _ = validate_quote_geometry_and_rr("SELL_SHORT", 100.0, 105.0, 110.0)
        self.assertFalse(ok)
        self.assertIn("卖空几何不合法", reason)

    def test_quote_geometry_and_rr_insufficient_rr(self):
        # Long: RR = (115 - 100) / (100 - 90) = 1.5 < 2.0
        ok, reason, rr = validate_quote_geometry_and_rr("BUY_LONG", 100.0, 115.0, 90.0)
        self.assertFalse(ok)
        self.assertIn("盈亏比不足 2.0", reason)
        self.assertAlmostEqual(rr, 1.5)

    def test_quote_validation_exposes_structured_failure_codes(self):
        cases = [
            (("BUY_LONG", "bad", 120.0, 90.0), "quote_parse_error"),
            (("BUY_LONG", 100.0, 120.0, 105.0), "quote_geometry_invalid"),
            (("BUY_LONG", 100.0, 115.0, 90.0), "rr_below_floor"),
        ]
        for args, expected_code in cases:
            with self.subTest(code=expected_code):
                valid, reason, _rr, code = validate_quote_geometry_and_rr_detailed(*args)
                self.assertFalse(valid)
                self.assertTrue(reason)
                self.assertEqual(code, expected_code)

    def test_quote_geometry_and_rr_non_finite_or_nan(self):
        ok, reason, _ = validate_quote_geometry_and_rr("BUY_LONG", float("nan"), 120.0, 90.0)
        self.assertFalse(ok)
        self.assertIn("有限数值", reason)

        ok, reason, _ = validate_quote_geometry_and_rr("BUY_LONG", 100.0, float("inf"), 90.0)
        self.assertFalse(ok)
        self.assertIn("有限数值", reason)

    def test_pipeline_rejects_unsupported_action_without_losing_raw_value(self):
        pkg = {"instId": "BTC-USDT-SWAP", "data_quality": "valid"}
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}
        decision = {"action": "BROKEN", "confidence": 90.0}

        action, reason, rr = im.run_interceptor_pipeline(pkg, decision, ctx)

        self.assertEqual(action, "WAIT")
        self.assertEqual(rr, 0.0)
        self.assertIn("不支持的动作", reason)
        self.assertEqual(ctx["_decision_trace"]["raw_action"], "BROKEN")
        self.assertEqual(
            ctx["_decision_trace"]["rejection_code"], "unsupported_action")
        self.assertEqual(
            ctx["_decision_trace"]["outcome_source"], "interceptor_reject")

    def test_pipeline_core_floor_active_when_all_plugins_disabled(self):
        # Set all plugins to disabled in config
        im.save_config({"pipeline_order": [], "enabled": {}})

        pkg = {"instId": "BTC-USDT-SWAP", "data_quality": "valid"}
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}

        # Low confidence (< 75)
        dec = {"action": "BUY_LONG", "confidence": 70.0, "entry_price": 100.0, "take_profit_price": 130.0, "stop_loss_price": 90.0}
        act, reason, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("置信度低于安全底线", reason)
        self.assertEqual(ctx["_decision_trace"]["outcome_source"], "interceptor_reject")
        self.assertEqual(ctx["_decision_trace"]["rejection_code"], "confidence_below_floor")

        # Insufficient RR (< 2.0)
        dec = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0, "take_profit_price": 110.0, "stop_loss_price": 90.0}
        act, reason, rr = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("盈亏比不足 2.0", reason)
        self.assertEqual(ctx["_decision_trace"]["rejection_code"], "rr_below_floor")

        # DOGE confidence floor 80
        pkg_doge = {"instId": "DOGE-USDT-SWAP", "data_quality": "valid"}
        dec_doge = {"action": "BUY_LONG", "confidence": 78.0, "entry_price": 0.10, "take_profit_price": 0.13, "stop_loss_price": 0.09}
        act, reason, _ = im.run_interceptor_pipeline(pkg_doge, dec_doge, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("80.0%", reason)

        # Valid trade passes core check even with no plugins enabled
        dec_valid = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0, "take_profit_price": 125.0, "stop_loss_price": 90.0}
        act, reason, rr = im.run_interceptor_pipeline(pkg, dec_valid, ctx)
        self.assertEqual(act, "BUY_LONG")
        self.assertEqual(reason, "")
        self.assertAlmostEqual(rr, 2.5)
        self.assertEqual(ctx["_decision_trace"]["outcome_source"], "accepted_entry")
        self.assertEqual(ctx["_decision_trace"]["rejection_code"], "")

    def test_directional_momentum_gate_rejects_all_four_countertrend_cases(self):
        im.save_config({"pipeline_order": [], "enabled": {}})
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}
        long_decision = {
            "action": "BUY_LONG", "confidence": 85.0,
            "entry_price": 100.0, "take_profit_price": 125.0,
            "stop_loss_price": 90.0,
        }
        short_decision = {
            "action": "SELL_SHORT", "confidence": 85.0,
            "entry_price": 100.0, "take_profit_price": 75.0,
            "stop_loss_price": 110.0,
        }
        cases = [
            (long_decision, {"regime": "BEAR_ACCELERATING"},
             "bear_acceleration_blocks_long"),
            (long_decision, {
                "regime": "MIXED_TRANSITION", "acceleration": -0.31,
                "probability_theory": {
                    "continuation_prob_pct": 35.0,
                    "breakdown_prob_pct": 65.0,
                },
            }, "breakdown_dominance_blocks_long"),
            (short_decision, {"regime": "BULL_ACCELERATING"},
             "bull_acceleration_blocks_short"),
            (short_decision, {
                "regime": "MIXED_TRANSITION", "acceleration": 0.05,
                "power": 0.01, "power_regime": "STEADY_FLUX",
            }, "positive_momentum_blocks_short"),
        ]

        for decision, calculus, rejection_code in cases:
            with self.subTest(rejection_code=rejection_code):
                package = {
                    "instId": "BTC-USDT-SWAP", "data_quality": "valid",
                    "calculus": calculus,
                }
                action, reason, rr = im.run_interceptor_pipeline(
                    package, decision, ctx)
                self.assertEqual(action, "WAIT")
                self.assertTrue(reason)
                self.assertGreaterEqual(rr, 2.0)
                self.assertEqual(
                    ctx["_decision_trace"]["rejection_code"], rejection_code)
                self.assertEqual(
                    ctx["_decision_trace"]["rejection_evidence"]["action"],
                    decision["action"],
                )


    def test_pipeline_fail_closed_when_plugin_missing_file_or_entry(self):
        # Configure an enabled plugin that does not exist on disk
        im.save_config({
            "pipeline_order": ["missing_filter.py", "bad_syntax.py"],
            "enabled": {"missing_filter.py": True, "bad_syntax.py": False}
        })

        pkg = {"instId": "BTC-USDT-SWAP", "data_quality": "valid"}
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}
        dec = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0, "take_profit_price": 125.0, "stop_loss_price": 90.0}

        act, reason, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("文件缺失", reason)

        # Now create file but omit check_risk function
        no_entry = self.mock_plugins / "no_entry.py"
        no_entry.write_text("def other_function(): pass\n", encoding="utf-8")
        im.save_config({
            "pipeline_order": ["no_entry.py"],
            "enabled": {"no_entry.py": True}
        })
        act, reason, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("缺少 check_risk 入口", reason)

    def test_pipeline_plugin_input_mutation_isolation(self):
        # Plugin attempts to mutate decision object
        mutating_plugin = self.mock_plugins / "mutator.py"
        mutating_plugin.write_text(
            "def check_risk(package, decision, context):\n"
            "    decision['confidence'] = 999.0\n"
            "    decision['entry_price'] = 0.0\n"
            "    return True, ''\n",
            encoding="utf-8"
        )
        im.save_config({
            "pipeline_order": ["mutator.py"],
            "enabled": {"mutator.py": True}
        })

        pkg = {"instId": "BTC-USDT-SWAP", "data_quality": "valid"}
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}
        dec = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0, "take_profit_price": 125.0, "stop_loss_price": 90.0}
        dec_copy = copy.deepcopy(dec)

        act, _, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "BUY_LONG")
        # Ensure dec was not mutated by the plugin
        self.assertEqual(dec, dec_copy)


class DirectionalMomentumGateTests(unittest.TestCase):
    def test_missing_or_neutral_calculus_does_not_create_a_new_data_gate(self):
        for package in ({}, {"calculus": {}}, {
            "calculus": {
                "regime": "RANGE_LOW_VELOCITY", "acceleration": 0.0,
                "power": 0.0,
                "probability_theory": {
                    "continuation_prob_pct": 50.0,
                    "breakdown_prob_pct": 50.0,
                },
            },
        }):
            for action in ("BUY_LONG", "SELL_SHORT", "WAIT"):
                with self.subTest(package=package, action=action):
                    passed, code, reason, _evidence = (
                        evaluate_directional_momentum_gate(package, action)
                    )
                    self.assertTrue(passed)
                    self.assertEqual((code, reason), ("", ""))

    def test_probability_boundary_is_inclusive_but_acceleration_is_strict(self):
        package = {"calculus": {
            "regime": "MIXED_TRANSITION", "acceleration": -0.30,
            "probability_theory": {
                "continuation_prob_pct": 35.0,
                "breakdown_prob_pct": 65.0,
            },
        }}
        self.assertTrue(evaluate_directional_momentum_gate(
            package, "BUY_LONG")[0])
        package["calculus"]["acceleration"] = -0.3001
        passed, code, _reason, _evidence = evaluate_directional_momentum_gate(
            package, "BUY_LONG")
        self.assertFalse(passed)
        self.assertEqual(code, "breakdown_dominance_blocks_long")

    def test_bearish_kinetic_acceleration_does_not_block_a_short(self):
        package = {"calculus": {
            "regime": "BEAR_ACCELERATING", "acceleration": -0.4,
            "power": 0.3, "power_regime": "KINETIC_ACCELERATING",
        }}
        passed, code, _reason, _evidence = evaluate_directional_momentum_gate(
            package, "SELL_SHORT")
        self.assertTrue(passed)
        self.assertEqual(code, "")


if __name__ == "__main__":
    unittest.main()
