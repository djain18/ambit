"""Ambit's HTTP surface.

Three groups of routes, one per feature:

* `/agent/*`   OPEN    - the machine-readable shop and its checkout
* `/bound/*`   BOUND   - grants, decisions, revocation
* `/explain/*` EXPLAIN - the timeline, the chain, the numbers

The order of operations inside `complete` is the part that matters: the
decision is made and written to the audit chain **before** any Razorpay call
happens. A crash between the two leaves a record of what was intended, which
is the difference between an audit trail and a log file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from ambit.bound.checks import PurchaseRequest
from ambit.bound.decide import ALLOW, DENY, STEP_UP
from ambit.bound.grant import to_iso, utc_now
from ambit.open import catalog
from ambit.open.catalog import CartError, build_cart
from ambit.explain.report import explain_session, metrics
from ambit.open.sessions import CheckoutSession, new_session_id
from ambit.razorpay_client import RazorpayError, verify_webhook_signature
from ambit.runtime import Runtime, get_runtime

agent_router = APIRouter(prefix="/agent", tags=["OPEN"])
bound_router = APIRouter(prefix="/bound", tags=["BOUND"])
explain_router = APIRouter(prefix="/explain", tags=["EXPLAIN"])
webhook_router = APIRouter(tags=["OPEN"])


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, **extra}}
    )


# ---------------------------------------------------------------- OPEN --
@agent_router.get("/catalog")
def get_catalog() -> dict[str, Any]:
    """Everything an agent needs to build a cart, and nothing it has to infer."""
    return catalog.as_feed()


@agent_router.post("/checkout_sessions")
def create_checkout_session(body: dict[str, Any]) -> Any:
    rt = get_runtime()
    agent_id = str(body.get("agent_id", "")).strip()
    grant_id = str(body.get("grant_id", "")).strip()
    items = body.get("items") or []

    if not agent_id or not grant_id:
        return _error(400, "MISSING_FIELDS", "agent_id and grant_id are both required")

    grant = rt.store.load_grant(grant_id)
    if grant is None:
        return _error(404, "UNKNOWN_GRANT", f"no grant with id {grant_id}")

    try:
        cart = build_cart(items)
    except CartError as exc:
        return _error(400, "BAD_CART", str(exc))

    session = CheckoutSession(
        session_id=new_session_id(),
        agent_id=agent_id,
        grant_id=grant_id,
        merchant_id=catalog.MERCHANT_ID,
        cart=cart.as_dict(),
        created_at=to_iso(utc_now()),
    )
    session.note("session_created", {"items": len(cart.lines), "total_paise": cart.total_paise})

    # The dry run: real checks, no side effects on the grant.
    preview = rt.engine.preview(
        grant,
        PurchaseRequest(
            request_id=session.request_id,
            agent_id=agent_id,
            merchant_id=catalog.MERCHANT_ID,
            category=cart.dominant_category,
            amount_paise=cart.total_paise,
            now=utc_now(),
        ),
    )
    session.preview = preview.as_dict()
    session.note("preview", {"outcome": preview.outcome, "binding_check": preview.binding_check})
    rt.sessions.save(session)

    return {
        "session_id": session.session_id,
        "state": session.state,
        "cart": session.cart,
        "decision_preview": {
            "outcome": preview.outcome,
            "binding_check": preview.binding_check,
            "reason": preview.reason,
            "checks": [c.as_dict() for c in preview.checks],
        },
        "next": f"POST /agent/checkout_sessions/{session.session_id}/complete",
        "note": (
            "This preview changed nothing. Completing runs the same ten checks "
            "again, for real, against the state at that moment."
        ),
    }


@agent_router.get("/checkout_sessions/{session_id}")
def get_checkout_session(session_id: str) -> Any:
    session = get_runtime().sessions.get(session_id)
    if session is None:
        return _error(404, "UNKNOWN_SESSION", f"no session with id {session_id}")
    return session.as_dict()


@agent_router.post("/checkout_sessions/{session_id}/complete")
def complete_checkout_session(session_id: str) -> Any:
    rt = get_runtime()
    session = rt.sessions.get(session_id)
    if session is None:
        return _error(404, "UNKNOWN_SESSION", f"no session with id {session_id}")

    # A session that already has an order is finished with this endpoint.
    # BOUND's IDEMPOTENCY check makes the *decision* replay-safe, but the
    # decision is not the money write - without this guard a second call
    # returns the cached ALLOW and then cheerfully creates a second order.
    if session.order and session.state in ("awaiting_payment", "paid"):
        rt.chain.append(
            "COMPLETE_REPLAYED",
            {"session_id": session.session_id, "order_id": session.order.get("id")},
        )
        return {
            "session_id": session.session_id,
            "state": session.state,
            "outcome": ALLOW,
            "order": session.order,
            "payment_link": session.payment_link,
            "pay_here": (session.payment_link or {}).get("short_url"),
            "replayed": True,
            "note": "This session was already completed. Returning the original order.",
        }

    grant = rt.store.load_grant(session.grant_id)
    if grant is None:
        return _error(404, "UNKNOWN_GRANT", f"no grant with id {session.grant_id}")

    req = PurchaseRequest(
        request_id=session.request_id,
        agent_id=session.agent_id,
        merchant_id=session.merchant_id,
        category=session.cart.get("checked_as_category", "unknown"),
        amount_paise=session.total_paise,
        now=utc_now(),
    )

    # 1. Decide, and record the decision, before touching anything external.
    decision = rt.engine.decide(grant, req)
    session.decision = decision.as_dict()
    session.note(
        "decision",
        {"outcome": decision.outcome, "binding_check": decision.binding_check},
    )

    if decision.outcome == DENY:
        session.state = "denied"
        rt.sessions.save(session)
        # A POLICY denial is a correct outcome, not an error. 200, not 4xx -
        # and self-anneal forbids ever retrying it.
        return {
            "session_id": session.session_id,
            "state": session.state,
            "outcome": DENY,
            "binding_check": decision.binding_check,
            "reason": decision.reason,
            "checks": [c.as_dict() for c in decision.checks],
            "retryable": False,
            "note": "This is a policy decision. Retrying it will not change it.",
        }

    if decision.outcome == STEP_UP:
        session.state = "step_up"
        rt.sessions.save(session)
        return {
            "session_id": session.session_id,
            "state": session.state,
            "outcome": STEP_UP,
            "reason": decision.reason,
            "checks": [c.as_dict() for c in decision.checks],
            "retryable": False,
            "note": (
                "A human has to approve this amount. It will not resolve on its own. "
                f"Approve with: POST /bound/step_up/{session.session_id}/approve"
            ),
        }

    # 2. ALLOW. Now, and only now, real money plumbing.
    client = rt.razorpay
    if client is None:
        session.state = "failed"
        session.failure = {
            "classification": "PERMANENT",
            "message": "no Razorpay credentials configured; the shop is readable but not buyable",
        }
        rt.sessions.save(session)
        rt.chain.append("ORDER_FAILED", {"session_id": session.session_id, **session.failure})
        return _error(503, "NO_PAYMENT_BACKEND", session.failure["message"])

    try:
        order = client.create_order(
            amount_paise=session.total_paise,
            receipt=session.request_id,
            notes={
                "ambit_session": session.session_id,
                "ambit_grant": session.grant_id,
                "ambit_agent": session.agent_id,
            },
            idempotency_key=session.request_id,
        )
        link = client.create_payment_link(
            amount_paise=session.total_paise,
            description=f"Ambit agent order {session.session_id}",
            reference_id=session.request_id,
            notes={"ambit_session": session.session_id, "ambit_order": order.get("id", "")},
            callback_url=(
                f"{rt.settings.public_url}/agent/checkout_sessions/{session.session_id}"
                if rt.settings.public_url
                else None
            ),
            idempotency_key=session.request_id + "-link",
        )
    except RazorpayError as exc:
        session.state = "failed"
        session.failure = exc.as_dict()
        session.note("razorpay_failed", exc.as_dict())
        rt.sessions.save(session)
        rt.engine.record_failure(session.request_id, exc.code or "UNKNOWN", str(exc))
        return _error(
            502,
            "PAYMENT_BACKEND_FAILED",
            str(exc),
            classification=exc.classification,
            attempts=exc.attempts,
            retryable=exc.classification == "TRANSIENT",
        )

    session.order = {"id": order.get("id"), "amount": order.get("amount"), "status": order.get("status")}
    session.payment_link = {
        "id": link.get("id"),
        "short_url": link.get("short_url"),
        "reference_id": link.get("reference_id"),
        "status": link.get("status"),
    }
    session.state = "awaiting_payment"
    session.note("order_created", session.order)
    session.note("payment_link_created", session.payment_link)
    rt.sessions.save(session)

    rt.chain.append(
        "ORDER_CREATED",
        {
            "session_id": session.session_id,
            "request_id": session.request_id,
            "order": session.order,
            "payment_link": session.payment_link,
        },
    )

    return {
        "session_id": session.session_id,
        "state": session.state,
        "outcome": ALLOW,
        "order": session.order,
        "payment_link": session.payment_link,
        "pay_here": session.payment_link.get("short_url"),
        "note": "Budget is debited when the payment is captured, not now.",
    }


@webhook_router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> Any:
    """Fulfilment. The only place budget is actually debited.

    The signature is checked against the **raw body bytes**. Re-serialising the
    JSON first would change the bytes and the signature would never match.
    """
    rt = get_runtime()
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    secret = rt.settings.razorpay_webhook_secret

    if secret:
        if not verify_webhook_signature(raw, signature, secret):
            rt.chain.append(
                "WEBHOOK_REJECTED",
                {"reason": "signature mismatch", "has_signature": bool(signature)},
            )
            return _error(400, "BAD_SIGNATURE", "webhook signature did not verify")
    else:
        # Refuse to silently trust an unsigned webhook. Say so in the record.
        rt.chain.append("WEBHOOK_UNVERIFIED", {"reason": "RAZORPAY_WEBHOOK_SECRET is not set"})

    try:
        body = await request.json()
    except Exception:
        return _error(400, "BAD_BODY", "webhook body was not JSON")

    event = body.get("event", "")
    payload = body.get("payload") or {}
    entity = (payload.get("payment") or {}).get("entity") or {}
    reference = (
        entity.get("notes", {}).get("ambit_session")
        or entity.get("order_id")
        or (payload.get("payment_link") or {}).get("entity", {}).get("reference_id")
        or ""
    )

    session = rt.sessions.get(reference) or rt.sessions.find_by_reference(reference)
    rt.chain.append(
        "WEBHOOK_RECEIVED",
        {
            "event": event,
            "reference": reference,
            "matched_session": session.session_id if session else None,
        },
    )

    if session is None:
        # Not ours, or arrived before the session was saved. Acknowledge so
        # Razorpay stops retrying, but leave the trace.
        return {"status": "ignored", "reason": "no matching session", "event": event}

    if event == "payment.captured":
        rt.engine.record_capture(
            session.request_id,
            entity.get("id", "unknown"),
            int(entity.get("amount") or session.total_paise),
        )
        session.state = "paid"
        session.payment = {
            "id": entity.get("id"),
            "amount": entity.get("amount"),
            "method": entity.get("method"),
            "status": entity.get("status"),
        }
        session.note("payment_captured", session.payment)
        rt.sessions.save(session)
        return {"status": "fulfilled", "session_id": session.session_id}

    if event == "payment.failed":
        code = entity.get("error_code") or "UNKNOWN"
        description = entity.get("error_description") or "payment failed"
        rt.engine.record_failure(session.request_id, code, description)
        session.state = "failed"
        session.failure = {
            "code": code,
            "description": description,
            "reason": entity.get("error_reason"),
            "source": entity.get("error_source"),
            "step": entity.get("error_step"),
        }
        session.note("payment_failed", session.failure)
        rt.sessions.save(session)
        return {"status": "recorded_failure", "session_id": session.session_id}

    return {"status": "ignored", "event": event, "session_id": session.session_id}


# --------------------------------------------------------------- BOUND --
@bound_router.get("/grants")
def list_grants() -> dict[str, Any]:
    rt = get_runtime()
    now = utc_now()
    out = []
    for grant in rt.store.list_grants():
        captured = rt.store.captured_paise(grant.grant_id)
        revocation = rt.store.revocation(grant.grant_id)
        out.append(
            {
                "grant_id": grant.grant_id,
                "agent_id": grant.agent_id,
                "principal": grant.principal,
                "signature_verifies": grant.verify(rt.public_key) if rt.public_key else None,
                "revoked": bool(revocation) or grant.revoked,
                "revocation": revocation,
                "expires_at": grant.expires_at,
                "limits": grant.limits.as_dict(),
                "allow": {
                    "merchants": list(grant.allow_merchants),
                    "categories": list(grant.allow_categories),
                },
                "deny": {
                    "merchants": list(grant.deny_merchants),
                    "categories": list(grant.deny_categories),
                },
                "step_up_above_paise": grant.step_up_above_paise,
                "captured_paise": captured,
                "in_flight_paise": rt.store.in_flight_paise(grant.grant_id, now),
                "remaining_paise": max(0, grant.limits.total_paise - captured),
                "transactions": [e.as_dict() for e in rt.store.ledger_for(grant.grant_id)],
            }
        )
    return {"grants": out}


@bound_router.post("/grants/{grant_id}/revoke")
def revoke_grant(grant_id: str, body: dict[str, Any] | None = None) -> Any:
    rt = get_runtime()
    if rt.store.load_grant(grant_id) is None:
        return _error(404, "UNKNOWN_GRANT", f"no grant with id {grant_id}")
    reason = (body or {}).get("reason") or "revoked from the console"
    record = rt.engine.revoke(grant_id, reason)
    return {
        "grant_id": grant_id,
        "revoked": True,
        **record,
        "effect": "every future decision on this grant now denies on GRANT_REVOKED",
    }


@bound_router.post("/step_up/{session_id}/approve")
def approve_step_up(session_id: str, body: dict[str, Any] | None = None) -> Any:
    rt = get_runtime()
    session = rt.sessions.get(session_id)
    if session is None:
        return _error(404, "UNKNOWN_SESSION", f"no session with id {session_id}")
    if session.state != "step_up":
        return _error(409, "NOT_AWAITING_APPROVAL", f"session is {session.state}, not step_up")

    grant = rt.store.load_grant(session.grant_id)
    if grant is None:
        return _error(404, "UNKNOWN_GRANT", f"no grant with id {session.grant_id}")

    approver = (body or {}).get("approver") or "user_daksh"
    req = PurchaseRequest(
        request_id=session.request_id,
        agent_id=session.agent_id,
        merchant_id=session.merchant_id,
        category=session.cart.get("checked_as_category", "unknown"),
        amount_paise=session.total_paise,
        now=utc_now(),
    )
    decision = rt.engine.approve_step_up(grant, req, approver=approver)
    session.decision = decision.as_dict()
    session.note("step_up_resolved", {"approver": approver, "outcome": decision.outcome})
    session.state = "created" if decision.outcome == ALLOW else "denied"
    rt.sessions.save(session)

    return {
        "session_id": session_id,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "next": (
            f"POST /agent/checkout_sessions/{session_id}/complete"
            if decision.outcome == ALLOW
            else None
        ),
    }


# ------------------------------------------------------------- EXPLAIN --
@explain_router.get("/chain")
def get_chain(limit: int = 100) -> dict[str, Any]:
    rt = get_runtime()
    entries = rt.chain.entries()
    result = rt.chain.verify()
    return {
        "verification": result.as_dict(),
        "total_entries": len(entries),
        "entries": entries[-limit:],
    }


@explain_router.get("/verify")
def verify_chain() -> dict[str, Any]:
    rt = get_runtime()
    return {"path": str(rt.chain.path), **rt.chain.verify().as_dict()}


@explain_router.get("/sessions")
def list_sessions(limit: int = 50) -> dict[str, Any]:
    rt = get_runtime()
    return {"sessions": [s.as_dict() for s in rt.sessions.list(limit)]}


@explain_router.get("/sessions/{session_id}")
def explain_one_session(session_id: str) -> Any:
    """What the agent did, why it was stopped, and what happens next."""
    rt = get_runtime()
    session = rt.sessions.get(session_id)
    if session is None:
        return _error(404, "UNKNOWN_SESSION", f"no session with id {session_id}")
    history = [s.as_dict() for s in rt.sessions.list(500) if s.session_id != session_id]
    return explain_session(session.as_dict(), history)


@explain_router.get("/metrics")
def explain_metrics() -> dict[str, Any]:
    rt = get_runtime()
    sessions = [s.as_dict() for s in rt.sessions.list(1000)]
    body = metrics(sessions)
    body["chain"] = rt.chain.verify().as_dict()
    return body


# ----------------------------------------------------------------- app --
def create_app() -> FastAPI:
    app = FastAPI(
        title="Ambit",
        version="0.1.0",
        description=(
            "The operating limits for AI buyers. OPEN makes the shop readable and "
            "buyable by agents, BOUND decides what may be spent, EXPLAIN proves what "
            "happened. There is no model call in the money-decision path."
        ),
    )

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, Any]:
        rt = get_runtime()
        return {
            "ok": True,
            "at": to_iso(datetime.now(timezone.utc)),
            "test_mode_enforced": rt.settings.require_test_mode,
            "razorpay_configured": rt.settings.has_razorpay_credentials,
            "webhook_secret_configured": bool(rt.settings.razorpay_webhook_secret),
            "issuer_key_loaded": rt.signing_key_present,
            "chain": rt.chain.verify().as_dict(),
        }

    app.include_router(agent_router)
    app.include_router(bound_router)
    app.include_router(explain_router)
    app.include_router(webhook_router)
    return app


app = create_app()
