"""多周期方向观测：只写观测字段，绝不决定下单或拦截。"""
from __future__ import annotations

import math
from typing import Any

_CANDLE_TIMESTAMPS: dict[tuple[str, str], int] = {}

SCHEMA_VERSION = 1
LEGACY_CALCULUS_VERSION = "equal_weight_v1"


def remember_candle_timestamp(inst_id: str, bar: str, candles: Any) -> None:
    stamp = candle_timestamp(candles)
    if stamp is not None:
        _CANDLE_TIMESTAMPS[(str(inst_id), str(bar))] = stamp


def latest_candle_timestamp(inst_id: str, bar: str) -> int | None:
    return _CANDLE_TIMESTAMPS.get((str(inst_id), str(bar)))


def candle_timestamp(candles: Any) -> int | None:
    """返回最新 K 线毫秒时间戳；无法验证时显式返回 None。"""
    try:
        stamp = int(candles[0][0])
        return stamp if stamp > 0 else None
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def four_hour_range(candles: Any, price: Any, *, lookback: int = 12) -> dict:
    """用最近 12 根已收盘 4H K 线的最高/最低价测位置；未收盘 K 线不参与画箱。"""
    out = {"range_4h_high": None, "range_4h_low": None,
           "range_4h_width": None, "price_position_in_range": None,
           "range_4h_bars": 0, "range_4h_candle_ts": candle_timestamp(candles)}
    try:
        price = float(price)
        rows = list(candles or [])
        # OKX confirm=1 表示已收盘；无 confirm 的输入按最新一根尚未收盘处理。
        closed = [c for c in rows if len(c) > 8 and str(c[8]) == "1"] if any(
            len(c) > 8 for c in rows) else rows[1:]
        if not math.isfinite(price) or price <= 0 or len(closed) < lookback:
            return out
        selected = closed[:lookback]
        high = max(float(c[2]) for c in selected)
        low = min(float(c[3]) for c in selected)
        if not all(math.isfinite(v) for v in (high, low)) or low <= 0 or high <= low:
            return out
        out.update(range_4h_high=high, range_4h_low=low,
                   range_4h_width=high - low,
                   price_position_in_range=(price - low) / (high - low),
                   range_4h_bars=lookback)
    except (TypeError, ValueError, IndexError):
        pass
    return out


def direction_layers(calculus: Any) -> dict:
    """新增分层字段，旧的等权聚合值保持原样，缺一层明确为不可用。"""
    calc = calculus if isinstance(calculus, dict) else {}
    tfs = calc.get("timeframes") or {}
    def layer(tf):
        row = tfs.get(tf) or {}
        if not row.get("valid"):
            return None
        return {"direction": row.get("direction"), "velocity": row.get("velocity"),
                "acceleration": row.get("acceleration"), "quality": row.get("quality")}
    return {"calculus_schema_version": LEGACY_CALCULUS_VERSION,
            "direction_schema_version": SCHEMA_VERSION,
            "direction_4h": layer("4H"), "strength_1h": layer("1H"),
            "entry_15m": layer("15M")}


def _side(value: Any, *, source: str) -> str | None:
    if not isinstance(value, str):
        return None
    if source == "macro":
        return ("BULL" if value.startswith("4H_MACRO_BULL") else
                "BEAR" if value.startswith("4H_MACRO_BEAR") else
                "RANGE" if value.startswith("4H_MACRO_RANGE") else None)
    if source == "market":
        return {"BULL_TREND": "BULL", "BEAR_TREND": "BEAR", "CHOP": "RANGE"}.get(value)
    if value == "DATA_UNRELIABLE":
        return None
    if value.startswith("BULL_"):
        return "BULL"
    if value.startswith("BEAR_"):
        return "BEAR"
    if value.startswith("RANGE_") or value == "MIXED_TRANSITION":
        return "RANGE"
    return None



