"""Deterministic directional momentum vetoes shared by brain and Jev lanes."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate_directional_momentum_gate(
    package: Mapping[str, Any], action: Any,
) -> tuple[bool, str, str, dict[str, Any]]:
    """Reject entries that fight an accelerating directional regime.

    Missing calculus fields are observationally neutral here. Data completeness is
    owned by the existing core gate; this helper only vetoes affirmative evidence.
    """
    normalized_action = str(action or "WAIT").strip().upper()
    calculus = package.get("calculus")
    if not isinstance(calculus, Mapping):
        calculus = package.get("calculus_dynamics")
    if not isinstance(calculus, Mapping):
        calculus = {}

    probabilities = calculus.get("probability_theory")
    if not isinstance(probabilities, Mapping):
        probabilities = package.get("probability_theory")
    if not isinstance(probabilities, Mapping):
        probabilities = {}

    regime = str(calculus.get("regime") or "").strip().upper()
    power_regime = str(calculus.get("power_regime") or "").strip().upper()
    acceleration = _finite_float(calculus.get("acceleration"))
    power = _finite_float(calculus.get("power"))
    continuation = _finite_float(probabilities.get("continuation_prob_pct"))
    breakdown = _finite_float(probabilities.get("breakdown_prob_pct"))
    evidence = {
        "action": normalized_action,
        "regime": regime or None,
        "acceleration": acceleration,
        "power": power,
        "power_regime": power_regime or None,
        "continuation_prob_pct": continuation,
        "breakdown_prob_pct": breakdown,
    }

    if normalized_action == "BUY_LONG":
        if regime == "BEAR_ACCELERATING":
            return (
                False,
                "bear_acceleration_blocks_long",
                "方向动量门控：空头处于加速阶段，禁止逆势追多。",
                evidence,
            )
        if (
            breakdown is not None and breakdown >= 65.0
            and continuation is not None and continuation <= 35.0
            and acceleration is not None and acceleration < -0.30
        ):
            return (
                False,
                "breakdown_dominance_blocks_long",
                "方向动量门控：击穿概率占优且下行加速度扩大，禁止追多。",
                evidence,
            )

    if normalized_action == "SELL_SHORT":
        if regime == "BULL_ACCELERATING":
            return (
                False,
                "bull_acceleration_blocks_short",
                "方向动量门控：多头处于加速阶段，禁止逆势开空。",
                evidence,
            )
        if (
            acceleration is not None and acceleration > 0.0
            and power is not None and power > 0.0
        ):
            return (
                False,
                "positive_momentum_blocks_short",
                "方向动量门控：正加速度与正动能同时成立，禁止开空。",
                evidence,
            )

    return True, "", "", evidence
