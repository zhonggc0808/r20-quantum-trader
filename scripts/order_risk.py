"""Shared deterministic safety checks for trade quotes and risk gates.
No state, no price repair. The R:R floor comes from the single source of truth
in scripts/risk_constants.py (configurable via the admin risk-control page / .env).
"""
from __future__ import annotations

import math
from typing import Any, Tuple

try:
    from scripts.risk_constants import MIN_RISK_REWARD_RATIO, MAX_RISK_REWARD_RATIO
except ImportError:  # flat import when scripts/ itself is on sys.path
    from risk_constants import MIN_RISK_REWARD_RATIO, MAX_RISK_REWARD_RATIO


def validate_quote_geometry_and_rr_detailed(
    action: str,
    entry: Any,
    tp: Any,
    sl: Any,
    enforce_max_rr: bool = False,
    confidence: float = 0.0,
    min_rr_floor: float = 0.0,
) -> Tuple[bool, str, float, str]:
    """Validate geometry and return a stable structured failure code.

    Quotes below the configured R:R floor may pass only when confidence is at
    least 80%, R:R remains above the absolute/minimum floor, and expected value
    is at least +0.30R.
    """
    raw_act = str(action or "").upper()
    if raw_act not in {"BUY_LONG", "SELL_SHORT"}:
        return False, f"不支持的开仓方向: {action}", 0.0, "unsupported_action"

    try:
        e = float(entry)
        t = float(tp)
        s = float(sl)
    except (TypeError, ValueError, OverflowError):
        return (False, "核心风控拦截：入场价、止盈价、止损价必须是有效数字",
                0.0, "quote_parse_error")

    if not (math.isfinite(e) and math.isfinite(t) and math.isfinite(s)):
        return (False, "核心风控拦截：入场价、止盈价、止损价必须是有限数值 (NaN/Inf 拒绝)",
                0.0, "quote_parse_error")

    if e <= 0 or t <= 0 or s <= 0:
        return (False, "核心风控拦截：入场价、止盈价、止损价必须大于 0",
                0.0, "quote_parse_error")

    if raw_act == "BUY_LONG":
        if not (s < e < t):
            return (False,
                    f"核心风控拦截：买多几何不合法 (须 止损 {s} < 限价 {e} < 止盈 {t})",
                    0.0, "quote_geometry_invalid")
        risk = e - s
        reward = t - e
    else:
        if not (t < e < s):
            return (False,
                    f"核心风控拦截：卖空几何不合法 (须 止盈 {t} < 限价 {e} < 止损 {s})",
                    0.0, "quote_geometry_invalid")
        risk = s - e
        reward = e - t

    if risk <= 0:
        return (False, "核心风控拦截：单笔承担风险必须大于 0",
                0.0, "risk_unit_invalid")

    rr = reward / risk
    if not math.isfinite(rr):
        return (False, "核心风控拦截：盈亏比计算异常",
                0.0, "rr_calculation_error")

    absolute_min_rr = 1.2
    effective_floor = max(absolute_min_rr, float(min_rr_floor or 0.0))
    if rr < MIN_RISK_REWARD_RATIO:
        try:
            conf_val = float(confidence or 0.0)
        except (TypeError, ValueError, OverflowError):
            conf_val = 0.0
        expected_val = -math.inf
        if conf_val >= 80.0 and rr >= effective_floor:
            p_win = conf_val / 100.0
            expected_val = p_win * rr - (1.0 - p_win)
        if expected_val < 0.30:
            return (False,
                    f"核心风控拦截：盈亏比不足 {MIN_RISK_REWARD_RATIO:.1f} "
                    f"(当前 R:R = {rr:.2f}:1，底线 {MIN_RISK_REWARD_RATIO:.1f}:1)",
                    rr, "rr_below_floor")

    if enforce_max_rr:
        current_max_rr = float(MAX_RISK_REWARD_RATIO or 0.0)
        if current_max_rr > 0 and rr > current_max_rr + 1e-4:
            return (False,
                    f"核心风控拦截：盈亏比超出上限 {current_max_rr:.1f}:1 "
                    f"(当前 R:R = {rr:.2f}:1，止盈过远拒单)",
                    rr, "rr_above_ceiling")

    return True, "", rr, ""


def validate_quote_geometry_and_rr(
    action: str,
    entry: Any,
    tp: Any,
    sl: Any,
    enforce_max_rr: bool = False,
    confidence: float = 0.0,
    min_rr_floor: float = 0.0,
) -> Tuple[bool, str, float]:
    """Backward-compatible three-value quote validation contract."""
    valid, reason, rr, _code = validate_quote_geometry_and_rr_detailed(
        action,
        entry,
        tp,
        sl,
        enforce_max_rr=enforce_max_rr,
        confidence=confidence,
        min_rr_floor=min_rr_floor,
    )
    return valid, reason, rr
