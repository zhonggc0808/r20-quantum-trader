"""JEV 影子通道契约 —— 钉死 2026-09-26 修掉的两条回归。

## 为什么有这条门

**回归一（通道全灭）**：双通道重构给「独立决策」通道加了 `type: "choice"`
选择题，而唯一在用的 provider（typesafe）只接受 `boolean`（发送时转成
`noul`）—— 它对 choice 要求一套未公开的 `choice.criteria` 结构，服务端直接
`422 Unprocessable Entity`。结果：独立通道连续 **62/62 轮失败**（16 小时），
同期审计通道因为是纯 boolean 而 **100% 正常**。两通道唯一的结构差异就是这
两道 choice 题。

**回归二（无判别力的恒 REJECT）**：审计通道拿 `main=WAIT` 的「提议」去问
`proposal_complete`。WAIT 提议按定义没有 entry/stop/target/size/leverage，
于是该题中位数塌到 **0.07**、触发硬 flag `proposal_data_incomplete`，
让 **604/620 = 98%** 的轮次恒定输出 `AUDIT_REJECT`。这不是「JEV 不认同」，
是「没有提议可审」——一个恒为同一值的信号没有判别力，还会污染统计。

本门钉住三件事：
1. 任何通道的 payload 只允许出现 provider 能接受的类型；
2. WAIT 提议不生成审计问题，也不被记成 REJECT，而是显式 `NOT_APPLICABLE`；
3. 去掉 choice 之后，独立通道的动作仍由 `would_*` 票决**带 margin** 推导
   （而不是退化成永远 WAIT）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

import scripts.ai_brain_trader as abt

#: provider 唯一接受的类型（boolean 在发送前被就地转成 noul）。
_ACCEPTED_TYPES = {"noul"}
#: 入场提议应获得的审计问题数（7 道原子审计题）。
_ENTRY_AUDIT_QUESTIONS = 7


def _pkg(inst_id: str) -> Dict[str, Any]:
    """一个**代码侧完整且自洽**的候选。

    2026-09-26 拆分后，完整性/一致性由代码判定（`_jev_candidate_state_quality`）
    并独占 `INSUFFICIENT_DATA`。因此夹具必须带上做方向判断所需的字段
    （`direction_observation` / `direction_layers`），否则候选会被正确地判为
    「state 不完整」——那会让票决测试测不到它本来想测的东西。
    """
    return {"instId": inst_id, "name": inst_id.split("-")[0], "price": 100.0,
            "bidPx": 99.9, "askPx": 100.1, "ctVal": 1.0, "minSz": 1.0,
            "base_sz": 1.0, "data_quality": "valid",
            "direction_observation": {
                "status": "ALIGNED_BULL", "brain_candle_ts_4h": 1000,
                "trader_candle_ts_4h": 1000, "price_position_in_range": 0.5,
            },
            "direction_layers": {
                "direction_4h": {"direction": 1}, "strength_1h": {"direction": 1},
                "entry_15m": {"direction": 1},
            }}


def _factor(inst_id: str) -> Dict[str, Any]:
    """trader 因子快照：生产里由 `observe_cycle` 写入 `direction_observation`。

    候选的 `direction_observation` / `direction_layers` 实际取自这里（而不是
    brain package），因此夹具必须通过它注入，否则 `compare_directions` 会因
    缺少 trader 侧数据而算出 `status=INSUFFICIENT_DATA` —— 那会让代码侧正确地
    判定 state 不自洽，测试也就测不到票决逻辑了。
    """
    return {
        "instId": inst_id,
        "direction_observation": {
            "schema_version": 1, "status": "ALIGNED_BULL",
            "sources": {"macro_4h": "BULL", "market_regime": "BULL",
                        "calculus_regime": "BULL"},
            "macro_4h": "4H_MACRO_BULL", "market_regime": "BULL_TREND",
            "calculus_regime": "BULL_ACCELERATING",
            "brain_candle_ts_4h": 1000, "trader_candle_ts_4h": 1000,
            "price_position_in_range": 0.5,
            "range_4h_high": 110.0, "range_4h_low": 90.0,
        },
        "direction_layers": {
            "direction_4h": {"direction": 1, "quality": 0.9},
            "strength_1h": {"direction": 1, "quality": 1.0},
            "entry_15m": {"direction": 1, "quality": 1.0},
        },
    }


def _cache(inst_id: str, action: str) -> Dict[str, Any]:
    return {"instId": inst_id, "cycle_id": "cyc-1", "decision_id": f"cyc-1:{inst_id}",
            "timestamp": 1, "data_quality": "valid",
            "decision": {"action": action, "confidence": 85.0, "leverage": 6,
                         "margin_usdt": 5.0, "entry_price": 100.0,
                         "take_profit_price": 105.0, "stop_loss_price": 98.0}}


class _Harness(unittest.TestCase):
    """把 `_run_jev_shadow_review` 跑起来但拦住一切出网与落盘。"""

    #: 子类可覆盖：每个问题的假答案概率
    probabilities: Dict[str, float] = {}

    def setUp(self) -> None:
        self.captured: List[Dict[str, Any]] = []
        self.tmp = tempfile.TemporaryDirectory(prefix="r20-jev-")
        self.addCleanup(self.tmp.cleanup)
        env = {
            "R20_JEV_SHADOW_ENABLED": "1",
            "R20_JEV_INDEPENDENT_ENABLED": "1",
            "R20_JEV_TYPESAFE_API_KEY": "fake-key-for-contract-test",
            "R20_JEV_PROVIDER": "typesafe",
            "R20_JEV_MODEL": "jev-latest",
        }
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._p_req = patch.object(abt, "_jev_shadow_request", self._fake_request)
        self._p_dir = patch.object(abt, "DATA_DIR", self.tmp.name)
        self._p_tick = patch.object(abt, "fetch_okx_ticker",
                                    lambda *a, **k: {"last": 100.0, "bidPx": 99.9,
                                                     "askPx": 100.1})
        self._p_algo = patch.object(abt.okx_rest, "pending_algo_orders",
                                    lambda *a, **k: [])
        for p in (self._p_req, self._p_dir, self._p_tick, self._p_algo):
            p.start()
            self.addCleanup(p.stop)

    def _probability(self, name: str) -> float:
        """`candidate_3_would_buy_long` / `position_0_would_hold` 这类问题的概率查表。

        必须先剥掉 `candidate_<i>_` / `position_<i>_` 前缀，否则查表恒 miss，假答案
        会变成 None —— 那会让票决全灭并静默退化成 WAIT（本文件第一版就踩了这个坑，
        被「方向应仍能产出」那条断言抓了出来）。
        """
        short = name
        for prefix in ("candidate_", "position_"):
            if name.startswith(prefix):
                short = name[len(prefix):].split("_", 1)[1]
                break
        prob = self.probabilities.get(name)
        return self.probabilities.get(short, 0.5) if prob is None else prob

    def _fake_request(self, endpoint, api_key, payload, timeout, channel):
        """拦住出网：记录 payload，按 boolean 问题回 noul 概率。"""
        self.captured.append({"channel": channel, "payload": payload})
        answers = {}
        for name in (payload.get("questions") or {}):
            prob = self._probability(name)
            answers[name] = {"type": "noul", "noul": prob, "probability": prob}
        return {"status": "ok", "response": {"model": "fake", "answers": answers,
                                            "usage": {}},
                "attempts": [{"attempt": 1, "status": "ok"}],
                "request_id": "", "latency_ms": 1}

    def run_review(self, mapping: Dict[str, str], **kwargs) -> Dict[str, Any]:
        abt._run_jev_shadow_review(
            standard_cache={k: _cache(k, v) for k, v in mapping.items()},
            packages=[_pkg(k) for k in mapping],
            time_str="2026-09-26 08:00:00",
            active_positions_detail=kwargs.pop("positions", []),
            position_management=kwargs.pop("management", []),
            usdt_available=44.0,
            trader_factors=kwargs.pop("factors", [_factor(k) for k in mapping]),
        )
        path = os.path.join(self.tmp.name, "jev_shadow_reviews.jsonl")
        with open(path, "r", encoding="utf-8") as handle:
            return json.loads(handle.read().strip().splitlines()[-1])

    def questions(self, channel: str) -> Dict[str, Any]:
        for row in self.captured:
            if row["channel"] == channel:
                return row["payload"].get("questions") or {}
        self.fail(f"未捕获到通道 {channel} 的 payload")


class CompletenessIsCodeOwnedTest(_Harness):
    """拆分契约：完整性/一致性归**代码**，且只有代码能触发 `INSUFFICIENT_DATA`。

    2026-09-26 之前，模型的一道 `*_data_valid` 同时承担「数据够不够」与「有没
    有机会」，低分被记成 `invalid_data` —— 线上 65/80 个候选如此，而它们的代码
    `data_quality` 全是 `valid`，标签与事实 100% 矛盾。本类钉住拆分后的三条边界。
    """

    #: 高信心方向票：若模型侧仍在数据门槛上判门，低 edge 就会杀掉这个方向。
    probabilities: Dict[str, float] = {
        "edge_present": 0.95, "execution_ready": 0.95,
        "would_buy_long": 0.9, "would_sell_short": 0.05, "would_wait": 0.1,
    }

    def test_low_edge_alone_does_not_veto_a_confident_direction(self):
        """**有意为之**：`edge_present` 本轮只记录、不作否决门。

        该题是全新的、没有任何历史分布可标定；让一个未标定的问题去否决方向，
        正是此前「90 个候选 0 个方向输出」的成因。方向由三票票决决定，
        edge 答案单独记录以便日后按已结算结果标定成门槛。若哪天有人把它改成
        否决门，这条断言会失败，提醒他先拿出标定证据。
        """
        case = self._with_probabilities({"edge_present": 0.05})
        row = case.run_review({"ETH-USDT-SWAP": "BUY_LONG"})["instrument_reviews"][0]
        self.assertAlmostEqual(row["edge_probability"], 0.05, places=6,
                               msg="低 edge 必须被如实记录")
        self.assertEqual(row["suggested_action"], "BUY_LONG",
                         "edge 低不得单独否决高信心方向票（无标定证据前）")

    def test_confident_wait_is_labelled_no_edge(self):
        """票决明确选 WAIT ⇒ 「无优势」，与「拿不准」分开记录。"""
        case = self._with_probabilities({
            "edge_present": 0.05, "execution_ready": 0.95,
            "would_buy_long": 0.1, "would_sell_short": 0.05, "would_wait": 0.9,
        })
        row = case.run_review({"ETH-USDT-SWAP": "BUY_LONG"})["instrument_reviews"][0]
        self.assertEqual(row["suggested_action"], "WAIT")
        self.assertEqual(row["jev_action_status"], "no_edge")
        self.assertEqual(row["code_state_status"], "ok",
                         "夹具完整自洽，代码侧不该报缺陷")
        self.assertNotIn(row["jev_action_status"],
                         {"invalid_data", "missing_data_valid"},
                         "「无优势」不得再冒充数据故障")

    def _with_probabilities(self, overrides: Dict[str, float]):
        """克隆本测试类并覆盖假答案表（夹具需要不同的答案分布）。"""
        base = self

        class Case(self.__class__):
            probabilities = {**self.probabilities, **overrides}

        case = Case(base._testMethodName)
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_inconsistent_state_is_insufficient_data(self):
        """代码侧发现矛盾 ⇒ INSUFFICIENT_DATA，且理由是 `code_state_*`。"""
        review = self.run_review({"ETH-USDT-SWAP": "BUY_LONG"})
        self.assertEqual(review["instrument_reviews"][0]["code_state_status"], "ok",
                         "完整夹具的代码侧基线必须是 ok")
        # 不给 trader 因子：代码无法构成方向观测，是**载荷**问题。
        row = self.run_review_missing_direction(
            {"ETH-USDT-SWAP": "BUY_LONG"})["instrument_reviews"][0]
        self.assertEqual(row["code_state_status"], "inconsistent")
        self.assertIn("direction_observation_insufficient",
                      row["code_state_consistency_flags"])
        self.assertEqual(row["suggested_action"], "INSUFFICIENT_DATA")
        self.assertEqual(row["jev_action_status"], "code_state_inconsistent")

    def test_quality_helper_classifies_the_three_cases(self):
        """`_jev_candidate_state_quality` 的三种判定，直接单测（不经出网）。"""
        ok = _factor("ETH-USDT-SWAP")
        healthy = {"instId": "ETH-USDT-SWAP", "price": 100.0, "bidPx": 99.9,
                   "askPx": 100.1, "direction_observation": ok["direction_observation"],
                   "direction_layers": ok["direction_layers"]}
        self.assertEqual(abt._jev_candidate_state_quality(healthy)["status"], "ok")

        incomplete = dict(healthy)
        incomplete.pop("direction_layers")
        got = abt._jev_candidate_state_quality(incomplete)
        self.assertEqual(got["status"], "insufficient_data")
        self.assertEqual(got["missing_fields"], ["direction_layers"])

        # CONFLICT 是**市场事实**（多周期证据互相矛盾），不是载荷损坏。
        conflicted = dict(healthy)
        conflicted["direction_observation"] = {
            **ok["direction_observation"], "status": "CONFLICT"}
        self.assertEqual(abt._jev_candidate_state_quality(conflicted)["status"], "ok",
                         "多周期分歧不得被当成数据不一致")

        crossed = dict(healthy, bidPx=100.2, askPx=99.9)
        got = abt._jev_candidate_state_quality(crossed)
        self.assertEqual(got["status"], "inconsistent")
        self.assertIn("crossed_book", got["consistency_flags"])

    def test_model_never_answers_a_data_question(self):
        """独立通道不得再出现 `*_data_valid` —— 那是代码拥有的属性。"""
        self.run_review({"ETH-USDT-SWAP": "BUY_LONG"})
        leaked = [k for k in self.questions("independent") if k.endswith("_data_valid")]
        self.assertEqual(leaked, [],
                         "模型侧又在答数据有效性了 —— 那会重新引入标签矛盾")
        self.assertIn("candidate_0_edge_present", self.questions("independent"))

    def test_edge_answer_is_recorded_but_does_not_veto_direction(self):
        """`edge_present` 只记录、不作否决门 —— 未标定的门会重现「0 方向」。"""
        row = self.run_review({"ETH-USDT-SWAP": "BUY_LONG"})["instrument_reviews"][0]
        self.assertAlmostEqual(row["edge_probability"], 0.95, places=6)
        self.assertEqual(row["suggested_action"], "BUY_LONG",
                         "高 edge + 高方向票必须仍能产出方向")

    def run_review_missing_direction(self, mapping: Dict[str, str]) -> Dict[str, Any]:
        """跑一轮但把 `direction_observation`/`direction_layers` 从包里拿掉。"""
        stripped = []
        for key in mapping:
            pkg = _pkg(key)
            pkg.pop("direction_observation")
            pkg.pop("direction_layers")
            stripped.append(pkg)
        abt._run_jev_shadow_review(
            standard_cache={k: _cache(k, v) for k, v in mapping.items()},
            packages=stripped,
            time_str="2026-09-26 08:00:00",
            active_positions_detail=[], position_management=[], usdt_available=44.0)
        path = os.path.join(self.tmp.name, "jev_shadow_reviews.jsonl")
        with open(path, "r", encoding="utf-8") as handle:
            return json.loads(handle.read().strip().splitlines()[-1])


class PayloadTypeContractTest(_Harness):
    probabilities: Dict[str, float] = {}

    def test_no_choice_question_in_any_lane(self):
        """回归一：choice 会让 typesafe 直接 422，任何通道都不得再出现。"""
        self.run_review({"BTC-USDT-SWAP": "WAIT", "ETH-USDT-SWAP": "BUY_LONG"},
                        positions=[{"instId": "BTC-USDT-SWAP", "side": "long",
                                    "pos": 1.0, "avgPx": 100.0, "markPx": 101.0,
                                    "venue": "okx"}],
                        management=[{"instId": "BTC-USDT-SWAP", "action": "HOLD"}])
        self.assertEqual([r["channel"] for r in self.captured],
                         ["independent", "audit"], "两通道都应在同一轮发出")
        offenders = []
        for row in self.captured:
            for name, question in (row["payload"].get("questions") or {}).items():
                if question.get("type") not in _ACCEPTED_TYPES:
                    offenders.append((row["channel"], name, question.get("type")))
        self.assertEqual(offenders, [],
                         "payload 里出现了 provider 不接受的类型（typesafe 会 422）")

    def test_questions_are_declared_boolean_before_provider_conversion(self):
        """门只钉「发出时」的类型；声明侧应全部是 boolean（转换前的合法源头）。"""
        with open(abt.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('"type": "choice"', source,
                         "choice 声明又回来了 —— 它会让独立通道整体 422 全灭")


class WaitProposalIsNotAuditedTest(_Harness):
    probabilities: Dict[str, float] = {}

    def setUp(self):
        super().setUp()
        self.review = self.run_review(
            {"BTC-USDT-SWAP": "WAIT", "ETH-USDT-SWAP": "BUY_LONG"},
            positions=[{"instId": "BTC-USDT-SWAP", "side": "long", "pos": 1.0,
                        "avgPx": 100.0, "markPx": 101.0, "venue": "okx"}],
            management=[{"instId": "BTC-USDT-SWAP", "action": "HOLD"}])
        self.audit = self.questions("audit")
        self.by_inst = {r["instId"]: r for r in self.review["instrument_reviews"]}

    def test_wait_proposal_gets_no_audit_questions(self):
        """回归二：没有提议可审 ⇒ 一道题都不该问。"""
        leaked = [k for k in self.audit if k.startswith("candidate_0_")]
        self.assertEqual(leaked, [],
                         "WAIT 提议仍被送去审计 —— 这正是 98% 恒 REJECT 的成因")

    def test_entry_proposal_keeps_full_audit_question_set(self):
        """真入场提议必须照旧拿到完整审计题（修复不得把审计通道一起关掉）。"""
        asked = [k for k in self.audit if k.startswith("candidate_1_")]
        self.assertEqual(len(asked), _ENTRY_AUDIT_QUESTIONS,
                         f"入场提议审计题数变了：{sorted(asked)}")

    def test_wait_is_reported_not_applicable_not_reject(self):
        """WAIT 必须是显式 NOT_APPLICABLE，不得再冒充 AUDIT_REJECT。"""
        btc = self.by_inst["BTC-USDT-SWAP"]
        self.assertEqual(btc["jev_audit_verdict"], "NOT_APPLICABLE")
        self.assertEqual(btc["jev_relation_to_main"], "NOT_APPLICABLE")
        self.assertIsNone(btc["audit_proposal_complete"],
                          "未问过 proposal_complete 时不得编造一个值")
        self.assertEqual(btc["audit_flags"], [])

    def test_entry_still_gets_a_real_verdict(self):
        """入场提议仍必须落到三态之一（不得被 NOT_APPLICABLE 吞掉）。"""
        eth = self.by_inst["ETH-USDT-SWAP"]
        self.assertIn(eth["jev_audit_verdict"], {"APPROVE", "REVIEW", "REJECT"})
        self.assertIsInstance(eth["audit_flags"], list)


class AuditDataIsSelfDescribingTest(_Harness):
    """回归二的可观测性补丁：审计评分必须能自证"这一条到底有没有被审过"。

    2026-09-26 复盘时踩到的坑：审计通道 98% 输出恒 REJECT，看起来像"JEV 一贯
    否决主链"。真实原因是当时把 `main=WAIT`（entry/stop/target 全为 0）的提议也
    送去审计 —— 问"这个止损结构是否有效"，模型只能答"无效"。
    同轮对照（同一 state、同一次调用）：
        candidate#0-#6 WAIT  entry=0/sl=0        stop_ok 0.21~0.40
        candidate#7 BUY_LONG entry=13.68/sl=13.28 stop_ok 0.80
    汇总 650 条：有 entry/sl 的 stop_ok 中位数 0.79，没有的 0.21；complete 0.70 vs 0.07。

    根因已由「不再审计 WAIT」修掉。这里再钉住"数据自身能区分"，防止下一个人
    重算一遍同样被污染的均值。
    """

    probabilities = {
        "edge_present": 0.9, "execution_ready": 0.9,
        "would_buy_long": 0.8, "would_sell_short": 0.05, "would_wait": 0.15,
        "thesis_supported": 0.8, "direction_conflict": 0.1, "entry_is_chasing": 0.2,
        "stop_structurally_valid": 0.85, "reward_after_cost_sufficient": 0.8,
        "omits_counter_evidence": 0.2, "proposal_complete": 0.9,
    }

    def setUp(self):
        super().setUp()
        self.review = self.run_review({"BTC-USDT-SWAP": "WAIT",
                                       "ETH-USDT-SWAP": "BUY_LONG"})
        self.by_inst = {r["instId"]: r for r in self.review["instrument_reviews"]}

    def test_wait_candidate_carries_no_audit_probabilities(self):
        """WAIT 候选必须是 None —— 不能是"全 0 的分数"，否则又会被均值污染。"""
        btc = self.by_inst["BTC-USDT-SWAP"]
        self.assertIsNone(
            btc["audit_probabilities"],
            "未审计的候选必须显式为 None：任何数字（含 0）都会被下游当成评分")

    def test_entry_candidate_records_every_audit_probability(self):
        """真候选必须留下完整 7 题原值，便于事后独立核对（无需按 index 反查）。"""
        probs = self.by_inst["ETH-USDT-SWAP"]["audit_probabilities"]
        self.assertIsInstance(probs, dict)
        self.assertEqual(
            set(probs),
            {"thesis_supported", "direction_conflict", "entry_is_chasing",
             "stop_structurally_valid", "reward_after_cost_sufficient",
             "omits_counter_evidence", "proposal_complete"},
            f"审计原值集合不完整：{sorted(probs)}")
        self.assertAlmostEqual(probs["stop_structurally_valid"], 0.85, places=6)
        self.assertAlmostEqual(probs["proposal_complete"], 0.9, places=6)

    def test_missing_answer_uses_the_negative_sentinel_not_zero(self):
        """缺答案要用 -1.0 哨兵，不能塌成 0 —— 否则"没答"会被读成"强烈否定"。"""
        review = self.run_review({"ETH-USDT-SWAP": "BUY_LONG"})
        probs = review["instrument_reviews"][0]["audit_probabilities"]
        self.assertTrue(all(v >= -1.0 for v in probs.values()))
        self.assertTrue(all(isinstance(v, (int, float)) for v in probs.values()))

    def test_model_side_answers_keep_the_missing_sentinel(self):
        """A：`max(0.0, x)` 会把「没问过」压成 0.0，与「模型答 0.0」无法区分。

        同一失效模式曾污染审计通道（98% 恒 REJECT）。`audit_probabilities` 一直
        保留 -1.0 哨兵并有测试守护；这里把模型侧的其他答案
        （edge_probability / execution_ready_probability）也钉住。
        """
        # 测试装置总会回答所有问题，所以这里用局部 patch 造出「真的一个答案都没有」。
        empty = {"status": "ok",
                 "response": {"model": "fake", "answers": {}, "usage": {}},
                 "attempts": [{"attempt": 1, "status": "ok"}],
                 "request_id": "", "latency_ms": 1}
        with patch.object(abt, "_jev_shadow_request", lambda *a, **k: dict(empty)):
            review = self.run_review({"ETH-USDT-SWAP": "BUY_LONG"})
        row = review["instrument_reviews"][0]
        self.assertEqual(row["edge_probability"], -1.0,
                         "缺答必须是 -1.0 哨兵，不得塌成 0.0")
        self.assertEqual(row["execution_ready_probability"], -1.0)

    def test_wait_and_entry_are_separable_from_the_record_alone(self):
        """一条记录里就能看出谁被审计过 —— 不需要再去 join response.answers。"""
        audited = [inst for inst, r in self.by_inst.items()
                   if r["audit_probabilities"] is not None]
        self.assertEqual(audited, ["ETH-USDT-SWAP"],
                         "只有真入场候选应留下审计评分")


class IndependentStateLeakGuardTest(_Harness):
    """独立通道的 state 里绝不能出现主脑结论 —— 这是重构的立身之本。

    2026-09-26 复盘时发现：候选侧是真白名单（16 键元组），但持仓侧是
    「完整上下文减去 4 个具名 main_* 键」的黑名单，而注释却宣称两者都走白名单。
    本类把两个通道都钉成「结构上不可能泄漏」。
    """

    probabilities: Dict[str, float] = {}

    #: 任何通道的独立 state 里都不允许出现的叶子键名（主脑结论）。
    #:
    #: 注意 `margin_usdt` **不在**这个集合里：在持仓侧它是仓位事实（方案 §4.2
    #: 明确允许），只有在候选侧它才是主脑的下单意图。同一个键名在两侧语义不同，
    #: 所以候选侧另有专门断言，见 `test_candidates_carry_no_sizing_intent`。
    _FORBIDDEN = {
        "action", "confidence", "entry_price", "stop_loss_price",
        "take_profit_price", "risk_reward_ratio", "leverage", "entry_mode",
        "main_action", "main_confidence", "main_reason", "main_suggested_sl_price",
        "shadow_margin_usdt", "shadow_leverage",
        "shadow_margin_source", "shadow_leverage_source",
    }

    #: 候选侧专有的主脑意图键（在持仓侧是事实，故不能全 state 禁用）。
    _CANDIDATE_FORBIDDEN = _FORBIDDEN | {"margin_usdt"}

    @staticmethod
    def _walk(node: Any) -> List[str]:
        """收集所有字典键名（含嵌套），用于泄漏扫描。"""
        found: List[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                found.append(str(key))
                found.extend(IndependentStateLeakGuardTest._walk(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(IndependentStateLeakGuardTest._walk(item))
        return found

    def _independent_state(self) -> Dict[str, Any]:
        self.run_review(
            {"BTC-USDT-SWAP": "WAIT", "ETH-USDT-SWAP": "BUY_LONG"},
            positions=[{"instId": "BTC-USDT-SWAP", "side": "long", "pos": 1.0,
                        "avgPx": 100.0, "markPx": 101.0, "venue": "okx",
                        "main_action": "HOLD", "main_confidence": 91.0,
                        "main_reason": "主脑理由绝不外泄",
                        "main_suggested_sl_price": 97.5}],
            management=[{"instId": "BTC-USDT-SWAP", "action": "HOLD"}])
        path = os.path.join(self.tmp.name, "jev_shadow_reviews.jsonl")
        with open(path, "r", encoding="utf-8") as handle:
            return json.loads(handle.read().strip().splitlines()[-1])["independent_state"]

    def test_independent_state_carries_no_main_conclusion(self):
        """持仓侧同样必须是白名单：主脑结论不得出现在独立 state 任何层级。"""
        state = self._independent_state()
        leaked = sorted(set(self._walk(state)) & self._FORBIDDEN)
        self.assertEqual(leaked, [],
                         f"独立 state 泄漏了主脑结论字段：{leaked}")

    def test_candidates_carry_no_sizing_intent(self):
        """候选侧不得带主脑的下单意图（margin/leverage/entry/stop/target 等）。"""
        state = self._independent_state()
        offenders = []
        for candidate in (state.get("market") or {}).get("candidates") or []:
            offenders.extend(sorted(set(candidate) & self._CANDIDATE_FORBIDDEN))
        self.assertEqual(offenders, [],
                         f"候选泄漏了主脑下单意图：{sorted(set(offenders))}")

    def test_position_margin_is_still_visible_as_a_fact(self):
        """守卫不得把持仓的保证金事实一起误删（方案 §4.2 允许 margin_usdt）。"""
        state = self._independent_state()
        positions = state.get("positions") or []
        self.assertTrue(positions, "本用例应至少有一个持仓")
        self.assertIn("margin_usdt", positions[0],
                      "持仓侧 margin_usdt 是事实字段，必须保留")

    def test_audit_state_still_carries_the_main_proposal(self):
        """守卫不得把审计通道一起关掉 —— 审计本来就必须看到主脑提案。"""
        self.run_review({"ETH-USDT-SWAP": "BUY_LONG"})
        path = os.path.join(self.tmp.name, "jev_shadow_reviews.jsonl")
        with open(path, "r", encoding="utf-8") as handle:
            record = json.loads(handle.read().strip().splitlines()[-1])
        keys = set(self._walk(record["audit_state"]))
        self.assertIn("main_proposals", keys,
                      "审计 state 必须携带主脑提案")

    def test_position_whitelist_excludes_every_main_key(self):
        """白名单是显式常量：不得包含任何 main_* 键。"""
        offenders = sorted(k for k in abt._JEV_NEUTRAL_POSITION_KEYS
                           if k.startswith("main_"))
        self.assertEqual(offenders, [])


class IndependentActionStillDerivedFromVotesTest(_Harness):
    """去掉 choice 后，独立动作必须仍由票决（带 margin）推导。"""

    probabilities = {
        "edge_present": 0.95,
        "execution_ready": 0.95,
        "would_buy_long": 0.90,
        "would_sell_short": 0.05,
        "would_wait": 0.20,
    }

    def test_directional_call_survives_without_choice_question(self):
        review = self.run_review({"ETH-USDT-SWAP": "WAIT"})
        row = review["instrument_reviews"][0]
        self.assertNotIn("candidate_0_action", self.questions("independent"),
                         "choice 问题已被移除")
        self.assertEqual(row["suggested_action"], "BUY_LONG",
                         "票决优势方向必须仍能产出独立动作（否则通道退化成永远 WAIT）")
        self.assertGreaterEqual(row["jev_action_margin"], 0.15,
                                "动作间距应达到 R20_JEV_INDEPENDENT_MIN_ACTION_MARGIN")
        self.assertEqual(row["jev_action_status"], "accepted")

    def test_votes_are_still_present_and_recorded(self):
        review = self.run_review({"ETH-USDT-SWAP": "WAIT"})
        votes = review["instrument_reviews"][0]["suggested_action_votes"]
        self.assertEqual(set(votes), {"BUY_LONG", "SELL_SHORT", "WAIT"},
                         "三个兼容票决必须仍在记录里（choice 的信息并未丢失）")


class JevVerdictHelperTest(unittest.TestCase):
    def test_audit_verdict_not_applicable_short_circuits(self):
        got = abt._jev_audit_verdict(["direction_conflict"], has_proposal=False)
        self.assertEqual(got["verdict"], "NOT_APPLICABLE")
        self.assertEqual(got["flags"], [],
                         "没有提议时不得把传入的 flag 带出去")

    def test_policy_relation_maps_not_applicable(self):
        self.assertEqual(
            abt._jev_policy_relation("WAIT", "BUY_LONG", data_status="valid",
                                     audit_verdict_value="NOT_APPLICABLE"),
            "NOT_APPLICABLE")

    def test_not_applicable_is_not_a_reject(self):
        for main in ("WAIT", "BUY_LONG"):
            got = abt._jev_policy_relation(main, "WAIT", data_status="valid",
                                           audit_verdict_value="NOT_APPLICABLE")
            self.assertNotEqual(got, "AUDIT_REJECT",
                                "NOT_APPLICABLE 绝不能被算成审计否决")


if __name__ == "__main__":
    unittest.main()
