"""AI 批次决策的「持仓全景」装配（结构优化阶段 4·B3 第三十一刀）。

原样搬自 `scripts/ai_factor_trader.py::execute_portfolio` 里构造
`active_pos_list` 的两段（共 45 行）：

1. `collect_okx_position_payloads` —— 从因子快照里摘出 OKX 在仓，补上追踪器
   （trailingStopPx / highWaterMark / lowWaterMark / takeProfitPx / stage_desc）与 ATR；
2. `merge_cross_venue_positions` —— 汇入 Binance / Gate 的在管持仓，
   形成**三所平权持仓全景**。

## 为什么这两段值得单独抽

它们决定**送给 AI 的持仓全景是什么样**。AI 看到的"持仓"与真实持仓不一致，
就会基于不存在的仓位做决策 —— 这类错误不会报错，只会让模型持续误判。

## 四处易错点（均原样保留）

1. **OKX 侧的 `venue` 用 `setdefault` 补默认值**，不是无条件覆盖 ——
   因子快照里若已带 `venue`，必须尊重原值。
2. **追踪器键是 `f"{instId}_{side}"`**，而 `side` 取自 `position.get('side','')`
   （**不是**规范化后的值）。键拼错就静默拿不到追踪器 → 止损/水位全变 `None`
   → 提示词里显示"无止损线"。
3. **外所的 `instId` 是合成串 `f"{VENUE_UPPER}:{inst}"`**，不是原始 id ——
   要与 OKX 侧的 `BTC-USDT-SWAP` 区分开，避免 AI 把两所同一标的混为一谈。
4. **`base` 用 `p.get("base")` 回退 `inst_id`**，且**匹配因子用的是 `name`**
   （不是 instId）→ `match_f` 找不到时给 `{}`，ATR 取默认 `0.0`。
   另外 `size_signed` 取 `abs`（`pos` 恒为正，方向由 `side` 表达）。

## 边界与失败语义

`merge_cross_venue_positions` 整体包在 `try/except` 里，**外所汇入失败不影响
OKX 主路径**（打印告警后继续）。`collect_okx_position_payloads` 则**不吞异常** ——
原实现也没有吞（它在更外层的 `try` 里）。

## 与门面的分工

纯数据装配：只吃 `all_factors` / `trackers` / 外所快照，无 I/O、无全局状态。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["collect_okx_position_payloads", "merge_cross_venue_positions"]


def collect_okx_position_payloads(all_factors: List[Dict[str, Any]],
                                  trackers: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从因子快照摘出在仓，补上追踪器字段与 ATR。

    - 无 `position` 的因子项跳过；
    - `venue` 用 `setdefault`（**尊重**因子快照里已有的值）；
    - 追踪器键为 `f"{instId}_{position.get('side','')}"`；
    - 追踪器缺失时字段为 `None`（`stage_desc` 例外，给 `""`）。
    """
    active_pos_list: List[Dict[str, Any]] = []
    for f in all_factors:
        position = f.get("position")
        if not position:
            continue
        position_payload = dict(position)
        position_payload.setdefault("venue", "okx")
        tracker = trackers.get(f"{f['instId']}_{position.get('side', '')}", {})
        position_payload["trailingStopPx"] = tracker.get("trailingStopPx")
        position_payload["highWaterMark"] = tracker.get("highWaterMark")
        position_payload["lowWaterMark"] = tracker.get("lowWaterMark")
        position_payload["takeProfitPx"] = tracker.get("takeProfitPx")
        position_payload["stage_desc"] = tracker.get("stage_desc", "")
        # Keep the decision identity on the live-position snapshot so the Jev
        # entry counterfactual can distinguish a real fill from a same-symbol
        # position opened by another cycle.
        position_payload["cycle_id"] = tracker.get("cycle_id", "")
        position_payload["decision_id"] = tracker.get("decision_id", "")
        position_payload["entry_order_id"] = tracker.get("entry_order_id")
        position_payload["entry_intent_id"] = tracker.get("entry_intent_id")
        position_payload["entry_order_ts"] = tracker.get("entry_order_ts")
        position_payload["entry_order_avg_px"] = tracker.get("entry_order_avg_px")
        position_payload["entry_order_fill_sz"] = tracker.get("entry_order_fill_sz")
        position_payload["entry_order_source"] = tracker.get("entry_order_source")
        position_payload["entry_identity_status"] = tracker.get("entry_identity_status")
        position_payload["entry_venue"] = tracker.get("entry_venue", "okx")
        position_payload["entry_time"] = tracker.get("entryTime")
        position_payload["entryTime"] = tracker.get("entryTime")
        position_payload["entryTs"] = tracker.get("entryTs")
        position_payload["atr"] = f.get("atr", 0.0)
        position_payload["ctVal"] = f.get("ctVal", position_payload.get("ctVal", 1.0))
        position_payload["bidPx"] = f.get("bidPx", position_payload.get("bidPx"))
        position_payload["askPx"] = f.get("askPx", position_payload.get("askPx"))
        active_pos_list.append(position_payload)
    return active_pos_list


def merge_cross_venue_positions(active_pos_list: List[Dict[str, Any]],
                                xv_positions_by_venue: Optional[Dict[str, Any]],
                                all_factors: List[Dict[str, Any]]) -> None:
    """把外所（Binance / Gate）在管持仓**原地追加**进 `active_pos_list`。

    **整体失败不影响 OKX 主路径**：任何异常都只打印告警后继续
    （外所不可用时提示词里少几条外所仓位，好过整个周期中断）。

    ⚠️ 审计(2026-09-13)：调用方必须传**已冻结的周期快照**。旧实现在主循环内
    **重新拉取**外所快照 —— 同一周期两次读取既重复出网、又可在瞬时不一致里
    撕裂展示（18:00 实锤日志双份打印）。
    """
    try:
        _xv_snap = xv_positions_by_venue
        if _xv_snap:
            for v_name, v_rows in _xv_snap.items():
                for p in v_rows:
                    base = str(p.get("base") or "").upper()
                    inst = str(p.get("inst_id") or base)
                    side = str(p.get("side") or "net").lower()
                    match_f = next((x for x in all_factors if x.get("name") == base), {})
                    active_pos_list.append({
                        "venue": v_name,
                        "instId": f"{v_name.upper()}:{inst}",
                        "name": base,
                        "side": side,
                        "pos": abs(float(p.get("size_signed") or 0)),
                        "avgPx": float(p.get("entry_price") or 0),
                        "markPx": float(p.get("mark_price") or 0),
                        "ctVal": float(p.get("ct_val") or 1.0),
                        "margin": float(p.get("margin") or 0),
                        "upl": float(p.get("unrealized_pnl") or 0),
                        "atr": match_f.get("atr", 0.0),
                        "leverage": float(p.get("leverage") or 0),
                    })
    except Exception as _xv_e:
        print(f"[三所持仓全景] 外所持仓汇入异常: {_xv_e}")
