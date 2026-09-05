"""Mutable state that a signed grant deliberately cannot hold.

A grant is signed and therefore immutable. Spend, revocation, transaction
history and idempotency records all change *after* issue, so writing them back
into the grant would break its own signature. They live here instead, and the
checks read them from here.

Storage is plain JSON and JSONL on disk. That is a deliberate choice for a
demo: every piece of state can be opened in an editor and read aloud on a
video, which a SQLite file cannot.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ambit.canonical import canonical_bytes
from ambit.bound.grant import Grant, from_iso, to_iso

# How long an ALLOW is treated as reserving budget before it is assumed dead.
# Budget is debited on capture, not on ALLOW - but an authorisation still has
# to hold its slice of the budget for a while, or two agents racing the same
# grant could both pass BUDGET_REMAINING and together overspend it.
AUTHORISATION_TTL_SECONDS = 900


def request_fingerprint(
    grant_id: str, agent_id: str, merchant_id: str, category: str, amount_paise: int
) -> str:
    """Identity of *what was asked for*, independent of the request id.

    Used to tell an honest retry (same id, same ask) from a replay attack
    (same id, different ask).
    """
    return hashlib.sha256(
        canonical_bytes(
            {
                "grant_id": grant_id,
                "agent_id": agent_id,
                "merchant_id": merchant_id,
                "category": category,
                "amount_paise": amount_paise,
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class LedgerEntry:
    request_id: str
    grant_id: str
    agent_id: str
    merchant_id: str
    category: str
    amount_paise: int
    decision: str
    state: str  # authorised | captured | failed | cancelled
    ts: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "grant_id": self.grant_id,
            "agent_id": self.agent_id,
            "merchant_id": self.merchant_id,
            "category": self.category,
            "amount_paise": self.amount_paise,
            "decision": self.decision,
            "state": self.state,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerEntry":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class BoundStore:
    """Everything BOUND needs to know that the grant itself cannot say."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.grants_dir = self.root / "grants"
        self.revocations_path = self.root / "revocations.json"
        self.ledger_path = self.root / "ledger.jsonl"
        self.decisions_path = self.root / "decisions.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.grants_dir.mkdir(parents=True, exist_ok=True)

    # -- grants ----------------------------------------------------------
    def save_grant(self, grant: Grant) -> Path:
        path = self.grants_dir / f"{grant.grant_id}.json"
        _atomic_write(path, json.dumps(grant.as_dict(), indent=2, ensure_ascii=False))
        return path

    def load_grant(self, grant_id: str) -> Grant | None:
        path = self.grants_dir / f"{grant_id}.json"
        if not path.exists():
            return None
        return Grant.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_grants(self) -> list[Grant]:
        out = []
        for path in sorted(self.grants_dir.glob("*.json")):
            out.append(Grant.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return out

    # -- revocation ------------------------------------------------------
    def _revocations(self) -> dict[str, Any]:
        if not self.revocations_path.exists():
            return {}
        return json.loads(self.revocations_path.read_text(encoding="utf-8"))

    def revoke(self, grant_id: str, reason: str, at: datetime | None = None) -> dict[str, Any]:
        record = {"revoked_at": to_iso(at or datetime.now(timezone.utc)), "reason": reason}
        data = self._revocations()
        data[grant_id] = record
        _atomic_write(self.revocations_path, json.dumps(data, indent=2, ensure_ascii=False))
        return record

    def revocation(self, grant_id: str) -> dict[str, Any] | None:
        return self._revocations().get(grant_id)

    # -- ledger ----------------------------------------------------------
    def _ledger(self) -> Iterator[LedgerEntry]:
        if not self.ledger_path.exists():
            return
        with self.ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield LedgerEntry.from_dict(json.loads(line))

    def ledger_for(self, grant_id: str) -> list[LedgerEntry]:
        return [e for e in self._ledger() if e.grant_id == grant_id]

    def append_ledger(self, entry: LedgerEntry) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")

    def set_state(self, request_id: str, state: str) -> bool:
        """Rewrite one ledger row's state. Returns True if a row changed.

        The ledger is a working record, not the tamper-evident one - the audit
        chain in `explain/chain.py` is append-only and is what proves history.
        """
        rows = [e.as_dict() for e in self._ledger()]
        changed = False
        for row in rows:
            if row["request_id"] == request_id and row["state"] != state:
                row["state"] = state
                changed = True
        if changed:
            _atomic_write(
                self.ledger_path,
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            )
        return changed

    # -- derived spend figures -------------------------------------------
    def captured_paise(self, grant_id: str) -> int:
        return sum(e.amount_paise for e in self.ledger_for(grant_id) if e.state == "captured")

    def in_flight_paise(self, grant_id: str, now: datetime) -> int:
        """Authorised, not yet captured, not yet timed out."""
        cutoff = now - timedelta(seconds=AUTHORISATION_TTL_SECONDS)
        total = 0
        for e in self.ledger_for(grant_id):
            if e.state == "authorised" and from_iso(e.ts) >= cutoff:
                total += e.amount_paise
        return total

    def count_in_window(self, grant_id: str, now: datetime, window_seconds: int) -> int:
        cutoff = now - timedelta(seconds=window_seconds)
        return sum(
            1
            for e in self.ledger_for(grant_id)
            if e.state in ("authorised", "captured") and from_iso(e.ts) > cutoff
        )

    def count_today(self, grant_id: str, now: datetime) -> int:
        day = now.astimezone(timezone.utc).date()
        return sum(
            1
            for e in self.ledger_for(grant_id)
            if e.state in ("authorised", "captured") and from_iso(e.ts).date() == day
        )

    # -- idempotency -----------------------------------------------------
    def _decisions(self) -> dict[str, Any]:
        if not self.decisions_path.exists():
            return {}
        return json.loads(self.decisions_path.read_text(encoding="utf-8"))

    def recall_decision(self, request_id: str) -> dict[str, Any] | None:
        return self._decisions().get(request_id)

    def remember_decision(self, request_id: str, fingerprint: str, decision: dict[str, Any]) -> None:
        data = self._decisions()
        data[request_id] = {"fingerprint": fingerprint, "decision": decision}
        _atomic_write(self.decisions_path, json.dumps(data, indent=2, ensure_ascii=False))