def enrich_brain_package(pkg: dict) -> dict:
    """在主脑搬运函数之外补充观测字段，保持抽取函数逐行兼容。"""
    recent = pkg.get("recent_4h") or []
    high = low = position = None
    try:
        if len(recent) >= 6 and float(pkg.get("price", 0) or 0) > 0:
            high = max(float(row[1]) for row in recent)
            low = min(float(row[2]) for row in recent)
            width = high - low
            if low > 0 and width > 0:
                position = (float(pkg["price"]) - low) / width
            else:
                high = low = None
    except (TypeError, ValueError, IndexError):
        high = low = position = None
    pkg.update({
        "market_data_timestamps": {
            "15M": latest_candle_timestamp(pkg.get("instId", ""), "15m"),
            "1H": latest_candle_timestamp(pkg.get("instId", ""), "1H"),
            "4H": latest_candle_timestamp(pkg.get("instId", ""), "4H"),
        },
        "range_4h_high": high, "range_4h_low": low,
        "range_4h_width": (high - low) if high is not None and low is not None else None,
        # NOTE(2026-09-26): 这个值来自 `recent_4h`（最新 8 根，含未收盘那根），
        # 与 `four_hour_range()` 用的「12 根已收盘」是两个不同的箱体。两者曾共用
        # `price_position_in_range` 这个名字，导致同一条记录里出现两个互斥值
        # （710 行中 707 行不一致），模型看到的是自相矛盾的位置。权威口径是
        # 已收盘 12 根的 `four_hour_range()`；这里改名，使两个窗口无法再被混淆。
        "price_position_in_range_recent8": position,
        "range_4h_bars": len(recent) if high is not None else 0,
        "range_4h_candle_ts": latest_candle_timestamp(pkg.get("instId", ""), "4H"),
    })
    return pkg

def compare_directions(f: dict, brain: Any) -> dict:
    """合并两条取数路径，记录三源差异；不同 4H K 线不假装同一时刻。"""
    source = brain.get("direction_input") if isinstance(brain, dict) else None
    source = source if isinstance(source, dict) else {}
    calc = f.get("calculus") or {}
    sides = {"macro_4h": _side(source.get("macro_4h"), source="macro"),
             "market_regime": _side(f.get("market_regime"), source="market"),
             "calculus_regime": _side(calc.get("regime"), source="calculus")}
    brain_ts = source.get("candle_ts_4h")
    trader_ts = f.get("range_4h_candle_ts")
    layers = direction_layers(calc)
    data_ready = (source.get("data_quality") == "valid" and
                  f.get("market_data_valid") is True and
                  all(layers[k] is not None for k in ("direction_4h", "strength_1h", "entry_15m")) and
                  isinstance(brain_ts, int) and isinstance(trader_ts, int) and
                  abs(brain_ts - trader_ts) <= 4 * 3600 * 1000 and
                  all(sides.values()))
    if not data_ready:
        status = "INSUFFICIENT_DATA"
    elif len(set(sides.values())) == 1:
        status = "ALIGNED_" + next(iter(sides.values()))
    elif "BULL" in sides.values() and "BEAR" in sides.values():
        status = "CONFLICT"
    else:
        status = "MIXED"
    return {"schema_version": SCHEMA_VERSION, "status": status, "sources": sides,
            "macro_4h": source.get("macro_4h"),
            "market_regime": f.get("market_regime"), "calculus_regime": calc.get("regime"),
            "brain_candle_ts_4h": brain_ts, "trader_candle_ts_4h": trader_ts,
            "brain_sample_ts": source.get("sample_ts"),
            "range_4h_high": f.get("range_4h_high"),
            "range_4h_low": f.get("range_4h_low"),
            "price_position_in_range": f.get("price_position_in_range")}


def observe_cycle(factors: list[dict], brain_cache: Any) -> None:
    """一轮一次：回填可持久化字段，只摘要记录非对齐方向；不会修改动作字段。"""
    import json
    cache = brain_cache if isinstance(brain_cache, dict) else {}
    alerts = []
    for f in factors:
        if not isinstance(f, dict):
            continue
        brain_row = cache.get(f.get("instId"))
        obs = compare_directions(f, brain_row)
        if isinstance(brain_row, dict):
            # Keep the review join key on the factor snapshot so the later
            # tracker/signal journal write can point back to this brain cycle.
            f["cycle_id"] = brain_row.get("cycle_id", "")
            f["decision_id"] = brain_row.get("decision_id", "")
        f["direction_observation"] = obs
        f["direction_layers"] = direction_layers(f.get("calculus"))
        if not str(obs.get("status") or "").startswith("ALIGNED_"):
            alerts.append({
                "instId": f.get("instId"),
                "status": obs.get("status"),
                "sources": obs.get("sources"),
            })
    if alerts:
        print("[方向观测摘要] " + json.dumps(alerts, ensure_ascii=False, separators=(",", ":")))
