#!/usr/bin/env python3
"""
ASTRA AI Brain Six-Crypto Quantitative Trading Decision Engine (ai_brain_trader.py)
Batch ingests six crypto perpetuals into one macro-context LLM call.
Maintains a validated live decision cache and durable Web audit history.
"""

import os
import sys
from pathlib import Path

from astra_backend.math_utils import safe_float as _shared_safe_float

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = Path(PROJECT_ROOT)
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import scripts.okx_rest as okx_rest
# 风控提示词与执行层共用单一事实源，防止「提示词口径 vs 代码口径」漂移
from risk_constants import (
    DAILY_LOSS_EQUITY_RATIO,
    MAX_CONCURRENT_POSITIONS_CAP,
    MAX_DAILY_LOSS_USDT,
    MAX_LEVERAGE,
    MIN_LEVERAGE,
    MAX_MARGIN_EQUITY_RATIO,
    MAX_SAME_DIRECTION_POSITIONS,
    MAX_SCALE_IN_COUNT,
    MAX_SINGLE_ASSET_MARGIN,
    MIN_ENTRY_CONFIDENCE,
    MIN_RISK_REWARD_RATIO,
    MIN_SCALE_IN_CONFIDENCE,
    MIN_SCALE_IN_PROFIT_RATIO,
    PORTFOLIO_RISK_BUDGET_USDT,
    RISK_PER_TRADE_EQUITY_RATIO,
    SINGLE_ASSET_EQUITY_RATIO,
    STOP_COOLDOWN_MINUTES,
    TIME_STOP_ATR_BAND,
    TIME_STOP_HOURS,
    effective_daily_loss_limit,
    effective_single_asset_margin,    MAX_TOTAL_EXPOSURE_USDT,
)
import json
import hashlib
import copy
import time
import datetime
import urllib.request
import subprocess
import tempfile
import fcntl
import urllib.error
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

ENTRY_ACTIONS = {"BUY_LONG", "SELL_SHORT"}
POSITION_ACTIONS = {"HOLD", "CLOSE_MARKET", "UPDATE_SL"}
from concurrent.futures import ThreadPoolExecutor, as_completed

_JEV_SHADOW_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="jev-shadow")

try:
    from astra_backend.config import settings as standalone_settings
except ImportError:
    standalone_settings = None

try:
    from astra_backend.version import __version__
except Exception:
    __version__ = "7.6.0"


def _get_system_version_tag() -> str:
    return f"v{__version__}"

WORKSPACE_DIR = PROJECT_ROOT
DATA_DIR = os.path.join(WORKSPACE_DIR, "data")
from market_data_service import fetch_single_indicator, fetch_ticker, fetch_okx_ticker, fetch_candles
# 结构优化阶段4·B3：单标的数据包装配已搬入 scripts/brain/packages.py（门面保留薄壳）
from scripts.brain.packages import fetch_single_instrument_package as _fetch_single_instrument_package
from scripts.direction_observation import (
    compare_directions,
    direction_layers,
    enrich_brain_package,
)
from scripts.brain.prompt import (
    construct_full_market_prompt as _construct_full_market_prompt_impl,
)
# 第三十刀：在途持仓/挂单文本装配搬入 account_text，按调用期注入（同名参数解析陷阱见
# construct_full_market_prompt 的 docstring）。
from scripts.brain.account_text import (
    build_position_lines as _build_position_lines,
    build_pending_order_lines as _build_pending_order_lines,
)
from scripts.brain.runtime import (
    capture_policy_snapshot,
    resolve_llm_runtime,
)
from scripts.brain.snapshots import (
    update_factor_library_snapshot,
    write_calculus_snapshot,
    write_prompt_snapshot,
)
from scripts.brain.dispatch import (
    dispatch_llm_and_persist_decisions,
)
from scripts.brain.cycle_parts import (
    normalize_position_management as _normalize_position_management,
    build_effective_prompt_text as _build_effective_prompt_text,
    build_history_record as _build_history_record,
)
from scripts.brain.decisions import (
    validate_and_filter_decision as _validate_and_filter_decision_impl,
    assemble_decision_cache as _assemble_decision_cache_impl,
)
AI_DECISION_CACHE_FILE = os.path.join(DATA_DIR, "ai_brain_decisions.json")
AI_DECISION_HISTORY_FILE = os.path.join(DATA_DIR, "ai_brain_history.json")


def _ai_health_path() -> str:
    # 调用时解析 DATA_DIR——测试 patch 模块属性即封闭（律①）
    return os.path.join(DATA_DIR, "ai_health.json")


def read_cycle_health() -> dict:
    """供 trader/面板读取最近批次健康；缺文件=无记录（不误伤）。"""
    try:
        p = _ai_health_path()
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_cycle_health(status: str, reason: str = "") -> None:
    """审计监控面：trader 为 15 分钟短驻进程，内存计数跨轮即失忆——连续失败
    计数持久化到 data/ai_health.json。04:45 起 14 轮 LLM 停摆但巡检 rc=0 全绿
    的根因就是失败终态没有任何跨进程可查痕迹。>=2 连续失败由 data_health 降
    PARTIAL、由 trader 在 executed_actions 显式告警。"""
    prev = read_cycle_health()
    now_iso = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
    if status == "ok":
        payload = {
            "last_status": "ok", "last_at": now_iso, "consecutive_failures": 0,
            "last_ok_at": now_iso, "last_error": None,
            "total_failures": int(prev.get("total_failures", 0) or 0),
        }
    else:
        payload = {
            "last_status": "failed", "last_at": now_iso,
            "consecutive_failures": int(prev.get("consecutive_failures", 0) or 0) + 1,
            "last_error": str(reason)[:300], "last_ok_at": prev.get("last_ok_at"),
            "total_failures": int(prev.get("total_failures", 0) or 0) + 1,
        }
    try:
        atomic_write_json(_ai_health_path(), payload)
    except Exception as exc:
        print(f"[AI Brain Batch] warn ai_health 旁车写入失败: {exc}")
AI_POSITION_MANAGEMENT_FILE = os.path.join(DATA_DIR, "ai_position_management.json")
AI_LAST_PROMPT_FILE = os.path.join(DATA_DIR, "ai_brain_last_prompt.txt")
VENUE_HEALTH_FILE = os.path.join(DATA_DIR, "venue_health.json")
FACTOR_LIBRARY_FILE = os.path.join(DATA_DIR, "factor_library_snapshot.json")
NEWS_SENTIMENT_FILE = os.path.join(DATA_DIR, "news_sentiment.json")
AI_MEMORY_MD_FILE = os.path.join(DATA_DIR, "AI_TRADING_MEMORY.md")
CALCULUS_SNAPSHOT_FILE = os.path.join(DATA_DIR, "calculus_snapshot.json")
AI_MEMORY_FILE = os.path.join(DATA_DIR, "ai_trading_memory.json")
PROMPT_OVERRIDE_FILE = os.path.join(DATA_DIR, "system_prompt_override.txt")
AI_BRAIN_LOCK_FILE = os.path.join(DATA_DIR, ".ai_brain_cycle.lock")
DECISION_MAX_AGE_SECONDS = 300

from astra_backend.version import __version__
from instrument_pool import load_instruments
from prompt_library import active_profile, append_layer, apply_module_layout
from astra_gateway.telemetry import ModelCallTelemetry
from llm_credentials import get_cpa_client_config as _get_cpa_client_config  # noqa: E402

TARGET_INSTRUMENTS = load_instruments()

try:  # 符号归一（审计 P2-12）：把历史合成 id / 原生命中写法统一成 OKX 形态
    from astra_backend.exchanges.base import canonical_base as _canonical_base_name
except Exception:  # pragma: no cover - scripts/ 直接运行时走兜底
    try:
        from exchanges.base import canonical_base as _canonical_base_name  # type: ignore
    except Exception:
        def _canonical_base_name(symbol: str) -> str:
            s = str(symbol or "").strip().upper()
            for marker in ("-USDT-SWAP", "USDT", "_USDT", "-USDT"):
                if s.endswith(marker):
                    s = s[: -len(marker)]
                    break
            return s.replace("-", "").replace("_", "")


def atomic_write_json(path: str, payload: Any) -> None:
    """Replace JSON atomically so readers never observe a partial cache."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".ai-brain-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def single_brain_cycle(func):
    """Prevent overlapping cron runs from overwriting the shared decision cache."""
    def wrapped(*args, **kwargs):
        os.makedirs(DATA_DIR, exist_ok=True)
        lock_handle = open(AI_BRAIN_LOCK_FILE, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_handle.close()
            print("[AI Brain Batch] Skip: another inference cycle is still running")
            return None
        try:
            lock_handle.seek(0)
            lock_handle.truncate()
            lock_handle.write(str(os.getpid()))
            lock_handle.flush()
            return func(*args, **kwargs)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
    return wrapped


def safe_float(value: Any, default: float = 0.0) -> float:
    """薄壳：转调单一事实源（`astra_backend.math_utils.safe_float`，第一百五十刀）。

    语义与既有实现逐条一致（`nan`/`±inf`/不可转 ⇒ `default`；`bool` 按 `float()`）——
    只是不再各写一份（三份等价实现的漂移代价是"因子与风控静默算出不同的数"）。
    """
    return _shared_safe_float(value, default)


def is_same_direction_scale_request(position_side: str, action: str) -> bool:
    """Allow only same-direction scale-in requests to reach execution hard gateways."""
    side = str(position_side or "").lower()
    decision = str(action or "").upper()
    return (side == "long" and decision == "BUY_LONG") or (side == "short" and decision == "SELL_SHORT")


def get_cpa_client_config() -> Tuple[str, str]:
    """薄壳：调用时解析门面全局，使测试的 patch / 直接赋值生效。

    实现已迁往 astra_backend.llm.credentials（结构优化阶段 4·B3 第四十六刀）。
    ⚠️ `standalone_settings` 必须**在这里**读取后传入 —— 门面全局会被测试
    patch / 原地 reload，子模块 import 期绑定会读到陈旧副本。
    """
    return _get_cpa_client_config(standalone_settings)

def read_prompt_override() -> str:
    """读取管理员提示词覆盖层（不存在/不可读 → 空串，绝不抛）。"""
    try:
        if os.path.exists(PROMPT_OVERRIDE_FILE):
            with open(PROMPT_OVERRIDE_FILE, "r", encoding="utf-8") as handle:
                return handle.read().strip()
    except OSError:
        pass
    return ""


# 止损基准（审计 P2-5）：池条目 per-instrument 值优先——与 ai_factor_trader.instrument_profile
# 同一优先级；TRADFI 等不在池内的标的回落到资产类别档（数值与 ai_factor_trader.
# ASSET_CLASS_PROFILES 逐项对齐，tests/audit/test_audit_config_p4_cleanup.py 有源码钉守着）。
_SL_ATR_BY_ASSET_CLASS = {"commodity": 1.3, "index": 1.2, "stock": 1.3, "crypto": 1.4}


def _prefer_pool_inst(candidate: str, current: str) -> bool:
    """同币多合约时的**确定性**优选（顺序无关）：USDT 永续优先，其次字典序更小。

    为什么需要它：`setdefault` 的"首值优先"会把选择权交给 `TARGET_INSTRUMENTS` 的排列顺序，
    那是**静默的任意选择**（改一行配置就换了合约）。本函数让它可解释、可复现。
    """
    cand_swap = str(candidate).endswith("-USDT-SWAP")
    curr_swap = str(current).endswith("-USDT-SWAP")
    if cand_swap != curr_swap:
        return cand_swap
    return str(candidate) < str(current)


def canonical_position_inst_id(raw: Any) -> str:
    """跨所持仓符号 → OKX 形态（审计 P2-12，模块级便于直接测试）。

    规则：池内币种映射回池内 instId；标准 USDT 永续写法补齐成 OKX 形态；
    其余（日期合约/币本位/不认识的写法）原样保留——绝不假装认识。
    """
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    bare = text.split(":")[-1]
    # 第一百八十三刀：这里原本是 `pool_by_base.setdefault(base, iid)` —— **首值优先**，
    # 于是"同一个币有多个池内合约"时选哪个**取决于 TARGET_INSTRUMENTS 的顺序**（静默的
    # 任意选择；增删一个条目就会换合约，进而换下单标的）。真机核对：当前 9 个目标合约
    # **同币重复为 0**，所以这是潜在风险而非现行错误。改成**与顺序无关的确定性优选**：
    #   1) 优先标准 USDT 永续（`BASE-USDT-SWAP`）；
    #   2) 其余按字典序取最小。
    pool_by_base: Dict[str, str] = {}
    for item in (TARGET_INSTRUMENTS if isinstance(TARGET_INSTRUMENTS, list) else []):
        iid = str((item or {}).get("instId") or "").strip().upper()
        if not iid:
            continue
        base = _canonical_base_name(iid)
        current = pool_by_base.get(base)
        if current is None or _prefer_pool_inst(iid, current):
            pool_by_base[base] = iid
    base = _canonical_base_name(bare)
    # 第一百八十五刀：`canonical_base` 修好"非 USDT 计价"的提取后（`BTC-USDC` → `BTC`、
    # `BTC-USD-SWAP` → `BTC`），**池查找必须加一道"标准形态"闸**，否则币本位/日期合约
    # 会因为币种相同而被映射到池内的 **USDT 永续**（`BTC-USD-SWAP` → `BTC-USDT-SWAP`）——
    # 那是**换了下单标的**，直接违背本函数"其余原样保留，绝不假装认识"的契约
    # （既有用例 `test_unknown_forms_are_preserved_verbatim` 当场判红，救回一刀）。
    standard = bool(base) and bare in (base, f"{base}USDT", f"{base}_USDT",
                                       f"{base}-USDT", f"{base}-USDT-SWAP")
    if standard and base in pool_by_base:
        return pool_by_base[base]
    if standard:
        return f"{base}-USDT-SWAP"
    return text


def _sl_atr_mult_for(package: Dict[str, Any]) -> float:
    """提示词里展示的止损基准＝执行层真正会用的那个数（不再硬编码 1.5~2.0x）。"""
    raw = (package or {}).get("sl_atr_mult")
    try:
        value = float(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    name = str((package or {}).get("name") or "").strip().upper()
    for item in (TARGET_INSTRUMENTS if isinstance(TARGET_INSTRUMENTS, list) else []):
        if str((item or {}).get("name") or "").strip().upper() == name:
            try:
                pooled = float((item or {}).get("sl_atr_mult"))
                if pooled > 0:
                    return pooled
            except (TypeError, ValueError):
                pass
            break
    return float(_SL_ATR_BY_ASSET_CLASS.get(str((package or {}).get("type") or "crypto"), 1.4))


def get_effective_system_prompt(profile: Dict[str, Any] = None, context: Dict[str, Any] = None) -> str:
    """模型真正收到的 System Prompt = 模块布局(SYSTEM_PROMPT) → **之后**再追加管理员覆盖层。

    审计 P1-3(2026-09-13)：旧实现把覆盖层拼在 SYSTEM_PROMPT 尾部再交给
    `apply_module_layout(..., "trading_system", ...)`——而该布局只输出被列名的模块，
    未列名的 base 段（正是这段覆盖层）被直接丢弃，且 fail-closed 只对 trading_user 生效。
    于是 UI 承诺"下一次 AI 推演循环将自动叠加此提示词覆盖层"，模型却从未收到过。
    现在覆盖层在布局**之后**拼接，顺序与 UI 呈现一致；接口侧同样调用本函数，
    保证「看到的」= 「模型收到的」。
    """
    prof = profile if isinstance(profile, dict) and profile else active_profile()
    effective = apply_module_layout(
        SYSTEM_PROMPT, prof, "trading_system", f"{prof.get('name', '稳健')}交易系统提示词模板", context=context
    )
    override = read_prompt_override()
    if override:
        effective = f"{effective}\n\n【管理员提示词覆盖层（同样必须遵守上述风控和 JSON 约束）】\n{override}"
    return effective


def fetch_single_instrument_package(item: Dict[str, Any]) -> Dict[str, Any]:
    """装配单标的数据包。实现见 scripts/brain/packages.py。

    门面保留同名壳：调用点（`execute_batch_ai_brain_cycle` 里的线程池提交）
    与其他模块的引用都按全局名查找，故调用点无需改动。
    两个行情函数在**调用时**注入，而不是被子模块 import 期烘焙 ——
    `pin_baseline_risk_env()` 的重载名单不含子模块，import 期绑定会让
    `patch.object(门面, "fetch_candles")` 失效。详见该模块 docstring。
    """
    pkg = _fetch_single_instrument_package(
        item,
        fetch_candles=fetch_candles,
        fetch_single_indicator=fetch_single_indicator,
    )
    return enrich_brain_package(pkg)

# ── SYSTEM_PROMPT · v7.6 优质预设基线 ──────────────────────────────────────
# 设计契约：
# 1) 分节标题与 data/prompt_library.json 的 trading_system 布局 8 个 base 模块一一对应——
#    标题即接口，线上布局按标题实时取用本代码最新文本，杜绝快照漂移；
# 2) 全部风控数值由 scripts/risk_constants.py 插值（后台风控管理页写入 .env，下一巡检周期生效），
#    保证「提示词口径 == 执行层口径」，模型永远不会被告知过期规则；
# 3) JSON 契约段含花括号，作为独立普通字符串，不参与 format 插值。
_SYSTEM_CORE = """==== 【系统角色定位与核心使命】 ====
你是 AstraQuant 的首席 AI 交易官，负责 1H~4H 加密合约多空双向波段的高胜率交易裁决。你的使命按优先级排列：
1. 捍卫本金：单笔风险有界、日亏有熔断、敞口有上限，任何单笔损失都不得伤及账户根基；
2. 捕捉正期望：只在数学期望为正（概率优势 × 盈亏比 > 摩擦成本）的机会上下注，用高确定性波段积累复利；
3. 拒绝懈怠与恐惧：当空仓且存在至少一个合法顺势候选时（符合顺势高胜率形态）并通过全部硬门禁，必须果断在候选标的池中选优输出限价进场指令，不得无故放弃合规机会——空仓不是风控，无优势硬开才是风险；日内波动活跃，只要具备顺势回踩确认、反弹承压或动能初现，必须敏锐捕获，拒绝无为懈怠！
一切金额类参数（保证金、风险额、熔断线）一律以每轮用户消息中【本周期风险预算】小节的实时推导值为准，严禁引用或臆想任何固定绝对金额。

==== 【核心军规：反割肉·反磨损·选优开单五大铁律】 ====
1. 宽止损隔绝杂波：止损必须放在市场结构失效点之外，距离 1.8x~2.2x 1H ATR（或现价外 1.8%~3.0% 安全垫）。严禁把止损设在 15M/5M 噪音区间被插针扫损；宁可压低杠杆与保证金，也绝不压缩止损呼吸空间。
2. 三阶利润棘轮（兼顾波段奔跑与胜率锁定，杜绝赢小输大）：
   阶梯1（浮盈 < 1.0R）：保持原宽止损给波段充分展开时间，禁止微小浮盈过早提至成本位被杂波扫出；
   阶梯2（浮盈 ≥ 1.5R 且 ROI ≥ +2.2%）：输出 UPDATE_SL 将止损移至保本位（开仓成本 +0.20%），彻底切断本金风险；
   阶梯3（浮盈 ≥ 2.2R 且 ROI ≥ +3.5%）：输出 UPDATE_SL 锁定成本上方至少 +1.0R，扎实锁定波段核心利润。
   主动止盈三道防线（兼顾大波段奔跑与落袋防倒亏）：① 峰值回撤——最高浮盈曾达 ROI ≥ +3.5% 或 ≥ 1.8R，当前浮盈较极值回撤超 45%~55% 且 1H 动能明显破位时，果断 CLOSE_MARKET 或紧贴现价 UPDATE_SL 锁定剩余利润，严禁在微幅浮盈（<1.5R）的正常日内回踩中恐慌砸盘提前出局；② 动能耗散——浮盈充沛（ROI ≥ +2.5%）下 1H 做功功率 Φ = v · a < -0.15 且曲率 κ ≥ 1.8（高位急刹车力竭、长上影假突破受挫）时，提前落袋为安，死等极远挂单是禁止行为；③ 阻力锚定——止盈价优先锚定前方关键阻力/支撑位或 2.0~2.8x ATR 可达位，确保实现高盈亏比正期望，充分享受波段主浪溢价。
3. 敞口纪律（执行层硬拦截，不得试探边界）：
   - 全系统同向持仓上限、单笔保证金占比硬顶、杠杆上限与当日亏损熔断线，一律以每轮用户消息【本周期风险预算】的实时声明为准（执行层硬拦截，不得试探边界）；同向在手 1~2 笔时积极顺势出击捕捉机会，同向已有 3 笔时，新开同向单的置信度必须自律提升至 82% 以上；严禁在 BTC/ETH/SOL/DOGE 等高相关标的上无节制同向堆叠单边敞口；
   - 标的一旦止损出局，【本周期风险预算】声明的冷静期分钟数内不得再申请该标的，严禁情绪化盲目反手；开仓逻辑必须能在声明的最长持仓时间（时间止损）量级内兑现——超时横盘仓位将被执行层强制离场，禁止寄希望于死扛。
4. 选优开单契约：空仓且候选池存在合法顺势形态时，从概率期望与微积分动能最优的标的中果断输出 BUY_LONG 或 SELL_SHORT 限价单；置信度自信标定：形态达标且空间充足时，按【本周期风险预算】给出的置信度标定带给值（低于该带下沿＝低于执行层门禁的报价会被物理拦截，绝不试探）；只有全部候选均触发明确硬否决或优势不足时才全体 WAIT。目标 R:R 与绝对盈亏比底线一律以【本周期风险预算】声明的目标盈亏比/硬底线为准。
5. 反磨损意识：入场优先用微距 Maker 限价单锚定现价外 0.05%~0.25% 紧贴盘口深度挂单，既享受 Maker 手续费优势与零滑点，又极大提升即时撮合成交率（Fill Rate），彻底杜绝过深挂单导致踏空；震荡市拒绝追涨杀跌磨损手续费。

==== 【决策优先级：高层级永远覆盖低层级】 ====
P0 不可覆盖硬约束：数据有效性核验、交易执行层 Fail-Closed、4H 方向否决、真实价格几何合法性、R:R 盈亏比硬底线、杠杆/保证金/持仓数上限、云端 OCO 全覆盖、禁止逆势补仓、严格 JSON 契约。
P1 核心方向证据（最高权重）：4H 宏观结构与 1H 三大数理基石硬证据（延续/击穿概率、微积分速度 v 与加速度 a、能量积分 E）。
P2 质量确认：1H ADX 趋势强度（ADX ≥ 14 即可作为有效动量参与，在结构清晰或均线回踩企稳时果断发单；ADX 18~22 小仓参与，ADX < 18 严禁半山腰开仓）、量能/OI 异动、聪明钱资金流向与衍生品持仓结构。
P3 执行定位：15M K线、盘口与 Maker 限价挂单位置。P3 优化入场成本，不能单独改变 P1 方向。
不得把“稳健”解释为长期空仓，更不得被解释成“只有完美共振才允许交易”。“减速”不是永久禁令：在 4H 顺势大浪中普通回抽优先作为打折买点与限价入场定位。当市场出现【顺势回踩确认】、【弱势反弹承压】或【箱体边界极值超伸回归】时，必须果断给出精准限价挂单决策。P2/P3 的轻微分歧应通过减小保证金处理，绝不能机械全盘 WAIT。

==== 【三大底层数理基石：强化概率优势与微积分因果审计】 ====
本系统坚决破除感性猜单与盲目猜顶抄底，决策逻辑由纯数理统计驱动，并必须在输出中明确引用具体数值：
1. ⚅ 概率论与统计风险（最高权重核心）：使用偏度、超额峰度、条件延续概率 continuation_prob_pct、击穿概率 breakdown_prob_pct、Cornish-Fisher 95% VaR 与 CVaR。
   - 【胜率数学期望定价】：当条件延续概率 P续 ≥ 46%~50%（做多）或击穿概率 P破 ≥ 46%~50%（做空），且具备【本周期风险预算】声明的目标盈亏比空间时，单笔数学期望已具备极高正 Alpha，果断作为首选发单依据；
   - 【概率优势定方向】：P续 显著高于 P破（差值 ≥ 8%~10%）时概率天平全面向多头倾斜，专注找回踩低吸；P破 显著占优时反之，专注找反弹承压做空；若差值在 5%~8% 的弱优势区，结合 1H 微积分速度减速回踩支撑均线与加速度 a 转正企稳，亦可果断进场；
   - 【极端肥尾折减】：超额峰度过大或 CVaR 偏高代表潜在波动剧烈，应把保证金降至【本周期风险预算】常规区间的下沿、止损按其止损基准适当放宽以抵御噪音，或直接 WAIT 放弃该机会。
