"""Ambit as an MCP server - the merchant, exposed natively to any MCP client.

Razorpay ships [its own MCP server](https://github.com/razorpay/razorpay-mcp-server)
with 50+ tools. Theirs exposes payment capability to a merchant's own agent.
This one exposes **bounded** payment capability to somebody else's agent - an
untrusted third-party buyer that the merchant has never met.

That difference is the whole design. Every tool here is either read-only or
runs through the same ten checks the HTTP API does. There is no tool that
moves money without a decision, and no tool that can change a limit. An agent
holding this server cannot talk its way past anything, because the tools it
has do not include one that would let it.

Run it:

    python -m ambit.mcp_server                    # stdio
    claude mcp add ambit -- python -m ambit.mcp_server
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ambit.bound.checks import PurchaseRequest
from ambit.bound.decide import ALLOW, DENY, STEP_UP
from ambit.bound.grant import utc_now
from ambit.canonical import format_paise
from ambit.open import catalog
from ambit.open.catalog import CartError, build_cart
from ambit.open.sessions import CheckoutSession, new_session_id
from ambit.razorpay_client import RazorpayError
from ambit.runtime import get_runtime

mcp = FastMCP(
    "ambit",
    instructions=(
        "Ambit makes a merchant safely transactable by AI buyers. Browse the catalog, "
        "open a checkout session to see whether a purchase would be allowed, then "
        "complete it. Every purchase is checked against a signed spending grant that "
        "you do not control and cannot modify. A denial is final: it is a policy "
        "decision, not an error, and retrying it will not change the answer."
    ),
)


@mcp.tool(
    title="Browse the catalog",
    description="List everything this merchant sells, with prices in paise as integers.",
)
def list_catalog(category: str | None = None) -> dict[str, Any]:
    feed = catalog.as_feed()
    if category:
        feed["products"] = [p for p in feed["products"] if p["category"] == category]
        feed["filtered_by_category"] = category
    return feed


@mcp.tool(
    title="Check what a grant allows",
    description=(
        "Look up the spending limits you are operating under: caps, allowed merchants "
        "and categories, what has been spent, and when it expires. Read-only - you "
        "cannot change any of this."
    ),
)
def describe_grant(grant_id: str) -> dict[str, Any]:
    rt = get_runtime()
    grant = rt.store.load_grant(grant_id)
    if grant is None:
        return {"error": f"no grant with id {grant_id}"}
    captured = rt.store.captured_paise(grant_id)
    revocation = rt.store.revocation(grant_id)
    return {
        "grant_id": grant.grant_id,
        "agent_id": grant.agent_id,
        "revoked": bool(revocation) or grant.revoked,
        "expires_at": grant.expires_at,
        "per_transaction_limit": format_paise(grant.limits.per_transaction_paise),
        "per_transaction_paise": grant.limits.per_transaction_paise,
        "total_budget_paise": grant.limits.total_paise,
        "spent_paise": captured,
        "remaining_paise": max(0, grant.limits.total_paise - captured),
        "step_up_above_paise": grant.step_up_above_paise,
        "allowed_merchants": list(grant.allow_merchants),
        "allowed_categories": list(grant.allow_categories),
        "denied_categories": list(grant.deny_categories),
        "max_transactions_per_day": grant.limits.max_transactions_per_day,
        "note": (
            "These limits are enforced by deterministic code, not by a model. "
            "Nothing you say can change them."
        ),
    }


@mcp.tool(
    title="Open a checkout session",
    description=(
        "Price a cart and get a dry run of the spending decision - would this be "
        "allowed, and if not, which check stops it. Changes nothing: previewing does "
        "not spend budget or count against your transaction limits. "
        "items is a list of {sku, quantity}."
    ),
)
def create_checkout_session(
    agent_id: str, grant_id: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    rt = get_runtime()
    grant = rt.store.load_grant(grant_id)
    if grant is None:
        return {"error": f"no grant with id {grant_id}"}

    try:
        cart = build_cart(items)
    except CartError as exc:
        return {"error": str(exc), "hint": "check the sku and quantity against the catalog"}

    session = CheckoutSession(
        session_id=new_session_id(),
        agent_id=agent_id,
        grant_id=grant_id,
        merchant_id=catalog.MERCHANT_ID,
        cart=cart.as_dict(),
        created_at=utc_now().isoformat().replace("+00:00", "Z"),
    )
    session.note("session_created", {"via": "mcp", "total_paise": cart.total_paise})

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
    rt.sessions.save(session)

    return {
        "session_id": session.session_id,
        "total": format_paise(cart.total_paise),
        "total_paise": cart.total_paise,
        "lines": cart.as_dict()["lines"],
        "would_be": preview.outcome,
        "binding_check": preview.binding_check,
        "reason": preview.reason,
        "checks": [
            {"check": c.code, "passed": c.passed, "detail": c.message} for c in preview.checks
        ],
        "next": "call complete_checkout_session with this session_id to do it for real",
    }


@mcp.tool(
    title="Complete a checkout session",
    description=(
        "Run the ten checks for real and, if allowed, create the order and a payable "
        "payment link. A DENY here is final - it is a policy decision, not a transient "
        "error, and calling this again will return the same answer."
    ),
)
def complete_checkout_session(session_id: str) -> dict[str, Any]:
    rt = get_runtime()
    session = rt.sessions.get(session_id)
    if session is None:
        return {"error": f"no session with id {session_id}"}

    if session.order and session.state in ("awaiting_payment", "paid"):
        return {
            "outcome": ALLOW,
            "replayed": True,
            "order_id": session.order.get("id"),
            "pay_here": (session.payment_link or {}).get("short_url"),
            "note": "already completed; this is the original order",
        }

    grant = rt.store.load_grant(session.grant_id)
    if grant is None:
        return {"error": f"no grant with id {session.grant_id}"}

    req = PurchaseRequest(
        request_id=session.request_id,
        agent_id=session.agent_id,
        merchant_id=session.merchant_id,
        category=session.cart.get("checked_as_category", "unknown"),
        amount_paise=session.total_paise,
        now=utc_now(),
    )
    decision = rt.engine.decide(grant, req)
    session.decision = decision.as_dict()

    if decision.outcome == DENY:
        session.state = "denied"
        rt.sessions.save(session)
        return {
            "outcome": DENY,
            "binding_check": decision.binding_check,
            "reason": decision.reason,
            "retryable": False,
            "checks": [
                {"check": c.code, "passed": c.passed, "detail": c.message}
                for c in decision.checks
            ],
            "note": (
                "This is a policy decision made by deterministic code. Do not retry it "
                "and do not try to work around it. If you believe it is wrong, say so "
                "in your summary and stop."
            ),
        }

    if decision.outcome == STEP_UP:
        session.state = "step_up"
        rt.sessions.save(session)
        return {
            "outcome": STEP_UP,
            "reason": decision.reason,
            "retryable": False,
            "note": "A human has to approve this. It will not resolve on its own - stop here.",
        }

    client = rt.razorpay
    if client is None:
        session.state = "failed"
        rt.sessions.save(session)
        return {
            "outcome": "UNAVAILABLE",
            "reason": "no Razorpay credentials configured; the shop is readable but not buyable",
            "retryable": False,
        }

    try:
        order = client.create_order(
            amount_paise=session.total_paise,
            receipt=session.request_id,
            notes={"ambit_session": session.session_id, "ambit_grant": session.grant_id},
            idempotency_key=session.request_id,
        )
        link = client.create_payment_link(
            amount_paise=session.total_paise,
            description=f"Ambit agent order {session.session_id}",
            reference_id=session.request_id,
            notes={"ambit_session": session.session_id},
            idempotency_key=session.request_id + "-link",
        )
    except RazorpayError as exc:
        session.state = "failed"
        session.failure = exc.as_dict()
        rt.sessions.save(session)
        rt.engine.record_failure(session.request_id, exc.code or "UNKNOWN", str(exc))
        return {
            "outcome": "FAILED",
            "reason": str(exc),
            "classification": exc.classification,
            "retryable": exc.classification == "TRANSIENT",
            "attempts_already_made": exc.attempts,
        }

    session.order = {"id": order.get("id"), "amount": order.get("amount"), "status": order.get("status")}
    session.payment_link = {
        "id": link.get("id"),
        "short_url": link.get("short_url"),
        "reference_id": link.get("reference_id"),
        "status": link.get("status"),
    }
    session.state = "awaiting_payment"
    rt.sessions.save(session)
    rt.chain.append(
        "ORDER_CREATED",
        {
            "session_id": session.session_id,
            "request_id": session.request_id,
            "order": session.order,
            "payment_link": session.payment_link,
            "via": "mcp",
        },
    )

    return {
        "outcome": ALLOW,
        "order_id": session.order["id"],
        "amount": format_paise(session.total_paise),
        "pay_here": session.payment_link["short_url"],
        "note": "Budget is debited when the payment is captured, not now.",
    }


@mcp.tool(
    title="Look at a session",
    description="Everything that happened in one checkout session, in order.",
)
def get_session(session_id: str) -> dict[str, Any]:
    session = get_runtime().sessions.get(session_id)
    if session is None:
        return {"error": f"no session with id {session_id}"}
    return session.as_dict()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
