"""受保护限价单提交（下单主路径）（B3 抽取·trader 瘦身第九刀，第八十八刀）。

从 `scripts/ai_factor_trader.py` **纯搬家** `submit_protected_limit_order`（198 行）。

## 这一刀为什么最敏感

它是唯一真正**落单**的函数：决策面前置闸（选所路由 + 预算原子预留）→
US-007 环境维合约存在性对账 → 价格锚定 → **入场价穿价幻觉闸** → demo rescale
→ 多所平权分发。审计④ 的"穿价幻觉拒单/回踩远挂放行"锚点全在本函数体内。

## 同名注入（11 项）

`route_and_reserve_signal` / `confirm_signal_reservation` /
`release_signal_reservation` / `record_open_intent` / `okx_rest` / `fetch_ticker` /
`venue_registry` / `canonical_base` / `current_environment` / `MAX_LEVERAGE` /
`MIN_LEVERAGE` 同名注入 ⇒ 函数体 AST **零例外全等**（`os` 由本模块自 import）。

⚠️ **源码锚点已同步**：`tests/audit/test_audit_batch2_risk_gates_live.py::
TestPriceSanityAnchor::test_guard_code_landed_in_submit_path` 原用
`inspect.getsource(aft)`（整门面）扫三段文本并检查**先后顺序**
（几何复验 < 穿价闸 < 多所平权分发）——搬壳后门面里这三段一个都不在，
锚改为扫**该域**（`tests/source_scan.combined(..., pkg_name="trader")`），
并先断言实现确实住在 `order_submit.py`，顺序判据原样保留。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple


def _cancel_okx_order_after_intent_failure(okx_rest, inst_id: str,
                                           order_id: str) -> Tuple[bool, str]:
    """撤销已接受的 OKX 入场单，并用挂单回读确认它已离开在途列表。"""
    try:
        response = okx_rest.cancel_order(inst_id, str(order_id))
    except Exception as exc:
        return False, f"OKX 撤单请求失败({exc})"
    rows = response if isinstance(response, list) else [response]
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get("sCode") or row.get("code")
        if code not in (None, "", 0, "0"):
            return False, f"OKX 撤单被拒({row.get('sMsg') or row.get('msg') or code})"
    try:
        pending = okx_rest.pending_orders(inst_id)
    except Exception as exc:
        return False, f"OKX 撤单请求已发送但最终状态回读失败({exc})"
    for row in pending or []:
        if not isinstance(row, dict):
            continue
        current_id = row.get("ordId") or row.get("orderId") or row.get("id")
        if str(current_id or "") == str(order_id):
            return False, "OKX 入场单回读仍在途"
    history_fn = getattr(okx_rest, "orders_history", None)
    if callable(history_fn):
        try:
            history = history_fn(inst_id=inst_id) or []
        except Exception as exc:
            return False, f"OKX 撤单后历史状态回读失败({exc})"
        for row in history:
            if not isinstance(row, dict):
                continue
            current_id = row.get("ordId") or row.get("orderId") or row.get("id")
            if str(current_id or "") != str(order_id):
                continue
            state = str(row.get("state") or row.get("status") or "").lower()
            if state in {"filled", "partially_filled", "partial"}:
                return False, f"OKX 入场单已成交({state})"
            if state in {"canceled", "cancelled", "expired"}:
                return True, f"OKX 入场单已确认终态({state})"
            return False, f"OKX 入场单历史状态未确认({state or 'unknown'})"
        return False, "OKX 入场单不在途但未找到可确认的历史终态"
    return True, "OKX 入场单已确认离开在途列表"


def submit_protected_limit_order(inst_id: str, side: str, pos_side: str, size: float, price: float, tp_px: float, sl_px: float, venue_ctx: Optional[Dict[str, Any]] = None,
    *,
    confirm_signal_reservation,
    record_open_intent,
    release_signal_reservation,
    route_and_reserve_signal,
    MAX_LEVERAGE,
    MIN_LEVERAGE,
    canonical_base,
    current_environment,
    fetch_ticker,
    okx_rest,
    venue_registry) -> Tuple[bool, str]:
    """Submit a protected limit order; acceptance is not treated as a fill.

    venue_ctx：US-003 决策面上下文（AI 信号入口单必须带）。带上下文 → 先过选所路由
    + 预算原子预留，任一失败返回 (False, "路由拒绝/预算预留拒绝: <reason>")，本轮
    不下单；不带上下文 = 非 AI 信号的通用提交（保留 US-007 listing gate 契约），
    只 warn 不闸门——新增开仓路径时必须传 ctx。
    """
    env = current_environment()  # 审计 C2：冻结周期环境单源（同 close 路径）
    _reservation = None
    target_venue = "okx"
    if isinstance(venue_ctx, dict):
        # ---- US-003 决策面前置闸：选所路由 + 预算原子预留（失败即本轮不下单）----
        _routing = route_and_reserve_signal(
            inst_id, side, size, price,
            notional_usdt=float(venue_ctx.get("notional_usdt") or 0.0),
            margin_usdt=float(venue_ctx.get("margin_usdt") or 0.0),
            intent_id=str(venue_ctx.get("intent_id") or ""))
        if not _routing["ok"]:
            return False, str(_routing.get("error") or "路由拒绝")
        _reservation = _routing.get("reservation")
        target_venue = str(_routing.get("venue") or "okx").lower()
    else:
        print(f"[US-003 决策面] warn {inst_id} 提交未携带 venue_ctx——"
              f"未经选所路由/预算预留，仅限非 AI 信号通用路径")

    # 环境维合约存在性对账（US-007）：目录拉不到 → fail-closed 拒绝；
    # 已下架/未上市（如 SUI 在 demo 被下架）→ fail-closed 拒单，reason 透传。
    #
    # Listing Gate Parity（三所平权命门）：inst_id 是 OKX 形态（BTC-USDT-SWAP），而
    # binance 目录键是 BTCUSDT、gate 是 BTC_USDT——直接拿 inst_id 去外所目录对账必然
    # 查不到 → 误判「沙盒未上市」，导致非 OKX 所一单都开不了。对账前必须先经
    # native_symbol_pure 翻译成目标所原生合约码（纯元数据，绝不实例化适配器→零出网）。
    try:
        from r20_backend.exchanges.listing import ensure_contract_listed
        native_contract = venue_registry.native_symbol_pure(
            canonical_base(inst_id), target_venue)
        _check = ensure_contract_listed(target_venue, "demo" if env.simulated else "live", native_contract)
        if not _check.ok:
            print(f"[listing gate] 拒绝下单 {inst_id}→{native_contract} ({target_venue}): {_check.reason}")
            release_signal_reservation(_reservation, "合约对账拒绝")
            return False, f"合约对账拒绝: {_check.reason}"
    except Exception as _le:
        release_signal_reservation(_reservation, f"合约对账异常: {_le}")
        return False, f"合约目录对账不可用，拒绝下单: {_le}"
    # Check if we are running in simulated/demo mode and price diverged significantly from demo orderbook
    effective_px = price
    effective_tp = tp_px
    effective_sl = sl_px

    # 审计④(2026-09-13)：现价单次读取，demo rescale 与幻觉锚共用——两次读可互相
    # 错位，且旧代码只有 simulated+okx 才取价，live/外所永远拿不到锚（裸奔真身）。
    _tick_last_raw = None
    _anchor_last = 0.0
    if target_venue == "okx":
        try:
            _tick_last_raw = (fetch_ticker(inst_id) or {}).get("last")
            if _tick_last_raw:
                _anchor_last = float(_tick_last_raw)
        except Exception as _ae:
            release_signal_reservation(_reservation, f"现价锚定异常: {_ae}")
            return False, f"现价锚定不可用，拒绝下单: {_ae}"
        if _anchor_last <= 0:
            release_signal_reservation(_reservation, "现价锚定缺失")
            return False, "现价锚定缺失，拒绝下单"

    if env.simulated and target_venue == "okx":
        try:
            demo_last = _anchor_last
            if demo_last:
                if demo_last > 0 and price > 0:
                    divergence = abs(price - demo_last) / demo_last
                    # If live market price diverged from demo sandbox by more than 5% (e.g. ASTER / illiquid demo pair)
                    if divergence > 0.05:
                        scale = demo_last / price
                        prec = len(str(_tick_last_raw).split(".")[1]) if "." in str(_tick_last_raw) else 4
                        effective_px = round(price * scale, prec)
                        effective_tp = round(tp_px * scale, prec)
                        effective_sl = round(sl_px * scale, prec)
                        # Re-verify boundary constraints for demo sandbox
                        if pos_side == "long":
                            if effective_sl >= effective_px:
                                effective_sl = round(effective_px * 0.98, prec)
                            if effective_tp <= effective_px:
                                effective_tp = round(effective_px * 1.04, prec)
                        else:
                            if effective_sl <= effective_px:
                                effective_sl = round(effective_px * 1.02, prec)
                            if effective_tp >= effective_px:
                                effective_tp = round(effective_px * 0.96, prec)
        except Exception:
            pass

    # Final Non-Bypassable Verification: verify actual effective price, tp and sl
    from scripts.order_risk import validate_quote_geometry_and_rr
    action_type = "BUY_LONG" if pos_side == "long" else "SELL_SHORT"
    is_valid, reason, _ = validate_quote_geometry_and_rr(action_type, effective_px, effective_tp, effective_sl)
    if not is_valid:
        print(f"[Order Rejected] 最终有效开仓报价未通过核心安全复验: {reason} (px={effective_px}, tp={effective_tp}, sl={effective_sl})")
        release_signal_reservation(_reservation, "核心安全复验拒绝")
        return False, f"最终订单核心安全复验拒绝: {reason}"

    # 审计④(2026-09-13)：LLM 幻觉入场价锚定——几何/R:R 只验 entry/tp/sl 相互关系，
    # 从不比对现价。危险形态是「穿价」：BUY 限价挂在现价上方 → 即时成交于意外价，
    # 而配套 SL 触发价锚在幻觉 entry 上、相对真实成交价可能即刻触发 → 开-秒平循环
    # 放血（demo+okx 有 5% rescale 兜底，live 与外所此前裸奔）。回踩方向的远挂单
    # 是合法策略（不穿价即放行，OKX 侧 4 分钟超时撤兜底）。_anchor_last 来自上方
    # 单次读价；取价失败不阻断（行情断时黑天鹅哨兵/熔断已另行 fail-closed），但必吼。
    if _anchor_last > 0 and effective_px > 0:
        _cross_pct = float(os.getenv("R20_MAX_PRICE_CROSS_PCT", "0.005") or 0.005)
        _far_pct = float(os.getenv("R20_MAX_PRICE_FAR_PCT", "0.50") or 0.50)
        if action_type == "BUY_LONG" and effective_px > _anchor_last * (1.0 + _cross_pct):
            _rej = f"入场价穿价幻觉：BUY 限价 {effective_px:g} 高于现价 {_anchor_last:g} 超阈值({max(0.0,(effective_px/_anchor_last-1)*100):.2f}%>{_cross_pct*100:.1f}%)，将即时成交于意外价且 SL 锚点失真"
            print(f"[价格锚定] 拒单 {inst_id}: {_rej}")
            release_signal_reservation(_reservation, "价格锚定拒绝")
            return False, f"价格锚定拒绝: {_rej}"
        if action_type == "SELL_SHORT" and effective_px < _anchor_last * (1.0 - _cross_pct):
            _rej = f"入场价穿价幻觉：SELL 限价 {effective_px:g} 低于现价 {_anchor_last:g} 超阈值({max(0.0,(1-effective_px/_anchor_last)*100):.2f}%>{_cross_pct*100:.1f}%)，将即时成交于意外价且 SL 锚点失真"
            print(f"[价格锚定] 拒单 {inst_id}: {_rej}")
            release_signal_reservation(_reservation, "价格锚定拒绝")
            return False, f"价格锚定拒绝: {_rej}"
        if abs(effective_px - _anchor_last) / _anchor_last > _far_pct:
            _rej = f"入场价与现价距离 {abs(effective_px/_anchor_last-1)*100:.1f}% 超荒谬阈值 {_far_pct*100:.0f}%，判定为幻觉报价拒单"
            print(f"[价格锚定] 拒单 {inst_id}: {_rej}")
            release_signal_reservation(_reservation, "价格锚定拒绝")
            return False, f"价格锚定拒绝: {_rej}"

    # 多所平权执行：若路由选定 Gate 或 Binance，走统一原生受保护执行路由
    if target_venue in ("gate", "binance"):
        _execution_result = None
        try:
            from r20_backend import execution_router
            asset_canonical = str(inst_id).split("-")[0].upper()
            default_lever = float(MIN_LEVERAGE or 3.0)
            margin_val = float(venue_ctx.get("margin_usdt") or (size * price / default_lever)) if isinstance(venue_ctx, dict) else (size * price / default_lever)
            lever_val = float(venue_ctx.get("leverage") or default_lever) if isinstance(venue_ctx, dict) else default_lever
            min_lev = float(MIN_LEVERAGE or 1.0)
            max_lev = float(MAX_LEVERAGE or 20.0)
            if lever_val < min_lev:
                release_signal_reservation(_reservation, "模型杠杆低于执行下限")
                return False, f"杠杆 {lever_val:g}x 低于执行下限 {min_lev:g}x，拒绝下单"
            lever_val = min(max_lev, lever_val)

            res = execution_router.open_protected_position({
                "venue": target_venue,
                "asset": asset_canonical,
                "action": action_type,
                "margin_usdt": margin_val,
                # 审计 P0-1：把权益占比顶一并下传，router 侧再兜一层（本处已夹过）
                "max_margin_usdt": float(venue_ctx.get("max_margin_usdt") or 0.0),
                "leverage": lever_val,
                "entry_price": effective_px,
                "take_profit_price": effective_tp,
                "stop_loss_price": effective_sl,
                "environment": str(env.mode),
                # 审计 P1-7：per-venue min_confidence 生效所需的原始 AI 置信度（缺失=不做该检查）
                "confidence": float(venue_ctx.get("confidence") or 0.0) if isinstance(venue_ctx, dict) else 0.0,
            }, environment=str(env.mode))
            _execution_result = res
            if not res.get("ok"):
                detail = res.get("detail") or "多所执行路由拒绝"
                release_signal_reservation(_reservation, detail)
                return False, f"{target_venue.upper()} 下单失败: {detail}"

            order_id = str(res.get("order_id") or res.get("tp_id") or f"{target_venue}-ok")
            _intent_recorded = record_open_intent(
                inst_id, side, metadata={
                    "order_id": order_id,
                    "decision_id": (venue_ctx or {}).get("decision_id"),
                    "cycle_id": (venue_ctx or {}).get("cycle_id"),
                    "pos_side": pos_side,
                    "venue": target_venue,
                    "requested_price": effective_px,
                    "size": size,
                    "intent_id": (venue_ctx or {}).get("intent_id"),
                })
            if _intent_recorded is False:
                _rollback = execution_router.rollback_open_position(
                    res, environment=str(env.mode))
                _detail = "本地开仓意图落盘失败"
                if _rollback.get("confirmed"):
                    release_signal_reservation(_reservation, _detail)
                else:
                    _detail += "；交易所补偿回滚未完全确认，风险预留保留待对账"
                return False, f"{_detail}: {'；'.join(_rollback.get('notes') or [])}"
            confirm_signal_reservation(_reservation)
            return True, order_id
        except Exception as exc:
            if isinstance(_execution_result, dict) and _execution_result.get("ok"):
                _rollback = execution_router.rollback_open_position(
                    _execution_result, environment=str(env.mode))
                _reason = f"多所执行异常: {exc}"
                if _rollback.get("confirmed"):
                    release_signal_reservation(_reservation, _reason)
                else:
                    _reason += "；补偿回滚未完全确认，风险预留保留待对账"
                return False, f"{_reason}: {'；'.join(_rollback.get('notes') or [])}"
            release_signal_reservation(_reservation, f"多所执行异常: {exc}")
            return False, f"{target_venue.upper()} 执行异常: {exc}"

    # 审计④5(2026-09-13)：OKX 直下路径从不落 AI 裁决杠杆——张数按 ai_lever 折算，
    # 但账户档位不变 → 实际保证金/强平价按旧档算，风险模型与实况脱节（净模式或
    # 10x 旧档可把 3x 计划仓的强平价拉得极近）。best-effort 发单前对齐档位：失败仅
    # warn 不阻断（保护腿/张数/几何已定，杠杆只影响保证金效率，绝不因此裸奔）。
    _want_lever = 0.0
    if isinstance(venue_ctx, dict):
        try:
            _want_lever = float(venue_ctx.get("leverage") or 0.0)
        except (TypeError, ValueError):
            _want_lever = 0.0
    if _want_lever > 0:
        _want_lever = max(1.0, min(_want_lever, float(MAX_LEVERAGE or 20.0)))
        try:
            okx_rest.set_leverage(inst_id, int(_want_lever), mgn_mode="cross",
                                  pos_side=(pos_side or None))
        except Exception as lev_exc:
            release_signal_reservation(_reservation, f"杠杆设置失败: {lev_exc}")
            return False, f"杠杆设置失败，拒绝下单: {lev_exc}"

    try:
        rows = okx_rest.place_order(
            inst_id, side, f"{size:g}",
            pos_side=pos_side, td_mode="cross", ord_type="limit",
            px=effective_px, attach_tp=effective_tp, attach_sl=effective_sl,
        )
    except Exception as exc:
        release_signal_reservation(_reservation, "下单异常")
        return False, str(exc)
    order_id = None
    for row in rows:
        order_id = row.get("ordId") or row.get("orderId")
        if order_id:
            break
    if not order_id:
        release_signal_reservation(_reservation, "交易所未返回可核验订单号")
        return False, "exchange accepted response without a verifiable order id"
    _intent_recorded = record_open_intent(
        inst_id, side, metadata={
                    "order_id": order_id,
                    "decision_id": (venue_ctx or {}).get("decision_id"),
                    "cycle_id": (venue_ctx or {}).get("cycle_id"),
                    "pos_side": pos_side,
                    "venue": target_venue,
                    "requested_price": effective_px,
                    "size": size,
                    "intent_id": (venue_ctx or {}).get("intent_id"),
                })
    if _intent_recorded is False:
        _cancelled, _cancel_note = _cancel_okx_order_after_intent_failure(
            okx_rest, inst_id, str(order_id))
        if _cancelled:
            release_signal_reservation(_reservation, "本地开仓意图落盘失败，已补偿撤单")
        else:
            print(f"[下单补偿] {inst_id} 本地开仓意图落盘失败，{_cancel_note}；风险预留保留待对账")
        return False, f"本地开仓意图落盘失败；{_cancel_note}"
    confirm_signal_reservation(_reservation)
    return True, str(order_id)