2. ∂ 因果微积分动力学：只使用已闭合历史 K 线，解释对数价格速度 v、加速度 a、冲击 j 与指数衰减累计冲量 I。1H 是硬阈值与波段裁决周期。
   BULL_DECELERATING/BEAR_DECELERATING 表示趋势失速与回抽，不等于已经反转：在 4H 顺势大浪中，1H 减速回抽正是触碰支撑均线（EMA21/55）时的极佳打折买点，当 a 由负转正、j 趋缓（回踩企稳）必须果断顺势做多；在 4H 空头通道中，1H 弱反弹减速遇阻正是逢高做空的极佳卖点。
   模型输出必须在 calculus_dynamics 中明确列出当前标的 1H 的 v 与 a 真实数值，严禁只写空泛定性词句！
3. ∫ 定积分能量学：使用梯形积分计算 energy_integral（速度路径净位移/净做功）与 deviation_area_integral（相对窗口起点基线的价格路径偏离面积）。
   正负能量表示方向性累计做功；绝对偏离面积过大表示路径过度伸展与均值回归驱动。在宽幅震荡箱体中，偏离面积积分超伸至极限且伴随超买超卖时，是高胜率箱体边界反转契机！

==== 【多空对称研判与四大王牌高胜率入场形态】 ====
1. 多空双向对称顺势原则（Dual-Direction Trend Following）：多与空同等重要，核心是绝对顺应 4H 宏观与 1H 动量中枢方向。
   做多条件（4H多头主浪或箱体下沿）：4H 顺势向上或 1H 均线多头排列时专注顺势做多；1H 回调减速定性为寻找支撑均线的打折买点，在现价下方 0.05%~0.25% 挂微距限价多单；100% 严禁任何逆势摸顶开空。
   做空条件（4H空头承压或箱体上沿）：4H 宏观受压（4H_MACRO_BEAR）或 1H 均线空头排列时专注顺势做空；1H 向上弱反弹遇阻回落时逢高做空，在现价上方 0.05%~0.25% 挂微距限价空单；100% 严禁任何逆势抄底做多。
   震荡箱体双向作战：4H 处于区间震荡（CHOP/RANGE）时，下沿支撑低吸做多，上沿阻力高抛做空；箱体中间（半山腰）禁止开仓。
2. 四大王牌高胜率入场形态（形态达标必须果断发单）：
   ① 顺势回踩均线/支撑位缩量企稳（Pullback to Value / 做多）；
   ② 顺势空头反弹承压阻力位遇阻回落（Throwback to Resistance / 做空）；
   ③ 假跌破流动性掠夺后迅速收回（Liquidity Sweep & Reclaim / 诱空收网做多）；
   ④ 假突破流动性衰竭后迅速跌回（Liquidity Sweep & Fail / 诱多受挫做空）。
3. 选优开单纪律：只要形态达标且风险收益比达到【本周期风险预算】的目标盈亏比，置信度按该小节的标定带给值；不得以“再等等完美共振”为由放弃合法机会。

==== 【开仓参数与科学价格几何】 ====
- 顺势铁律（Fail-Closed）：4H_MACRO_BULL 大级别多头通道下 100% 严禁输出 SELL_SHORT 逆势摸顶；4H_MACRO_BEAR 大级别空头承压下 100% 严禁输出 BUY_LONG 逆势抄底！
- 震荡过滤：箱体正中间无序乱跳时一律强制 WAIT，严禁追涨杀跌磨损手续费。
- 价格几何：BUY_LONG 必须满足 stop_loss_price < entry_price < take_profit_price；SELL_SHORT 必须满足 take_profit_price < entry_price < stop_loss_price。目标盈亏比见【本周期风险预算】；执行层绝对拒绝低于其硬底线的报价。
- 入场一律 Maker 限价：挂在支撑/阻力位附近（如现价下方/上方 0.05%~0.25% 微距挂单），严禁市价追单；止损基于结构性保护点（前低支撑位或箱体边缘下方 0.3%~0.5%），参考 1.8~2.2x 1H ATR，绝不贴脸设损。
- 保证金与杠杆：常规取【本周期风险预算】给出的常规区间，强信号（P0 全通过 + 概率优势 ≥ 15% + ADX ≥ 22）可上浮至其单笔保证金硬顶；杠杆不超过其声明的杠杆上限。资金规模过小时宁可少开标的，也不得压缩止损距离或放弃盈亏比底线；若某标的在当前余额下无法同时满足交易所最小下单量、止损呼吸空间与 R:R 底线，该标的必须输出 WAIT 并说明资金不匹配。
"""

_PYRAMID = """==== 【顺势浮盈金字塔加仓：模型只能申请，执行层拥有最终否决权】 ====
- 已有多仓只能申请同向 BUY_LONG，已有空仓只能申请同向 SELL_SHORT；反向指令不得借加仓通道执行。
- 申请前置条件（缺一不可）：底仓浮盈与保本移损达标、该标的累计加仓次数未超上限、AI 置信度达到加仓门禁、加仓后单标的累计保证金不超过单标的上限——全部阈值以每轮用户消息【本周期风险预算】的实时声明为准；若其声明加仓已禁用（上限 0 次），则一律不得申请加仓，仅可 HOLD / UPDATE_SL / CLOSE_MARKET。
- 加多门禁：多周期聚合加速度 a ≥ -0.25 且 continuation_prob_pct ≥ 40%；加空门禁：a ≤ +0.25 且 breakdown_prob_pct ≥ 40%。
- 浮亏、未脱离成本区、顶部/底部失速、概率不足或肥尾冲击时不得申请加仓。即使模型申请，执行器仍将独立硬校验并保留最终否决权。
"""

_SYSTEM_JSON_CONTRACT = """==== 【严格 JSON 规范契约与完整输出骨架 (JSON Schema)】 ====
你必须直接输出一个严格合法的 JSON 对象，禁止输出任何 Markdown 代码围栏、前缀或额外文字。结构必须严格完全符合以下 JSON Schema 骨架：

{
  "macro_assessment": "30字内全市场宏观流动性与大盘走势总结",
  "position_management": [
    {
      "instId": "BTC-USDT-SWAP",
      "action": "HOLD",
      "suggested_sl_price": 0.0,
      "confidence": 85.0,
      "reason": "30字内持仓调整原因与动能简述"
    }
  ],
  "pending_orders_management": [
    {
      "ordId": "在途挂单ID",
      "instId": "BTC-USDT-SWAP",
      "action": "KEEP",
      "reason": "30字内维持或撤单原因"
    }
  ],
  "decisions": {
    "BTC-USDT-SWAP": {
      "action": "BUY_LONG",
      "confidence": 85.0,
      "leverage": 3,
      "margin_usdt": 100.0,
      "entry_price": 79500.0,
      "take_profit_price": 83000.0,
      "stop_loss_price": 77800.0,
      "summary_reason": "顺势回踩支撑企稳限价做多",
      "market_structure": "4H大势多头，1H均线回踩企稳",
      "calculus_dynamics": "1H: v=+0.05, a=+0.20 动能转正",
      "math_prob_rationale": "延续概率65%显著占优，R:R=2.5",
      "volume_and_oi": "量能缩量企稳，主力净流入"
    }
  }
}

▍字段审计说明：
- position_management.action 只允许: "HOLD" | "CLOSE_MARKET" | "UPDATE_SL"；触发峰值回撤超 35% 或 1H 负功率衰竭时果断输出 CLOSE_MARKET 止盈；action 为 UPDATE_SL 时 suggested_sl_price 填目标价格，否则必须填 0.0；
- pending_orders_management.action 只允许: "KEEP" | "CANCEL"；挂单已大幅偏离盘口或入场逻辑失效时必须 CANCEL；
- decisions[标的].action 只允许: "BUY_LONG" | "SELL_SHORT" | "WAIT"；action 为 WAIT 时 entry_price/take_profit_price/stop_loss_price 填 0.0；
- decisions 只包含有明确结论的标的，未涉及的标的不得出现；
- 每个决策的 calculus_dynamics 与 math_prob_rationale 必须明确引用具体 1H v, a 与概率数值，严禁只写空泛定性词句！"""

# System 宪法保持静态：全部动态风控阈值由每轮 construct_full_market_prompt 注入的
# 【本周期风险预算】小节实时携带（该小节直接从 risk_constants 推导，永不进快照）。
# 这样即使策略快照布局缓存了本节文本，风控改参也不会造成「提示词口径过期」。
SYSTEM_PROMPT = _SYSTEM_CORE + _PYRAMID + "\n" + _SYSTEM_JSON_CONTRACT


def build_risk_budget_text(usdt_available: float = None) -> str:
    """【本周期风险预算】小节（审计 P1-1，2026-09-13）。

    旧实现自己算「权益×5% / 权益×30%」，漏掉了绝对封顶，于是模型看到
    单标的 1496.82U / 日亏 −249.47U，而引擎执行 `min(600, 1496.82)`=600U、
    `min(150, 249.47)`=150U（虚高 2.49× / 1.66×）——偏偏 SYSTEM PROMPT 要求模型
    "一切金额类参数一律以【本周期风险预算】小节为准"，模型据此规划的是不存在的空间。

    现在两个封顶值直接取 `risk_constants.effective_*`（与执行层同一函数对象），
    并把此前对模型完全不可见的 5 个旋钮一并披露：组合风险总预算、并发持仓上限、
    单标的绝对封顶、日亏绝对封顶、时间止损带宽。
    """
    if usdt_available is None or usdt_available < 0:
        return "[MISSING_CONTEXT:risk_budget]"
    # 动态取单一事实源对象（支持后台热重载与多用例隔离）
    import scripts.risk_constants as rc

    # 风险预算按「实际可用余额」自适应推导：预设绝不写死绝对金额，避免与小资金账户(如 80U)冲突。
    _eq = float(usdt_available)
    _max_ratio = float(rc.MAX_MARGIN_EQUITY_RATIO or 0.20)
    _m_lo = round(_eq * 0.03, 2)
    # 动态常规区间上限：基准模式下保持 12% 稳健仓位（与历史基准对齐），激进模式下跟随 MAX_MARGIN_EQUITY_RATIO 放宽至 20%~25%
    _regular_hi_ratio = min(0.25, round(_max_ratio * 0.70, 3)) if _max_ratio > 0.20 else min(0.12, _max_ratio)
    _m_hi = round(_eq * _regular_hi_ratio, 2)
    _m_strong = round(_eq * _max_ratio, 2)
    # 单标的累计 = min(绝对封顶 600U, 权益×30%)；日亏熔断 = min(绝对封顶 150U, 权益×5%)
    _asset_cap = rc.effective_single_asset_margin(_eq)
    _daily_stop = rc.effective_daily_loss_limit(_eq)
    _daily_stop_note = (f"min({rc.MAX_DAILY_LOSS_USDT:g} 绝对封顶, 可用余额 {rc.DAILY_LOSS_EQUITY_RATIO:.0%})"
                        if _daily_stop < round(max(_eq * rc.DAILY_LOSS_EQUITY_RATIO, 1.0), 2) else f"可用余额 {rc.DAILY_LOSS_EQUITY_RATIO:.0%}")
    # 单笔上限：若配置绝对封顶则受其约束，0=不设绝对硬顶纯按可用余额比例推导
    if rc.MAX_SINGLE_ASSET_MARGIN and rc.MAX_SINGLE_ASSET_MARGIN > 0:
        _m_strong_cap = min(_m_strong, _asset_cap)
        _strong_cap_note = f"(min(权益 {_max_ratio:.0%}={_m_strong}, 单标的封顶 {round(_asset_cap, 2)})，执行层硬顶)"
        _asset_cap_note = (f"min({rc.MAX_SINGLE_ASSET_MARGIN:g} 绝对封顶, 可用余额 {rc.SINGLE_ASSET_EQUITY_RATIO:.0%})"
                           if _asset_cap < round(_eq * rc.SINGLE_ASSET_EQUITY_RATIO, 2)
                           else f"可用余额 {rc.SINGLE_ASSET_EQUITY_RATIO:.0%} (按比例计算，低于 {rc.MAX_SINGLE_ASSET_MARGIN:g} 绝对封顶)")
    else:
        _m_strong_cap = _m_strong
        _strong_cap_note = f"(可用余额 {_max_ratio:.0%}={_m_strong}，纯按比例动态推导，不设绝对金额硬顶)"
        _asset_cap_note = f"可用余额 {rc.SINGLE_ASSET_EQUITY_RATIO:.0%} ({rc.MAX_SINGLE_ASSET_MARGIN:g} 绝对封顶=不设绝对硬顶，纯按比例动态推导)"
    if getattr(rc, "MAX_RISK_PER_TRADE_USDT", 0.0) and rc.MAX_RISK_PER_TRADE_USDT > 0:
        _risk_1r_note = f"min({rc.MAX_RISK_PER_TRADE_USDT:g} 绝对封顶, 可用余额 {rc.RISK_PER_TRADE_EQUITY_RATIO:.0%})"
    else:
        _risk_1r_note = f"可用余额 {rc.RISK_PER_TRADE_EQUITY_RATIO:.0%} (纯按比例动态推导，不设绝对金额硬顶)"
    text = (
        f"【本周期风险预算｜按实际可用余额 {_eq:.2f} USDT 与后台风控配置自适应推导，严禁套用任何固定绝对金额】:\n"
        f"- 常规单笔保证金: {_m_lo} ~ {_m_hi} USDT (可用余额 3%~{_regular_hi_ratio:.0%})\n"
        f"- 强信号单笔保证金上限: {round(_m_strong_cap, 2)} USDT {_strong_cap_note}\n"
        f"- 单标的累计保证金上限(含金字塔加仓): {_asset_cap} USDT ({_asset_cap_note}，执行层已按同一 min() 硬夹)\n"
        f"- 单笔最大可承受亏损: 以 1.0R 为基准 ({_risk_1r_note}，执行层已按同一 min() 硬夹)\n"
        f"- 当日累计亏损熔断线: -{_daily_stop} USDT ({_daily_stop_note}，执行层已按同一 min() 硬夹)\n"
        f"- 全系统同向持仓上限: {rc.MAX_SAME_DIRECTION_POSITIONS} 笔 (多/空各自封顶，执行层硬拦截)\n"
        f"- 全系统并发持仓上限: "
        + (f"{rc.MAX_CONCURRENT_POSITIONS_CAP} 笔 (执行层硬拦截)\n" if rc.MAX_CONCURRENT_POSITIONS_CAP > 0
           else "未单独设限 (0=不额外收紧；实际受标的池容量与同向上限约束)\n")
        + (
            f"- 组合风险总预算(跨所合算): {rc.PORTFOLIO_RISK_BUDGET_USDT:.2f} USDT (执行层按总名义敞口强制)\n"
            if rc.PORTFOLIO_RISK_BUDGET_USDT > 0 else
            "- 组合风险总预算(跨所合算): 未设上限 (0=引擎不封顶，仅受单标的/同向/并发上限约束)\n"
        )
        # 审计 P2-1：同向敞口上限现已真执行（下单前入场闸门拒开），
        # 这里必须同源披露，否则"提示词口径 == 代码口径"又多一处例外。
        + (
            f"- 跨所同向敞口上限: {rc.MAX_TOTAL_EXPOSURE_USDT:.2f} USDT (同一标同方向跨所合计名义额，含本单；超出执行层拒开)\n"
            if rc.MAX_TOTAL_EXPOSURE_USDT > 0 else
            "- 跨所同向敞口上限: 未设上限 (0=不限制；仍受单标的/同向/并发上限约束)\n"
        )
        + f"- 最长持仓时间: {rc.TIME_STOP_HOURS:g} 小时 (超时且横盘无突破将被时间止损离场；横盘判定带宽 ±{rc.TIME_STOP_ATR_BAND:.0%} ATR)\n"
        f"- 单笔杠杆区间: {rc.MIN_LEVERAGE:g}x ~ {rc.MAX_LEVERAGE:g}x (在区间内按信号强度自主裁决；区间外执行层自动钳制)\n"
        f"- 盈亏比 R:R 硬底线: {rc.MIN_RISK_REWARD_RATIO:.1f} (低于此值的报价执行层物理拒绝)\n"
        # 审计 P3-4：宪法里的"目标 R:R ≥2.2/2.5"与"置信度 78%~88%"是硬编码，
        # 与可配的硬底线/门禁冲突（稳健套件门禁 85 → 78~88 一带整片必拒）。
        # 目标与标定带统一在此派生，宪法只指向本节。
        f"- 目标盈亏比 R:R: {max(2.2, float(rc.MIN_RISK_REWARD_RATIO or 0.0)):.1f} ~ {rc.MAX_RISK_REWARD_RATIO:.1f} "
        f"(上限 {rc.MAX_RISK_REWARD_RATIO:.1f}；低于硬底线一律被拒，超出上限执行层自动平滑收窄钳制，防止止盈过远)\n"
        f"- 单笔止盈止损宽度: 基准止损 {rc.STOP_LOSS_ATR_MULT:g}x 1H ATR，最大止盈宽度 ≤ {rc.MAX_TAKE_PROFIT_ATR:g}x 1H ATR (超出上限执行层自动平滑收窄至合理波段)\n"
        f"- 置信度标定带: {max(float(rc.MIN_ENTRY_CONFIDENCE or 0.0), 78.0):.0f}% ~ "
        f"{max(float(rc.MIN_ENTRY_CONFIDENCE or 0.0), 78.0) + 8.0:.0f}% "
        f"(下沿=执行层新开仓门禁 {rc.MIN_ENTRY_CONFIDENCE:.0f}%，低于下沿必被物理拦截)\n"
        f"- 新开仓最低置信度门禁: {rc.MIN_ENTRY_CONFIDENCE:g}% (低于此值禁止新开仓)\n"
        + (
            "- 金字塔加仓: 已禁用 (最大加仓次数 0，在途持仓仅可 HOLD/UPDATE_SL/CLOSE_MARKET)\n"
            if rc.MAX_SCALE_IN_COUNT <= 0 else
            f"- 金字塔加仓门禁: 最多 {rc.MAX_SCALE_IN_COUNT} 次 · 底仓浮盈 ≥ {rc.MIN_SCALE_IN_PROFIT_RATIO:.1%} 且已保本 · 置信度 ≥ {rc.MIN_SCALE_IN_CONFIDENCE:g}%\n"
        )
        + (
            f"- 分批止盈机制: 【已启用】(底仓浮盈达到 {rc.SCALE_OUT_TRIGGER_ATR:g}x ATR 时，执行层自动市价平仓 {rc.SCALE_OUT_RATIO:.0%} 锁定现金利润并提损保本；模型可让剩余仓位充分奔跑博取大波段)\n"
            if rc.SCALE_OUT_ENABLED else
            "- 分批止盈机制: 【已禁用】(全仓奔跑至目标止盈位或触发动态追踪止损)\n"
        )
        + f"- 止损后同标的冷静期: {rc.STOP_COOLDOWN_MINUTES} 分钟"
    )
    if _eq < 200.0:
        text += (
            "\n- ⚠️ 小资金账户提示: 可用余额偏小，按百分比推导的保证金可能低于部分永续合约的交易所最小下单名义价值"
            "（如高单价币种 BTC 一张合约的名义价值就可能超过账户余额）。此时应当【减少同时持有的标的数量】、"
            "优先选择最小名义价值与账户规模匹配的标的，或适度提高单笔保证金占比；"
            "绝不允许通过压缩止损距离或降低盈亏比来迁就资金规模。"
            "若某标的在当前余额下无法同时满足最小下单量、止损呼吸空间与 R:R≥2.0，该标的必须输出 WAIT 并说明资金不匹配。"
        )
    return text


def construct_full_market_prompt(
    packages: List[Dict[str, Any]],
    pos_summary: str = "[MISSING_CONTEXT:account_positions]",
    active_positions_detail: List[Dict[str, Any]] = None,
    pending_orders_detail: List[Dict[str, Any]] = None,
    current_time_str: str = "",
    usdt_available: float = None,
    runtime_context_out: Dict[str, Any] = None,
    policy_snapshot: Dict[str, Any] = None,
) -> str:
    """把本轮全市场数据渲染成用户提示词。实现见 scripts/brain/prompt.py。

    15 项依赖在**调用时**从门面全局取名 —— 其中风控常量必须与执行层同源
    （`risk_constants` 改参后由门面重载刷新），文件路径与 `safe_float` 则是
    既有测试缝。理由逐一列在 scripts/brain/prompt.py 的 docstring。

    注：不要把默认值写成同名形参（`safe_float=None` 之类）—— 那会让函数体里的
    裸名解析到形参、静默关掉这些缝。
    """
    return _construct_full_market_prompt_impl(
        packages, pos_summary, active_positions_detail, pending_orders_detail,
        current_time_str, usdt_available, runtime_context_out, policy_snapshot,
        safe_float=safe_float,
        sl_atr_mult_for=_sl_atr_mult_for,
        build_risk_budget_text=build_risk_budget_text,
        active_profile=active_profile,
        apply_module_layout=apply_module_layout,
        system_version=__version__,
        ai_memory_md_file=AI_MEMORY_MD_FILE,
        ai_memory_file=AI_MEMORY_FILE,
        news_sentiment_file=NEWS_SENTIMENT_FILE,
        max_leverage=MAX_LEVERAGE,
        min_leverage=MIN_LEVERAGE,
        max_scale_in_count=MAX_SCALE_IN_COUNT,
        min_scale_in_confidence=MIN_SCALE_IN_CONFIDENCE,
        max_margin_equity_ratio=MAX_MARGIN_EQUITY_RATIO,
        _build_position_lines=_build_position_lines,
        _build_pending_order_lines=_build_pending_order_lines,
    )



def validate_and_filter_decision(p: Dict[str, Any], d_item: Dict[str, Any],
                                 active_inst_ids: set,
                                 active_position_sides: Dict[str, str]) -> tuple[str, str, float]:
    """单条决策校验。实现见 scripts/brain/decisions.py。

    `safe_float` 在调用时注入（它定义在本门面，不是共享叶子函数）。
    """
    return _validate_and_filter_decision_impl(
        p, d_item, active_inst_ids, active_position_sides, safe_float=safe_float)


def assemble_decision_cache(
    packages: List[Dict[str, Any]],
    decisions_dict: Dict[str, Any],
    active_inst_ids: set,
    active_position_sides: Dict[str, str],
    time_str: str,
    macro_summary: str,
    policy_snapshot: Optional[Dict[str, Any]] = None,
    council_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把已验证决策装配成标准缓存契约。实现见 scripts/brain/decisions.py。

    依赖一律在**调用时**从门面全局取名，以便既有测试缝继续生效：
    - `patch.object(abt, "DATA_DIR", …)`（test_ai_health_sidecar）
    - `patch.object(abt, "MAX_LEVERAGE"/"MIN_LEVERAGE", …)`（test_leverage_range_and_council
      断言"模型给 3 被下限抬到 5"，夹取必须读到补丁值）
    - `safe_float` / `_get_system_version_tag` 定义在本门面。

    注：不要把默认值写成同名形参 —— 那会让函数体里的裸名解析到形参、
    静默关掉补丁缝。
    """
    return _assemble_decision_cache_impl(
        packages, decisions_dict, active_inst_ids, active_position_sides,
        time_str, macro_summary,
        policy_snapshot=policy_snapshot, council_status=council_status,
        data_dir=DATA_DIR,
        max_leverage=MAX_LEVERAGE,
        min_leverage=MIN_LEVERAGE,
        safe_float=safe_float,
        get_system_version_tag=_get_system_version_tag,
        validate=lambda p_, d_, ids_, sides_: _validate_and_filter_decision_impl(
            p_, d_, ids_, sides_, safe_float=safe_float),
    )




@single_brain_cycle
def _pending_order_margin_usdt(o: Dict[str, Any]) -> Optional[float]:
    """挂单的**保证金**（USDT）。

    用户 2026-09-28 拍板：全系统不再用「张」表达仓位 —— 各币种的合约面值
    算法都不一样（BTC 一张 0.01 币、XRP 一张 100 币），
    模型看到"5 张"根本无从判断规模。保证金是唯一跨场所、跨币种可比的量。

    取不到（缺面值/缺杠杆/数值非法）返回 `None`，由文案层写 `--` ——
    **绝不回落张数**。
    """
    try:
        sz = abs(float(o.get("sz") or 0))
        px = float(o.get("px") or 0)
        lev = float(str(o.get("lever") or "").replace("x", "") or 0)
    except (TypeError, ValueError):
        return None
    if sz <= 0 or px <= 0 or lev <= 0:
        return None
    inst = str(o.get("instId") or "")
    ct = 0.0
    for item in TARGET_INSTRUMENTS or []:
        if item.get("instId") == inst:
            try:
                ct = float(item.get("ctVal") or 0.0)
            except (TypeError, ValueError):
                ct = 0.0
            break
    if ct <= 0:
        return None
    return round(sz * ct * px / lev, 2)


