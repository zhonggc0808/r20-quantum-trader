"""Contract tests for the read-only JEV shadow evaluator."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import scripts.jev_shadow_evaluator as evaluator


def _instrument(
    decision_id: str,
    *,
    main: str,
    jev: str,
    status: str = "accepted",
    relation: str | None = None,
    audit: str = "APPROVE",
    data_status: str = "valid",
    entry_mode: str = "initial",
) -> dict:
    cycle_id, inst_id = decision_id.split(":", 1)
    return {
        "decision_id": decision_id,
        "cycle_id": cycle_id,
        "instId": inst_id,
        "main_action": main,
        "jev_independent_action": jev,
        "jev_action_status": status,
        "jev_independent_data_status": data_status,
        "jev_audit_verdict": audit,
        "jev_relation_to_main": relation,
        "entry_mode": entry_mode,
    }


def _review(version: int | None, timestamp: float, instruments: list[dict]) -> dict:
    return {
        "question_semantics_version": version,
        "review_started_timestamp": timestamp,
        "cycle_id": instruments[0]["cycle_id"] if instruments else "empty",
        "instrument_reviews": instruments,
    }


def _outcome(
    decision_id: str,
    *,
    main: str,
    jev: str,
    timestamp: float = 1_000.0,
    jev_1cycle: float | None = None,
    jev_source: str | None = None,
    main_1cycle: float | None = None,
    main_source: str | None = None,
    entry_mode: str = "initial",
    paper_status: str | None = None,
    main_source_kind: str | None = None,
    rejection_code: str | None = None,
) -> dict:
    cycle_id, inst_id = decision_id.split(":", 1)
    row = {
        "record_id": f"{decision_id}:{inst_id}",
        "decision_id": decision_id,
        "cycle_id": cycle_id,
        "instId": inst_id,
        "review_timestamp": timestamp,
        "main_action": main,
        "jev_action": jev,
        "entry_mode": entry_mode,
        "cost_assumptions": {
            "fee_rate": 0.0005,
            "slippage_bps": 2.0,
            "cycle_seconds": 900.0,
            "horizon_4h_seconds": 14400.0,
        },
    }
    if paper_status is not None:
        row["jev_delayed_entry_status"] = paper_status
    if main_source_kind is not None:
        row["main_decision_outcome_source"] = main_source_kind
    if rejection_code is not None:
        row["main_decision_rejection_code"] = rejection_code
    if jev_1cycle is not None:
        row["pnl_after_1_cycle"] = jev_1cycle
    if jev_source is not None:
        row["pnl_after_1_cycle_source"] = jev_source
    if main_1cycle is not None:
        row["main_pnl_after_1_cycle"] = main_1cycle
    if main_source is not None:
        row["main_pnl_after_1_cycle_source"] = main_source
    return row


class CategoryDerivationTest(unittest.TestCase):
    def test_all_seven_evaluation_categories(self):
        cases = [
            ("BUY_LONG", "BUY_LONG", "accepted", "APPROVE", "valid", "AGREE"),
            ("BUY_LONG", "WAIT", "no_edge", "APPROVE", "valid", "WAIT_VS_ENTRY"),
            ("BUY_LONG", "SELL_SHORT", "accepted", "APPROVE", "valid", "OPPOSITE_DIRECTION"),
            ("WAIT", "SELL_SHORT", "accepted", "NOT_APPLICABLE", "valid", "MAIN_WAIT_JEV_ENTRY"),
            ("WAIT", "WAIT", "no_edge", "NOT_APPLICABLE", "valid", "BOTH_WAIT_NO_EDGE"),
            ("BUY_LONG", "WAIT", "low_confidence", "APPROVE", "valid", "INSUFFICIENT_DATA"),
            ("BUY_LONG", "BUY_LONG", "accepted", "REJECT", "valid", "AUDIT_REJECT"),
        ]
        for index, (main, jev, status, audit, data_status, expected) in enumerate(cases):
            decision = f"c{index}:BTC-USDT-SWAP"
            review = _instrument(
                decision, main=main, jev=jev, status=status, audit=audit,
                data_status=data_status,
            )
            outcome = _outcome(decision, main=main, jev=jev)
            self.assertEqual(evaluator.derive_category(review, outcome), expected)

    def test_position_management_direction_is_out_of_entry_scope(self):
        decision = "c-position:BTC-USDT-SWAP"
        review = _instrument(
            decision, main="WAIT", jev="BUY_LONG",
            relation="MAIN_WAIT_JEV_ENTRY", entry_mode="position_management",
        )
        outcome = _outcome(
            decision, main="WAIT", jev="BUY_LONG",
            entry_mode="position_management", paper_status="NOT_ENTRY",
        )
        self.assertEqual(evaluator.derive_category(review, outcome), "OUT_OF_SCOPE")



class CohortIsolationTest(unittest.TestCase):
    def setUp(self):
        self.v1_id = "c1:BTC-USDT-SWAP"
        self.v2_id = "c2:ETH-USDT-SWAP"
        self.reviews = [
            _review(1, 1_000, [_instrument(self.v1_id, main="BUY_LONG", jev="BUY_LONG", relation="AGREE")]),
            _review(2, 2_000, [_instrument(self.v2_id, main="WAIT", jev="SELL_SHORT", relation="MAIN_WAIT_JEV_ENTRY")]),
        ]
        self.outcomes = [
            _outcome(self.v1_id, main="BUY_LONG", jev="BUY_LONG", jev_1cycle=1, jev_source="mark_to_market", main_1cycle=1, main_source="mark_to_market"),
            _outcome(self.v2_id, main="WAIT", jev="SELL_SHORT", jev_1cycle=2, jev_source="mark_to_market"),
        ]

    def test_latest_selects_only_highest_semantics_version(self):
        report = evaluator.build_report(
            self.reviews, self.outcomes, semantics_version="latest",
            horizons=("1cycle",), now_ts=20_000,
        )
        self.assertEqual(report["filters"]["selected_versions"], [2])
        self.assertEqual(report["data_quality"]["matched_records"], 1)
        self.assertEqual(len(report["cohorts"]), 1)
        self.assertEqual(report["cohorts"][0]["question_semantics_version"], 2)

    def test_all_versions_are_reported_but_never_aggregated(self):
        report = evaluator.build_report(
            self.reviews, self.outcomes, semantics_version="all",
            horizons=("1cycle",), now_ts=20_000,
        )
        self.assertEqual([row["question_semantics_version"] for row in report["cohorts"]], [1, 2])
        self.assertEqual([row["n_records"] for row in report["cohorts"]], [1, 1])


class BaselineAndMetricsTest(unittest.TestCase):
    def test_wait_baseline_is_not_counted_as_measured_return(self):
        decision = "c1:BTC-USDT-SWAP"
        reviews = [_review(2, 1_000, [
            _instrument(decision, main="BUY_LONG", jev="WAIT", status="no_edge", relation="WAIT_VS_ENTRY")
        ])]
        outcomes = [_outcome(
            decision, main="BUY_LONG", jev="WAIT",
            jev_1cycle=0.0, jev_source="no_entry_baseline",
            main_1cycle=-2.0, main_source="mark_to_market",
        )]
        report = evaluator.build_report(
            reviews, outcomes, semantics_version="2",
            horizons=("1cycle",), now_ts=20_000,
        )
        bucket = report["cohorts"][0]["categories"]["WAIT_VS_ENTRY"]["1cycle"]
        self.assertEqual(bucket["n_main_measured"], 1)
        self.assertEqual(bucket["n_jev_measured"], 0)
        self.assertEqual(bucket["n_paired"], 1)
        self.assertEqual(bucket["n_baseline_assignments"], 1)
        self.assertEqual(bucket["jev_policy_return"]["mean_net"], 0.0)
        self.assertEqual(bucket["jev_minus_main"]["mean_net"], 2.0)

    def test_stats_include_tail_wilson_and_sequence_drawdown(self):
        got = evaluator._series_stats([(1, 2.0), (2, -3.0), (3, 1.0)])
        self.assertEqual(got["n"], 3)
        self.assertAlmostEqual(got["mean_net"], 0.0)
        self.assertEqual(got["worst"], -3.0)
        self.assertEqual(got["sample_sequence_max_drawdown"], 3.0)
        self.assertTrue(got["drawdown_meaningful"])
        self.assertEqual(got["drawdown_overlap_factor"], 1.0)
        self.assertEqual(len(got["win_rate_wilson_95"]), 2)

    def test_trading_ledger_is_a_measured_terminal_source(self):
        value = {
            "main_pnl_after_1_cycle": 1.25,
            "main_pnl_after_1_cycle_source": "trading_ledger",
        }
        self.assertEqual(
            evaluator._pnl_value(value, "main_pnl_after_1_cycle"),
            ("measured", 1.25, "trading_ledger"),
        )

    def test_late_mark_snapshot_is_not_measured(self):
        value = {
            "pnl_after_1_cycle": 1.25,
            "pnl_after_1_cycle_source": "mark_to_market",
            "pnl_after_1_cycle_lag_seconds": 900.01,
            "cost_assumptions": {"cycle_seconds": 900.0},
        }
        self.assertEqual(
            evaluator._pnl_value(value, "pnl_after_1_cycle"),
            ("invalid", None, "observation_lag_exceeded"),
        )

    def test_malformed_mark_snapshot_lag_is_not_measured(self):
        value = {
            "pnl_after_1_cycle": 1.25,
            "pnl_after_1_cycle_source": "mark_to_market",
            "pnl_after_1_cycle_lag_seconds": "not-a-number",
            "cost_assumptions": {"cycle_seconds": 900.0},
        }
        self.assertEqual(
            evaluator._pnl_value(value, "pnl_after_1_cycle"),
            ("invalid", None, "observation_lag_invalid"),
        )

    def test_baseline_source_requires_explicit_zero_value(self):
        self.assertEqual(
            evaluator._pnl_value(
                {"pnl_after_1_cycle_source": "no_entry_baseline"},
                "pnl_after_1_cycle",
            ),
            ("invalid", None, "baseline_missing_value"),
        )
        self.assertEqual(
            evaluator._pnl_value(
                {
                    "pnl_after_1_cycle": 9.0,
                    "pnl_after_1_cycle_source": "no_entry_baseline",
                },
                "pnl_after_1_cycle",
            ),
            ("invalid", None, "baseline_nonzero_value"),
        )

    def test_invalid_baseline_does_not_create_policy_return(self):
        decision = "c-invalid-baseline:BTC-USDT-SWAP"
        reviews = [_review(2, 1_000, [
            _instrument(
                decision, main="BUY_LONG", jev="WAIT",
                status="no_edge", relation="WAIT_VS_ENTRY",
            )
        ])]
        outcome = _outcome(
            decision, main="BUY_LONG", jev="WAIT",
            jev_1cycle=9.0, jev_source="no_entry_baseline",
            main_1cycle=-2.0, main_source="mark_to_market",
        )

        report = evaluator.build_report(
            reviews, [outcome], semantics_version="2",
            horizons=("1cycle",), now_ts=20_000,
        )
        bucket = report["cohorts"][0]["categories"]["WAIT_VS_ENTRY"]["1cycle"]

        self.assertEqual(bucket["n_baseline_assignments"], 0)
        self.assertEqual(bucket["n_paired"], 0)
        self.assertIsNone(bucket["jev_policy_return"])
        self.assertIn(
            "jev:baseline_nonzero_value", bucket["source_counts"])

    def test_drawdown_aggregates_equal_timestamps_before_sequence(self):
        first = evaluator._series_stats([
            (1, -100.0), (1, 60.0), (2, -100.0), (2, 60.0),
        ])
        reordered = evaluator._series_stats([
            (1, 60.0), (1, -100.0), (2, 60.0), (2, -100.0),
        ])

        self.assertEqual(
            first["sample_sequence_max_drawdown"],
            reordered["sample_sequence_max_drawdown"],
        )
        self.assertEqual(first["sample_sequence_max_drawdown"], 80.0)
        self.assertEqual(first["drawdown_sequence_points"], 2)
        self.assertEqual(first["drawdown_timestamp_collisions"], 2)

    def test_sample_gate_boundaries(self):
        expected = {
            29: "INSUFFICIENT_EVIDENCE",
            30: "EXPLORATORY_ONLY",
            99: "EXPLORATORY_ONLY",
            100: "DECISION_EVALUABLE",
        }
        for n, status in expected.items():
            self.assertEqual(evaluator._evidence_status(n, 30, 100), status)

    def test_horizon_drawdown_marks_four_hour_overlap_as_not_meaningful(self):
        decision = "c-overlap:BTC-USDT-SWAP"
        reviews = [_review(2, 1_000, [
            _instrument(decision, main="WAIT", jev="BUY_LONG",
                        relation="MAIN_WAIT_JEV_ENTRY")
        ])]
        outcome = _outcome(
            decision, main="WAIT", jev="BUY_LONG", timestamp=1_000,
            jev_1cycle=1.0, jev_source="mark_to_market")
        outcome["pnl_after_4h"] = 2.0
        outcome["pnl_after_4h_source"] = "mark_to_market"
        report = evaluator.build_report(
            reviews, [outcome], semantics_version="2",
            horizons=("1cycle", "4h"), now_ts=20_000)
        category = report["cohorts"][0]["categories"]["MAIN_WAIT_JEV_ENTRY"]
        self.assertTrue(category["1cycle"]["jev_policy_return"]["drawdown_meaningful"])
        self.assertEqual(category["1cycle"]["drawdown_overlap_factor"], 1.0)
        self.assertFalse(category["4h"]["jev_policy_return"]["drawdown_meaningful"])
        self.assertEqual(category["4h"]["drawdown_overlap_factor"], 16.0)

    def test_both_wait_no_edge_is_descriptive_not_out_of_scope(self):
        decision = "c-no-edge:BTC-USDT-SWAP"
        reviews = [_review(2, 1_000, [
            _instrument(decision, main="WAIT", jev="WAIT", status="no_edge",
                        relation="AGREE")
        ])]
        outcomes = [_outcome(
            decision, main="WAIT", jev="WAIT", timestamp=1_000,
            paper_status="NO_EDGE")]
        report = evaluator.build_report(
            reviews, outcomes, semantics_version="2",
            horizons=("1cycle",), now_ts=20_000)
        bucket = report["cohorts"][0]["categories"]["BOTH_WAIT_NO_EDGE"]["1cycle"]
        self.assertEqual(bucket["n_total"], 1)
        self.assertEqual(bucket["comparison_kind"], "no_entry_agreement")
        self.assertEqual(bucket["evidence_status"], "DESCRIPTIVE_ONLY")
        self.assertEqual(report["data_quality"]["out_of_scope_records"], 0)

    def test_insufficient_wait_does_not_create_zero_return_statistics(self):
        decision = "c1:BTC-USDT-SWAP"
        reviews = [_review(2, 1_000, [
            _instrument(decision, main="WAIT", jev="WAIT", status="low_confidence", relation="ABSTAIN")
        ])]
        outcomes = [_outcome(
            decision, main="WAIT", jev="WAIT",
            jev_1cycle=0.0, jev_source="no_entry_baseline",
        )]
        report = evaluator.build_report(
            reviews, outcomes, semantics_version="2",
            horizons=("1cycle",), now_ts=20_000,
        )
        bucket = report["cohorts"][0]["categories"]["INSUFFICIENT_DATA"]["1cycle"]
        self.assertEqual(bucket["n_measured"], 0)
        self.assertEqual(bucket["n_baseline_rows"], 1)
        summary = evaluator.render_summary(report)
        self.assertIn("baseline measured", summary)
        self.assertIsNone(bucket["main_policy_return"])
        self.assertIsNone(bucket["jev_policy_return"])

    def test_unmatured_row_is_pending_not_zero(self):
        decision = "c1:BTC-USDT-SWAP"
        reviews = [_review(2, 1_000, [
            _instrument(decision, main="WAIT", jev="BUY_LONG", relation="MAIN_WAIT_JEV_ENTRY")
        ])]
        outcomes = [_outcome(decision, main="WAIT", jev="BUY_LONG", timestamp=1_000)]
        report = evaluator.build_report(
            reviews, outcomes, semantics_version="2",
            horizons=("1cycle",), now_ts=1_100,
        )
        bucket = report["cohorts"][0]["categories"]["MAIN_WAIT_JEV_ENTRY"]["1cycle"]
        self.assertEqual(bucket["n_pending"], 1)
        self.assertEqual(bucket["n_matured"], 0)
        self.assertIsNone(bucket["jev_policy_return"])


class DataQualityAndCliTest(unittest.TestCase):
    def test_relation_drift_duplicates_and_parse_errors_are_visible(self):
        decision = "c1:BTC-USDT-SWAP"
        reviews = [_review(2, 1_000, [
            _instrument(decision, main="WAIT", jev="BUY_LONG", relation="NOT_APPLICABLE")
        ])]
        outcome = _outcome(decision, main="WAIT", jev="BUY_LONG")
        report = evaluator.build_report(
            reviews, [outcome], semantics_version="2", horizons=("1cycle",),
            now_ts=20_000, review_parse_issues=[{"line": 2}],
        )
        self.assertEqual(report["data_quality"]["relation_mismatches"], 1)
        self.assertEqual(report["data_quality"]["review_parse_errors"], 1)

        duplicate = evaluator.build_report(
            reviews, [outcome, dict(outcome)], semantics_version="2",
            horizons=("1cycle",), now_ts=20_000,
        )
        self.assertEqual(duplicate["data_quality"]["matched_records"], 0)
        self.assertEqual(duplicate["data_quality"]["outcome_duplicate_decision_ids"], [decision])

    def test_raw_action_diagnostics_do_not_apply_action_aliases(self):
        decision = "c-raw-action:BTC-USDT-SWAP"
        review_row = _instrument(
            decision, main="WAIT", jev="WAIT", status="no_edge", relation="AGREE")
        review_row["main_raw_action"] = "HOLD"
        outcome = _outcome(
            decision, main="WAIT", jev="WAIT", paper_status="NO_EDGE")
        outcome["main_raw_action"] = "HOLD"
        report = evaluator.build_report(
            [_review(2, 1_000, [review_row])], [outcome],
            semantics_version="2", horizons=("1cycle",), now_ts=20_000)
        self.assertEqual(
            report["population_diagnostics"]["main_raw_action_counts"],
            {"HOLD": 1},
        )

    def test_population_diagnostics_explain_scope_and_wait_origin(self):
        scoped = "c1:BTC-USDT-SWAP"
        excluded = "c2:ETH-USDT-SWAP"
        reviews = [
            _review(2, 1_000, [
                _instrument(scoped, main="WAIT", jev="BUY_LONG",
                            relation="MAIN_WAIT_JEV_ENTRY"),
                _instrument(excluded, main="WAIT", jev="SELL_SHORT",
                            relation="MAIN_WAIT_JEV_ENTRY",
                            entry_mode="position_management"),
            ]),
        ]
        outcomes = [
            _outcome(scoped, main="WAIT", jev="BUY_LONG",
                     paper_status="PAPER_ESTIMATED_FILL",
                     main_source_kind="model_wait", rejection_code="model_wait"),
            _outcome(excluded, main="WAIT", jev="SELL_SHORT",
                     entry_mode="position_management", paper_status="NOT_ENTRY",
                     main_source_kind="model_wait", rejection_code="model_wait"),
        ]
        report = evaluator.build_report(
            reviews, outcomes, semantics_version="2", horizons=("1cycle",),
            now_ts=1_100,
        )
        diagnostics = report["population_diagnostics"]
        self.assertEqual(diagnostics["entry_mode_counts"], {
            "initial": 1, "position_management": 1})
        self.assertEqual(diagnostics["jev_delayed_entry_status_counts"], {
            "NOT_ENTRY": 1, "PAPER_ESTIMATED_FILL": 1})
        self.assertEqual(diagnostics["main_decision_outcome_source_counts"], {
            "model_wait": 2})
        self.assertEqual(report["data_quality"]["out_of_scope_records"], 1)
        self.assertEqual(report["cohorts"][0]["n_records"], 1)

    def test_relation_mismatch_timeline_distinguishes_historical_prefix(self):
        old_id = "c-old:BTC-USDT-SWAP"
        new_id = "c-new:ETH-USDT-SWAP"
        reviews = [
            _review(2, 1_000, [
                _instrument(old_id, main="WAIT", jev="BUY_LONG",
                            relation="AGREE")
            ]),
            _review(2, 2_000, [
                _instrument(new_id, main="WAIT", jev="BUY_LONG",
                            relation="MAIN_WAIT_JEV_ENTRY")
            ]),
        ]
        outcomes = [
            _outcome(old_id, main="WAIT", jev="BUY_LONG", timestamp=1_000),
            _outcome(new_id, main="WAIT", jev="BUY_LONG", timestamp=2_000),
        ]
        report = evaluator.build_report(
            reviews, outcomes, semantics_version="2",
            horizons=("1cycle",), now_ts=20_000)
        diagnostics = report["data_quality"]["relation_mismatch_diagnostics"]
        self.assertEqual(diagnostics["count"], 1)
        self.assertEqual(diagnostics["scope"], "historical_prefix")
        self.assertEqual(diagnostics["records_after_last_mismatch"], 1)
        self.assertEqual(diagnostics["last_mismatch_at"], "1970-01-01T08:16:40+08:00")
        summary = evaluator.render_summary(report)
        self.assertIn("relation_mismatches=1 scope=historical_prefix", summary)
        self.assertIn("newer_records=1", summary)

    def test_relation_tail_with_missing_rows_is_not_called_clean(self):
        ids = [
            "c-valid:BTC-USDT-SWAP",
            "c-mismatch:ETH-USDT-SWAP",
            "c-missing:SOL-USDT-SWAP",
            "c-missing-2:XRP-USDT-SWAP",
        ]
        relations = ["MAIN_WAIT_JEV_ENTRY", "AGREE", None, None]
        reviews = [
            _review(2, timestamp, [
                _instrument(decision_id, main="WAIT", jev="BUY_LONG",
                            relation=relation)
            ])
            for decision_id, timestamp, relation in zip(
                ids, (500, 1_000, 2_000, 3_000), relations)
        ]
        outcomes = [
            _outcome(decision_id, main="WAIT", jev="BUY_LONG", timestamp=timestamp)
            for decision_id, timestamp in zip(ids, (500, 1_000, 2_000, 3_000))
        ]

        report = evaluator.build_report(
            reviews, outcomes, semantics_version="2",
            horizons=("1cycle",), now_ts=20_000)

        diagnostics = report["data_quality"]["relation_mismatch_diagnostics"]
        self.assertEqual(diagnostics["validated_match_count"], 1)
        self.assertEqual(diagnostics["count"], 1)
        self.assertEqual(diagnostics["missing_recorded_relation_count"], 2)
        self.assertEqual(diagnostics["unvalidated_records_after_last_mismatch"], 2)
        self.assertEqual(
            diagnostics["scope"], "historical_segment_with_unvalidated_tail")
        self.assertEqual(diagnostics["records_after_last_mismatch"], 0)

    def test_relation_mismatch_with_older_clean_rows_is_not_called_prefix(self):
        ids = [
            "c-clean-old:BTC-USDT-SWAP",
            "c-mismatch:ETH-USDT-SWAP",
            "c-clean-new:SOL-USDT-SWAP",
        ]
        relations = ["MAIN_WAIT_JEV_ENTRY", "AGREE", "MAIN_WAIT_JEV_ENTRY"]
        reviews = [
            _review(2, timestamp, [
                _instrument(decision_id, main="WAIT", jev="BUY_LONG",
                            relation=relation)
            ])
            for decision_id, timestamp, relation in zip(ids, (1_000, 2_000, 3_000), relations)
        ]
        outcomes = [
            _outcome(decision_id, main="WAIT", jev="BUY_LONG", timestamp=timestamp)
            for decision_id, timestamp in zip(ids, (1_000, 2_000, 3_000))
        ]

        report = evaluator.build_report(
            reviews, outcomes, semantics_version="2",
            horizons=("1cycle",), now_ts=20_000)

        diagnostics = report["data_quality"]["relation_mismatch_diagnostics"]
        self.assertEqual(diagnostics["count"], 1)
        self.assertEqual(
            diagnostics["scope"], "historical_segment_with_clean_tail")
        self.assertEqual(diagnostics["records_after_last_mismatch"], 1)

    def test_cli_naive_time_uses_beijing_contract_and_echoes_offset(self):
        expected = 1_790_409_600.0
        self.assertEqual(
            evaluator._parse_cli_time("2026-09-26T16:00:00"), expected)
        self.assertEqual(
            evaluator._parse_cli_time("2026-09-26T16:00:00+08:00"), expected)
        self.assertEqual(
            evaluator._parse_cli_time("2026-09-26T08:00:00Z"), expected)
        self.assertEqual(evaluator._parse_cli_time(str(int(expected))), expected)
        millis = int(expected * 1000)
        self.assertEqual(evaluator._parse_cli_time(str(millis)), expected)
        self.assertEqual(evaluator._parse_timestamp(millis), expected)

        decision = "c-time:BTC-USDT-SWAP"
        review = _review(2, expected, [
            _instrument(decision, main="WAIT", jev="BUY_LONG",
                        relation="MAIN_WAIT_JEV_ENTRY")
        ])
        outcome = _outcome(
            decision, main="WAIT", jev="BUY_LONG", timestamp=expected)
        with tempfile.TemporaryDirectory() as tmp:
            review_path = Path(tmp) / "reviews.jsonl"
            outcome_path = Path(tmp) / "outcomes.jsonl"
            review_path.write_text(json.dumps(review) + "\n")
            outcome_path.write_text(json.dumps(outcome) + "\n")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = evaluator.main([
                    "--reviews", str(review_path),
                    "--outcomes", str(outcome_path),
                    "--semantics-version", "2",
                    "--since", "2026-09-26T16:00:00",
                    "--until", "2026-09-26T16:00:01",
                    "--horizon", "1cycle",
                    "--format", "json",
                ])
            self.assertEqual(rc, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["data_quality"]["matched_records"], 1)
            self.assertEqual(
                payload["filters"]["since"], "2026-09-26T16:00:00+08:00")
            self.assertEqual(
                payload["filters"]["until"], "2026-09-26T16:00:01+08:00")

    def test_cli_json_is_read_only_and_valid(self):
        decision = "c1:BTC-USDT-SWAP"
        review = _review(2, 1_000, [
            _instrument(decision, main="BUY_LONG", jev="WAIT", status="no_edge", relation="WAIT_VS_ENTRY")
        ])
        outcome = _outcome(
            decision, main="BUY_LONG", jev="WAIT",
            jev_1cycle=0.0, jev_source="no_entry_baseline",
            main_1cycle=1.0, main_source="mark_to_market",
        )
        with tempfile.TemporaryDirectory() as tmp:
            review_path = Path(tmp) / "reviews.jsonl"
            outcome_path = Path(tmp) / "outcomes.jsonl"
            review_path.write_text(json.dumps(review) + "\n{broken\n")
            outcome_path.write_text(json.dumps(outcome) + "\n")
            before = {
                review_path: hashlib.sha256(review_path.read_bytes()).hexdigest(),
                outcome_path: hashlib.sha256(outcome_path.read_bytes()).hexdigest(),
            }
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = evaluator.main([
                    "--reviews", str(review_path),
                    "--outcomes", str(outcome_path),
                    "--semantics-version", "2",
                    "--horizon", "1cycle",
                    "--format", "json",
                ])
            self.assertEqual(rc, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["report_version"], 1)
            self.assertEqual(payload["data_quality"]["review_parse_errors"], 1)
            for path, digest in before.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
