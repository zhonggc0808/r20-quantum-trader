"""Direct signed OKX V5 control-plane client with seamless CLI OAuth fallback."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
import logging
from typing import Any
from scripts.okx_runtime import OKXEnvironment, selected_environment

logger = logging.getLogger(__name__)

_INTENTS: dict[str, dict[str, Any]] = {}
_INTENT_LOCK = threading.Lock()
INTENT_TTL_SECONDS = 90


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _run_cli(command: list[str], timeout: int = 20) -> list[dict[str, Any]]:
    try:
        res = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f"OKX CLI 执行失败：{type(exc).__name__}: {exc}") from exc
    if res.returncode != 0:
        err_msg = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
        raise RuntimeError(f"OKX CLI 错误：{err_msg}")
    try:
        data = json.loads(res.stdout or "[]")
        rows = data if isinstance(data, list) else [data]
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OKX CLI JSON 解析失败：{exc} stdout={res.stdout[:200]}") from exc
    failures = [row for row in rows if isinstance(row, dict) and str(row.get("sCode", row.get("code", "0"))) != "0"]
    if failures:
        code = failures[0].get("sCode", failures[0].get("code", "--"))
        message = failures[0].get("sMsg") or failures[0].get("msg") or "业务请求失败"
        raise RuntimeError(f"OKX CLI {code}: {message}")
    return [row for row in rows if isinstance(row, dict)]


def _request(method: str, path: str, params: dict[str, Any] | None = None, env: OKXEnvironment | None = None, timeout: int = 20) -> list[dict[str, Any]]:
    selected = env or selected_environment()
    if not selected.configured:
        # Fallback to CLI
        mode_flag = f"--{selected.mode}"
        if path == "/api/v5/account/positions":
            cmd = ["okx", mode_flag, "account", "positions", "--json"]
            if params and params.get("instId"):
                cmd.extend(["--instId", str(params["instId"])])
            return _run_cli(cmd, timeout=timeout)
        elif path == "/api/v5/trade/orders-pending":
            cmd = ["okx", mode_flag, "swap", "orders", "--json"]
            if params and params.get("instId"):
                cmd.extend(["--instId", str(params["instId"])])
            return _run_cli(cmd, timeout=timeout)
        elif path == "/api/v5/trade/cancel-order":
            cmd = ["okx", mode_flag, "swap", "cancel", str(params.get("instId")), "--ordId", str(params.get("ordId")), "--json"]
            return _run_cli(cmd, timeout=timeout)
        elif path == "/api/v5/trade/close-position":
            cmd = ["okx", mode_flag, "swap", "close", "--instId", str(params.get("instId")), "--mgnMode", str(params.get("mgnMode", "cross")), "--posSide", str(params.get("posSide", "net")), "--autoCxl", "--json"]
            return _run_cli(cmd, timeout=timeout)
        raise RuntimeError(f"OKX {selected.mode.upper()} 静态 API Key 未配置，且不支持该操作的 CLI 回退：{path}")

    params = params or {}; method = method.upper()
    query = urllib.parse.urlencode({k:v for k,v in params.items() if v not in (None, "")}) if method == "GET" else ""
    request_path = path + (f"?{query}" if query else "")
    body_text = json.dumps({k:v for k,v in params.items() if v not in (None, "")}, separators=(",", ":"), ensure_ascii=False) if method != "GET" else ""
    timestamp = _timestamp(); prehash = timestamp + method + request_path + body_text
    signature = base64.b64encode(hmac.new(selected.secret_key.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "OK-ACCESS-KEY": selected.api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": selected.passphrase,
    }
    if selected.simulated: headers["x-simulated-trading"] = "1"
    request = urllib.request.Request(selected.base_url + request_path, data=body_text.encode() if body_text else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response: payload = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as exc: raise RuntimeError(f"OKX V5 网络请求失败：{type(exc).__name__}: {exc}") from exc
    if str(payload.get("code", "0")) != "0": raise RuntimeError(f"OKX {payload.get('code')}: {payload.get('msg') or '请求失败'}")
    data = payload.get("data") or []
    if not isinstance(data, list): data = [data]
    failures = [row for row in data if isinstance(row, dict) and str(row.get("sCode", "0")) != "0"]
    if failures: raise RuntimeError(f"OKX {failures[0].get('sCode')}: {failures[0].get('sMsg') or '业务请求失败'}")
    return [row for row in data if isinstance(row, dict)]


def _create_intent(env: OKXEnvironment, position: dict[str, Any]) -> tuple[str, str]:
    token = secrets.token_urlsafe(32); size = abs(float(position.get("pos", 0) or 0)); side = str(position.get("posSide") or "net").lower()
    confirmation = f"CLOSE {env.mode.upper()} {position.get('instId')} {side.upper()} {size:g}"
    record = {"environment_id":env.identity,"instId":str(position.get("instId")),"posSide":side,"posId":str(position.get("posId") or ""),"expected_size":size,"confirmation":confirmation,"expires_at":time.time()+INTENT_TTL_SECONDS}
    with _INTENT_LOCK:
        now=time.time(); stale=[key for key,value in _INTENTS.items() if value["expires_at"]<now]
        for key in stale: _INTENTS.pop(key,None)
        _INTENTS[token]=record
    return token, confirmation


def account_snapshot() -> dict[str, Any]:
    env = selected_environment()
    positions = [p for p in _request("GET", "/api/v5/account/positions", {"instType":"SWAP"}, env) if abs(float(p.get("pos",0) or 0))>1e-12]
    orders = _request("GET", "/api/v5/trade/orders-pending", {"instType":"SWAP"}, env)
    public_positions=[]
    for position in positions:
        token, confirmation = _create_intent(env, position)
        public_positions.append({**position,"close_token":token,"close_confirmation":confirmation,"close_token_expires_in":INTENT_TTL_SECONDS})
    return {"environment":env.mode,"environment_id":env.identity,"credential_source":"static-v5-key" if env.configured else "cli-oauth","positions":public_positions,"orders":orders,"captured_at_ms":int(time.time()*1000)}


def _consume_intent(token: str) -> dict[str, Any]:
    with _INTENT_LOCK: intent=_INTENTS.pop(str(token),None)
    if not intent: raise ValueError("平仓令牌无效或已使用，请刷新当前持仓")
    if intent["expires_at"]<time.time(): raise ValueError("平仓令牌已过期，请刷新当前持仓")
    return intent


def _position_match(positions: list[dict[str, Any]], intent: dict[str, Any]) -> dict[str, Any] | None:
    candidates=[p for p in positions if p.get("instId")==intent["instId"] and str(p.get("posSide","")).lower()==intent["posSide"]]
    if intent["posId"]: candidates=[p for p in candidates if str(p.get("posId", ""))==intent["posId"]]
    return candidates[0] if candidates else None


def fast_close_confirmed(close_token: str, confirmation: str) -> dict[str, Any]:
    intent=_consume_intent(close_token); env=selected_environment()
    if env.identity!=intent["environment_id"]: raise ValueError("OKX 环境或凭证已变化，请刷新当前持仓")
    if confirmation.strip().upper()!=intent["confirmation"]: raise ValueError(f"确认短语必须精确为：{intent['confirmation']}")
    target=_position_match(_request("GET","/api/v5/account/positions",{"instType":"SWAP","instId":intent["instId"]},env),intent)
    if not target: raise ValueError("目标仓位已不存在，请刷新")
    actual=abs(float(target.get("pos",0) or 0)); tolerance=max(1e-12,actual*1e-6)
    if abs(actual-intent["expected_size"])>tolerance: raise ValueError(f"仓位数量已从 {intent['expected_size']} 变化为 {actual}，请刷新")
    target_side=intent["posSide"] if intent["posSide"] in {"long","short"} else ("long" if float(target.get("pos",0) or 0)>0 else "short")
    canceled=[]; cancel_failures=[]
    # Cancel active regular orders
    for order in _request("GET","/api/v5/trade/orders-pending",{"instType":"SWAP","instId":intent["instId"]},env):
        order_side=str(order.get("posSide") or "net").lower()
        if order_side not in {target_side,"net"}: continue
        order_id=str(order.get("ordId") or "")
        if order_id:
            try:
                _request("POST","/api/v5/trade/cancel-order",{"instId":intent["instId"],"ordId":order_id},env)
                canceled.append(order_id)
            except Exception as exc:
                cancel_failures.append(f"{order_id}: {exc}")

    # Also cancel any attached/standalone algo orders (such as native cloud OCO orders) to avoid conflicts
    try:
        mode_flag = f"--{env.mode}"
        algo_orders = _run_cli(["okx", mode_flag, "swap", "algo", "orders", "--instId", intent["instId"], "--json"])
        for ao in algo_orders:
            ao_side = str(ao.get("posSide") or "net").lower()
            if ao_side in {target_side, "net"}:
                algo_id = str(ao.get("algoId") or "")
                if algo_id:
                    try:
                        _run_cli(["okx", mode_flag, "swap", "algo", "cancel", intent["instId"], "--algoId", algo_id, "--json"])
                        canceled.append(f"algo:{algo_id}")
                    except Exception as exc:
                        logger.warning("Cancel algo order %s failed during close: %s", algo_id, exc)
    except Exception as exc:
        logger.warning("Scanning algo orders failed during close: %s", exc)

    if cancel_failures:
        raise RuntimeError("平仓前存在无法撤销的同仓位委托：" + "; ".join(cancel_failures))
    close_side = intent["posSide"] if intent["posSide"] in {"long", "short"} else "net"
    close_result=_request("POST","/api/v5/trade/close-position",{"instId":intent["instId"],"mgnMode":str(target.get("mgnMode") or "cross"),"posSide":close_side,"autoCxl":True,"clOrdId":f"r20close{int(time.time())}"},env)
    remaining=actual
    for _ in range(10):
        time.sleep(.7); current=_position_match(_request("GET","/api/v5/account/positions",{"instType":"SWAP","instId":intent["instId"]},env),intent)
        remaining=abs(float(current.get("pos",0) or 0)) if current else 0.0
        if remaining<=tolerance: break
    if remaining>tolerance: raise RuntimeError(f"平仓请求已受理但仓位未确认归零，剩余 {remaining}；请刷新，禁止重复点击")
    return {"status":"confirmed_closed","environment":env.mode,"instId":intent["instId"],"posSide":intent["posSide"],"closed_size":actual,"canceled_entry_orders":canceled,"close_result":close_result}