def fetch_pending_orders_list() -> Optional[List[Dict[str, Any]]]:
    """拉取交易所当前全部 SWAP 挂单（V5 直签 REST，US-003）。

    行为契约（对齐历史 CLI 挂单查询）：返回列表=成功；查询失败/未配置
    凭证（OKXNotConfigured）→ 告警并返回 None。fail-closed：绝不回退命令行子进程。

    每笔挂单额外附上 `margin_usdt`（保证金，钱口径）供提示词展示 ——
    消费方（`brain/account_text.build_pending_order_lines`）不得再显示张数。
    """
    try:
        fetched = okx_rest.pending_orders()
    except Exception as e:
        print(f"[AI Brain Batch] Pending orders fetch warning: {e}")
        return None
    if not isinstance(fetched, list):
        return None
    for _o in fetched:
        if isinstance(_o, dict):
            _o["margin_usdt"] = _pending_order_margin_usdt(_o)
    return fetched


def execute_brain_pending_cancels(pending_mgmt_list: List[Any]) -> List[Dict[str, Any]]:
    """执行 AI 决策的 CANCEL 清单（V5 直签 REST，US-003）。

    仅当撤单真实成功才打印成功；单笔失败继续处理其余项，并返回审计日志。
    """
    log: List[Dict[str, Any]] = []
    for p_order in pending_mgmt_list or []:
        if not isinstance(p_order, dict):
            continue
        p_act = str(p_order.get("action", "")).upper()
        p_ord_id = str(p_order.get("ordId", ""))
        p_inst_id = str(p_order.get("instId", ""))
        p_reason = str(p_order.get("reason", "模型指示撤销该挂单"))
        if p_act == "CANCEL" and p_ord_id and p_inst_id:
            try:
                okx_rest.cancel_order(p_inst_id, p_ord_id)
                print(f"[AI Brain Batch] 🛑 AI自主撤回失效/过时限价单: {p_inst_id} (ordId={p_ord_id}, 原因={p_reason})")
                log.append({"ok": True, "instId": p_inst_id, "ordId": p_ord_id, "reason": p_reason})
            except Exception as exc:
                print(f"[AI Brain Batch] ⚠️ 撤单失败（直签 REST fail-closed，不做假成功，待下一周期重试）: {p_inst_id} ordId={p_ord_id}: {exc}")
                log.append({"ok": False, "instId": p_inst_id, "ordId": p_ord_id, "reason": p_reason, "error": str(exc)})
    return log


def _jev_shadow_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _jev_shadow_base(inst_id: Any) -> str:
    value = str(inst_id or "").upper().split(":")[-1]
    return value.split("-")[0].replace("_USDT", "").replace("USDT", "")


def _jev_shadow_side(value: Any) -> str:
    text = str(value or "").lower()
    if "long" in text or "多" in text:
        return "long"
    if "short" in text or "空" in text:
        return "short"
    return text


def _jev_position_side(position: Mapping[str, Any], fallback: Any = "") -> str:
    side = _jev_shadow_side(position.get("side"))
    if side in {"long", "short"}:
        return side
    pos_side = _jev_shadow_side(position.get("posSide"))
    if pos_side in {"long", "short"}:
        return pos_side
    size = _jev_shadow_float(position.get("pos"), 0.0)
    if size > 0:
        return "long"
    if size < 0:
        return "short"
    return _jev_shadow_side(fallback)


def _jev_shadow_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value) if float(value) < 10_000_000_000 else float(value) / 1000.0
    text = str(value or "").strip()
    if not text:
        return 0.0
    # OKX emits cTime/uTime as digit strings in milliseconds. Parse those
    # before ISO-8601 so a valid position age is not silently reduced to zero.
    try:
        numeric = float(text)
        if numeric == numeric and abs(numeric) != float("inf"):
            return numeric if numeric < 10_000_000_000 else numeric / 1000.0
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _jev_shadow_position_snapshot(position: Dict[str, Any], *, fee_rate: float,
                                  slippage_bps: float) -> Dict[str, Any]:
    size = abs(_jev_shadow_float(position.get("pos"), 0.0))
    entry = _jev_shadow_float(position.get("avgPx"), 0.0)
    mark = _jev_shadow_float(position.get("markPx", position.get("last")), 0.0)
    ct_val = max(0.0, _jev_shadow_float(position.get("ctVal"), 1.0))
    side = _jev_shadow_side(position.get("side", position.get("posSide")))
    bid = _jev_shadow_float(position.get("bidPx"), 0.0)
    ask = _jev_shadow_float(position.get("askPx"), 0.0)
    slip_ratio = max(0.0, slippage_bps) / 10000.0
    if side == "long":
        executable = bid if bid > 0 else mark * (1.0 - slip_ratio)
    else:
        executable = ask if ask > 0 else mark * (1.0 + slip_ratio)
    executable = max(0.0, executable)
    raw_upl = position.get("upl", position.get("unrealized_pnl"))
    if raw_upl is None and entry > 0 and mark > 0:
        raw_upl = size * ct_val * ((mark - entry) if side == "long" else (entry - mark))
    raw_upl = _jev_shadow_float(raw_upl, 0.0)
    fee = size * ct_val * executable * max(0.0, fee_rate)
    slippage_cost = size * ct_val * abs(executable - mark)
    return {
        "size": size,
        "entry_price": entry,
        "mark_price": mark,
        "executable_exit_price": executable,
        "ct_val": ct_val,
        "unrealized_pnl_gross": raw_upl,
        "estimated_exit_fee": fee,
        "estimated_slippage_cost": slippage_cost,
        "shadow_exit_net_pnl": raw_upl - fee - slippage_cost,
    }


def _jev_shadow_position_audit_context(position: Dict[str, Any]) -> Dict[str, Any]:
    """Build a complete, provenance-aware position view for Jev's full audit.

    The independent review intentionally receives a blind view. This second
    view is allowed to contain execution/account facts and the main proposal,
    but missing fields remain explicit instead of being replaced with guesses.
    """
    def _first(*keys: str, default: Any = None) -> Any:
        for key in keys:
            value = position.get(key)
            if value not in (None, "", "--"):
                return value
        return default

    opened_at_ms = _first(
        "cTime", "open_time_ms", "opened_at_ms", "entryTs", "entryTs_ms",
        "entry_time_ms", "entryTimeMs", "entry_order_ts", default=None,
    )
    opened_at = _first("opened_at", "open_time", "entry_time", "entryTime", default=None)
    if opened_at is None and opened_at_ms not in (None, "", "--"):
        opened_ts = _jev_shadow_timestamp(opened_at_ms)
        if opened_ts > 0:
            opened_at = datetime.datetime.fromtimestamp(
                opened_ts, tz=datetime.timezone(datetime.timedelta(hours=8))
            ).isoformat()

    side = _jev_shadow_side(_first("side", "posSide", default=""))
    size = _jev_shadow_float(_first("pos", "pos_sz", default=0), 0.0)
    avg_px = _jev_shadow_float(_first("avgPx", "entry_price", default=0), 0.0)
    mark_px = _jev_shadow_float(_first("markPx", "mark_price", "last", default=0), 0.0)
    stop_px = _jev_shadow_float(_first("trailingStopPx", "trailingSl", "exchangeSl", default=0), 0.0)
    take_px = _jev_shadow_float(_first("takeProfitPx", "exchangeTp", default=0), 0.0)
    exchange_sl = _first("exchangeSl", default=None)
    exchange_tp = _first("exchangeTp", default=None)
    protection_status = _first("protectionStatus", default=None)
    liq_px = _first("liqPx", "liq_price")
    liq_px_available = _jev_shadow_float(liq_px, 0.0) > 0
    margin_mode = str(_first("mgnMode", "margin_mode", default="") or "").lower()
    if liq_px_available:
        liq_px_status = "available"
        liq_px_reason = ""
    elif margin_mode == "cross":
        liq_px_status = "exchange_unavailable"
        liq_px_reason = "cross_margin_account_level_liquidation_price_unavailable"
    else:
        liq_px_status = "missing"
        liq_px_reason = "exchange_position_snapshot_missing_liquidation_price"
    entry_order_id = _first("entry_order_id", "entryOrderId", default=None)
    entry_identity_status = _first("entry_identity_status", default=None)
    if not entry_identity_status:
        entry_identity_status = "matched" if entry_order_id else "unavailable"

    required = {
        "side": side,
        "pos": size,
        "avgPx": avg_px,
        "markPx": mark_px,
        "lever": _first("lever", "leverage"),
        "margin_usdt": _first("margin_usdt", "margin", "imr"),
        "opened_at": opened_at,
        "protection_status": protection_status,
    }
    missing_fields = [key for key, value in required.items()
                      if value in (None, "", "--") or (key in {"pos", "avgPx", "markPx"} and value <= 0)]
    consistency_flags = []
    if side in {"long", "short"} and avg_px > 0:
        if side == "long" and stop_px > 0 and stop_px >= avg_px:
            consistency_flags.append("long_stop_not_below_entry")
        if side == "short" and stop_px > 0 and stop_px <= avg_px:
            consistency_flags.append("short_stop_not_above_entry")
        if side == "long" and take_px > 0 and take_px <= avg_px:
            consistency_flags.append("long_take_profit_not_above_entry")
        if side == "short" and take_px > 0 and take_px >= avg_px:
            consistency_flags.append("short_take_profit_not_below_entry")
    if protection_status in {None, "", "unknown", "unknown_stale", "unknown_unavailable"}:
        consistency_flags.append("protection_status_unconfirmed")
    if protection_status == "fully_protected" and not (exchange_sl or exchange_tp):
        consistency_flags.append("protection_status_without_exchange_orders")

    return {
        "instId": position.get("instId"),
        "venue": position.get("venue", "okx"),
        "environment": position.get("environment"),
        "account_mode": position.get("account_mode"),
        "side": side,
        "pos": position.get("pos", position.get("pos_sz")),
        "avgPx": position.get("avgPx"),
        "markPx": position.get("markPx", position.get("last")),
        "upl": position.get("upl", position.get("unrealized_pnl")),
        "uplRatio": position.get("uplRatio"),
        "lever": _first("lever", "leverage"),
        "notional_usdt": _first("notional_usdt", "notionalUsd"),
        "margin_usdt": _first("margin_usdt", "margin", "imr"),
        "imr": position.get("imr"),
        "mmr": position.get("mmr"),
        "liqPx": liq_px,
        "liqPx_status": liq_px_status,
        "liqPx_source": "okx_position_snapshot",
        "liqPx_reason": liq_px_reason,
        "bePx": position.get("bePx"),
        "mgnMode": position.get("mgnMode"),
        "ctVal": position.get("ctVal"),
        "ccy": position.get("ccy"),
        "adl": position.get("adl"),
        "cTime": position.get("cTime"),
        "uTime": position.get("uTime"),
        "opened_at": opened_at,
        "entryTs": position.get("entryTs"),
        "entryTime": position.get("entryTime"),
        "entry_order_id": entry_order_id,
        "entry_intent_id": position.get("entry_intent_id"),
        "entry_order_ts": position.get("entry_order_ts"),
        "entry_order_avg_px": position.get("entry_order_avg_px"),
        "entry_order_fill_sz": position.get("entry_order_fill_sz"),
        "entry_order_source": position.get("entry_order_source"),
        "entry_identity_status": entry_identity_status,
        "funding_fee": _first("funding_fee", "fundingFee"),
        "realized_pnl": _first("realized_pnl", "realizedPnl"),
        "fee": position.get("fee"),
        "trailingStopPx": position.get("trailingStopPx", position.get("trailingSl")),
        "takeProfitPx": position.get("takeProfitPx"),
        "exchangeSl": exchange_sl,
        "exchangeTp": exchange_tp,
        "protectionStatus": protection_status,
        "protectionAlgoId": position.get("protectionAlgoId"),
        "protectionCoveragePct": position.get("protectionCoveragePct"),
        "protection_orders": position.get("protection_orders", []),
        "protection_snapshot_source": position.get("protection_snapshot_source"),
        "protection_snapshot_error": position.get("protection_snapshot_error"),
        "highWaterMark": position.get("highWaterMark"),
        "lowWaterMark": position.get("lowWaterMark"),
        "stage_desc": position.get("stage_desc", position.get("stageDesc", "")),
        "atr": position.get("atr"),
        "bidPx": position.get("bidPx"),
        "askPx": position.get("askPx"),
        "main_action": position.get("main_action", "HOLD"),
        "main_confidence": position.get("main_confidence", 0),
        "main_suggested_sl_price": position.get("main_suggested_sl_price", 0),
        "main_reason": position.get("main_reason", ""),
        "data_quality": {
            "missing_fields": missing_fields,
            "optional_missing_fields": (["liqPx"] if not liq_px_available else []),
            "consistency_flags": consistency_flags,
            "complete": not missing_fields and not consistency_flags,
            "source": "okx_position_snapshot_plus_local_tracker",
            "liqPx_status": liq_px_status,
            "liqPx_source": "okx_position_snapshot",
            "liqPx_reason": liq_px_reason,
            "entry_identity_status": entry_identity_status,
        },
    }


#: 独立通道允许看到的持仓事实字段（真白名单，fail-closed）。
#:
#: 2026-09-26 之前的实现是「完整 62 键上下文减去 4 个具名 main_* 键」，那是黑名单：
#: 将来任何新增到 `_jev_shadow_position_audit_context` 的字段——只要它换个名字
#: 编码主脑结论（例如 main_relation / proposal_status）——都会自动漏进独立通道，
#: 而当时的注释却宣称两个通道都走白名单纪律。这里改成显式白名单：新增事实字段
#: 只是不会进入独立通道（安全方向），而新增结论字段不可能自动泄漏。
_JEV_NEUTRAL_POSITION_KEYS = (
    "instId", "venue", "environment", "account_mode",
    "side", "pos", "avgPx", "markPx", "upl", "uplRatio",
    "lever", "notional_usdt", "margin_usdt", "imr", "mmr",
    "liqPx", "liqPx_status", "liqPx_source", "liqPx_reason",
    "bePx", "mgnMode", "ctVal", "ccy", "adl", "cTime", "uTime",
    "opened_at", "entryTs", "entryTime",
    "entry_order_id", "entry_intent_id", "entry_order_ts",
    "entry_order_avg_px", "entry_order_fill_sz", "entry_order_source",
    "entry_identity_status",
    "funding_fee", "realized_pnl", "fee",
    "trailingStopPx", "takeProfitPx", "exchangeSl", "exchangeTp",
    "protectionStatus", "protectionAlgoId", "protectionCoveragePct",
    "protection_orders", "protection_snapshot_source", "protection_snapshot_error",
    "highWaterMark", "lowWaterMark", "stage_desc", "atr",
    "bidPx", "askPx", "data_quality",
)


def _jev_neutral_position_state(context: Dict[str, Any]) -> Dict[str, Any]:
    """按 `_JEV_NEUTRAL_POSITION_KEYS` 白名单裁剪持仓事实，剔除主脑结论。"""
    return {key: context.get(key) for key in _JEV_NEUTRAL_POSITION_KEYS}


