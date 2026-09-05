"""Checkout sessions - the agent-callable half of OPEN.

A session is created, previewed, and then either completed or abandoned. The
preview is the part worth noticing: an agent can ask *"would this be
allowed?"* and get the real answer, from the real checks, before committing to
anything. That is what makes the shop safely agent-callable rather than merely
machine-readable.

Completing a session is idempotent by construction: the BOUND request id is
derived from the session id, so calling complete twice returns the first
decision instead of creating a second order.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ambit.bound.grant import to_iso

SESSION_STATES = ("created", "awaiting_payment", "paid", "denied", "failed", "step_up")


def new_session_id() -> str:
    return "ses_" + secrets.token_hex(8)


def request_id_for(session_id: str) -> str:
    """One session, one money request. This is what makes complete idempotent."""
    return "req_" + session_id.removeprefix("ses_")


@dataclass
class CheckoutSession:
    session_id: str
    agent_id: str
    grant_id: str
    merchant_id: str
    cart: dict[str, Any]
    created_at: str
    state: str = "created"
    preview: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    payment_link: dict[str, Any] | None = None
    payment: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def request_id(self) -> str:
        return request_id_for(self.session_id)

    @property
    def total_paise(self) -> int:
        return int(self.cart.get("total_paise", 0))

    def note(self, event: str, detail: dict[str, Any] | None = None) -> None:
        self.events.append(
            {
                "at": to_iso(datetime.now(timezone.utc)),
                "event": event,
                "detail": detail or {},
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "grant_id": self.grant_id,
            "merchant_id": self.merchant_id,
            "request_id": self.request_id,
            "cart": self.cart,
            "created_at": self.created_at,
            "state": self.state,
            "preview": self.preview,
            "decision": self.decision,
            "order": self.order,
            "payment_link": self.payment_link,
            "payment": self.payment,
            "failure": self.failure,
            "events": self.events,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckoutSession":
        known = {k: data.get(k) for k in cls.__dataclass_fields__ if k in data}
        known.setdefault("events", [])
        return cls(**known)  # type: ignore[arg-type]


class SessionStore:
    """Flat JSON. Small enough that a file is the right database."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.path = Path(root) / "sessions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self, session: CheckoutSession) -> CheckoutSession:
        data = self._all()
        data[session.session_id] = session.as_dict()
        self._write(data)
        return session

    def get(self, session_id: str) -> CheckoutSession | None:
        raw = self._all().get(session_id)
        return CheckoutSession.from_dict(raw) if raw else None

    def list(self, limit: int = 100) -> list[CheckoutSession]:
        sessions = [CheckoutSession.from_dict(v) for v in self._all().values()]
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        return sessions[:limit]

    def find_by_reference(self, reference_id: str) -> CheckoutSession | None:
        """Webhooks arrive carrying our reference, not our session id."""
        for session in self.list(limit=1000):
            link = session.payment_link or {}
            order = session.order or {}
            if reference_id in (
                link.get("reference_id"),
                link.get("id"),
                order.get("id"),
                order.get("receipt"),
                session.request_id,
                session.session_id,
            ):
                return session
        return None
