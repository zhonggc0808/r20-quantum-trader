"""Policy Snapshot proxy module for scripts workspace."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from r20_backend.policy_snapshot import (
    DEFAULT_BASE_VERSION,
    compute_layout_hash,
    extract_prompt_profile_fingerprint,
    extract_evolution_mind_fingerprint,
    extract_interceptors_fingerprint,
    extract_council_fingerprint,
    format_policy_snapshot_summary,
    generate_policy_snapshot,
    get_current_policy_snapshot,
)

__all__ = [
    "DEFAULT_BASE_VERSION",
    "compute_layout_hash",
    "extract_prompt_profile_fingerprint",
    "extract_evolution_mind_fingerprint",
    "extract_interceptors_fingerprint",
    "extract_council_fingerprint",
    "format_policy_snapshot_summary",
    "generate_policy_snapshot",
    "get_current_policy_snapshot",
]
