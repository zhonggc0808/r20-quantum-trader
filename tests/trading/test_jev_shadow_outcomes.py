"""Focused lifecycle tests for fixed-horizon JEV shadow outcomes."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import scripts.ai_brain_trader as abt


class ShadowOutcomeWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="astra-jev-outcome-")
        self.addCleanup(self.tmp.cleanup)
        self.patch_dir = patch.object(abt, "DATA_DIR", self.tmp.name)
        self.patch_dir.start()
        self.addCleanup(self.patch_dir.stop)
        self.patch_clock = patch.object(abt.time, "time", return_value=10_000_000.0)
        self.patch_clock.start()
        self.addCleanup(self.patch_clock.stop)
        self.patch_ticker = patch.object(
            abt, "fetch_okx_ticker",
            lambda *args, **kwargs: {"last": 100.0, "bidPx": 99.9, "askPx": 100.1},
        )
        self.patch_ticker.start()
        self.addCleanup(self.patch_ticker.stop)

    @staticmethod
    def _proposal(action="WAIT"):
        return {
            "instId": "BTC-USDT-SWAP",
            "decision_id": "cycle-1:BTC-USDT-SWAP",
            "cycle_id": "cycle-1",
            "action": action,
            "entry_mode": "initial",
            "entry_price": 100.0,
            "price": 100.0,
            "bidPx": 99.9,
            "askPx": 100.1,
            "ctVal": 1.0,
            "base_sz": 1.0,
            "minSz": 0.01,
            "shadow_margin_usdt": 10.0,
            "shadow_leverage": 2.0,
        }

    @staticmethod
    def _review(timestamp, *, jev_action="BUY_LONG", jev_status="accepted"):
        return {
            "timestamp": timestamp,
            "time_str": "2026-09-26 08:00:00",
            "cycle_id": "cycle-1",
            "status": "ok",
            "instrument_reviews": [{
                "instId": "BTC-USDT-SWAP",
                "suggested_action": jev_action,
                "jev_action_status": jev_status,
                "suggested_action_votes": {jev_action: 0.9},
                "jev_confidence": 0.9,
                "jev_action_margin": 0.8,
            }],
        }

    def _rows(self):
        path = os.path.join(self.tmp.name, "jev_shadow_entry_outcomes.jsonl")
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_late_mark_snapshot_is_marked_missed_and_not_written(self):
        proposal = self._proposal()
        abt._jev_shadow_update_entry_outcomes(
            [proposal], self._review(10_000_000)["instrument_reviews"], [], self._review(10_000_000))
        abt._jev_shadow_update_entry_outcomes(
            [proposal], self._review(10_001_801)["instrument_reviews"], [], self._review(10_001_801))

        row = self._rows()[0]
        self.assertIsNone(row["pnl_after_1_cycle"])
        self.assertEqual(
            row["pnl_after_1_cycle_measurement_status"], "missed_target_window")
        self.assertGreater(row["pnl_after_1_cycle_lag_seconds"], 900.0)
        self.assertEqual(row["main_pnl_after_1_cycle"], 0.0)
        self.assertEqual(row["main_pnl_after_1_cycle_source"], "no_entry_baseline")

    def test_no_edge_uses_the_same_explicit_jev_baseline(self):
        proposal = self._proposal()
        abt._jev_shadow_update_entry_outcomes(
            [proposal], self._review(10_000_000, jev_action="WAIT", jev_status="no_edge")["instrument_reviews"], [],
            self._review(10_000_000, jev_action="WAIT", jev_status="no_edge"),
        )
        abt._jev_shadow_update_entry_outcomes(
            [proposal], self._review(10_001_000, jev_action="WAIT", jev_status="no_edge")["instrument_reviews"], [],
            self._review(10_001_000, jev_action="WAIT", jev_status="no_edge"),
        )

        row = self._rows()[0]
        self.assertEqual(row["jev_delayed_entry_status"], "NO_EDGE")
        self.assertEqual(row["pnl_after_1_cycle"], 0.0)
        self.assertEqual(row["pnl_after_1_cycle_source"], "no_entry_baseline")


if __name__ == "__main__":
    unittest.main()
