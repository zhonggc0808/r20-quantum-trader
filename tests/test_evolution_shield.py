import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import evolution_shield as shield
from scripts.evolution_shield import (
    load_structured_memory,
    audit_proposed_lesson,
    toggle_lesson,
    rollback_to_baseline,
    add_safe_lesson,
)


class EvolutionShieldTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in {
            "WORKSPACE_DIR": root,
            "DATA_DIR": root / "data",
            "STRUCTURED_MEMORY_FILE": root / "data" / "structured_trading_memory.json",
            "AI_MEMORY_MD_FILE": root / "data" / "AI_TRADING_MEMORY.md",
        }.items():
            patcher = patch.object(shield, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_constitution_red_lines_block_extreme_biases(self):
        # Test 1: Block extreme long bias
        passed, reason = audit_proposed_lesson("【偏见】极端暴跌后未来绝对不要做多，所有买点都是陷阱", sample_size=10)
        self.assertFalse(passed)
        self.assertIn("极端做多偏见阻断", reason)

        # Test 2: Block stop loss cancellation
        passed, reason = audit_proposed_lesson("【抗单】遇到插针可以取消止损，扛单等待解套", sample_size=10)
        self.assertFalse(passed)
        self.assertIn("违规抗单放大止损", reason)

        # Test 3: Block Martingale doubling
        passed, reason = audit_proposed_lesson("【翻倍】亏损后加倍仓位摊平亏损", sample_size=10)
        self.assertFalse(passed)
        self.assertIn("马丁格尔赌徒加仓倾向", reason)

    def test_outlier_single_event_rejection(self):
        # Single event (<2 samples) must be rejected
        passed, reason = audit_proposed_lesson("【合理经验】4H多头回踩均线支撑时开多", sample_size=1)
        self.assertFalse(passed)
        self.assertIn("样本量不足", reason)

        # Sufficient sample size passes
        passed, reason = audit_proposed_lesson("【合理经验】4H多头回踩均线支撑时开多", sample_size=3)
        self.assertTrue(passed)

    def test_white_box_memory_crud_and_rollback(self):
        # Rollback initializes clean golden baseline
        baseline = rollback_to_baseline(expected_version=shield.read_memory_snapshot()["version"])
        self.assertGreaterEqual(len(baseline), 4)

        # Toggle first lesson
        first_id = baseline[0]["id"]
        toggled = toggle_lesson(first_id, expected_version=shield.read_memory_snapshot()["version"])
        self.assertIsNotNone(toggled)
        self.assertFalse(toggled["enabled"])

        # Toggle back
        toggled_back = toggle_lesson(first_id, expected_version=shield.read_memory_snapshot()["version"])
        self.assertTrue(toggled_back["enabled"])


if __name__ == "__main__":
    unittest.main()
