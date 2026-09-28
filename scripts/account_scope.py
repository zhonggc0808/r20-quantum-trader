"""Persistent deployment environment fence and account-record selection.

A scoped checkout owns one environment. Other environments use a separate
checkout and data directory. Unknown historical rows stay in the migration
archive, never in the active account's performance or learning data.
"""
from __future__ import annotations
import json
import os
import warnings
from pathlib import Path
from typing import Mapping


def runtime_data_dir(default):
    """Resolve the account-state data directory exactly like the rest of the repo.

    ``ASTRA_DATA_DIR`` always wins. Six other modules already honour it —
    ``factor_library`` / ``news_sentiment_harvester`` / ``sync_full_ledger`` /
    ``self_improvement_engine`` / ``trader.position_exit``
    — and ``tests/config_sandbox.isolate_config`` sets it so that both in-process
    code *and spawned subprocesses* stop touching production ``data/``.

    ⚠️ This helper exists because the account fence used to read ``ROOT / "data"``
    directly. ``ROOT`` is a module-level constant that no sandbox can redirect (a
    trap the repo already documents in ``instrument_pool.py``), so a sandboxed run
    opened the **production** ``account_scope.json`` and died with
    "此实例固定为 live" — taking ~70 unrelated cases down with it. Anyone reading
    account state must resolve the directory through here, never through ``ROOT``.
    """
    override = os.environ.get("ASTRA_DATA_DIR")
    return Path(override) if override else Path(default)


def load_scope(data_dir):
    path = Path(data_dir) / 'account_scope.json'
    if not path.exists():
        return None
    scope = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(scope, dict) or scope.get('environment') not in ('live', 'demo'):
        raise ValueError('账户隔离配置无效，拒绝混用数据')
    return scope


def assert_environment(data_dir, environment):
    scope = load_scope(data_dir)
    if scope and environment != scope['environment']:
        raise ValueError(f"此实例固定为 {scope['environment']}，请在独立实例使用 {environment}，禁止共用账户数据")


def validate_environment_update(data_dir, values: Mapping, *, removing=False):
    scope = load_scope(data_dir)
    if not scope:
        return
    for key in ('ASTRA_OKX_ENV', 'OKX_IS_SIMULATED'):
        if key not in values:
            continue
        if removing:
            raise ValueError('隔离实例不允许删除账户环境设置')
        value = values[key]
        if value is None:
            continue
        mode = str(value).lower() if key == 'ASTRA_OKX_ENV' else ('demo' if str(value).lower() in ('1','true','yes') else 'live')
        assert_environment(data_dir, mode)


def scoped_rows(rows, data_dir):
    scope = load_scope(data_dir)
    if not scope:
        return rows
    mode = scope['environment']
    start = scope.get('history_start', '')
    kept = []
    dropped = {
        'non_dict': 0,
        'missing_environment': 0,
        'other_environment': 0,
        'before_history_start': 0,
    }
    for row in rows:
        if not isinstance(row, dict):
            dropped['non_dict'] += 1
            continue
        row_mode = str(row.get('environment') or row.get('account_mode') or '').lower()
        if row_mode in ('sandbox', 'testnet'):
            row_mode = 'demo'
        if not row_mode:
            dropped['missing_environment'] += 1
            continue
        if row_mode != mode:
            dropped['other_environment'] += 1
            continue
        if row.get('status') != 'holding' and start:
            stamp = str(row.get('close_time') or row.get('time') or '')
            if not stamp or stamp < start:
                dropped['before_history_start'] += 1
                continue
        kept.append(row)
    dropped_total = sum(dropped.values())
    if dropped_total:
        reasons = ', '.join(f'{key}={value}' for key, value in dropped.items() if value)
        warnings.warn(
            f'[账户隔离] scoped_rows 丢弃 {dropped_total} 行（目标环境={mode}；{reasons}）',
            RuntimeWarning,
            stacklevel=2,
        )
    return kept
