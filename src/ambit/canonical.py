"""Canonical JSON — one byte-exact serialisation for the whole project.

Grant signatures and the audit hash chain both depend on two processes
serialising the same object to the same bytes. If anything reimplements this
with different separators or key ordering, signatures stop verifying and the
chain stops re-walking. So this is the single definition and nothing else may
roll its own.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Sorted keys, no insignificant whitespace, unicode preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def format_paise(paise: int) -> str:
    """Display helper only. Never feed the result back into a calculation."""
    sign = "-" if paise < 0 else ""
    p = abs(int(paise))
    return f"{sign}₹{p // 100:,}.{p % 100:02d}"