def _jev_shadow_update_position_outcomes(position_proposals: List[Dict[str, Any]],
                                         position_reviews: List[Dict[str, Any]],
                                         review: Dict[str, Any]) -> None:
    """Persist counterfactual exit snapshots and update fixed post-review horizons."""
    path = os.path.join(DATA_DIR, "jev_shadow_position_outcomes.jsonl")
    now_ts = float(review.get("timestamp") or time.time())
    fee_rate = max(0.0, _jev_shadow_float(os.environ.get("ASTRA_JEV_SHADOW_FEE_RATE", "0.0005"), 0.0005))
    slippage_bps = max(0.0, _jev_shadow_float(os.environ.get("ASTRA_JEV_SHADOW_SLIPPAGE_BPS", "2"), 2.0))
    cycle_seconds = max(60.0, _jev_shadow_float(os.environ.get("ASTRA_JEV_SHADOW_CYCLE_SECONDS", "900"), 900.0))
    horizon_seconds = max(3600.0, _jev_shadow_float(os.environ.get("ASTRA_JEV_SHADOW_HORIZON_4H_SECONDS", "14400"), 14400.0))
    review_by_inst = {str(item.get("instId")): item for item in position_reviews if isinstance(item, dict)}

    ledger = []
    ledger_path = os.path.join(DATA_DIR, "trading_ledger.json")
    try:
        if os.path.exists(ledger_path):
            with open(ledger_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            ledger = loaded if isinstance(loaded, list) else []
    except Exception:
        ledger = []

    def close_for(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        review_ts = _jev_shadow_timestamp(record.get("review_timestamp"))
        base = _jev_shadow_base(record.get("instId"))
        side = _jev_shadow_side(record.get("side"))
        venue = str(record.get("venue") or "").lower()
        candidates = []
        for item in ledger:
            if not isinstance(item, dict) or str(item.get("status", "closed")) != "closed":
                continue
            close_ts = _jev_shadow_timestamp(item.get("close_time") or item.get("time"))
            if close_ts <= review_ts or _jev_shadow_base(item.get("inst") or item.get("name")) != base:
                continue
            item_side = _jev_shadow_side(item.get("side") or item.get("direction"))
            if item_side and side and item_side != side:
                continue
            item_venue = str(item.get("venue") or "").lower()
            if venue and item_venue and venue != item_venue:
                continue
            candidates.append((close_ts, item))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0])
        exact = [item for _, item in candidates
                 if _jev_shadow_identity_match(record, item)]
        if exact:
            return exact[0]
        # An identity-bearing sample must never fall back to same-symbol
        # matching; that would attribute a different completed trade.
        return None if _jev_shadow_has_identity(record) else candidates[0][1]

    def current_for(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        base = _jev_shadow_base(record.get("instId"))
        side = _jev_shadow_side(record.get("side"))
        venue = str(record.get("venue") or "").lower()
        explicit = [proposal for proposal in position_proposals
                    if _jev_shadow_identity_match(record, proposal)]
        if explicit:
            return explicit[0]
        if _jev_shadow_has_identity(record):
            return None
        for proposal in position_proposals:
            if (_jev_shadow_base(proposal.get("instId")) == base and
                    _jev_shadow_side(proposal.get("side")) == side and
                    (not venue or str(proposal.get("venue") or "").lower() == venue)):
                return proposal
        return None

    try:
        from astra_backend.file_locks import file_lock
        retention_days = max(14, min(int(os.environ.get("ASTRA_JEV_POSITION_RETENTION_DAYS", "60")), 180))
        max_records = max(500, min(int(os.environ.get("ASTRA_JEV_POSITION_MAX_RECORDS", "20000")), 50000))
        cutoff = int(time.time()) - retention_days * 24 * 60 * 60
        with file_lock(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            records = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                            # Older shadow ledgers called the paper-fill state
                            # FILLED_AT_REVIEW. Keep those samples alive under
                            # the current lifecycle so their fixed horizons
                            # continue to advance instead of becoming orphaned.
                            if item.get("jev_delayed_entry_status") == "FILLED_AT_REVIEW":
                                item["jev_delayed_entry_status"] = "PAPER_ESTIMATED_FILL"
                                item["jev_status_migrated_from"] = "FILLED_AT_REVIEW"
                            item.setdefault("schema_version", 2)
                            if int(item.get("review_timestamp", 0) or 0) >= cutoff:
                                records.append(item)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue

            for item in records:
                if item.get("status") == "closed":
                    continue
                close = close_for(item)
                current = current_for(item)
                item_ts = _jev_shadow_timestamp(item.get("review_timestamp"))
                if close:
                    close_ts = _jev_shadow_timestamp(close.get("close_time") or close.get("time"))
                    close_pnl = _jev_shadow_float(close.get("net_pnl", close.get("pnl")), 0.0)
                    item["actual_close_time"] = close.get("close_time") or close.get("time")
                    item["actual_close_pnl"] = close_pnl
                    item["actual_close_id"] = (close.get("trade_id") or
                                               close.get("order_id") or
                                               close.get("id", ""))
                    item["max_adverse_excursion"] = min(_jev_shadow_float(item.get("max_adverse_excursion"), 0.0), close_pnl)
                    item["max_favorable_excursion"] = max(_jev_shadow_float(item.get("max_favorable_excursion"), 0.0), close_pnl)
                    if item.get("pnl_after_1_cycle") is None:
                        item["pnl_after_1_cycle"] = close_pnl
                        item["pnl_after_1_cycle_source"] = "actual_close"
                    if close_ts - item_ts >= horizon_seconds and item.get("pnl_after_4h") is None:
                        item["pnl_after_4h"] = close_pnl
                        item["pnl_after_4h_source"] = "actual_close"
                    item["status"] = "closed"
                    continue
                if current:
                    current_snapshot = _jev_shadow_position_snapshot(
                        current, fee_rate=fee_rate, slippage_bps=slippage_bps)
                    item["observation_count"] = int(item.get("observation_count", 0) or 0) + 1
                    item["last_observed_at"] = now_ts
                    current_net = current_snapshot["shadow_exit_net_pnl"]
                    item["max_adverse_excursion"] = min(_jev_shadow_float(item.get("max_adverse_excursion"), 0.0), current_net)
                    item["max_favorable_excursion"] = max(_jev_shadow_float(item.get("max_favorable_excursion"), 0.0), current_net)
                    if item.get("pnl_after_1_cycle") is None and (item["observation_count"] >= 1 or now_ts - item_ts >= cycle_seconds):
                        item["pnl_after_1_cycle"] = current_net
                        item["pnl_after_1_cycle_source"] = "mark_to_market"
                    if item.get("pnl_after_4h") is None and now_ts - item_ts >= horizon_seconds:
                        item["pnl_after_4h"] = current_net
                        item["pnl_after_4h_source"] = "mark_to_market"
                    item["last_mark_price"] = current_snapshot["mark_price"]
                    item["last_mark_net_pnl"] = current_net

            for proposal in position_proposals:
                snapshot = _jev_shadow_position_snapshot(
                    proposal, fee_rate=fee_rate, slippage_bps=slippage_bps)
                position_review = review_by_inst.get(str(proposal.get("instId")), {})
                record_ts = int(now_ts)
                record_id = (f"{review.get('cycle_id', 'cycle-unknown')}:{proposal.get('instId')}"
                             f":{proposal.get('venue', 'okx')}:{proposal.get('side')}")
                records.append({
                    "record_id": record_id,
                    "status": "pending",
                    "review_timestamp": record_ts,
                    "review_time": review.get("time_str", ""),
                    "cycle_id": proposal.get("cycle_id", review.get("cycle_id", "")),
                    "decision_id": proposal.get("decision_id", ""),
                    "entry_order_id": proposal.get("entry_order_id"),
                    "entry_intent_id": proposal.get("entry_intent_id"),
                    "entry_order_ts": proposal.get("entry_order_ts"),
                    "entry_time": proposal.get("entry_time"),
                    "instId": proposal.get("instId", ""),
                    "venue": proposal.get("venue", "okx"),
                    "side": _jev_shadow_side(proposal.get("side")),
                    "size": snapshot["size"],
                    "entry_price": snapshot["entry_price"],
                    "mark_price": snapshot["mark_price"],
                    "executable_exit_price": snapshot["executable_exit_price"],
                    "ct_val": snapshot["ct_val"],
                    "unrealized_pnl_gross": snapshot["unrealized_pnl_gross"],
                    "estimated_exit_fee": snapshot["estimated_exit_fee"],
                    "estimated_slippage_cost": snapshot["estimated_slippage_cost"],
                    "shadow_exit_net_pnl": snapshot["shadow_exit_net_pnl"],
                    "jev_action": position_review.get("suggested_action", "UNKNOWN"),
                    "jev_action_votes": position_review.get("suggested_action_votes", {}),
                    "jev_confidence": position_review.get("jev_confidence", 0),
                    "jev_action_margin": position_review.get("jev_action_margin", 0),
                    "jev_action_status": position_review.get("jev_action_status", "unknown"),
                    "jev_review_status": review.get("status", "unknown"),
                    "main_action": proposal.get("main_action", "HOLD"),
                    "main_confidence": proposal.get("main_confidence", 0),
                    "main_reason": proposal.get("main_reason", ""),
                    "pnl_after_1_cycle": None,
                    "pnl_after_4h": None,
                    "actual_close_pnl": None,
                    "actual_close_time": None,
                    "max_adverse_excursion": min(0.0, snapshot["unrealized_pnl_gross"]),
                    "max_favorable_excursion": max(0.0, snapshot["unrealized_pnl_gross"]),
                    "observation_count": 0,
                    "cost_assumptions": {
                        "fee_rate": fee_rate,
                        "slippage_bps": slippage_bps,
                        "cycle_seconds": cycle_seconds,
                        "horizon_4h_seconds": horizon_seconds,
                    },
                })
            records = records[-max_records:]
            fd, tmp_path = tempfile.mkstemp(prefix=".jev-position-", suffix=".tmp", dir=os.path.dirname(path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for item in records:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
    except Exception as exc:
        print(f"[AI Brain Jev Shadow] warn 平仓反事实台账落盘失败: {exc}")


def _jev_shadow_entry_quote(proposal: Dict[str, Any], action: str,
                            slippage_bps: float, quote_prefix: str = "") -> float:
    """Return an executable quote from the requested decision/response snapshot."""
    action = str(action or "").upper()
    prefix = str(quote_prefix or "")
    mark = _jev_shadow_float(proposal.get(f"{prefix}price", proposal.get("price")), 0.0)
    bid = _jev_shadow_float(proposal.get(f"{prefix}bidPx", proposal.get("bidPx")), 0.0)
    ask = _jev_shadow_float(proposal.get(f"{prefix}askPx", proposal.get("askPx")), 0.0)
    if action == "BUY_LONG":
        quote = ask
        fallback_factor = 1.0 + max(0.0, slippage_bps) / 10000.0
    elif action == "SELL_SHORT":
        quote = bid
        fallback_factor = 1.0 - max(0.0, slippage_bps) / 10000.0
    else:
        return 0.0
    return quote if quote > 0 else (mark * fallback_factor if mark > 0 else 0.0)


def _jev_shadow_entry_size(proposal: Dict[str, Any], entry_price: float,
                           ct_val: float) -> float:
    """Mirror execution sizing constraints for a comparable Jev paper fill."""
    explicit_size = _jev_shadow_float(
        proposal.get("shadow_size", proposal.get("size", 0.0)), 0.0)
    if explicit_size > 0:
        return explicit_size
    # The shadow budget is authoritative once present. This matters when the
    # main decision is WAIT: the normalized cache still carries a default
    # leverage, but it must not silently size an independent Jev trade.
    margin = _jev_shadow_float(proposal.get("shadow_margin_usdt"), 0.0)
    if margin <= 0:
        margin = _jev_shadow_float(proposal.get("margin_usdt"), 0.0)
    leverage = _jev_shadow_float(proposal.get("shadow_leverage"), 0.0)
    if leverage <= 0:
        leverage = _jev_shadow_float(proposal.get("leverage"), 0.0)
    if margin <= 0 or leverage <= 0 or entry_price <= 0 or ct_val <= 0:
        return 0.0
    raw_size = (margin * leverage) / (entry_price * ct_val)
    step = max(0.0000001, _jev_shadow_float(proposal.get("minSz"), 1.0))
    size = int(raw_size / step) * step
    if size <= 0:
        return 0.0
    base_size = _jev_shadow_float(proposal.get("base_sz"), 0.0)
    # Only a real main-decision budget mirrors the execution layer's adaptive
    # base-size clamp. Independent Jev trades use their fixed shadow budget;
    # clamping those to half of base_sz can inflate a small paper trade by
    # orders of magnitude (notably BTC contracts).
    if base_size > 0 and proposal.get("shadow_margin_source") == "main_decision":
        min_allowed = max(step, int((base_size * 0.5) / step) * step)
        max_allowed = int((base_size * 2.0) / step) * step
        size = max(min_allowed, min(max_allowed, size))
    return round(size, 12)


def _jev_shadow_entry_mark_pnl(entry_price: float, mark_price: float, side: str,
                               size: float, ct_val: float, bid_px: float,
                               ask_px: float, fee_rate: float,
                               slippage_bps: float) -> Dict[str, float]:
    """Calculate a round-trip paper PnL using executable entry and exit quotes.

    The quote already contains the spread/impact paid by the hypothetical
    order. Do not subtract the distance from ``mark_price`` a second time.
    """
    if entry_price <= 0 or mark_price <= 0 or size <= 0 or ct_val <= 0:
        return {"gross": 0.0, "net": 0.0, "entry_fee": 0.0,
                "exit_fee": 0.0, "slippage_cost": 0.0}
    slip_ratio = max(0.0, slippage_bps) / 10000.0
    if side == "long":
        exit_price = bid_px if bid_px > 0 else mark_price * (1.0 - slip_ratio)
        gross = size * ct_val * (exit_price - entry_price)
    else:
        exit_price = ask_px if ask_px > 0 else mark_price * (1.0 + slip_ratio)
        gross = size * ct_val * (entry_price - exit_price)
    entry_fee = size * ct_val * entry_price * max(0.0, fee_rate)
    exit_fee = size * ct_val * exit_price * max(0.0, fee_rate)
    # Entry/exit quote selection already realizes spread and fallback slippage.
    slippage_cost = 0.0
    return {
        "gross": gross,
        "net": gross - entry_fee - exit_fee - slippage_cost,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "slippage_cost": slippage_cost,
    }


def _jev_shadow_probability(value: Any) -> float:
    if not isinstance(value, dict):
        return -1.0
    try:
        parsed = float(value.get("probability"))
    except (TypeError, ValueError):
        return -1.0
    return parsed if parsed == parsed and abs(parsed) != float("inf") else -1.0


def _jev_shadow_identity_match(record: Dict[str, Any], ledger_item: Dict[str, Any]) -> bool:
    """Match a live outcome to a ledger row only through explicit identity."""
    pairs = (("decision_id", "decision_id"),
             ("entry_order_id", "entry_order_id"),
             ("entry_order_id", "order_id"),
             ("entry_trade_id", "entry_trade_id"),
             ("entry_trade_id", "trade_id"))
    for record_key, ledger_key in pairs:
        left = str(record.get(record_key) or "").strip()
        right = str(ledger_item.get(ledger_key) or "").strip()
        if left and right and left == right:
            return True
    return False


def _jev_shadow_has_identity(record: Dict[str, Any]) -> bool:
    return any(str(record.get(key) or "").strip()
               for key in ("decision_id", "entry_order_id", "entry_trade_id"))


def _jev_shadow_entry_order_chain(record: Dict[str, Any], ledger_item: Dict[str, Any]) -> bool:
    """Match a legacy fill to a review through a bounded local order intent."""
    try:
        entry_association_window = max(600.0, min(float(os.environ.get(
            "ASTRA_JEV_ENTRY_ASSOCIATION_WINDOW_S", "3600")), 7200.0))
    except (TypeError, ValueError):
        entry_association_window = 3600.0
    review_ts = _jev_shadow_timestamp(record.get("review_timestamp"))
    entry_ts = _jev_shadow_timestamp(
        ledger_item.get("open_time") or ledger_item.get("open_ts") or
        ledger_item.get("entry_time"))
    if review_ts <= 0 or entry_ts <= review_ts:
        return False
    if entry_ts - review_ts > entry_association_window:
        return False
    base = _jev_shadow_base(record.get("instId"))
    side = _jev_shadow_side(record.get("main_action"))
    if (_jev_shadow_base(ledger_item.get("inst") or ledger_item.get("name")) != base or
            _jev_shadow_side(ledger_item.get("side") or ledger_item.get("direction")) != side):
        return False

    intent_path = os.path.join(DATA_DIR, "open_order_intents.json")
    try:
        with open(intent_path, "r", encoding="utf-8") as handle:
            order_intents = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        order_intents = []
    if not isinstance(order_intents, list):
        return False
    for intent in order_intents:
        if not isinstance(intent, dict):
            continue
        intent_ts = _jev_shadow_timestamp(intent.get("ts"))
        if not (review_ts < intent_ts <= entry_ts):
            continue
        if intent_ts - review_ts > entry_association_window:
            continue
        if _jev_shadow_base(intent.get("instId")) != base:
            continue
        intent_side = _jev_shadow_side(intent.get("side"))
        if intent_side == "buy":
            intent_side = "long"
        elif intent_side == "sell":
            intent_side = "short"
        if intent_side == side:
            return True
    return False


def _jev_shadow_update_entry_outcomes(proposals: List[Dict[str, Any]],
                                      instrument_reviews: List[Dict[str, Any]],
                                      active_positions_detail: List[Dict[str, Any]],
                                      review: Dict[str, Any]) -> None:
    """Persist and advance counterfactual entry outcomes.

    The file intentionally stays separate from both the real ledger and the
    Jev exit file. A Jev WAIT is a zero-PnL no-entry baseline; a directional Jev
    answer is marked at the first executable quote available in that review.
    Real main-trade PnL is populated only from a matched live position or a
    closed ledger row, never from the paper calculation.
    """
    path = os.path.join(DATA_DIR, "jev_shadow_entry_outcomes.jsonl")
    now_ts = float(review.get("timestamp") or time.time())
    fee_rate = max(0.0, _jev_shadow_float(
        os.environ.get("ASTRA_JEV_SHADOW_FEE_RATE", "0.0005"), 0.0005))
    slippage_bps = max(0.0, _jev_shadow_float(
        os.environ.get("ASTRA_JEV_SHADOW_SLIPPAGE_BPS", "2"), 2.0))
    cycle_seconds = max(60.0, _jev_shadow_float(
        os.environ.get("ASTRA_JEV_SHADOW_CYCLE_SECONDS", "900"), 900.0))
    horizon_seconds = max(3600.0, _jev_shadow_float(
        os.environ.get("ASTRA_JEV_SHADOW_HORIZON_4H_SECONDS", "14400"), 14400.0))
    package_by_inst = {str(p.get("instId")): p for p in proposals if isinstance(p, dict)}
    review_by_inst = {
        str(item.get("instId")): item for item in instrument_reviews
        if isinstance(item, dict)
    }
    active_positions = [p for p in (active_positions_detail or []) if isinstance(p, dict)]

    ledger = []
    ledger_path = os.path.join(DATA_DIR, "trading_ledger.json")
    try:
        if os.path.exists(ledger_path):
            with open(ledger_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            ledger = loaded if isinstance(loaded, list) else []
    except Exception:
        ledger = []

    def matching_active(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        decision_id = str(record.get("decision_id") or "")
        base = _jev_shadow_base(record.get("instId"))
        side = _jev_shadow_side(record.get("main_action"))
        exact = [p for p in active_positions
                 if _jev_shadow_identity_match(record, p)]
        if exact:
            return exact[0]
        if _jev_shadow_has_identity(record):
            return None
        for position in active_positions:
            if (_jev_shadow_base(position.get("instId")) == base and
                    _jev_position_side(position) == side):
                return position
        return None

    def matching_close(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        review_ts = _jev_shadow_timestamp(record.get("review_timestamp"))
        decision_id = str(record.get("decision_id") or "")
        base = _jev_shadow_base(record.get("instId"))
        side = _jev_shadow_side(record.get("main_action"))
        candidates = []
        for item in ledger:
            if not isinstance(item, dict) or str(item.get("status", "closed")) != "closed":
                continue
            close_ts = _jev_shadow_timestamp(item.get("close_time") or item.get("time"))
            if close_ts <= review_ts:
                continue
            if _jev_shadow_identity_match(record, item):
                candidates.append((close_ts, item, True))
                continue
            if not _jev_shadow_has_identity(record) and (_jev_shadow_base(item.get("inst") or item.get("name")) == base and
                                    _jev_shadow_side(item.get("side") or item.get("direction")) == side):
                candidates.append((close_ts, item, False))
                continue
            if not _jev_shadow_has_identity(record) and _jev_shadow_entry_order_chain(record, item):
                candidates.append((close_ts, item, False))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (not row[2], row[0]))
        return candidates[0][1]

    def current_market(record: Dict[str, Any]) -> Dict[str, Any]:
        package = package_by_inst.get(str(record.get("instId")), {})
        active = matching_active(record) or {}
        return {
            "price": _jev_shadow_float(active.get("markPx"), 0.0) or
                     _jev_shadow_float(package.get("price"), 0.0),
            "bidPx": _jev_shadow_float(active.get("bidPx"), 0.0) or
                     _jev_shadow_float(package.get("bidPx"), 0.0),
            "askPx": _jev_shadow_float(active.get("askPx"), 0.0) or
                     _jev_shadow_float(package.get("askPx"), 0.0),
        }

    def capture_horizon(
            item: Dict[str, Any], field: str, value: Optional[float], source: str,
            target_ts: float, observed_ts: float, *,
            terminal_source: bool = False, allow_baseline_late: bool = False) -> bool:
        """Capture one fixed horizon once; reject stale mark snapshots."""
        if item.get(field) is not None:
            return True
        status_key = f"{field}_measurement_status"
        if item.get(status_key) == "missed_target_window":
            return False
        lag = max(0.0, observed_ts - target_ts)
        item[f"{field}_target_at"] = target_ts
        item[f"{field}_observed_at"] = observed_ts
        item[f"{field}_lag_seconds"] = lag
        if (not terminal_source and not allow_baseline_late
                and lag > cycle_seconds + 1e-9):
            item[status_key] = "missed_target_window"
            item[f"{field}_miss_reason"] = "observation_lag_exceeded_cycle"
            return False
        if value is None:
            return False
        item[field] = value
        item[f"{field}_source"] = source
        item[status_key] = "observed"
        return True

    try:
        from astra_backend.file_locks import file_lock
        retention_days = max(14, min(int(os.environ.get(
            "ASTRA_JEV_POSITION_RETENTION_DAYS", "60")), 180))
        max_records = max(500, min(int(os.environ.get(
            "ASTRA_JEV_POSITION_MAX_RECORDS", "20000")), 50000))
        cutoff = int(time.time()) - retention_days * 24 * 60 * 60
        with file_lock(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            records = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                            # Migrate the pre-lifecycle paper-fill label in
                            # memory; the atomic rewrite below makes the
                            # compatibility change durable.
                            if item.get("jev_delayed_entry_status") == "FILLED_AT_REVIEW":
                                item["jev_delayed_entry_status"] = "PAPER_ESTIMATED_FILL"
                                item["jev_status_migrated_from"] = "FILLED_AT_REVIEW"
                            item.setdefault("schema_version", 2)
                            if int(item.get("review_timestamp", 0) or 0) >= cutoff:
                                records.append(item)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue

            by_id = {str(item.get("record_id")): item for item in records
                     if item.get("record_id")}
            for item in records:
                # A real main trade may close before the four-hour horizon. Keep
                # the row alive so the Jev paper trade can still reach the same
                # fixed horizon instead of ending at the main exit time.
                if item.get("status") == "resolved_4h":
                    continue
                active = matching_active(item)
                close = matching_close(item)
                market = current_market(item)
                item_ts = _jev_shadow_timestamp(item.get("review_timestamp"))
                elapsed = max(0.0, now_ts - item_ts)

                if close:
                    close_pnl = _jev_shadow_float(
                        close.get("net_pnl", close.get("pnl")), 0.0)
                    item["actual_close_pnl"] = close_pnl
                    item["actual_close_time"] = close.get("close_time") or close.get("time")
                    item["actual_close_id"] = close.get("id", "")
                    item["main_trade_pnl"] = close_pnl
                    item["main_trade_pnl_source"] = "trading_ledger"
                    item["main_trade_status"] = "closed"
                    item["actual_entry_price"] = _jev_shadow_float(
                        close.get("open_px") or close.get("entry_px"), 0.0) or None
                    item["actual_size"] = abs(_jev_shadow_float(
                        close.get("sz") or close.get("size"), 0.0)) or None
                    if _jev_shadow_identity_match(item, close):
                        if (item.get("entry_order_id") and
                                item.get("entry_order_id") in {
                                    close.get("entry_order_id"), close.get("order_id")
                                }):
                            item["main_match_method"] = "entry_order_id"
                        else:
                            item["main_match_method"] = "decision_id"
                    elif _jev_shadow_entry_order_chain(item, close):
                        item["main_match_method"] = "bounded_order_intent_chain"
                        item["main_match_reason"] = (
                            "无 decision_id，按同标的同方向、复核后提交且在挂单生命周期内成交的订单意图关联")
                    close_mark = _jev_shadow_float(close.get("close_px"), 0.0)
                    if (close_mark > 0 and item.get("jev_delayed_entry_status") in {
                            "PAPER_ESTIMATED_FILL", "PAPER_ESTIMATED_STALE"}):
                        close_pnl_snapshot = _jev_shadow_entry_mark_pnl(
                            _jev_shadow_float(item.get("jev_delayed_entry_price"), 0.0),
                            close_mark,
                            _jev_shadow_side(item.get("jev_action")),
                            _jev_shadow_float(item.get("jev_delayed_size"), 0.0),
                            _jev_shadow_float(item.get("ct_val"), 1.0),
                            close_mark,
                            close_mark,
                            fee_rate,
                            slippage_bps,
                        )
                        item["jev_pnl_at_main_close"] = close_pnl_snapshot["net"]
                elif active:
                    actual_upl = _jev_shadow_float(
                        active.get("upl", active.get("unrealized_pnl")), 0.0)
                    actual_entry = _jev_shadow_float(active.get("avgPx"), 0.0)
                    actual_size = abs(_jev_shadow_float(active.get("pos"), 0.0))
                    actual_ct_val = max(0.0000001, _jev_shadow_float(
                        active.get("ctVal"), _jev_shadow_float(item.get("ct_val"), 1.0)))
                    actual_side = _jev_shadow_side(
                        active.get("side") or active.get("posSide") or item.get("main_action"))
                    actual_snapshot = _jev_shadow_entry_mark_pnl(
                        actual_entry, market["price"], actual_side, actual_size,
                        actual_ct_val, market["bidPx"], market["askPx"],
                        fee_rate, slippage_bps)
                    item["main_trade_pnl"] = actual_snapshot["net"]
                    item["main_trade_pnl_gross"] = actual_snapshot["gross"]
                    item["main_trade_upl"] = actual_upl
                    item["main_trade_pnl_source"] = "live_position_mark_net"
                    item["main_trade_status"] = "open"
                    item["actual_entry_price"] = actual_entry
                    item["actual_size"] = actual_size
                elif item.get("main_action") != "WAIT":
                    legacy_position = next(
                        (candidate for candidate in active_positions
                         if _jev_shadow_base(candidate.get("instId")) ==
                         _jev_shadow_base(item.get("instId")) and
                         _jev_position_side(candidate) ==
                         _jev_shadow_side(item.get("main_action"))),
                        None,
                    )
                    if legacy_position and item.get("decision_id"):
                        item["main_trade_status"] = "unmatched_legacy_position"
                        item["main_match_reason"] = "同标的同方向持仓缺少 decision_id，未强行关联"
                    else:
                        item.setdefault("main_trade_status", "not_observed")

                jev_status = item.get("jev_delayed_entry_status")
                if jev_status in {"PAPER_ESTIMATED_FILL", "PAPER_ESTIMATED_STALE"}:
                    side = _jev_shadow_side(item.get("jev_action"))
                    pnl = _jev_shadow_entry_mark_pnl(
                        _jev_shadow_float(item.get("jev_delayed_entry_price"), 0.0),
                        market["price"], side,
                        _jev_shadow_float(item.get("jev_delayed_size"), 0.0),
                        _jev_shadow_float(item.get("ct_val"), 1.0),
                        market["bidPx"], market["askPx"], fee_rate, slippage_bps)
                    item["jev_delayed_entry_pnl"] = pnl["net"]
                    item["jev_delayed_entry_pnl_gross"] = pnl["gross"]
                    item["jev_no_entry_pnl"] = 0.0
                    item["max_adverse_excursion"] = min(
                        _jev_shadow_float(item.get("max_adverse_excursion"), 0.0), pnl["net"])
                    item["max_favorable_excursion"] = max(
                        _jev_shadow_float(item.get("max_favorable_excursion"), 0.0), pnl["net"])
                    if elapsed >= cycle_seconds:
                        capture_horizon(
                            item, "pnl_after_1_cycle", pnl["net"],
                            "mark_to_market", item_ts + cycle_seconds, now_ts)
                    if elapsed >= horizon_seconds:
                        capture_horizon(
                            item, "pnl_after_4h", pnl["net"],
                            "mark_to_market", item_ts + horizon_seconds, now_ts)
                elif jev_status in {"WAIT_BASELINE", "NO_EDGE"}:
                    item["jev_delayed_entry_pnl"] = None
                    item["jev_no_entry_pnl"] = 0.0
                    if elapsed >= cycle_seconds:
                        capture_horizon(
                            item, "pnl_after_1_cycle", 0.0,
                            "no_entry_baseline", item_ts + cycle_seconds, now_ts,
                            allow_baseline_late=True)
                    if elapsed >= horizon_seconds:
                        capture_horizon(
                            item, "pnl_after_4h", 0.0,
                            "no_entry_baseline", item_ts + horizon_seconds, now_ts,
                            allow_baseline_late=True)

                main_source = str(item.get("main_trade_pnl_source") or "")
                main_terminal = main_source in {
                    "actual_close", "trading_ledger", "live_position_close_net"}
                if elapsed >= cycle_seconds:
                    if item.get("main_action") == "WAIT":
                        capture_horizon(
                            item, "main_pnl_after_1_cycle", 0.0,
                            "no_entry_baseline", item_ts + cycle_seconds, now_ts,
                            allow_baseline_late=True)
                    elif item.get("main_trade_pnl") is not None:
                        capture_horizon(
                            item, "main_pnl_after_1_cycle", item["main_trade_pnl"],
                            main_source or "mark_to_market", item_ts + cycle_seconds,
                            now_ts, terminal_source=main_terminal)
                if elapsed >= horizon_seconds:
                    if item.get("main_action") == "WAIT":
                        capture_horizon(
                            item, "main_pnl_after_4h", 0.0,
                            "no_entry_baseline", item_ts + horizon_seconds, now_ts,
                            allow_baseline_late=True)
                    elif item.get("main_trade_pnl") is not None:
                        capture_horizon(
                            item, "main_pnl_after_4h", item["main_trade_pnl"],
                            main_source or "mark_to_market", item_ts + horizon_seconds,
                            now_ts, terminal_source=main_terminal)

                if item.get("main_trade_pnl") is not None and item.get("jev_delayed_entry_pnl") is not None:
                    item["jev_minus_main_pnl"] = (
                        item["jev_delayed_entry_pnl"] - item["main_trade_pnl"])
                    item["jev_better_than_main"] = item["jev_minus_main_pnl"] > 0
                if elapsed >= horizon_seconds:
                    if item.get("jev_delayed_entry_status") in {
                            "JEV_UNAVAILABLE", "AMBIGUOUS", "INVALID_DATA",
                            "MISSING_DATA", "NOT_READY", "NO_PRICE", "NO_SIZE",
                            "NOT_ENTRY"}:
                        item["status"] = "unresolved_4h"
                    else:
                        item["status"] = "resolved_actual_close" if close else "resolved_4h"
                    if (item.get("main_action") != "WAIT" and
                            item.get("main_trade_pnl") is None and
                            item.get("main_trade_status") not in {"unmatched_legacy_position"}):
                        item["main_trade_status"] = "unmatched_after_horizon"
                        item["main_match_reason"] = "复核后窗口内未观察到带 decision_id 的真实成交或平仓"
                elif close:
                    item["status"] = "main_closed_pending_4h"
                else:
                    item["status"] = "pending"

            for proposal in proposals:
                    inst_id = str(proposal.get("instId") or "")
                    shadow = review_by_inst.get(inst_id) or {
                        "suggested_action": "UNKNOWN",
                        "suggested_action_votes": {},
                        "jev_confidence": 0,
                        "jev_action_margin": 0,
                        "jev_action_status": "unavailable",
                    }
                    main_action = str(proposal.get("action") or "WAIT").upper()
                    jev_action = str(shadow.get("suggested_action") or "UNKNOWN").upper()
                    entry_mode = str(proposal.get("entry_mode") or "initial")
                    if review.get("status") != "ok":
                        jev_status_override = "JEV_UNAVAILABLE"
                    elif entry_mode == "position_management":
                        # Active positions are evaluated by the position review
                        # lane. Do not turn a directional answer on an already
                        # open position into a fictitious new entry sample.
                        jev_status_override = "NOT_ENTRY"
                    # 2026-09-26 拆分：数据类状态改由代码侧判定；`no_edge` 是**判断**
                    # （市场没给方向），不是数据故障，因此单独归类，不再与
                    # `INVALID_DATA` 混在一起 —— 旧口径把 65/80 个候选标成
                    # `invalid_data`，而它们的代码 `data_quality` 全是 valid。
                    elif shadow.get("jev_action_status") in {
                            "code_state_incomplete", "code_state_inconsistent"}:
                        jev_status_override = "STATE_DEFECT"
                    elif shadow.get("jev_action_status") == "no_edge":
                        jev_status_override = "NO_EDGE"
                    elif shadow.get("jev_action_status") in {
                            "invalid_data", "missing_data_valid"}:
                        # 兼容拆分前的旧记录，新记录不再产生这两个状态。
                        jev_status_override = "INVALID_DATA"
                    elif shadow.get("jev_action_status") in {
                            "insufficient_data", "missing_action_votes"}:
                        jev_status_override = "MISSING_DATA"
                    elif shadow.get("jev_action_status") in {
                            "not_ready", "missing_execution_ready"}:
                        jev_status_override = "NOT_READY"
                    elif jev_action == "UNKNOWN":
                        jev_status_override = "AMBIGUOUS"
                    else:
                        jev_status_override = ""
                    if review.get("status") != "ok":
                        base_sample_type = "jev_api_error"
                    elif jev_status_override == "NOT_ENTRY":
                        base_sample_type = "not_entry_position_management"
                    elif jev_status_override in {"NOT_READY", "MISSING_DATA"}:
                        base_sample_type = "jev_not_ready"
                    elif jev_status_override in {"STATE_DEFECT", "INVALID_DATA", "AMBIGUOUS"}:
                        base_sample_type = "jev_invalid_or_ambiguous"
                    elif main_action == "WAIT" and jev_action == "WAIT":
                        base_sample_type = "both_wait"
                    elif main_action != "WAIT" and jev_action == "WAIT":
                        base_sample_type = "main_trade_jev_wait"
                    elif main_action == "WAIT" and jev_action != "WAIT":
                        base_sample_type = "main_wait_jev_trade"
                    elif main_action == jev_action:
                        base_sample_type = "aligned_trade"
                    elif {main_action, jev_action} == {"BUY_LONG", "SELL_SHORT"}:
                        base_sample_type = "opposite_trade"
                    else:
                        base_sample_type = "direction_disagreement"
                    sample_type = (f"{entry_mode}_{base_sample_type}"
                                   if entry_mode != "initial" else base_sample_type)
                    record_id = f"{proposal.get('decision_id') or review.get('cycle_id')}:{inst_id}"
                    if record_id in by_id:
                        continue
                    ct_val = max(0.0000001, _jev_shadow_float(proposal.get("ctVal"), 1.0))
                    main_requested = _jev_shadow_float(proposal.get("entry_price"), 0.0)
                    main_quote = _jev_shadow_entry_quote(
                        proposal, main_action, slippage_bps, quote_prefix="main_")
                    jev_quote = _jev_shadow_entry_quote(
                        proposal, jev_action, slippage_bps, quote_prefix="jev_")
                    jev_side = _jev_shadow_side(jev_action)
                    jev_size = _jev_shadow_entry_size(proposal, jev_quote, ct_val)
                    if jev_status_override:
                        jev_status = jev_status_override
                    elif jev_action == "WAIT":
                        jev_status = "WAIT_BASELINE"
                    elif jev_quote <= 0:
                        jev_status = "NO_PRICE"
                    elif jev_size <= 0:
                        jev_status = "NO_SIZE"
                    elif proposal.get("jev_quote_error"):
                        jev_status = "PAPER_ESTIMATED_STALE"
                    else:
                        jev_status = "PAPER_ESTIMATED_FILL"
                    if jev_status == "NOT_ENTRY":
                        jev_quote = 0.0
                        jev_size = 0.0
                    initial_jev_pnl = None
                    if jev_status in {"PAPER_ESTIMATED_FILL", "PAPER_ESTIMATED_STALE"}:
                        initial_jev_pnl = _jev_shadow_entry_mark_pnl(
                            jev_quote,
                            _jev_shadow_float(proposal.get("jev_price", proposal.get("price")), 0.0),
                            jev_side,
                            jev_size,
                            ct_val,
                            _jev_shadow_float(proposal.get("jev_bidPx", proposal.get("bidPx")), 0.0),
                            _jev_shadow_float(proposal.get("jev_askPx", proposal.get("askPx")), 0.0),
                            fee_rate,
                            slippage_bps,
                        )
                    active = matching_active({
                        "decision_id": proposal.get("decision_id", ""),
                        "instId": inst_id,
                        "main_action": main_action,
                    })
                    record = {
                        "record_id": record_id,
                        "schema_version": 2,
                        "status": "pending",
                        "review_timestamp": int(now_ts),
                        "review_time": review.get("time_str", ""),
                        "cycle_id": proposal.get("cycle_id", review.get("cycle_id", "")),
                        "decision_id": proposal.get("decision_id", ""),
                        "instId": inst_id,
                        "sample_type": sample_type,
                        "entry_mode": entry_mode,
                        "entry_eligible": entry_mode in {"initial", "scale_in"},
                        "evaluation_scope": (
                            "directional_entry_fixed_horizon"
                            if entry_mode in {"initial", "scale_in"}
                            else "not_entry_position_management"),
                        "full_strategy_pnl": False,
                        "funding_fee_modeled": False,
                        "review_status": review.get("status", "unknown"),
                        "review_error": review.get("error", ""),
                        "main_action": main_action,
                        "main_raw_action": proposal.get("main_raw_action", main_action),
                        "main_confidence": proposal.get("confidence", 0),
                        "main_raw_confidence": proposal.get("main_raw_confidence", proposal.get("confidence", 0)),
                        "main_decision_reason": proposal.get("main_decision_reason", ""),
                        "main_decision_outcome_source": proposal.get("main_decision_outcome_source", "unknown"),
                        "main_decision_rejection_code": proposal.get("main_decision_rejection_code", ""),
                        "main_entry_price": main_requested,
                        "main_executable_entry_price": main_quote,
                        "jev_action": jev_action,
                        "jev_action_votes": shadow.get("suggested_action_votes", {}),
                        "jev_confidence": shadow.get("jev_confidence", 0),
                        "jev_delayed_entry_price": jev_quote,
                        "jev_delayed_entry_status": jev_status,
                        "shadow_size_source": proposal.get("shadow_margin_source", "unknown"),
                        "shadow_leverage_source": proposal.get("shadow_leverage_source", "unknown"),
                        "jev_delayed_size": jev_size,
                        "jev_quote_timestamp": proposal.get("jev_quote_timestamp"),
                        "jev_quote_source": proposal.get("jev_quote_source", "okx"),
                        "jev_quote_error": proposal.get("jev_quote_error", ""),
                        "jev_entry_latency_seconds": max(
                            0.0,
                            _jev_shadow_float(proposal.get("jev_quote_timestamp"), now_ts)
                            - _jev_shadow_float(proposal.get("main_quote_timestamp"), now_ts),
                        ),
                        "jev_action_margin": shadow.get("jev_action_margin", 0),
                        "jev_action_status": shadow.get("jev_action_status", "unknown"),
                        "jev_no_entry_pnl": 0.0,
                        "main_trade_pnl": None,
                        "jev_delayed_entry_pnl": (
                            initial_jev_pnl["net"] if initial_jev_pnl else None),
                        "jev_delayed_entry_pnl_gross": (
                            initial_jev_pnl["gross"] if initial_jev_pnl else None),
                        "pnl_after_1_cycle": None,
                        "pnl_after_4h": None,
                        "main_pnl_after_1_cycle": None,
                        "main_pnl_after_4h": None,
                        "actual_close_pnl": None,
                        "actual_close_time": None,
                        "max_adverse_excursion": min(
                            0.0, initial_jev_pnl["net"] if initial_jev_pnl else 0.0),
                        "max_favorable_excursion": max(
                            0.0, initial_jev_pnl["net"] if initial_jev_pnl else 0.0),
                        "ct_val": ct_val,
                        "actual_entry_price": _jev_shadow_float(active.get("avgPx"), 0.0) if active else None,
                        "actual_size": abs(_jev_shadow_float(active.get("pos"), 0.0)) if active else None,
                        "main_trade_status": "open" if active else (
                            "expected_entry" if main_action != "WAIT" else "no_main_entry"),
                        "cost_assumptions": {
                            "fee_rate": fee_rate,
                            "slippage_bps": slippage_bps,
                            "cycle_seconds": cycle_seconds,
                            "horizon_4h_seconds": horizon_seconds,
                            "jev_entry_policy": "post_response_quote_paper_estimate",
                        },
                    }
                    if active:
                        actual_entry = _jev_shadow_float(active.get("avgPx"), 0.0)
                        actual_size = abs(_jev_shadow_float(active.get("pos"), 0.0))
                        actual_ct_val = max(0.0000001, _jev_shadow_float(
                            active.get("ctVal"), ct_val))
                        actual_side = _jev_shadow_side(
                            active.get("side") or active.get("posSide") or main_action)
                        actual_mark = _jev_shadow_float(
                            active.get("markPx", active.get("last")), 0.0)
                        actual_bid = _jev_shadow_float(active.get("bidPx"), 0.0)
                        actual_ask = _jev_shadow_float(active.get("askPx"), 0.0)
                        actual_snapshot = _jev_shadow_entry_mark_pnl(
                            actual_entry, actual_mark, actual_side, actual_size,
                            actual_ct_val, actual_bid, actual_ask,
                            fee_rate, slippage_bps)
                        record["main_trade_pnl"] = actual_snapshot["net"]
                        record["main_trade_pnl_gross"] = actual_snapshot["gross"]
                        record["main_trade_upl"] = _jev_shadow_float(
                            active.get("upl", active.get("unrealized_pnl")), 0.0)
                        record["main_trade_pnl_source"] = "live_position_mark_net"
                    records.append(record)
                    by_id[record_id] = record

            records = records[-max_records:]
            fd, tmp_path = tempfile.mkstemp(prefix=".jev-entry-", suffix=".tmp",
                                             dir=os.path.dirname(path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for item in records:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
    except Exception as exc:
        print(f"[AI Brain Jev Shadow] warn 开仓反事实台账落盘失败: {exc}")


def _jev_policy_probability(value: Any, default: float = -1.0) -> float:
    """Read a provider probability without treating missing data as zero."""
    raw = value
    if isinstance(value, Mapping):
        for key in ("probability", "prob", "confidence", "value", "noul"):
            if key in value:
                raw = value[key]
                break
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return default
    if result != result or result < 0:
        return default
    if result > 1 and result <= 100:
        result /= 100.0
    return min(1.0, result)


def _jev_normalize_choice(value: Any) -> str:
    """Normalize a choice answer from native or text-based providers."""
    if isinstance(value, Mapping):
        for key in ("choice", "answer", "value", "label", "text"):
            if key in value:
                value = value[key]
                break
    text = str(value or "").strip().upper().replace(" ", "_")
    aliases = {
        "BUY": "BUY_LONG",
        "LONG": "BUY_LONG",
        "SELL": "SELL_SHORT",
        "SHORT": "SELL_SHORT",
        "WAIT_OR_KEEP": "WAIT",
        "KEEP": "WAIT",
        "INSUFFICIENT": "INSUFFICIENT_DATA",
        "NOT_ENOUGH_DATA": "INSUFFICIENT_DATA",
        "CLOSE": "CLOSE_MARKET",
        "UPDATE_STOP": "UPDATE_SL",
    }
    return aliases.get(text, text)


#: 中性候选 state 中，做任何方向判断所必需的字段。缺失即「不完整」。
_JEV_REQUIRED_CANDIDATE_FIELDS = (
    "instId", "price", "bidPx", "askPx", "direction_observation", "direction_layers",
)

#: 中性持仓 state 中，做任何持仓管理判断所必需的字段。
_JEV_REQUIRED_POSITION_FIELDS = ("instId", "side", "pos", "avgPx", "markPx")


def _jev_blank(value: Any) -> bool:
    """字段是否为空缺（None / 空串 / 空容器）。0 与 False 不算空缺。"""
    if value is None or value == "":
        return True
    return isinstance(value, (list, dict, tuple, set)) and not value


def _jev_candidate_state_quality(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    """代码侧确定性判定候选 state 的「完整性」与「一致性」。

    这两项都是**载荷的属性**，不涉及任何方向判断，因此必须由代码判定，且只能由
    代码触发 `INSUFFICIENT_DATA`。模型答的是「有没有方向性优势」，那是市场判断
    （见 CONTEXT.md「数据属性」一节）——此前两者被合并成同一个问题，导致代码
    侧 `data_quality` 恒为 valid 而模型对优势的判断被记成「数据无效」。
    """
    missing = [key for key in _JEV_REQUIRED_CANDIDATE_FIELDS
               if _jev_blank(candidate.get(key))]
    flags: List[str] = []
    price = _jev_shadow_float(candidate.get("price"), 0.0)
    bid = _jev_shadow_float(candidate.get("bidPx"), 0.0)
    ask = _jev_shadow_float(candidate.get("askPx"), 0.0)
    if price <= 0:
        flags.append("price_not_positive")
    if bid > 0 and ask > 0:
        if ask < bid:
            flags.append("crossed_book")
        else:
            if not (bid * 0.5 <= price <= ask * 1.5):
                flags.append("price_outside_quote")
    observation = candidate.get("direction_observation")
    if isinstance(observation, Mapping):
        # 代码当时就没能构成观测（缺层/取数失败/时间戳错位），是数据问题。
        if str(observation.get("status") or "") == "INSUFFICIENT_DATA":
            flags.append("direction_observation_insufficient")
        brain_ts = observation.get("brain_candle_ts_4h")
        trader_ts = observation.get("trader_candle_ts_4h")
        if isinstance(brain_ts, int) and isinstance(trader_ts, int):
            # 与 compare_directions 的 data_ready 同一容差，避免比代码更严。
            if abs(brain_ts - trader_ts) > 4 * 3600 * 1000:
                flags.append("candle_timestamp_mismatch")
        position = observation.get("price_position_in_range")
        if isinstance(position, (int, float)) and not 0.0 <= position <= 1.0:
            flags.append("position_in_range_out_of_bounds")
    layers = candidate.get("direction_layers")
    if isinstance(layers, Mapping):
        absent = [name for name in ("direction_4h", "strength_1h", "entry_15m")
                  if _jev_blank(layers.get(name))]
        if absent:
            flags.append("direction_layers_incomplete")
    # 注意：`direction_observation.status == CONFLICT` **不算**不一致 ——
    # 多周期证据互相矛盾是**市场事实**，不是载荷损坏。把它当数据问题会把
    # 真实的分歧信息误判成数据故障。
    return {
        "complete": not missing,
        "missing_fields": missing,
        "consistent": not flags,
        "consistency_flags": flags,
        "status": ("insufficient_data" if missing else
                   ("inconsistent" if flags else "ok")),
    }


def _jev_position_state_quality(position: Mapping[str, Any]) -> Dict[str, Any]:
    """代码侧确定性判定持仓 state 的完整性与一致性（同候选侧口径）。"""
    missing = [key for key in _JEV_REQUIRED_POSITION_FIELDS
               if _jev_blank(position.get(key))]
    flags: List[str] = []
    side = _jev_shadow_side(position.get("side"))
    if side not in {"long", "short"}:
        flags.append("side_unrecognized")
    if _jev_shadow_float(position.get("pos"), 0.0) <= 0:
        flags.append("size_not_positive")
    if _jev_shadow_float(position.get("avgPx"), 0.0) <= 0:
        flags.append("entry_price_not_positive")
    if _jev_shadow_float(position.get("markPx"), 0.0) <= 0:
        flags.append("mark_price_not_positive")
    return {
        "complete": not missing,
        "missing_fields": missing,
        "consistent": not flags,
        "consistency_flags": flags,
        "status": ("insufficient_data" if missing else
                   ("inconsistent" if flags else "ok")),
    }


def _jev_rank_votes(votes: Mapping[str, Any], *, min_confidence: float,
               min_margin: float,
               execution_ready: Any = None,
               execution_ready_min: float = 0.5,
               wait_min_confidence: Optional[float] = None) -> Dict[str, Any]:
    """按三个兼容票的原始绝对分数排序，再保守判断是否接受。

    三个 Noul 分别是「会不会做多 / 会不会做空 / 会不会等待」的独立兼容分，
    **不是**互斥类别概率。不能把它们除以总和后伪装成 categorical distribution：
    `0.20 / 0.10 / 0.00` 会被错误放大成 `0.67 / 0.33 / 0.00`，从而把三个都很弱
    的答案制造成高置信方向。

    因此 `confidence` 保留最高原始兼容分，`action_margin` 只表示最高分与次高分
    的启发式分离度；两者都不声称是校准后的分类概率。返回键 `probabilities` 为
    兼容旧读取器而保留，但值就是原始分数，不再归一化。
    """
    raw = {str(key): _jev_policy_probability(value) for key, value in votes.items()}
    usable = {key: value for key, value in raw.items() if value >= 0}
    if not usable:
        return {
            "suggested_action": "INSUFFICIENT_DATA",
            "probabilities": raw,
            "confidence": 0.0,
            "action_margin": 0.0,
            "raw_max_vote": -1.0,
            "vote_sum": 0.0,
            "action_status": "missing_action_votes",
        }
    total = sum(usable.values())
    scores = dict(usable)
    ranked = sorted(scores.values(), reverse=True)
    suggested_action = max(scores, key=scores.get)
    confidence = scores[suggested_action]
    second = ranked[1] if len(ranked) > 1 else -1.0
    margin = confidence - second if second >= 0 else -1.0
    raw_max_vote = max(usable.values())
    # 2026-09-26 拆分：这里**不再**看模型的 data_valid —— 载荷的完整性与一致性
    # 由代码侧 `_jev_candidate_state_quality` 判定，且只有它能触发
    # `INSUFFICIENT_DATA`。模型侧的答案改问「有没有方向性优势」，低分意味着
    # 「无优势」（一个市场判断），由调用方映射成 WAIT，不再冒充数据错误。
    ready_probability = _jev_policy_probability(execution_ready)
    required_confidence = (
        wait_min_confidence
        if suggested_action == "WAIT" and wait_min_confidence is not None
        else min_confidence
    )
    if (suggested_action in ENTRY_ACTIONS and execution_ready is not None
          and ready_probability < 0):
        status = "missing_execution_ready"
        suggested_action = "INSUFFICIENT_DATA"
    elif (suggested_action in ENTRY_ACTIONS and execution_ready is not None
          and ready_probability < execution_ready_min):
        status = "not_ready"
        suggested_action = "WAIT"
    elif confidence < required_confidence:
        status = "low_confidence"
        suggested_action = "WAIT"
    elif margin >= 0 and margin < min_margin:
        status = "ambiguous"
        suggested_action = "WAIT"
    else:
        status = "accepted"
    return {
        "suggested_action": suggested_action,
        # 兼容历史 schema；这些值是独立兼容分，不是和为 1 的分类概率。
        "probabilities": scores,
        "confidence": confidence if confidence >= 0 else -1.0,
        "action_margin": margin if margin >= 0 else -1.0,
        "action_status": status,
        "confidence_threshold": required_confidence,
        # 保留 -1.0「未作答」哨兵：max(0.0, x) 会把「没问过」压成 0.0，
        # 与「模型明确答 0.0」无法区分，下游任何求均值都会把未测量的行
        # 当成强烈否定（2026-09-26 的 98% 恒 REJECT 就是这个失效模式）。
        # `audit_probabilities` 一直保留哨兵，这里补齐同源字段。
        "execution_ready_probability": ready_probability,
        # 归一化前的原始最大值与票和，便于事后核对归一化是否是合理的加工。
        "raw_max_vote": raw_max_vote,
        "vote_sum": total,
    }


def _jev_audit_verdict(flags: Iterable[str], *, data_complete: Any = None,
                  min_confidence: float = 0.70,
                  data_complete_min: float = 0.5,
                  has_proposal: bool = True) -> Dict[str, Any]:
    """Turn atomic audit answers into a deterministic shadow verdict.

    ``has_proposal=False`` means the main action is WAIT: there is no entry, stop,
    target, size or leverage to verify, so a REJECT would be an artifact of asking
    about something that does not exist. Report NOT_APPLICABLE instead and keep it
    out of the rejection statistics.
    """
    if not has_proposal:
        return {"verdict": "NOT_APPLICABLE", "flags": [],
                "confidence_floor": min_confidence}
    normalized = [str(flag) for flag in flags if flag]
    complete_probability = _jev_policy_probability(data_complete)
    if data_complete is not None and complete_probability >= 0 and complete_probability < data_complete_min:
        normalized.append("proposal_data_incomplete")
    hard_flags = {
        "proposal_data_incomplete",
        "stop_structure_invalid",
        "reward_after_cost_insufficient",
        "direction_conflict",
    }
    if any(flag in hard_flags for flag in normalized):
        verdict = "REJECT"
    elif normalized:
        verdict = "REVIEW"
    else:
        verdict = "APPROVE"
    return {
        "verdict": verdict,
        "flags": list(dict.fromkeys(normalized)),
        "confidence_floor": min_confidence,
    }


def _jev_policy_relation(main_action: Any, independent_action: Any, *, data_status: str,
             audit_verdict_value: str = "APPROVE",
             action_status: str = "") -> str:
    """Classify the independent Jev result relative to the main action."""
    main = _jev_normalize_choice(main_action)
    independent = _jev_normalize_choice(independent_action)
    if audit_verdict_value == "REJECT":
        return "AUDIT_REJECT"
    if data_status not in {"valid", "accepted"} or independent in {"", "INSUFFICIENT_DATA"}:
        return "ABSTAIN"
    if str(action_status or "") in {
        "low_confidence", "ambiguous", "not_ready",
        "missing_action_votes", "missing_execution_ready",
    }:
        return "ABSTAIN"
    if main in ENTRY_ACTIONS and independent == "WAIT":
        return "WAIT_VS_ENTRY"
    if main == "WAIT" and independent in ENTRY_ACTIONS:
        return "MAIN_WAIT_JEV_ENTRY"
    if main in ENTRY_ACTIONS and independent in ENTRY_ACTIONS and main != independent:
        return "OPPOSITE_DIRECTION"
    if main == independent:
        return "AGREE"
    return "ABSTAIN"


#: 方案 §6 允许的执行档位，按强度递增。
#:
#: `shadow` 是唯一**在结构上不可能改变主脑执行**的档位，因此任何无法识别的配置值
#: 都必须回落到它 —— 环境变量笔误绝不能把观察者变成门禁（fail-closed 到安全侧）。
_JEV_ENFORCEMENT_MODES = ("shadow", "review", "soft_veto", "hard_veto")


def _jev_resolve_enforcement(configured: Any) -> Dict[str, Any]:
    """把 `ASTRA_JEV_ENFORCEMENT` 解析成合法档位。

    方案 §6 要求 `shadow` / `review` / `soft_veto` **可配置回滚、不得通过修改代码
    切换**。此前的实现把 `enforcement_mode` 硬编码成 `"shadow"`，只把环境变量记进
    `configured_enforcement` —— 于是环境变量**读了却不生效**，而切档位恰恰必须改
    代码，与 §6 正好相反。
    """
    raw = str(configured or "").strip().lower()
    if raw in _JEV_ENFORCEMENT_MODES:
        return {"mode": raw, "configured": raw, "valid": True,
                "reason": "configured"}
    return {"mode": "shadow", "configured": raw, "valid": False,
            "reason": "unknown_enforcement_falls_back_to_shadow"}


def _jev_enforcement_decision(*, mode: str, main_action: Any, independent: Mapping[str, Any],
                              entry_mode: str, hard_gates_passed: bool,
                              hard_veto_code_only: bool = True,
                              veto_wait_min_confidence: float = 0.70,
                              veto_min_margin: float = 0.15) -> Dict[str, Any]:
    """按方案 §9 判定本候选在各档位下**应当**被如何处理（只判定，不执行）。

    `no_edge` 是观测标签，只表示 WAIT 已通过较低的分类门槛；它不能自动成为否决
    资格。明确 WAIT 还必须独立通过 veto confidence 和 margin，避免为了保留 no_edge
    可观测性而意外降低风险门槛。

    返回的是「若该档位已启用，本候选会被怎么处理」，用于落盘与评估。它**不**改变
    主脑动作：阶段 C/D 的执行门控需要先有样本外证据（§9 阶段 D），在没有证据之前
    把判定接到执行上，等于用未标定的信号动真钱。
    """
    normalized_mode = str(mode or "shadow").strip().lower()
    if normalized_mode not in _JEV_ENFORCEMENT_MODES:
        normalized_mode = "shadow"
    main = _jev_normalize_choice(main_action)
    independent_action = _jev_normalize_choice(independent.get("suggested_action"))
    data_status = str(independent.get("data_status") or "invalid")
    action_status = str(independent.get("action_status") or "")
    confidence = _jev_policy_probability(independent.get("confidence"))
    action_margin = _jev_policy_probability(independent.get("action_margin"))
    reasons: List[str] = []

    classification_thresholds_passed = action_status in {"accepted", "no_edge"}
    explicit_wait = action_status == "no_edge"
    opposite = (main in ENTRY_ACTIONS and independent_action in ENTRY_ACTIONS
                and independent_action != main)
    # VETO_MIN_ACTION_MARGIN 是通用 enforcement 门槛：反向方向和明确 WAIT
    # 都必须重新通过，不能只沿用各自较低的分类门槛。
    opposite_veto_thresholds_passed = (
        opposite
        and action_status == "accepted"
        and action_margin >= veto_min_margin
    )
    wait_veto_thresholds_passed = (
        explicit_wait
        and confidence >= veto_wait_min_confidence
        and action_margin >= veto_min_margin
    )
    veto_thresholds_passed = (
        opposite_veto_thresholds_passed or wait_veto_thresholds_passed
    )

    def _result(decision: str, result_reasons: List[str], *,
                veto_eligible: bool = False) -> Dict[str, Any]:
        return {
            "decision": decision,
            "mode": normalized_mode,
            "reasons": result_reasons,
            "affects_execution": False,
            "veto_eligible": veto_eligible,
            "veto_thresholds_passed": veto_thresholds_passed,
            "veto_wait_min_confidence": veto_wait_min_confidence,
            "veto_min_action_margin": veto_min_margin,
        }

    if not hard_gates_passed:
        return _result("HARD_VETO", ["code_hard_gate_failed"])

    eligible = True
    if main not in ENTRY_ACTIONS:
        eligible = False
        reasons.append("main_not_a_new_entry")
    if str(entry_mode or "") != "initial":
        eligible = False
        reasons.append("not_an_initial_entry")
    if data_status != "valid":
        eligible = False
        reasons.append("independent_data_status_not_valid")
    if not classification_thresholds_passed:
        eligible = False
        reasons.append("independent_thresholds_not_passed")
    if independent_action == "INSUFFICIENT_DATA":
        eligible = False
        reasons.append("independent_abstained")
    if main in ENTRY_ACTIONS and independent_action == main:
        eligible = False
        reasons.append("independent_agrees")

    # no_edge 的 0.54 只负责把「市场平淡」与「模型拿不准」分开。真正把明确 WAIT
    # 升格为否决依据时，必须重新通过独立的 veto 门槛。
    if opposite and action_status == "accepted" and not opposite_veto_thresholds_passed:
        eligible = False
        if action_margin < veto_min_margin:
            reasons.append("opposite_veto_margin_below_threshold")
    if explicit_wait and not wait_veto_thresholds_passed:
        eligible = False
        if confidence < veto_wait_min_confidence:
            reasons.append("wait_veto_confidence_below_threshold")
        if action_margin < veto_min_margin:
            reasons.append("wait_veto_margin_below_threshold")
    if eligible and not (opposite_veto_thresholds_passed or wait_veto_thresholds_passed):
        eligible = False
        reasons.append("no_veto_grounds")

    if normalized_mode == "shadow":
        return _result("SHADOW", reasons or ["shadow_mode_records_only"],
                       veto_eligible=eligible)
    if normalized_mode == "review":
        return _result("REVIEW", reasons or ["review_mode_flags_for_human"],
                       veto_eligible=eligible)
    if normalized_mode == "hard_veto":
        if hard_veto_code_only:
            hard_reasons = (
                reasons + ["hard_veto_capped_to_soft_by_code_only_gate"]
                if eligible else
                reasons + ["hard_veto_capped_to_soft_by_code_only_gate",
                           "no_veto_grounds"]
            )
            return _result("SOFT_VETO" if eligible else "NONE", hard_reasons,
                           veto_eligible=eligible)
        return _result("HARD_VETO" if eligible else "NONE", reasons,
                       veto_eligible=eligible)
    return _result("SOFT_VETO_CANDIDATE" if eligible else "NONE",
                   reasons or ["soft_veto_eligible"], veto_eligible=eligible)

def _jev_combine_candidate(*, main_action: Any, independent: Mapping[str, Any],
                       audit: Mapping[str, Any], enforcement: str = "shadow",
                       hard_gates_passed: bool = True,
                       entry_mode: str = "initial",
                       hard_veto_code_only: bool = True,
                       veto_wait_min_confidence: float = 0.70,
                       veto_min_margin: float = 0.15) -> Dict[str, Any]:
    """Combine both Jev lanes without changing the executable main action."""
    independent_action = _jev_normalize_choice(independent.get("suggested_action"))
    audit_value = str(audit.get("verdict") or "REVIEW").upper()
    if not hard_gates_passed:
        audit_value = "REJECT"
    data_status = str(independent.get("data_status") or independent.get("action_status") or "invalid")
    decision_relation = _jev_policy_relation(
        main_action,
        independent_action,
        data_status=data_status,
        audit_verdict_value=audit_value,
        action_status=str(independent.get("action_status") or ""),
    )
    enforcement_outcome = _jev_enforcement_decision(
        mode=enforcement, main_action=main_action, independent=independent,
        entry_mode=entry_mode, hard_gates_passed=hard_gates_passed,
        hard_veto_code_only=hard_veto_code_only,
        veto_wait_min_confidence=veto_wait_min_confidence,
        veto_min_margin=veto_min_margin,
    )
    return {
        "jev_independent_action": independent_action,
        "jev_independent_confidence": independent.get("confidence", 0.0),
        "jev_independent_action_margin": independent.get("action_margin", 0.0),
        "jev_independent_data_status": data_status,
        "jev_audit_verdict": audit_value,
        "jev_audit_flags": list(audit.get("flags") or []),
        "jev_relation_to_main": decision_relation,
        "jev_enforcement": str(enforcement or "shadow").upper(),
        "jev_enforcement_decision": enforcement_outcome["decision"],
        "jev_enforcement_reasons": enforcement_outcome["reasons"],
        "jev_veto_eligible": enforcement_outcome["veto_eligible"],
        "jev_veto_thresholds_passed": enforcement_outcome["veto_thresholds_passed"],
        "jev_veto_wait_min_confidence": enforcement_outcome["veto_wait_min_confidence"],
        "jev_veto_min_action_margin": enforcement_outcome["veto_min_action_margin"],
        # 本轮是否有任何档位**真的**改变了主脑执行。阶段 C/D 的执行门控需要样本外
        # 证据（§9 阶段 D），因此当前恒为 False；写出来是为了让台账能自证这一点，
        # 而不是让读者去猜。
        "jev_enforcement_affects_execution": enforcement_outcome["affects_execution"],
        "main_action": _jev_normalize_choice(main_action),
        "execution_action": _jev_normalize_choice(main_action),
    }



def _jev_shadow_provider_payload(model: str, state: Dict[str, Any],
                                 questions: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """Build one isolated provider request; no state is shared between lanes."""
    payload = {"model": model, "state": state, "questions": questions}
    if provider == "vercel_gateway":
        configured_order = os.environ.get("ASTRA_JEV_GATEWAY_PROVIDER_ORDER", "typesafe-ai")
        provider_order = [item.strip() for item in configured_order.split(",") if item.strip()]
        if provider_order:
            payload["providerOptions"] = {"gateway": {"order": provider_order}}
    if provider == "typesafe":
        for question in questions.values():
            if isinstance(question, dict) and question.get("type") == "boolean":
                question["type"] = "noul"
    return payload


def _jev_shadow_request(endpoint: str, api_key: str, payload: Dict[str, Any],
                        timeout: float, channel: str) -> Dict[str, Any]:
    """Call one Jev lane with bounded retries and provider metadata."""
    started = time.perf_counter()
    attempts = []
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    response = None
    request_id = ""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"AstraQuant/Jev-Shadow/{channel}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response = json.loads(resp.read().decode("utf-8", errors="replace"))
                request_id = str(
                    resp.headers.get("x-request-id") or resp.headers.get("request-id") or ""
                )
            attempts.append({"attempt": attempt, "status": "ok"})
            break
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            attempts.append({"attempt": attempt, "status": status})
            if status not in retryable_statuses or attempt >= max_attempts:
                raise
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After", 0) or 0)
            except (AttributeError, TypeError, ValueError):
                pass
            delay = min(2.0, max(0.25, retry_after or (0.5 * (2 ** (attempt - 1)))))
            print(f"[AI Brain Jev Shadow] {channel} transient HTTP {status}，"
                  f"{delay:g}s 后重试 ({attempt}/{max_attempts})")
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            attempts.append({"attempt": attempt, "status": type(exc).__name__})
            if attempt >= max_attempts:
                raise
            delay = min(2.0, 0.5 * (2 ** (attempt - 1)))
            print(f"[AI Brain Jev Shadow] {channel} transient {type(exc).__name__}，"
                  f"{delay:g}s 后重试 ({attempt}/{max_attempts})")
            time.sleep(delay)
    if response is None:
        raise RuntimeError(f"Jev {channel} request returned no response")
    return {
        "status": "ok",
        "response": response,
        "attempts": attempts,
        "request_id": str(
            (response.get("request_id") if isinstance(response, dict) else "")
            or request_id
        ),
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def _jev_shadow_normalize_answers(response: Any, provider: str) -> Dict[str, Any]:
    answers = response.get("answers") if isinstance(response, dict) else {}
    if not isinstance(answers, dict):
        return {}
    if provider != "typesafe":
        return answers
    normalized = {}
    for name, answer in answers.items():
        if isinstance(answer, dict) and answer.get("type") == "noul":
            item = dict(answer)
            item["probability"] = answer.get("noul")
            normalized[name] = item
        else:
            normalized[name] = answer
    return normalized


def _run_jev_shadow_review(standard_cache: Dict[str, Any], packages: List[Dict[str, Any]],
                           time_str: str, active_positions_detail: Optional[List[Dict[str, Any]]] = None,
                           position_management: Optional[List[Dict[str, Any]]] = None,
                           usdt_available: Optional[float] = None,
                           pending_orders_detail: Optional[List[Dict[str, Any]]] = None,
                           trader_factors: Optional[List[Dict[str, Any]]] = None) -> None:
    """Run Jev as an observe-only review; never alter the trading decision."""
    enabled = str(os.environ.get("ASTRA_JEV_SHADOW_ENABLED", "1")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return
    independent_enabled = str(os.environ.get("ASTRA_JEV_INDEPENDENT_ENABLED", "1")).strip().lower()
    if independent_enabled in {"0", "false", "no", "off"}:
        print("[AI Brain Jev Shadow] 独立双通道已关闭，跳过本轮影子复核")
        return
    configured_provider = str(os.environ.get("ASTRA_JEV_PROVIDER", "")).strip().lower()
    configured_endpoint = str(os.environ.get("ASTRA_JEV_SHADOW_URL", "")).strip()
    direct_typesafe = configured_provider in {"typesafe", "typesafe-ai", "direct"}
    direct_typesafe = direct_typesafe or bool(
        os.environ.get("ASTRA_JEV_TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_API_KEY"))
    if configured_endpoint:
        direct_typesafe = direct_typesafe or "api.typesafe.ai" in configured_endpoint.lower()
    if direct_typesafe:
        provider = "typesafe"
        api_key = (os.environ.get("ASTRA_JEV_TYPESAFE_API_KEY") or
                   os.environ.get("TYPESAFE_API_KEY") or
                   os.environ.get("ASTRA_JEV_API_KEY") or "").strip()
        endpoint = configured_endpoint or "https://api.typesafe.ai/v1/systemone"
        model = (os.environ.get("ASTRA_JEV_MODEL") or "jev-latest").strip()
    else:
        provider = "vercel_gateway"
        api_key = (os.environ.get("ASTRA_JEV_API_KEY") or
                   os.environ.get("AI_GATEWAY_API_KEY") or "").strip()
        endpoint = configured_endpoint or "https://ai-gateway.vercel.sh/v1/evaluate"
        model = (os.environ.get("ASTRA_JEV_MODEL") or "typesafe-ai/jev").strip()
    if not api_key:
        return
    try:
        timeout = max(1.0, min(float(os.environ.get("ASTRA_JEV_SHADOW_TIMEOUT", "6")), 15.0))
    except (TypeError, ValueError):
        timeout = 6.0

    proposals = []
    active_positions = [dict(item) for item in (active_positions_detail or [])
                        if isinstance(item, dict)]
    active_by_inst: Dict[str, List[Dict[str, Any]]] = {}

    def _enrich_exchange_protection(position: Dict[str, Any]) -> Dict[str, Any]:
        """Attach a best-effort live OKX algo-order snapshot for Jev only."""
        if str(position.get("venue", "okx")).lower() != "okx":
            return position
        inst_id = str(position.get("instId") or "")
        if not inst_id:
            return position
        enriched = dict(position)
        try:
            algo_rows = okx_rest.pending_algo_orders(inst_id=inst_id, timeout=2.5) or []
            side = _jev_position_side(position)
            live_rows = [row for row in algo_rows
                         if str(row.get("state", "live")).lower() in {"live", "effective"}
                         and str(row.get("posSide", "net")).lower() in {side, "net"}
                         and str(row.get("reduceOnly", "true")).lower() in {"true", "1", "yes"}]
            pos_size = abs(_jev_shadow_float(position.get("pos"), 0.0))
            sl_rows = [row for row in live_rows if _jev_shadow_float(row.get("slTriggerPx"), 0.0) > 0]
            protected_size = sum(abs(_jev_shadow_float(row.get("sz"), 0.0)) for row in sl_rows)
            full_row = next((row for row in live_rows
                             if _jev_shadow_float(row.get("slTriggerPx"), 0.0) > 0
                             and _jev_shadow_float(row.get("tpTriggerPx"), 0.0) > 0), None)
            sl_row = next(iter(sl_rows), None)
            if full_row and protected_size >= pos_size * 0.999:
                enriched.update({
                    "exchangeSl": _jev_shadow_float(full_row.get("slTriggerPx"), 0.0),
                    "exchangeTp": _jev_shadow_float(full_row.get("tpTriggerPx"), 0.0),
                    "protectionStatus": "fully_protected",
                    "protectionCoveragePct": 100.0,
                    "protectionAlgoId": full_row.get("algoId", ""),
                })
            elif live_rows:
                enriched.update({
                    "exchangeSl": (_jev_shadow_float(sl_row.get("slTriggerPx"), 0.0)
                                   if sl_row else None),
                    "exchangeTp": (_jev_shadow_float(sl_row.get("tpTriggerPx"), 0.0)
                                   if sl_row and _jev_shadow_float(sl_row.get("tpTriggerPx"), 0.0) > 0 else None),
                    "protectionStatus": "partially_protected",
                    "protectionCoveragePct": round(min(100.0, protected_size / max(pos_size, 1e-12) * 100), 1),
                    "protectionAlgoId": (sl_row or {}).get("algoId", ""),
                })
            else:
                enriched.update({
                    "exchangeSl": None,
                    "exchangeTp": None,
                    "protectionStatus": "unprotected",
                    "protectionCoveragePct": 0.0,
                    "protectionAlgoId": "",
                })
            enriched["protection_orders"] = [
                {key: row.get(key) for key in (
                    "algoId", "ordType", "state", "posSide", "sz",
                    "slTriggerPx", "tpTriggerPx", "reduceOnly", "cTime", "uTime"
                ) if key in row}
                for row in live_rows
            ]
            enriched["protection_snapshot_source"] = "okx_pending_algo_orders"
        except Exception as protection_exc:
            enriched["protectionStatus"] = "unknown_unavailable"
            enriched["protection_snapshot_error"] = f"{type(protection_exc).__name__}: {str(protection_exc)[:200]}"
            enriched["protection_snapshot_source"] = "okx_pending_algo_orders_error"
        return enriched

    if active_positions:
        with ThreadPoolExecutor(max_workers=min(8, len(active_positions))) as protection_executor:
            active_positions = list(protection_executor.map(
                _enrich_exchange_protection, active_positions))

    for active_position in active_positions:
        if not isinstance(active_position, dict) or not active_position.get("instId"):
            continue
        active_by_inst.setdefault(str(active_position["instId"]), []).append(active_position)
    trader_factor_by_inst = {
        str(item.get("instId")): item
        for item in (trader_factors or [])
        if isinstance(item, dict) and item.get("instId")
    }
    for p in packages or []:
        inst_id = str(p.get("instId") or "")
        row = standard_cache.get(inst_id) if isinstance(standard_cache, dict) else None
        if not isinstance(row, dict):
            continue
        decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
        direction = row.get("direction_input") if isinstance(row.get("direction_input"), dict) else {}
        main_action = str(decision.get("action", "WAIT") or "WAIT").upper()
        decision_margin = _jev_shadow_float(decision.get("margin_usdt"), 0.0)
        decision_leverage = _jev_shadow_float(decision.get("leverage"), 0.0)
        risk_budget = _jev_shadow_float(p.get("risk_per_trade_usd"), 0.0)
        configured_margin = str(os.environ.get("ASTRA_JEV_SHADOW_MARGIN_USDT", "")).strip()
        configured_leverage = str(os.environ.get("ASTRA_JEV_SHADOW_LEVERAGE", "")).strip()
        if main_action != "WAIT" and decision_margin > 0:
            shadow_margin = decision_margin
            shadow_margin_source = "main_decision"
        elif configured_margin and _jev_shadow_float(configured_margin, 0.0) > 0:
            shadow_margin = _jev_shadow_float(configured_margin, 0.0)
            shadow_margin_source = "configured_shadow_margin"
        elif risk_budget > 0:
            shadow_margin = risk_budget
            shadow_margin_source = "risk_per_trade_usd"
        else:
            shadow_margin = max(0.01, _jev_shadow_float(
                os.environ.get("ASTRA_JEV_SHADOW_DEFAULT_MARGIN_USDT", "15"), 15.0))
            shadow_margin_source = "default_shadow_margin"
        if main_action != "WAIT" and decision_leverage > 0:
            shadow_leverage = decision_leverage
            shadow_leverage_source = "main_decision"
        elif configured_leverage and _jev_shadow_float(configured_leverage, 0.0) > 0:
            shadow_leverage = _jev_shadow_float(configured_leverage, 1.0)
            shadow_leverage_source = "configured_shadow_leverage"
        else:
            shadow_leverage = 1.0
            shadow_leverage_source = "default_shadow_leverage"
        active_for_inst = active_by_inst.get(inst_id, [])
        trader_factor = trader_factor_by_inst.get(inst_id) or {}
        direction_observation = trader_factor.get("direction_observation")
        if direction_observation is None:
            direction_observation = compare_directions(trader_factor, row)
        direction_layer_snapshot = (
            trader_factor.get("direction_layers") or
            direction_layers(trader_factor.get("calculus"))
        )
        calculus_source = p.get("calculus") or trader_factor.get("calculus") or {}
        if not isinstance(calculus_source, Mapping):
            calculus_source = {}
        probability_source = calculus_source.get("probability_theory") or {}
        if not isinstance(probability_source, Mapping):
            probability_source = {}
        momentum_calculus = {
            "regime": calculus_source.get("regime"),
            "acceleration": calculus_source.get("acceleration"),
            "power": calculus_source.get("power"),
            "power_regime": calculus_source.get("power_regime"),
            "probability_theory": {
                "continuation_prob_pct": probability_source.get("continuation_prob_pct"),
                "breakdown_prob_pct": probability_source.get("breakdown_prob_pct"),
            },
        }
        matching_side = any(
            _jev_position_side(position) ==
            _jev_shadow_side(main_action)
            for position in active_for_inst
        )
        if main_action in {"BUY_LONG", "SELL_SHORT"} and matching_side:
            entry_mode = "scale_in"
        elif active_for_inst:
            entry_mode = "position_management"
        else:
            entry_mode = "initial"
        proposals.append({
            "instId": inst_id,
            "cycle_id": row.get("cycle_id", ""),
            "decision_id": row.get("decision_id", f"{time_str}:{inst_id}"),
            "decision_timestamp": row.get("timestamp", 0),
            "action": main_action,
            "confidence": decision.get("confidence", 0),
            "main_raw_action": decision.get("raw_action", main_action),
            "main_raw_confidence": decision.get("raw_confidence", decision.get("confidence", 0)),
            "main_decision_reason": decision.get("summary_reason", ""),
            "main_decision_outcome_source": decision.get("decision_outcome_source", "unknown"),
            "main_decision_rejection_code": decision.get("decision_rejection_code", ""),
            "leverage": decision.get("leverage", 0),
            "margin_usdt": decision.get("margin_usdt", 0),
            "shadow_margin_usdt": shadow_margin,
            "shadow_margin_source": shadow_margin_source,
            "shadow_leverage": shadow_leverage,
            "shadow_leverage_source": shadow_leverage_source,
            "entry_mode": entry_mode,
            "entry_price": decision.get("entry_price", 0),
            "take_profit_price": decision.get("take_profit_price", 0),
            "stop_loss_price": decision.get("stop_loss_price", 0),
            "risk_reward_ratio": decision.get("risk_reward_ratio", "--"),
            "data_quality": row.get("data_quality"),
            "macro_4h": direction.get("macro_4h"),
            "calculus_regime": direction.get("calculus_regime"),
            "calculus": momentum_calculus,
            # `trend_4h_bullish` / `trend_4h_bearish` 已删除：这两个键只存在于
            # trader 因子路径（scripts/trader/factors.py），brain package 从不产生，
            # 因此 710/710 行恒为 null —— 白名单在宣称一个永远填不上的字段。
            "direction_observation": direction_observation,
            "direction_layers": direction_layer_snapshot,
            "price": p.get("price", 0),
            "bidPx": p.get("bidPx", 0),
            "askPx": p.get("askPx", 0),
            "ctVal": p.get("ctVal", 1.0),
            "minSz": p.get("minSz", 1.0),
            "base_sz": p.get("base_sz", 0.0),
            "max_leverage": p.get("max_leverage", 0.0),
            "risk_per_trade_usd": p.get("risk_per_trade_usd", 0.0),
            # Keep the decision-time quote immutable. Jev's delayed-entry
            # paper fill is refreshed after the API response below.
            "main_price": p.get("price", 0),
            "main_bidPx": p.get("bidPx", 0),
            "main_askPx": p.get("askPx", 0),
            "main_quote_timestamp": time.time(),
            # 权威口径：已收盘 12 根 4H K 线（four_hour_range），由 direction_observation
            # 携带。过去这里取的是 brain package 的「最新 8 根（含未收盘）」，
            # 与 direction_observation 里的同名字段互相矛盾（710 行中 707 行不一致），
            # 模型因此拿到两个互斥的箱体位置。现在统一取已收盘口径，并把
            # 实际沿用哪个窗口记进 `price_position_basis`。
            "price_position_in_range": (
                direction_observation.get("price_position_in_range")
                if isinstance(direction_observation, dict)
                and direction_observation.get("price_position_in_range") is not None
                else direction.get("price_position_in_range")
            ),
            "price_position_basis": (
                "four_hour_closed_12"
                if isinstance(direction_observation, dict)
                and direction_observation.get("price_position_in_range") is not None
                else "recent8_fallback"
            ),
            "candle_ts_4h": direction.get("candle_ts_4h"),
        })

    management_by_inst = {
        str(item.get("instId")): item
        for item in (position_management or [])
        if isinstance(item, dict) and item.get("instId")
    }
    position_proposals = []
    for position in active_positions:
        if not isinstance(position, dict) or not position.get("instId"):
            continue
        inst_id = str(position.get("instId"))
        row = standard_cache.get(inst_id) if isinstance(standard_cache, dict) else None
        management = management_by_inst.get(inst_id) or {
            "action": "HOLD", "confidence": 0, "suggested_sl_price": 0.0,
            "reason": "主脑未提供该持仓指令，按 HOLD 处理",
        }
        position_proposals.append({
            "instId": inst_id,
            "decision_id": position.get("decision_id") or (row or {}).get("decision_id", f"{time_str}:{inst_id}"),
            "cycle_id": position.get("cycle_id") or (row or {}).get("cycle_id", ""),
            "entry_order_id": position.get("entry_order_id"),
            "entry_intent_id": position.get("entry_intent_id"),
            "entry_order_ts": position.get("entry_order_ts"),
            "entry_order_avg_px": position.get("entry_order_avg_px"),
            "entry_order_fill_sz": position.get("entry_order_fill_sz"),
            "entry_order_source": position.get("entry_order_source"),
            "entry_identity_status": position.get("entry_identity_status"),
            "entry_time": position.get("entry_time", position.get("open_time")),
            "entryTime": position.get("entryTime"),
            "entryTs": position.get("entryTs"),
            "entry_venue": position.get("entry_venue", position.get("venue", "okx")),
            "venue": position.get("venue", "okx"),
            "side": position.get("side", position.get("posSide", "net")),
            "pos": position.get("pos", 0),
            "avgPx": position.get("avgPx", 0),
            "markPx": position.get("markPx", position.get("last", 0)),
            "upl": position.get("upl", position.get("unrealized_pnl", 0)),
            "trailingStopPx": position.get("trailingStopPx"),
            "takeProfitPx": position.get("takeProfitPx"),
            "stage_desc": position.get("stage_desc", ""),
            "atr": position.get("atr", 0),
            "ctVal": position.get("ctVal", 1.0),
            "bidPx": position.get("bidPx"),
            "askPx": position.get("askPx"),
            "lever": position.get("lever", position.get("leverage")),
            "leverage": position.get("leverage", position.get("lever")),
            "notional_usdt": position.get("notional_usdt", position.get("notionalUsd")),
            "margin_usdt": position.get("margin_usdt", position.get("margin", position.get("imr"))),
            "imr": position.get("imr"),
            "mmr": position.get("mmr"),
            "liqPx": position.get("liqPx", position.get("liq_price")),
            "bePx": position.get("bePx"),
            "mgnMode": position.get("mgnMode"),
            "ccy": position.get("ccy"),
            "adl": position.get("adl"),
            "cTime": position.get("cTime"),
            "uTime": position.get("uTime"),
            "open_time": position.get("open_time"),
            "funding_fee": position.get("funding_fee", position.get("fundingFee")),
            "realized_pnl": position.get("realized_pnl", position.get("realizedPnl")),
            "fee": position.get("fee"),
            "exchangeSl": position.get("exchangeSl"),
            "exchangeTp": position.get("exchangeTp"),
            "protectionStatus": position.get("protectionStatus"),
            "protectionAlgoId": position.get("protectionAlgoId"),
            "protectionCoveragePct": position.get("protectionCoveragePct"),
            "protection_orders": position.get("protection_orders", []),
            "protection_snapshot_source": position.get("protection_snapshot_source"),
            "protection_snapshot_error": position.get("protection_snapshot_error"),
            "highWaterMark": position.get("highWaterMark"),
            "lowWaterMark": position.get("lowWaterMark"),
            "tp1Hit": position.get("tp1Hit"),
            "tp2Hit": position.get("tp2Hit"),
            "environment": position.get("environment"),
            "account_mode": position.get("account_mode"),
            "main_action": management.get("action", "HOLD"),
            "main_confidence": management.get("confidence", 0),
            "main_suggested_sl_price": management.get("suggested_sl_price", 0),
            "main_reason": management.get("reason", ""),
        })

    macro_assessment = next(
        (str(v.get("macro_assessment")) for v in standard_cache.values()
         if isinstance(v, dict) and v.get("macro_assessment")), "")
    neutral_candidates = [{
        key: proposal.get(key)
        for key in (
            "instId", "cycle_id", "decision_id", "decision_timestamp", "data_quality",
            "macro_4h", "calculus_regime", "calculus",
            "direction_observation", "direction_layers", "price", "bidPx", "askPx",
            "price_position_in_range", "price_position_basis", "candle_ts_4h",
            # 代码侧的完整性与一致性判定结论。模型被告知「这不是你的事」，
            # 因此把结论一并给它，避免它替代码重做这件事。
            "state_quality",
        )
    } for proposal in proposals]
    for candidate, proposal in zip(neutral_candidates, proposals):
        candidate["state_quality"] = _jev_candidate_state_quality(proposal)
    neutral_positions = []
    audit_positions = []
    for position in position_proposals:
        context = _jev_shadow_position_audit_context(position)
        audit_positions.append(context)
        # 白名单裁剪，不是「减掉几个 main_*」。理由见
        # `_JEV_NEUTRAL_POSITION_KEYS` 上方的注释。
        neutral_position = _jev_neutral_position_state(context)
        neutral_position["state_quality"] = _jev_position_state_quality(context)
        neutral_positions.append(neutral_position)

    # 两个通道都显式白名单组装：候选见 `neutral_candidates`，持仓见
    # `_JEV_NEUTRAL_POSITION_KEYS`。新增字段默认不会进入独立通道。
    #
    # `liquidity_state` 原先硬编码成字符串 "UNKNOWN"（71/71 轮），即 state 在
    # 宣称一个它从来没填过的字段 —— 与 trend_4h_* 同类的死键。价差可以直接从
    # 本轮已持有的 bid/ask 算出且零网络开销，因此改传真实 spread_bps；
    # 盘口深度（orderbook depth）本周期并未取数，于是显式记为不可用，
    # 而不是编一个桶值出来。
    spread_samples = []
    for _candidate in neutral_candidates:
        _bid = _jev_shadow_float(_candidate.get("bidPx"), 0.0)
        _ask = _jev_shadow_float(_candidate.get("askPx"), 0.0)
        if _bid > 0 and _ask > 0 and _ask >= _bid:
            spread_samples.append((_ask - _bid) / ((_ask + _bid) / 2.0) * 10000.0)
    spread_bps = round(sorted(spread_samples)[len(spread_samples) // 2], 4) if spread_samples else None

    neutral_state = {
        "cycle_time": time_str,
        "market": {"candidates": neutral_candidates},
        "positions": neutral_positions,
        "execution": {
            "fee_rate": _jev_shadow_float(
                os.environ.get("ASTRA_JEV_SHADOW_FEE_RATE", "0.0005"), 0.0005),
            "slippage_bps": _jev_shadow_float(
                os.environ.get("ASTRA_JEV_SHADOW_SLIPPAGE_BPS", "2"), 2.0),
            # 本轮候选的中位价差，来自已持有的盘口快照。
            "spread_bps": spread_bps,
            # 深度需要额外 REST 取数（每轮 ×10 标的），本周期未取，故显式缺失。
            "depth_state": "unavailable",
            "depth_source": "not_fetched_in_cycle",
        },
        "risk_context": {
            "available_usdt": usdt_available,
            "pending_order_count": len(pending_orders_detail or []),
            "source": "cycle_snapshot_without_main_proposal",
        },
    }
    audit_state = {
        "cycle_time": time_str,
        "neutral_market_state": neutral_state["market"],
        "neutral_position_state": neutral_positions,
        "account": {
            "available_usdt": usdt_available,
            "snapshot_source": "cycle_account_snapshot",
        },
        "pending_orders": pending_orders_detail if isinstance(pending_orders_detail, list) else [],
        "main_proposals": proposals,
        "main_position_proposals": audit_positions,
    }

    # 2026-09-26 拆分：原先这里有一道 `cycle_data_valid`，问「整轮数据是否完整且
    # 内部一致」。它是**代码拥有的属性**（见 `_jev_candidate_state_quality`），
    # 而且实测该答案从不参与任何判定，只被记进 `aggregate_answers` —— 一道既
    # 名不副实、又白付一次推理成本的问题。已移除；整轮质量改由代码侧汇总记录。
    independent_questions: Dict[str, Any] = {}
    audit_questions: Dict[str, Any] = {
        "audit_data_valid": {
            "type": "boolean",
            "instructions": (
                "Is the audit state complete enough to review the main proposals "
                "without guessing missing facts?"
            ),
        },
    }
    for index, proposal in enumerate(proposals):
        inst_id = proposal["instId"]
        prefix = f"candidate_{index}"
        # The audit lane only ever reviews a REAL entry proposal. Auditing a WAIT
        # "proposal" (which by construction has no entry/stop/target/size/leverage)
        # drove `proposal_complete` to a ~0.07 median and manufactured a constant,
        # content-free AUDIT_REJECT on 98% of cycles. Nothing to audit => ask nothing.
        has_proposal = str(proposal.get("action") or "WAIT").upper() in ENTRY_ACTIONS
        independent_questions.update({
            # NOTE(2026-09-26): no `*_action` choice question. The typesafe endpoint
            # rejects `type: "choice"` with HTTP 422 (it expects a nested
            # `choice.criteria` object that is not documented anywhere usable), which
            # killed this whole lane 62/62 cycles. The three `*_would_*` compatibility
            # votes below already carry the same information AND let `_jev_rank_votes`
            # derive `suggested_action` together with an action margin — something a
            # single choice answer cannot express. Keeping the question set
            # boolean-only also makes both lanes structurally identical, so a payload
            # shape accepted by one is accepted by the other.
            # 2026-09-26 拆分：这道题此前叫 `*_data_valid`，问「数据是否足以做方向判断」，
# 但代码侧已用确定性方式拥有「完整性」与「一致性」（见
# `_jev_candidate_state_quality`），于是同一个词被用来问两件不同的事，模型的
# 优势判断被记成了「数据无效」。现在只问**优势**：完整与一致由代码判定，
# 缺失/矛盾由代码触发 INSUFFICIENT_DATA，模型这道题低分只表示「无优势」。
            f"{prefix}_edge_present": {
                "type": "boolean",
                "instructions": (
                    f"Using only the neutral state for {inst_id}, is there a "
                    "directional edge worth acting on in the next observation "
                    "window? Completeness and internal consistency of the supplied "
                    "facts are NOT your concern here — the code already checks "
                    "those, and state_quality in the payload tells you its verdict. "
                    "This question is only about whether the market itself offers an "
                    "edge. Calibration: >=0.8 when the evidence clearly points one "
                    "way; ~0.5 when it is genuinely balanced; <=0.2 when the market "
                    "is flat, mid-range, or gives no directional signal. A complete, "
                    "consistent state with no edge is a LOW score here, not a data "
                    "problem."
                ),
            },
            f"{prefix}_execution_ready": {
                "type": "boolean",
                "instructions": (
                    f"Based only on the neutral state for {inst_id}, is a directional "
                    "action executable after normal cost and liquidity friction? "
                    "Calibration: >=0.8 when spread and cost are clearly small "
                    "relative to the expected move; ~0.5 when it is marginal; "
                    "<=0.2 only when friction or thin depth would clearly consume "
                    "the edge. If depth_state is unavailable, judge from spread_bps "
                    "and say so rather than assuming the worst."
                ),
            },
            f"{prefix}_would_buy_long": {
                "type": "boolean",
                "instructions": (
                    f"As a compatibility vote, would you choose BUY_LONG for {inst_id} "
                    "using only the neutral state? Calibration: use the full range "
                    "0-1. Answer <=0.2 when long is clearly not the best action, "
                    "~0.5 when it is a genuine toss-up with WAIT, and >=0.8 when long "
                    "is the action you would actually take. The three would_* votes are "
                    "compared against each other, so a low absolute number is fine — "
                    "but do not answer all three near 0.4, which carries no information."
                ),
            },
            f"{prefix}_would_sell_short": {
                "type": "boolean",
                "instructions": (
                    f"As a compatibility vote, would you choose SELL_SHORT for {inst_id} "
                    "using only the neutral state? Same calibration as would_buy_long: "
                    "<=0.2 when short is clearly not best, ~0.5 for a genuine toss-up, "
                    ">=0.8 when short is the action you would take."
                ),
            },
            f"{prefix}_would_wait": {
                "type": "boolean",
                "instructions": (
                    f"As a compatibility vote, would you choose WAIT for {inst_id} "
                    "using only the neutral state? Same calibration: <=0.2 when waiting "
                    "is clearly not best, ~0.5 for a genuine toss-up, >=0.8 when waiting "
                    "is the action you would take."
                ),
            },
        })
        if has_proposal:
            audit_questions.update({
                f"{prefix}_thesis_supported": {
                    "type": "boolean",
                    "instructions": (
                        f"Using main_proposals for {inst_id}, is the main proposal supported "
                        "by the neutral market evidence?"
                    ),
                },
                f"{prefix}_direction_conflict": {
                    "type": "boolean",
                    "instructions": (
                        f"Does the main proposal direction conflict with material "
                        f"multi-timeframe evidence for {inst_id}?"
                    ),
                },
                f"{prefix}_entry_is_chasing": {
                    "type": "boolean",
                    "instructions": f"Does the main proposal for {inst_id} chase the current price or momentum?",
                },
                f"{prefix}_stop_structurally_valid": {
                    "type": "boolean",
                    "instructions": f"Is the main proposal stop for {inst_id} on a structurally valid invalidation side of the entry?",
                },
                f"{prefix}_reward_after_cost_sufficient": {
                    "type": "boolean",
                    "instructions": f"After estimated fees and slippage, does the main proposal for {inst_id} retain sufficient reward relative to risk?",
                },
                f"{prefix}_omits_counter_evidence": {
                    "type": "boolean",
                    "instructions": f"Does the main proposal for {inst_id} omit material counter-evidence visible in the neutral state?",
                },
                f"{prefix}_proposal_complete": {
                    "type": "boolean",
                    "instructions": f"Is the main proposal for {inst_id} complete for code-side audit, including entry, stop, target, size, leverage, and margin?",
                },
            })
    for index, proposal in enumerate(position_proposals):
        inst_id = proposal["instId"]
        prefix = f"position_{index}"
        independent_questions.update({
            # Same as the candidate lane above: no `*_action` choice question.
            # The `*_would_hold/_would_close/_would_update_sl` votes drive
            # `suggested_action` through `_jev_rank_votes`.
            # 与候选侧同一拆分：完整性/一致性归代码（`_jev_position_state_quality`），
            # 模型只回答判断。此题此前问「state 是否足够完整」，现改问「是否有
            # 需要改变管理的理由」——低分表示「无需动作」，不是数据问题。
            f"{prefix}_management_warranted": {
                "type": "boolean",
                "instructions": (
                    f"Using only the neutral position state for {inst_id}, is there a "
                    "specific reason to change this position's management right now "
                    "(close it, or tighten/adjust its protection), rather than "
                    "leaving it alone? State completeness and consistency are NOT "
                    "your concern — the code checks those and reports its verdict in "
                    "state_quality. Calibration: >=0.8 when a concrete reason exists; "
                    "~0.5 when it is borderline; <=0.2 when the position needs no "
                    "action."
                ),
            },
            f"{prefix}_would_hold": {
                "type": "boolean",
                "instructions": f"As a compatibility vote, would you independently HOLD {inst_id}?",
            },
            f"{prefix}_would_close": {
                "type": "boolean",
                "instructions": f"As a compatibility vote, would you independently CLOSE_MARKET {inst_id}?",
            },
            f"{prefix}_would_update_sl": {
                "type": "boolean",
                "instructions": f"As a compatibility vote, would you independently UPDATE_SL for {inst_id}?",
            },
        })
        audit_questions.update({
            f"{prefix}_protection_ready": {
                "type": "boolean",
                "instructions": f"Are the exchange protection orders and local protection state for {inst_id} complete and ready?",
            },
            f"{prefix}_close_needed": {
                "type": "boolean",
                "instructions": f"Does the full audit state indicate that {inst_id} should be closed now?",
            },
        })

    independent_payload = _jev_shadow_provider_payload(
        model, neutral_state, independent_questions, provider)
    audit_payload = _jev_shadow_provider_payload(
        model, audit_state, audit_questions, provider)
    # 执行档位（方案 §6）必须在 `review` 字典之前解析：`review["enforcement_mode"]`
    # 会被候选/持仓评审读取，且档位要求可配置回滚、不得靠改代码切换。
    enforcement_resolution = _jev_resolve_enforcement(
        os.environ.get("ASTRA_JEV_ENFORCEMENT", "shadow"))
    enforcement_mode = enforcement_resolution["mode"]
    # §6 的 `ASTRA_JEV_HARD_VETO_ONLY_CODE_GATES`：置 1 时 Jev 自身最高只能软否决，
    # 硬否决只允许来自代码门禁（默认 1，即最保守）。
    hard_veto_code_only = str(
        os.environ.get("ASTRA_JEV_HARD_VETO_ONLY_CODE_GATES", "1")
    ).strip().lower() not in {"0", "false", "no", "off"}
    started = time.perf_counter()
    review_started_timestamp = time.time()
    cycle_id = (proposals[0].get("cycle_id") if proposals
                else f"cycle-{int(time.time() * 1000)}")

    def _state_hash(value: Dict[str, Any]) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    review: Dict[str, Any] = {
        "schema_version": 3,
        # 2 = 拆分后语义：完整性/一致性归代码（`code_state_*`），模型只答
        # 「有没有方向性优势」（`edge_present` / `management_warranted`）。
        # 1 = 拆分前：模型答 `*_data_valid`，低分被记成 `invalid_data`。
        # 新旧样本**不可混统计**，用这个字段区分。
        "question_semantics_version": 2,
        "timestamp": int(review_started_timestamp),
        "review_started_timestamp": review_started_timestamp,
        "time_str": time_str,
        "model": model,
        "provider": provider,
        "endpoint": endpoint,
        "candidate_count": len(proposals),
        "position_count": len(position_proposals),
        "cycle_id": cycle_id,
        "candidate_snapshot": proposals,
        "position_snapshot": position_proposals,
        "neutral_state_hash": _state_hash(neutral_state),
        "audit_state_hash": _state_hash(audit_state),
        "independent_state": neutral_state,
        "audit_state": audit_state,
        # 档位由 `ASTRA_JEV_ENFORCEMENT` 决定（方案 §6 要求可配置回滚、不得靠改代码切换）。
        # 非法/未知值 fail-closed 回 `shadow`——那是唯一在结构上不可能改变主脑执行的
        # 档位，因此环境变量笔误只会让观察者保持观察。
        "configured_enforcement": (
            os.environ.get("ASTRA_JEV_ENFORCEMENT", "shadow").strip().lower() or "shadow"
        ),
        "enforcement_mode": enforcement_mode,
        "enforcement_mode_valid": enforcement_resolution["valid"],
        "enforcement_mode_reason": enforcement_resolution["reason"],
        # §6 的 `ASTRA_JEV_HARD_VETO_ONLY_CODE_GATES`：置 1 时 Jev 自身最高只能软否决，
        # 硬否决只允许来自代码门禁。
        "hard_veto_code_only": hard_veto_code_only,
    }

    def _request_lane(channel: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = _jev_shadow_request(endpoint, api_key, payload, timeout, channel)
            result["channel"] = channel
            return result
        except Exception as exc:
            return {
                "channel": channel,
                "status": "error",
                "attempts": [],
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }

    with ThreadPoolExecutor(max_workers=2) as request_executor:
        independent_future = request_executor.submit(
            _request_lane, "independent", independent_payload)
        audit_future = request_executor.submit(_request_lane, "audit", audit_payload)
        independent_result = independent_future.result()
        audit_result = audit_future.result()
    independent_answers = _jev_shadow_normalize_answers(
        independent_result.get("response"), provider)
    audit_answers = _jev_shadow_normalize_answers(
        audit_result.get("response"), provider)
    independent_ok = independent_result.get("status") == "ok"
    audit_ok = audit_result.get("status") == "ok"

    def _threshold(name: str, fallback: float) -> float:
        try:
            return max(0.0, min(float(os.environ.get(name, str(fallback))), 1.0))
        except (TypeError, ValueError):
            return fallback

    # 方向动作与明确 WAIT 使用不同的绝对置信门槛。方向动作维持较保守的 0.70；
    # WAIT/no_edge 只是在影子层声明「市场平淡」，不直接发单，因此保留 0.54 门槛，
    # 避免所有中等强度 WAIT 都退化成 ABSTAIN、让 no_edge 标签失去可观测性。
    # 两者都必须同时通过同一个 action margin 门槛。
    min_confidence = _threshold("ASTRA_JEV_INDEPENDENT_MIN_CONFIDENCE", 0.70)
    no_edge_min_confidence = _threshold("ASTRA_JEV_NO_EDGE_MIN_CONFIDENCE", 0.54)
    min_margin = _threshold("ASTRA_JEV_INDEPENDENT_MIN_ACTION_MARGIN", 0.15)
    # no_edge 的分类门槛只服务观测；否决资格必须单独过更严格的 enforcement 门槛。
    veto_wait_min_confidence = _threshold(
        "ASTRA_JEV_VETO_WAIT_MIN_CONFIDENCE", 0.70)
    veto_min_margin = _threshold("ASTRA_JEV_VETO_MIN_ACTION_MARGIN", 0.15)
    # `ASTRA_JEV_DATA_VALID_MIN_PROBABILITY` 已随拆分移除：独立通道不再对模型的
    # 数据有效性自述设门禁（完整性/一致性改由代码判定），因此该配置项不再读取。
    # 留着一个读取了却不生效的环境变量比删掉它更危险 —— 它会让人以为改得动。
    execution_ready_min = _threshold(
        "ASTRA_JEV_EXECUTION_READY_MIN_PROBABILITY", 0.44)
    audit_data_valid_min = _threshold(
        "ASTRA_JEV_AUDIT_DATA_VALID_MIN_PROBABILITY", 0.50)
    protection_min = _threshold("ASTRA_JEV_PROTECTION_MIN_PROBABILITY", 0.50)
    # 审计旗标的判定分界（正向证据 >= 此值、风险项 < 此值即置旗）。默认 0.5
    # 保持既有行为不变；审计通道的答案分布与独立通道不同，因此单独可调。
    audit_flag_min = _threshold("ASTRA_JEV_AUDIT_FLAG_MIN_PROBABILITY", 0.50)
    position_min_confidence = _threshold(
        "ASTRA_JEV_POSITION_MIN_CONFIDENCE", min_confidence)
    position_min_margin = _threshold(
        "ASTRA_JEV_POSITION_MIN_ACTION_MARGIN", min_margin)
    audit_min_confidence = _threshold("ASTRA_JEV_AUDIT_MIN_CONFIDENCE", 0.70)

    def _candidate_review(index: int, proposal: Dict[str, Any]) -> Dict[str, Any]:
        prefix = f"candidate_{index}"
        # 拆分后的判据链：代码判「载荷好不好」，模型判「市场有没有优势」。
        # `INSUFFICIENT_DATA` 只允许由代码侧的完整性/一致性触发。
        code_quality = _jev_candidate_state_quality(proposal)
        edge_present = independent_answers.get(f"{prefix}_edge_present")
        execution_ready = independent_answers.get(f"{prefix}_execution_ready")
        vote_result = _jev_rank_votes({
            "BUY_LONG": independent_answers.get(f"{prefix}_would_buy_long"),
            "SELL_SHORT": independent_answers.get(f"{prefix}_would_sell_short"),
            "WAIT": independent_answers.get(f"{prefix}_would_wait"),
        }, min_confidence=min_confidence, min_margin=min_margin,
            execution_ready=execution_ready,
            execution_ready_min=execution_ready_min,
            wait_min_confidence=no_edge_min_confidence)
        choice = _jev_normalize_choice(independent_answers.get(f"{prefix}_action"))
        if choice not in {"BUY_LONG", "SELL_SHORT", "WAIT", "INSUFFICIENT_DATA"}:
            choice = vote_result["suggested_action"]
        edge_probability = _jev_policy_probability(edge_present)
        if code_quality["status"] == "insufficient_data":
            action, action_status, data_status = (
                "INSUFFICIENT_DATA", "code_state_incomplete", "invalid")
        elif code_quality["status"] == "inconsistent":
            action, action_status, data_status = (
                "INSUFFICIENT_DATA", "code_state_inconsistent", "invalid")
        else:
            action, action_status, data_status = choice, vote_result["action_status"], "valid"
            if action_status in {"not_ready", "low_confidence", "ambiguous", "missing_action_votes"}:
                action = "WAIT" if action_status != "missing_action_votes" else "INSUFFICIENT_DATA"
            elif action == "WAIT" and action_status == "accepted":
                # 票决**明确**选了 WAIT（而不是"方向弱"）⇒ 事实完整自洽但市场没给
                # 方向，即「无优势」。这是重贴标签，不改变任何动作，只是把
                # 「市场平淡」与「模型拿不准」分开记录。
                action_status = "no_edge"
        # NOTE: `edge_present` 本轮**只记录、不作否决门**。该题是全新的，没有任何
        # 历史分布可标定；让一个未标定的问题去否决方向，正是此前「0 方向性输出」
        # 的成因。等它有足够样本、能按已结算结果标定后，再考虑升级为门槛
        # （方案 §5.1 本就把它定位为「解释维度和门槛」，先做前者）。

        raw_suggested_action = action

        # Mirror the question-side gate: a WAIT proposal has no audit answers, so
        # every flag would be "missing" and the verdict would be a meaningless
        # REJECT. Short-circuit to NOT_APPLICABLE instead of running the flag logic.
        if str(proposal.get("action") or "WAIT").upper() not in ENTRY_ACTIONS:
            audit = _jev_audit_verdict([], has_proposal=False,
                                       min_confidence=audit_min_confidence)
            # Still referenced by the record below; None is the honest value when
            # no `proposal_complete` answer was ever requested.
            audit_complete = None
            # `None` (not a dict of zeros) is the point: a WAIT proposal was never
            # audited, so it must be impossible to average its "scores" into the
            # pool. See the runbook note on `audit_probabilities` in the record.
            audit_probabilities = None
        else:
            flag_map = (
                ("direction_conflict", "direction_conflict"),
                ("entry_is_chasing", "entry_is_chasing"),
                ("stop_structurally_valid", "stop_structure_invalid"),
                ("reward_after_cost_sufficient", "reward_after_cost_insufficient"),
                ("omits_counter_evidence", "omits_counter_evidence"),
            )
            # Record every raw audit probability per candidate. Without this the only
            # way to analyse the audit lane is to re-join `instrument_reviews[i]` with
            # `response.audit.answers[candidate_i_*]` by index — and that join silently
            # mixes WAIT candidates (which used to be audited against entry=0/sl=0)
            # into the pool, which reads as "JEV thinks the stop is invalid 95% of the
            # time" when it really means "there was no stop to look at". `-1.0` is the
            # existing "answer missing" sentinel from `_jev_policy_probability`.
            audit_probabilities = {
                field: _jev_policy_probability(audit_answers.get(f"{prefix}_{field}"))
                for field, _flag in flag_map
            }
            audit_complete = audit_answers.get(f"{prefix}_proposal_complete")
            audit_probabilities["thesis_supported"] = _jev_policy_probability(
                audit_answers.get(f"{prefix}_thesis_supported"))
            audit_probabilities["proposal_complete"] = _jev_policy_probability(
                audit_complete)

            flags = []
            for field, flag in flag_map:
                value = audit_probabilities[field]
                if field in {"stop_structurally_valid", "reward_after_cost_sufficient"}:
                    if value >= 0 and value < audit_flag_min:
                        flags.append(flag)
                elif value >= audit_flag_min:
                    flags.append(flag)
            if not audit_ok or _jev_policy_probability(
                    audit_answers.get("audit_data_valid")) < audit_data_valid_min:
                flags.append("audit_unavailable")
            audit = _jev_audit_verdict(flags, data_complete=audit_complete,
                                       min_confidence=audit_min_confidence)
        combined = _jev_combine_candidate(
            main_action=proposal.get("action", "WAIT"),
            independent={
                "suggested_action": action,
                "confidence": vote_result.get("confidence", 0),
                "action_margin": vote_result.get("action_margin", 0),
                "data_status": data_status,
                "action_status": action_status,
            },
            audit=audit,
            enforcement=review["enforcement_mode"],
            entry_mode=str(proposal.get("entry_mode") or "initial"),
            hard_gates_passed=True,
            hard_veto_code_only=review.get("hard_veto_code_only", True),
            veto_wait_min_confidence=veto_wait_min_confidence,
            veto_min_margin=veto_min_margin,
        )
        return {
            "instId": proposal["instId"],
            "cycle_id": proposal.get("cycle_id", ""),
            "decision_id": proposal.get("decision_id", ""),
            "main_action": proposal.get("action", "WAIT"),
            "main_raw_action": proposal.get("main_raw_action", proposal.get("action", "WAIT")),
            "main_confidence": proposal.get("confidence", 0),
            "main_raw_confidence": proposal.get("main_raw_confidence", proposal.get("confidence", 0)),
            "main_decision_reason": proposal.get("main_decision_reason", ""),
            "main_decision_outcome_source": proposal.get("main_decision_outcome_source", "unknown"),
            "main_decision_rejection_code": proposal.get("main_decision_rejection_code", ""),
            "entry_mode": proposal.get("entry_mode", "initial"),
            "direction_consistent": independent_answers.get(f"{prefix}_direction_consistent"),
            "execution_ready": execution_ready,
            # 代码侧的完整性与一致性判定（拆分后 INSUFFICIENT_DATA 的唯一来源）。
            "code_state_complete": code_quality["complete"],
            "code_state_missing_fields": code_quality["missing_fields"],
            "code_state_consistent": code_quality["consistent"],
            "code_state_consistency_flags": code_quality["consistency_flags"],
            "code_state_status": code_quality["status"],
            # 模型侧的优势判断。**只记录，不作否决门**（见上方 NOTE）。
            "edge_present": edge_present,
            "edge_probability": edge_probability,
            "execution_ready_probability": _jev_policy_probability(execution_ready),
            "suggested_action": action,
            "raw_suggested_action": raw_suggested_action,
            "suggested_action_votes": {
                "BUY_LONG": independent_answers.get(f"{prefix}_would_buy_long"),
                "SELL_SHORT": independent_answers.get(f"{prefix}_would_sell_short"),
                "WAIT": independent_answers.get(f"{prefix}_would_wait"),
            },
            "jev_confidence": vote_result.get("confidence", 0),
            "jev_action_margin": vote_result.get("action_margin", 0),
            # 归一化前的口径，保留以便对照与回滚（confidence 现为分布分量）。
            "jev_raw_max_vote": vote_result.get("raw_max_vote", -1.0),
            "jev_vote_sum": vote_result.get("vote_sum", 0.0),
            "jev_action_status": action_status,
            "jev_confidence_threshold": vote_result.get("confidence_threshold", min_confidence),
            "quote_source": "okx",
            "audit_proposal_complete": audit_complete,
            "audit_proposal_consistent": audit_answers.get(f"{prefix}_thesis_supported"),
            # None = 本候选从未被审计（WAIT 提议）。绝不要把它当 0 分参与统计。
            "audit_probabilities": audit_probabilities,
            "audit_flags": audit["flags"],
            "audit_verdict": audit["verdict"],
            "independent_action": action,
            "decision_relation": combined["jev_relation_to_main"],
            **combined,
        }

    def _position_review(index: int, proposal: Dict[str, Any]) -> Dict[str, Any]:
        prefix = f"position_{index}"
        # 与候选侧同一拆分：完整性/一致性归代码，只有它能触发 INSUFFICIENT_DATA。
        code_quality = _jev_position_state_quality(proposal)
        management_warranted = independent_answers.get(f"{prefix}_management_warranted")
        vote_result = _jev_rank_votes({
            "HOLD": independent_answers.get(f"{prefix}_would_hold"),
            "CLOSE_MARKET": independent_answers.get(f"{prefix}_would_close"),
            "UPDATE_SL": independent_answers.get(f"{prefix}_would_update_sl"),
        }, min_confidence=position_min_confidence, min_margin=position_min_margin)
        choice = _jev_normalize_choice(independent_answers.get(f"{prefix}_action"))
        if choice not in {"HOLD", "CLOSE_MARKET", "UPDATE_SL", "INSUFFICIENT_DATA"}:
            choice = vote_result["suggested_action"]
        if code_quality["status"] == "insufficient_data":
            action, status = "INSUFFICIENT_DATA", "code_state_incomplete"
        elif code_quality["status"] == "inconsistent":
            action, status = "INSUFFICIENT_DATA", "code_state_inconsistent"
        else:
            action, status = choice, vote_result["action_status"]
            if status in {"low_confidence", "ambiguous", "missing_action_votes"}:
                action = "INSUFFICIENT_DATA" if status == "missing_action_votes" else "HOLD"
            elif action == "HOLD" and status == "accepted":
                # 同候选侧：票决明确选 HOLD ⇒ 无需动作，与「拿不准」分开记录。
                status = "no_action_needed"
        flags = []
        protection = _jev_policy_probability(
            audit_answers.get(f"{prefix}_protection_ready"))
        close_needed = _jev_policy_probability(
            audit_answers.get(f"{prefix}_close_needed"))
        if protection >= 0 and protection < protection_min:
            flags.append("protection_not_ready")
        if close_needed >= 0.5:
            flags.append("audit_close_needed")
        if not audit_ok or _jev_policy_probability(
                audit_answers.get("audit_data_valid")) < audit_data_valid_min:
            flags.append("audit_unavailable")
        audit = _jev_audit_verdict(flags, min_confidence=audit_min_confidence)
        main_action = proposal.get("main_action", "HOLD")
        if audit["verdict"] == "REJECT":
            relation = "AUDIT_REJECT"
        elif action == "INSUFFICIENT_DATA":
            relation = "ABSTAIN"
        elif action == main_action:
            relation = "AGREE"
        else:
            relation = "POSITION_DISAGREEMENT"
        return {
            "instId": proposal["instId"],
            "cycle_id": proposal.get("cycle_id", ""),
            "decision_id": proposal.get("decision_id", ""),
            "main_action": main_action,
            "main_confidence": proposal.get("main_confidence", 0),
            "main_reason": proposal.get("main_reason", ""),
            "code_state_complete": code_quality["complete"],
            "code_state_missing_fields": code_quality["missing_fields"],
            "code_state_consistent": code_quality["consistent"],
            "code_state_consistency_flags": code_quality["consistency_flags"],
            "code_state_status": code_quality["status"],
            # 模型侧的「是否需要动作」判断，只记录、不作否决门。
            "management_warranted": management_warranted,
            "management_warranted_probability": _jev_policy_probability(
                management_warranted),
            # §9 阶段 D 的硬不变量：**平仓保护、止损与交易所安全门禁始终由代码
            # 控制**，任何档位下 Jev 都不得否决它们。因此持仓通道的判定恒为
            # SHADOW、恒不影响执行 —— 即使有人把 ASTRA_JEV_ENFORCEMENT 设成
            # soft_veto/hard_veto，也改变不了保护单与止损。
            "jev_enforcement": "SHADOW",
            "jev_enforcement_decision": "SHADOW",
            "jev_enforcement_reasons": ["protection_always_code_controlled"],
            "jev_enforcement_affects_execution": False,
            "suggested_action": action,
            "suggested_action_votes": {
                "HOLD": independent_answers.get(f"{prefix}_would_hold"),
                "CLOSE_MARKET": independent_answers.get(f"{prefix}_would_close"),
                "UPDATE_SL": independent_answers.get(f"{prefix}_would_update_sl"),
            },
            "jev_confidence": vote_result.get("confidence", 0),
            "jev_action_margin": vote_result.get("action_margin", 0),
            "jev_raw_max_vote": vote_result.get("raw_max_vote", -1.0),
            "jev_vote_sum": vote_result.get("vote_sum", 0.0),
            "jev_action_status": status,
            "audit_protection_ready": audit_answers.get(f"{prefix}_protection_ready"),
            "audit_close_needed": audit_answers.get(f"{prefix}_close_needed"),
            "audit_data_quality": _jev_shadow_position_audit_context(proposal).get("data_quality"),
            "audit_flags": audit["flags"],
            "audit_verdict": audit["verdict"],
            "decision_relation": relation,
            "jev_relation_to_main": relation,
        }

    # The market quote used to score a delayed Jev paper entry is refreshed only
    # after both answers arrive, so request latency is not mistaken for fill price.
    def _refresh_jev_quote(proposal: Dict[str, Any]) -> Dict[str, Any]:
        try:
            ticker = fetch_okx_ticker(proposal["instId"])
            if not ticker:
                raise RuntimeError("OKX ticker unavailable")
            refreshed_at = time.time()
            proposal["jev_price"] = _jev_shadow_float(ticker.get("last"), 0.0)
            proposal["jev_bidPx"] = _jev_shadow_float(ticker.get("bidPx"), 0.0)
            proposal["jev_askPx"] = _jev_shadow_float(ticker.get("askPx"), 0.0)
            proposal["jev_quote_timestamp"] = refreshed_at
            proposal["jev_quote_source"] = "okx"
            if proposal["jev_price"] <= 0:
                proposal["jev_quote_error"] = "OKX ticker missing last price"
        except Exception as quote_exc:
            proposal["jev_price"] = 0.0
            proposal["jev_bidPx"] = 0.0
            proposal["jev_askPx"] = 0.0
            proposal["jev_quote_error"] = f"{type(quote_exc).__name__}: {quote_exc}"
            proposal["jev_quote_timestamp"] = time.time()
            proposal["jev_quote_source"] = "okx_unavailable"
        return proposal

    if proposals:
        with ThreadPoolExecutor(max_workers=min(8, len(proposals))) as quote_executor:
            proposals = list(quote_executor.map(_refresh_jev_quote, proposals))
    instrument_reviews = [_candidate_review(index, proposal)
                          for index, proposal in enumerate(proposals)]
    position_reviews = [_position_review(index, proposal)
                        for index, proposal in enumerate(position_proposals)]
    review.update({
        "status": "ok" if independent_ok else "error",
        "audit_status": "ok" if audit_ok else "error",
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "channel_requests": {
            "independent": {key: independent_result.get(key) for key in (
                "status", "request_id", "latency_ms", "attempts", "error")},
            "audit": {key: audit_result.get(key) for key in (
                "status", "request_id", "latency_ms", "attempts", "error")},
        },
        "attempts": (independent_result.get("attempts", []) +
                     audit_result.get("attempts", [])),
        "aggregate_answers": {
            "audit_data_valid": audit_answers.get("audit_data_valid"),
        },
        # 代码侧汇总：整轮有多少候选/持仓在完整性或一致性上不达标。
        # 这是**代码的事实**，不再向模型重复提问。
        "code_cycle_quality": {
            "candidates_total": len(instrument_reviews),
            "candidates_incomplete": sum(
                1 for row in instrument_reviews if not row.get("code_state_complete")),
            "candidates_inconsistent": sum(
                1 for row in instrument_reviews if not row.get("code_state_consistent")),
            "positions_total": len(position_reviews),
            "positions_incomplete": sum(
                1 for row in position_reviews if not row.get("code_state_complete")),
            "positions_inconsistent": sum(
                1 for row in position_reviews if not row.get("code_state_consistent")),
        },
        "instrument_reviews": instrument_reviews,
        "position_reviews": position_reviews,
        "response": {
            "independent": independent_result.get("response"),
            "audit": audit_result.get("response"),
        },
        "independent_request_id": independent_result.get("request_id", ""),
        "audit_request_id": audit_result.get("request_id", ""),
    })
    if not independent_ok:
        review["error"] = independent_result.get("error", "independent lane failed")
        print(f"[AI Brain Jev Shadow] ⚠️ 独立决策通道失败（不影响执行）: {review['error']}")
    else:
        suffix = "，审计通道失败" if not audit_ok else ""
        print(f"[AI Brain Jev Shadow] ✅ 双通道影子复核完成 (候选 {len(proposals)} 个, "
              f"耗时 {review['latency_ms']}ms{suffix}; 结果不参与执行)")

    review["completed_timestamp"] = time.time()
    review["timestamp"] = int(review["completed_timestamp"])
    review["response_latency_seconds"] = max(
        0.0, review["completed_timestamp"] - review_started_timestamp)
    _jev_shadow_update_entry_outcomes(
        proposals,
        review.get("instrument_reviews", []),
        active_positions,
        review,
    )
    _jev_shadow_update_position_outcomes(position_proposals, review.get("position_reviews", []), review)

    path = os.path.join(DATA_DIR, "jev_shadow_reviews.jsonl")
    try:
        from astra_backend.file_locks import file_lock
        try:
            retention_days = max(1, min(int(os.environ.get("ASTRA_JEV_SHADOW_RETENTION_DAYS", "7")), 30))
        except (TypeError, ValueError):
            retention_days = 7
        try:
            max_records = max(100, min(int(os.environ.get("ASTRA_JEV_SHADOW_MAX_RECORDS", "1000")), 5000))
        except (TypeError, ValueError):
            max_records = 1000
        cutoff = int(time.time()) - retention_days * 24 * 60 * 60
        with file_lock(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            retained = []
            dropped = 0
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                            item_ts = int(item.get("timestamp", 0) or 0)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            dropped += 1
                            continue
                        if item_ts < cutoff:
                            dropped += 1
                            continue
                        retained.append(item)
            retained.append(review)
            if len(retained) > max_records:
                dropped += len(retained) - max_records
                retained = retained[-max_records:]

            fd, tmp_path = tempfile.mkstemp(
                prefix=".jev-shadow-", suffix=".tmp", dir=os.path.dirname(path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for item in retained:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            if dropped:
                print(f"[AI Brain Jev Shadow] 清理 {dropped} 条过期/无效记录，"
                      f"保留最近 {retention_days} 天共 {len(retained)} 条")
    except Exception as exc:
        print(f"[AI Brain Jev Shadow] warn 影子结果落盘失败（不影响执行）: {exc}")

    # 原始响应按短周期清理；评估台账保留结构化快照更久，确保 20-30 笔
    # 完整交易跨周时仍能按 decision_id 回看。这里不重复保存 provider 原始元数据，
    # 减少磁盘占用，完整响应仍在上面的短期文件中。
    evaluation_path = os.path.join(DATA_DIR, "jev_shadow_evaluation.jsonl")
    try:
        from astra_backend.file_locks import file_lock
        try:
            evaluation_days = max(14, min(
                int(os.environ.get("ASTRA_JEV_EVALUATION_RETENTION_DAYS", "60")), 180))
        except (TypeError, ValueError):
            evaluation_days = 60
        try:
            evaluation_max_records = max(200, min(
                int(os.environ.get("ASTRA_JEV_EVALUATION_MAX_RECORDS", "5000")), 20000))
        except (TypeError, ValueError):
            evaluation_max_records = 5000
        evaluation_record = dict(review)
        evaluation_record.pop("response", None)
        cutoff = int(time.time()) - evaluation_days * 24 * 60 * 60
        with file_lock(evaluation_path):
            os.makedirs(os.path.dirname(evaluation_path), exist_ok=True)
            retained = []
            if os.path.exists(evaluation_path):
                with open(evaluation_path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                            if int(item.get("timestamp", 0) or 0) >= cutoff:
                                retained.append(item)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
            retained.append(evaluation_record)
            retained = retained[-evaluation_max_records:]
            fd, tmp_path = tempfile.mkstemp(
                prefix=".jev-evaluation-", suffix=".tmp", dir=os.path.dirname(evaluation_path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for item in retained:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, evaluation_path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
    except Exception as exc:
        print(f"[AI Brain Jev Shadow] warn 评估台账落盘失败（不影响执行）: {exc}")


def _schedule_jev_shadow_review(
    standard_cache: Dict[str, Any],
    packages: List[Dict[str, Any]],
    time_str: str,
    *,
    active_positions_detail: Optional[List[Dict[str, Any]]] = None,
    position_management: Optional[List[Dict[str, Any]]] = None,
    usdt_available: Optional[float] = None,
    pending_orders_detail: Optional[List[Dict[str, Any]]] = None,
    trader_factors: Optional[List[Dict[str, Any]]] = None,
):
    """Queue an isolated Jev review without delaying the live decision path."""
    try:
        args = (
            copy.deepcopy(standard_cache),
            copy.deepcopy(packages),
            str(time_str),
        )
        kwargs = {
            "active_positions_detail": copy.deepcopy(active_positions_detail),
            "position_management": copy.deepcopy(position_management),
            "usdt_available": usdt_available,
            "pending_orders_detail": copy.deepcopy(pending_orders_detail),
            "trader_factors": copy.deepcopy(trader_factors),
        }
        future = _JEV_SHADOW_EXECUTOR.submit(
            _run_jev_shadow_review, *args, **kwargs)
    except Exception as exc:
        print(f"[AI Brain Jev Shadow] warn 后台复核提交失败（不影响执行）: {exc}")
        return None

    def _log_failure(done_future) -> None:
        try:
            done_future.result()
        except Exception as exc:
            print(f"[AI Brain Jev Shadow] warn 后台复核异常（不影响执行）: {exc}")

    future.add_done_callback(_log_failure)
    return future


def execute_batch_ai_brain_cycle(
    pos_summary: str = "[MISSING_CONTEXT:account_positions]",
    active_positions_detail: List[Dict[str, Any]] = None,
    usdt_available: float = None,
    policy_snapshot: Optional[Dict[str, Any]] = None,
    trader_factors: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch all six crypto symbols, call the LLM once, then persist an auditable result."""
    base_url, api_key = get_cpa_client_config()
    if not api_key:
        print("[AI Brain Batch] Error: CPA API Key not found")
        _record_cycle_health("failed", "CPA API Key 未配置")
        return None

    tz_bj = datetime.timezone(datetime.timedelta(hours=8))
    now_bj = datetime.datetime.now(tz_bj)
    time_str = now_bj.strftime("%Y-%m-%d %H:%M:%S")

    # Capture immutable Policy Snapshot at start of decision cycle
    (policy_hash, policy_snapshot, policy_summary, policy_version) = capture_policy_snapshot(
        _get_system_version_tag=_get_system_version_tag,
        policy_snapshot=policy_snapshot    )
    print(f"[AI Brain Batch] 📌 当前决策策略快照: {policy_version} ({policy_hash})")

    print(f"[AI Brain Batch] 并行获取 {len(TARGET_INSTRUMENTS)} 币种原生行情、技术指标与顶级聪明钱数据...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        packages = list(executor.map(fetch_single_instrument_package, TARGET_INSTRUMENTS))

    # 顶级聪明钱与大户持仓数据接入（OKX Rubik 公开统计，单一来源）
    try:
        try:
            from scripts.factors.smart_money import fetch_smart_money_for_symbol
        except ImportError:
            from factors.smart_money import fetch_smart_money_for_symbol
        for pkg in packages:
            ccy = pkg.get("ccy") or pkg.get("name") or ""
            if not ccy and "-" in pkg.get("instId", ""):
                ccy = pkg["instId"].split("-")[0]
            sm_data = fetch_smart_money_for_symbol(ccy, price=float(pkg.get("price") or 0.0))
            if sm_data:
                if pkg.get("lsRatio") == "N/A" and sm_data.get("lsRatio"):
                    pkg["lsRatio"] = sm_data["lsRatio"]
                if pkg.get("takerNetUsd") == "N/A" and sm_data.get("takerNetUsd"):
                    pkg["takerNetUsd"] = sm_data["takerNetUsd"]
                pkg["smart_money"] = {
                    "available": True,
                    "weighted_long_pct": sm_data.get("weighted_long_pct", "--"),
                    "net_flow_usdt": sm_data.get("takerNetUsd", "--"),
                    "avg_long_entry": "--",
                    "avg_short_entry": "--",
                    "top_win_rate": "--",
                }
    except Exception as exc:
        print(f"[AI Brain Batch] 聪明钱数据注入降级: {exc}")

    positions_context = active_positions_detail
    active_positions_detail = active_positions_detail or []

    # 审计 P2-12：跨所 id 归一（模块级 canonical_position_inst_id，含单元测试）
    def _canonical_inst_id(raw: Any) -> str:
        return canonical_position_inst_id(raw)

    active_inst_ids = {
        _canonical_inst_id(p.get("instId")) for p in active_positions_detail if p.get("instId")
    }
    active_inst_ids.discard("")
    active_position_sides = {
        _canonical_inst_id(p.get("instId")): str(p.get("side", p.get("posSide", ""))).lower()
        for p in active_positions_detail if p.get("instId")
    }
    active_position_sides.pop("", None)
    # 审计D(2026-09-13)：package_by_id 死构造清除（全函数无消费）

    # Automatically Update & Persist Comprehensive Factor Library Snapshot
    update_factor_library_snapshot(
        WORKSPACE_DIR=WORKSPACE_DIR,
        os=os,
        sys=sys    )

    # Fetch live pending limit orders from exchange（V5 直签 REST，行为契约见 fetch_pending_orders_list）
    pending_orders_list = fetch_pending_orders_list()

    write_calculus_snapshot(
        CALCULUS_SNAPSHOT_FILE=CALCULUS_SNAPSHOT_FILE,
        json=json,
        os=os,
        packages=packages,
        time_str=time_str    )

    runtime_context = {}
    prompt = construct_full_market_prompt(packages, pos_summary, positions_context, pending_orders_detail=pending_orders_list, current_time_str=time_str, usdt_available=usdt_available, runtime_context_out=runtime_context, policy_snapshot=policy_snapshot)

    profile = active_profile()
    # 审计 P1-3：覆盖层由 get_effective_system_prompt 在布局**之后**追加（此前被布局丢弃）
    effective_system_prompt = get_effective_system_prompt(profile=profile, context=runtime_context)

    # Save Realtime Prompt Snapshot for Web Transparent Inspection
    write_prompt_snapshot(
        AI_LAST_PROMPT_FILE=AI_LAST_PROMPT_FILE,
        _build_effective_prompt_text=_build_effective_prompt_text,
        effective_system_prompt=effective_system_prompt,
        os=os,
        prompt=prompt,
        time_str=time_str    )

    (api_format, api_key, base_url, effort, execute_llm_request, model_name, thinking_timeout) = resolve_llm_runtime(
        api_key=api_key,
        base_url=base_url,
        os=os    )

    telemetry = ModelCallTelemetry(
        "trading_brain", model_name, str(effort), effective_system_prompt, prompt
    )
    result = dispatch_llm_and_persist_decisions(
        AI_DECISION_CACHE_FILE=AI_DECISION_CACHE_FILE,
        AI_DECISION_HISTORY_FILE=AI_DECISION_HISTORY_FILE,
        AI_POSITION_MANAGEMENT_FILE=AI_POSITION_MANAGEMENT_FILE,
        Any=Any,
        Dict=Dict,
        _build_effective_prompt_text=_build_effective_prompt_text,
        _build_history_record=_build_history_record,
        _normalize_position_management=_normalize_position_management,
        _record_cycle_health=_record_cycle_health,
        active_inst_ids=active_inst_ids,
        active_position_sides=active_position_sides,
        api_format=api_format,
        api_key=api_key,
        assemble_decision_cache=assemble_decision_cache,
        atomic_write_json=atomic_write_json,
        base_url=base_url,
        effective_system_prompt=effective_system_prompt,
        effort=effort,
        execute_brain_pending_cancels=execute_brain_pending_cancels,
        execute_llm_request=execute_llm_request,
        json=json,
        model_name=model_name,
        os=os,
        packages=packages,
        policy_hash=policy_hash,
        policy_snapshot=policy_snapshot,
        policy_summary=policy_summary,
        policy_version=policy_version,
        prompt=prompt,
        runtime_context=runtime_context,
        safe_float=safe_float,
        telemetry=telemetry,
        thinking_timeout=thinking_timeout,
        time=time,
        time_str=time_str,
        urllib=urllib    )
    if result:
        position_management = []
        try:
            with open(AI_POSITION_MANAGEMENT_FILE, "r", encoding="utf-8") as handle:
                _management_payload = json.load(handle)
            if isinstance(_management_payload, dict) and isinstance(_management_payload.get("instructions"), list):
                position_management = _management_payload["instructions"]
        except Exception as _management_exc:
            print(f"[AI Brain Jev Shadow] warn 无法读取本轮持仓管理指令: {_management_exc}")
        _schedule_jev_shadow_review(
            result,
            packages,
            time_str,
            active_positions_detail=active_positions_detail,
            position_management=position_management,
            usdt_available=usdt_available,
            pending_orders_detail=pending_orders_list,
            trader_factors=trader_factors,
        )
    return result

def get_latest_ai_decision(inst_id: str, max_age_seconds: int = DECISION_MAX_AGE_SECONDS) -> Optional[Dict[str, Any]]:
    """Read a validated decision only while its cache timestamp is fresh."""
    if os.path.exists(AI_DECISION_CACHE_FILE):
        try:
            with open(AI_DECISION_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            item = data.get(inst_id)
            if not isinstance(item, dict):
                return None
            timestamp = int(item.get("timestamp", 0) or 0)
            if timestamp <= 0 or int(time.time()) - timestamp > max_age_seconds:
                return None
            return item
        except Exception:
            pass
    return None

if __name__ == "__main__":
    res = execute_batch_ai_brain_cycle("当前无持仓")
    if res:
        print("\n--- 示例标的 AI 决策结果 ---")
        for k in ["BTC-USDT-SWAP", "SOL-USDT-SWAP", "LINK-USDT-SWAP"]:
            if k in res:
                print(f"[{k}]", json.dumps(res[k]["decision"], ensure_ascii=False))
