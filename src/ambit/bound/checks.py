"""The ten checks.

Run in this fixed order, **all of them, every time**. Never short-circuit on
the first failure: a trace that stops at the first problem is a useless audit
record, and the track's bar is that every money action is *explainable*.

Each check returns a stable reason code, a pass/fail, and the values it
actually compared - so the record shows not just that something was denied but
what the numbers were when it was.

No model call happens anywhere in this file, or anywhere it calls. That is the
central architectural claim of the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ambit.bound.grant import Grant, from_iso, to_iso
from ambit.bound.store import BoundStore, request_fingerprint

# The order is part of the contract. Tests assert on it.
CHECK_ORDER: tuple[str, ...] = (
    "GRANT_SIGNATURE",
    "GRANT_WINDOW",
    "GRANT_REVOKED",
    "AGENT_MATCH",
    "MERCHANT_ALLOWED",
    "CATEGORY_ALLOWED",
    "PER_TXN_LIMIT",
    "BUDGET_REMAINING",
    "VELOCITY",
    "IDEMPOTENCY",
)


@dataclass(frozen=True)
class PurchaseRequest:
    request_id: str
    agent_id: str
    merchant_id: str
    category: str
    amount_paise: int
    now: datetime

    def fingerprint(self, grant_id: str) -> str:
        return request_fingerprint(
            grant_id, self.agent_id, self.merchant_id, self.category, self.amount_paise
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "merchant_id": self.merchant_id,
            "category": self.category,
            "amount_paise": self.amount_paise,
            "now": to_iso(self.now),
        }


@dataclass(frozen=True)
class CheckResult:
    code: str
    passed: bool
    message: str
    observed: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "passed": self.passed,
            "message": self.message,
            "observed": self.observed,
        }


def _signature(grant: Grant, public_key: Ed25519PublicKey | None) -> CheckResult:
    if public_key is None:
        return CheckResult(
            "GRANT_SIGNATURE",
            False,
            "no public key available for this grant's issuer",
            {"issuer_key_id": grant.issuer_key_id},
        )
    ok = grant.verify(public_key)
    return CheckResult(
        "GRANT_SIGNATURE",
        ok,
        "signature verifies against the issuer key"
        if ok
        else "signature does not verify - the grant was altered or signed by another key",
        {"issuer_key_id": grant.issuer_key_id, "has_signature": bool(grant.signature)},
    )


def _window(grant: Grant, req: PurchaseRequest) -> CheckResult:
    not_before = from_iso(grant.not_before)
    expires_at = from_iso(grant.expires_at)
    ok = not_before <= req.now <= expires_at
    if req.now < not_before:
        message = "grant is not valid yet"
    elif req.now > expires_at:
        message = "grant has expired"
    else:
        message = "inside the validity window"
    return CheckResult(
        "GRANT_WINDOW",
        ok,
        message,
        {"now": to_iso(req.now), "not_before": grant.not_before, "expires_at": grant.expires_at},
    )


def _revoked(grant: Grant, store: BoundStore) -> CheckResult:
    revocation = store.revocation(grant.grant_id)
    revoked = bool(grant.revoked) or revocation is not None
    return CheckResult(
        "GRANT_REVOKED",
        not revoked,
        "grant has been revoked" if revoked else "grant is live",
        {
            "revoked_at_issue": bool(grant.revoked),
            "revocation": revocation,
        },
    )


def _agent(grant: Grant, req: PurchaseRequest) -> CheckResult:
    ok = req.agent_id == grant.agent_id
    return CheckResult(
        "AGENT_MATCH",
        ok,
        "requesting agent holds this grant"
        if ok
        else "this grant was not issued to the requesting agent",
        {"requesting_agent": req.agent_id, "grant_agent": grant.agent_id},
    )


def _merchant(grant: Grant, req: PurchaseRequest) -> CheckResult:
    denied = req.merchant_id in grant.deny_merchants
    allowed = req.merchant_id in grant.allow_merchants
    ok = allowed and not denied
    if denied:
        message = "merchant is on the deny list"
    elif not allowed:
        # An empty allow list means nothing is allowed. Fail closed.
        message = "merchant is not on the allow list"
    else:
        message = "merchant is allowed"
    return CheckResult(
        "MERCHANT_ALLOWED",
        ok,
        message,
        {
            "merchant": req.merchant_id,
            "allow": list(grant.allow_merchants),
            "deny": list(grant.deny_merchants),
        },
    )


def _category(grant: Grant, req: PurchaseRequest) -> CheckResult:
    denied = req.category in grant.deny_categories
    allowed = req.category in grant.allow_categories
    ok = allowed and not denied
    if denied:
        message = "category is on the deny list"
    elif not allowed:
        message = "category is not on the allow list"
    else:
        message = "category is allowed"
    return CheckResult(
        "CATEGORY_ALLOWED",
        ok,
        message,
        {
            "category": req.category,
            "allow": list(grant.allow_categories),
            "deny": list(grant.deny_categories),
        },
    )


def _per_txn(grant: Grant, req: PurchaseRequest) -> CheckResult:
    cap = grant.limits.per_transaction_paise
    positive = req.amount_paise > 0
    under = req.amount_paise <= cap
    ok = positive and under
    if not positive:
        message = "amount must be a positive number of paise"
    elif not under:
        message = "amount is over the per-transaction cap"
    else:
        message = "amount is within the per-transaction cap"
    return CheckResult(
        "PER_TXN_LIMIT",
        ok,
        message,
        {
            "amount_paise": req.amount_paise,
            "per_transaction_paise": cap,
            "over_by_paise": max(0, req.amount_paise - cap),
        },
    )


def _budget(grant: Grant, req: PurchaseRequest, store: BoundStore) -> CheckResult:
    captured = store.captured_paise(grant.grant_id)
    in_flight = store.in_flight_paise(grant.grant_id, req.now)
    # limits.spent_paise is the value at issue time - an opening balance, not
    # live state. Live spend is captured + still-valid authorisations.
    spent = grant.limits.spent_paise + captured
    committed = spent + in_flight
    total = grant.limits.total_paise
    ok = committed + req.amount_paise <= total
    return CheckResult(
        "BUDGET_REMAINING",
        ok,
        "enough budget remains"
        if ok
        else "this would take the grant over its total budget",
        {
            "amount_paise": req.amount_paise,
            "opening_spent_paise": grant.limits.spent_paise,
            "captured_paise": captured,
            "in_flight_paise": in_flight,
            "committed_paise": committed,
            "total_paise": total,
            "remaining_paise": max(0, total - committed),
        },
    )


def _velocity(grant: Grant, req: PurchaseRequest, store: BoundStore) -> CheckResult:
    window_seconds = grant.limits.velocity_window_seconds
    in_window = store.count_in_window(grant.grant_id, req.now, window_seconds)
    today = store.count_today(grant.grant_id, req.now)
    window_ok = in_window < grant.limits.max_transactions_per_window
    day_ok = today < grant.limits.max_transactions_per_day
    ok = window_ok and day_ok
    if not window_ok:
        message = "too many transactions inside the rolling window"
    elif not day_ok:
        message = "daily transaction count already reached"
    else:
        message = "within velocity limits"
    return CheckResult(
        "VELOCITY",
        ok,
        message,
        {
            "in_window": in_window,
            "max_per_window": grant.limits.max_transactions_per_window,
            "window_seconds": window_seconds,
            "today": today,
            "max_per_day": grant.limits.max_transactions_per_day,
        },
    )


def _idempotency(grant: Grant, req: PurchaseRequest, store: BoundStore) -> CheckResult:
    prior = store.recall_decision(req.request_id)
    fingerprint = req.fingerprint(grant.grant_id)
    if prior is None:
        return CheckResult(
            "IDEMPOTENCY",
            True,
            "request id has not been seen before",
            {"request_id": req.request_id, "seen_before": False},
        )
    same = prior.get("fingerprint") == fingerprint
    # Same id, same ask, is an honest retry - decide() returns the original
    # decision verbatim before ever reaching here. Same id, *different* ask is
    # a replay with altered parameters, and that is an attack.
    return CheckResult(
        "IDEMPOTENCY",
        same,
        "replay of an earlier request id with the same parameters"
        if same
        else "request id was already used for a different purchase",
        {
            "request_id": req.request_id,
            "seen_before": True,
            "same_parameters": same,
            "prior_outcome": (prior.get("decision") or {}).get("outcome"),
        },
    )


def run_checks(
    grant: Grant,
    req: PurchaseRequest,
    store: BoundStore,
    public_key: Ed25519PublicKey | None,
) -> list[CheckResult]:
    """All ten, in order, every time. No early return anywhere in this list."""
    results = [
        _signature(grant, public_key),
        _window(grant, req),
        _revoked(grant, store),
        _agent(grant, req),
        _merchant(grant, req),
        _category(grant, req),
        _per_txn(grant, req),
        _budget(grant, req, store),
        _velocity(grant, req, store),
        _idempotency(grant, req, store),
    ]
    observed_order = tuple(r.code for r in results)
    if observed_order != CHECK_ORDER:  # pragma: no cover - guards a refactor slip
        raise AssertionError(f"check order drifted: {observed_order}")
    return results
