from __future__ import annotations

import unittest

from scripts.direction_observation import (
    compare_directions,
    direction_layers,
    enrich_brain_package,
    four_hour_range,
)


def raw_4h(ts: int, high: float, low: float, close: float, confirm: str = "1"):
    # OKX: ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm
    return [str(ts), str(close), str(high), str(low), str(close), "1", "1", "1", confirm]


class DirectionObservationTests(unittest.TestCase):
    def test_range_uses_closed_candles_and_reports_position(self):
        rows = [raw_4h(3000, 110, 90, 100), raw_4h(2000, 108, 92, 100)]
        # 只提供两根时 lookback 调低；最新一根已收盘也可参与。
        got = four_hour_range(rows, 95, lookback=2)
        self.assertEqual(got["range_4h_high"], 110.0)
        self.assertEqual(got["range_4h_low"], 90.0)
        self.assertEqual(got["range_4h_width"], 20.0)
        self.assertEqual(got["price_position_in_range"], 0.25)
        self.assertEqual(got["range_4h_candle_ts"], 3000)

    def test_unconfirmed_latest_candle_is_excluded(self):
        rows = [raw_4h(3000, 150, 50, 100, "0"), raw_4h(2000, 110, 90, 100, "1")]
        got = four_hour_range(rows, 100, lookback=1)
        self.assertEqual(got["range_4h_high"], 110.0)
        self.assertEqual(got["range_4h_low"], 90.0)

    def test_missing_4h_never_defaults_to_bullish(self):
        got = enrich_brain_package({"price": 100.0, "recent_4h": []})
        self.assertIsNone(got["range_4h_high"])
        # 2026-09-26：这个「最新 8 根」窗口改名为
        # price_position_in_range_recent8，以免与权威的「已收盘 12 根」
        # 口径（four_hour_range → direction_observation）同名混淆。
        self.assertIsNone(got["price_position_in_range_recent8"])
        self.assertNotIn("price_position_in_range", got,
                         "两个窗口不得再共用同一个键名")
        self.assertIsNone(got["market_data_timestamps"]["4H"])

    def test_direction_layers_keep_legacy_aggregate_and_add_new_layers(self):
        calc = {"timeframes": {
            "4H": {"valid": True, "direction": 1, "velocity": 2, "acceleration": 3, "quality": .9},
            "1H": {"valid": True, "direction": 1, "velocity": 1, "acceleration": 2, "quality": .8},
            "15M": {"valid": True, "direction": 1, "velocity": .5, "acceleration": 1, "quality": .7},
        }, "regime": "BULL_ACCELERATING"}
        got = direction_layers(calc)
        self.assertEqual(got["calculus_schema_version"], "equal_weight_v1")
        self.assertEqual(got["direction_4h"]["direction"], 1)
        self.assertEqual(got["strength_1h"]["quality"], .8)
        self.assertEqual(got["entry_15m"]["velocity"], .5)

    def test_compare_marks_conflict_without_changing_actions(self):
        f = {
            "market_regime": "BULL_TREND", "market_data_valid": True,
            "range_4h_candle_ts": 1000, "range_4h_high": 110.0,
            "range_4h_low": 90.0, "price_position_in_range": .75,
            "calculus": {"regime": "BEAR_ACCELERATING", "timeframes": {
                "4H": {"valid": True, "direction": -1},
                "1H": {"valid": True, "direction": 1},
                "15M": {"valid": True, "direction": 1},
            }},
        }
        brain = {"direction_input": {
            "macro_4h": "4H_MACRO_BEAR (大级别空头承压)",
            "data_quality": "valid", "candle_ts_4h": 1000,
        }}
        got = compare_directions(f, brain)
        self.assertEqual(got["status"], "CONFLICT")
        self.assertEqual(got["sources"]["macro_4h"], "BEAR")
        self.assertEqual(got["sources"]["market_regime"], "BULL")
        self.assertEqual(got["sources"]["calculus_regime"], "BEAR")


if __name__ == "__main__":
    unittest.main()
