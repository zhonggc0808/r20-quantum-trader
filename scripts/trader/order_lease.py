"""AI pending-order KEEP leases shared by brain and trader processes."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from astra_backend.file_locks import file_lock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEASE_FILE_NAME = "pending_order_leases.json"
AI_KEEP_LEASE_MS = 20 * 60 * 1000


def lease_file(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the shared state path while preserving the sandbox redirect contract."""
    root = os.environ.get("ASTRA_DATA_DIR") or data_dir or (PROJECT_ROOT / "data")
    return Path(root) / LEASE_FILE_NAME


def _read_unlocked(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"[挂单租约] warn 租约文件不可读，按无有效租约处理: {exc}")
        return {}
    rows = payload.get("leases", payload) if isinstance(payload, dict) else {}
    if not isinstance(rows, dict):
        return {}
    return {
        str(order_id): dict(record)
        for order_id, record in rows.items()
        if order_id and isinstance(record, dict)
    }


def load_leases(data_dir: str | os.PathLike[str] | None = None) -> Dict[str, Dict[str, Any]]:
    path = lease_file(data_dir)
    with file_lock(path):
        return _read_unlocked(path)


def get_lease(order_id: str, data_dir: str | os.PathLike[str] | None = None) -> Optional[Dict[str, Any]]:
    return load_leases(data_dir).get(str(order_id))


def _write_unlocked(path: Path, leases: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": int(time.time() * 1000),
        "leases": leases,
    }
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def record_keep(order_id: str, inst_id: str, *, now_ms: int | None = None,
                lease_ms: int = AI_KEEP_LEASE_MS,
                data_dir: str | os.PathLike[str] | None = None) -> Dict[str, Any]:
    order_id = str(order_id or "").strip()
    inst_id = str(inst_id or "").strip()
    if not order_id or not inst_id:
        raise ValueError("order_id and inst_id are required for a KEEP lease")
    now_ms = int(now_ms or time.time() * 1000)
    record = {
        "ordId": order_id,
        "instId": inst_id,
        "action": "KEEP",
        "updated_at": now_ms,
        "lease_until": now_ms + int(lease_ms),
    }
    path = lease_file(data_dir)
    with file_lock(path):
        leases = _read_unlocked(path)
        leases[order_id] = record
        _write_unlocked(path, leases)
    return record


def remove_lease(order_id: str, *, data_dir: str | os.PathLike[str] | None = None) -> bool:
    order_id = str(order_id or "").strip()
    if not order_id:
        return False
    path = lease_file(data_dir)
    with file_lock(path):
        leases = _read_unlocked(path)
        removed = leases.pop(order_id, None) is not None
        if removed:
            _write_unlocked(path, leases)
    return removed


def prune_leases(active_order_ids: Iterable[str], *,
                 data_dir: str | os.PathLike[str] | None = None) -> int:
    active = {str(order_id) for order_id in active_order_ids if order_id}
    path = lease_file(data_dir)
    with file_lock(path):
        leases = _read_unlocked(path)
        stale = [order_id for order_id in leases if order_id not in active]
        if stale:
            for order_id in stale:
                leases.pop(order_id, None)
            _write_unlocked(path, leases)
    return len(stale)
