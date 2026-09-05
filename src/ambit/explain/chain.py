"""Append-only, tamper-evident audit chain.

    hash = sha256(prev_hash || canonical_json(entry_without_hash))

Every money-relevant event is appended here *before* it is attempted, so a
crash mid-flight still leaves evidence of intent. Editing any past row breaks
verification from that row onward, visibly — which is the point.

Storage is JSONL: one entry per line, human-readable, greppable, and easy to
show on screen during a demo.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ambit.canonical import canonical_bytes

GENESIS_PREV_HASH = "0" * 64


def _entry_hash(entry_without_hash: dict[str, Any]) -> str:
    prev = entry_without_hash["prev_hash"]
    digest = hashlib.sha256()
    digest.update(prev.encode("utf-8"))
    digest.update(canonical_bytes(entry_without_hash))
    return digest.hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries_checked: int
    broken_at_seq: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "entries_checked": self.entries_checked,
            "broken_at_seq": self.broken_at_seq,
            "reason": self.reason,
        }


class AuditChain:
    """A single append-only chain file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── reading ──────────────────────────────────────────────────────────
    def __iter__(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def entries(self) -> list[dict[str, Any]]:
        return list(self)

    def head(self) -> tuple[int, str]:
        """(last_seq, last_hash). Returns (-1, GENESIS) on an empty chain."""
        seq, prev = -1, GENESIS_PREV_HASH
        for entry in self:
            seq = entry["seq"]
            prev = entry["hash"]
        return seq, prev

    # ── writing ──────────────────────────────────────────────────────────
    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        last_seq, prev_hash = self.head()
        at = at or datetime.now(timezone.utc)
        entry = {
            "seq": last_seq + 1,
            "ts": at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": event_type,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        entry["hash"] = _entry_hash(entry)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    # ── verifying ────────────────────────────────────────────────────────
    def verify(self) -> VerifyResult:
        expected_prev = GENESIS_PREV_HASH
        expected_seq = 0
        checked = 0

        for entry in self:
            seq = entry.get("seq")
            if seq != expected_seq:
                return VerifyResult(
                    False, checked, seq,
                    f"sequence break: expected seq {expected_seq}, found {seq}",
                )
            if entry.get("prev_hash") != expected_prev:
                return VerifyResult(
                    False, checked, seq,
                    "prev_hash does not match the previous entry's hash — "
                    "a row was inserted, removed, or reordered",
                )
            core = {k: v for k, v in entry.items() if k != "hash"}
            recomputed = _entry_hash(core)
            if recomputed != entry.get("hash"):
                return VerifyResult(
                    False, checked, seq,
                    "content hash mismatch — this row was edited after it was written",
                )
            expected_prev = entry["hash"]
            expected_seq = seq + 1
            checked += 1

        return VerifyResult(True, checked)
