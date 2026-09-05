"""Why it failed - and whose fault it was.

"The card declined" is what a payments log tells you. It is not what a merchant
needs to know on day two of letting agents into their shop. The useful question
is what the *agent* did: did it hit a limit it was always going to hit, did it
misread a listing, did it loop, or did it get talked into something by text on
a product page.

Every rule in this file is deterministic. Classification reads reason codes,
Razorpay error codes and the catalog - never a model. A model may only be used
for the `UNCLASSIFIED` bucket, and only to *label* what already happened; by
that point every money decision has been made and recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ambit.open import catalog

# -- the classes ---------------------------------------------------------
LIMIT_HIT = "LIMIT_HIT"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
GRANT_EXPIRED = "GRANT_EXPIRED"
GRANT_REVOKED_MID_FLIGHT = "GRANT_REVOKED_MID_FLIGHT"
GRANT_TAMPERED = "GRANT_TAMPERED"
WRONG_AGENT = "WRONG_AGENT"
REPLAY_ATTEMPT = "REPLAY_ATTEMPT"
NEEDS_HUMAN = "NEEDS_HUMAN"
PROMPT_INJECTION = "PROMPT_INJECTION"
AGENT_MISREAD_LISTING = "AGENT_MISREAD_LISTING"
AGENT_LOOPED = "AGENT_LOOPED"
PAYMENT_DECLINED = "PAYMENT_DECLINED"
BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"
UNCLASSIFIED = "UNCLASSIFIED"
NOTHING_FAILED = "NONE"

# Who or what actually caused it. This is the column a merchant reads first.
BLAME = {
    LIMIT_HIT: "the agent asked for more than it was granted",
    OUT_OF_SCOPE: "the agent shopped outside what the grant covers",
    GRANT_EXPIRED: "the grant ran out while the purchase was in progress",
    GRANT_REVOKED_MID_FLIGHT: "a human revoked the grant",
    GRANT_TAMPERED: "the grant presented does not match its signature",
    WRONG_AGENT: "the grant was issued to a different agent",
    REPLAY_ATTEMPT: "a request id was reused with different parameters",
    NEEDS_HUMAN: "within limits, but above the amount a human has to sign off",
    PROMPT_INJECTION: "the agent was manipulated by text on a product page",
    AGENT_MISREAD_LISTING: "the agent misread the listing",
    AGENT_LOOPED: "the agent repeated itself without making progress",
    PAYMENT_DECLINED: "the payment itself failed at the bank or gateway",
    BACKEND_UNAVAILABLE: "the payment backend could not be reached",
    INTERNAL_ERROR: "Ambit failed and denied rather than risk approving",
    UNCLASSIFIED: "not classifiable from the record alone",
}

# Whether an automated retry could ever help. Anything POLICY is never retried:
# a denied purchase is the system working, and retrying it would be a security
# bug wearing a resilience costume.
RECOVERABILITY = {
    LIMIT_HIT: "POLICY",
    OUT_OF_SCOPE: "POLICY",
    GRANT_EXPIRED: "POLICY",
    GRANT_REVOKED_MID_FLIGHT: "POLICY",
    GRANT_TAMPERED: "POLICY",
    WRONG_AGENT: "POLICY",
    REPLAY_ATTEMPT: "POLICY",
    NEEDS_HUMAN: "POLICY",
    PROMPT_INJECTION: "POLICY",
    AGENT_MISREAD_LISTING: "PERMANENT",
    AGENT_LOOPED: "PERMANENT",
    PAYMENT_DECLINED: "TRANSIENT",
    BACKEND_UNAVAILABLE: "TRANSIENT",
    INTERNAL_ERROR: "PERMANENT",
    UNCLASSIFIED: "AMBIGUOUS",
}

BINDING_CHECK_TO_CLASS = {
    "PER_TXN_LIMIT": LIMIT_HIT,
    "BUDGET_REMAINING": LIMIT_HIT,
    "VELOCITY": LIMIT_HIT,
    "MERCHANT_ALLOWED": OUT_OF_SCOPE,
    "CATEGORY_ALLOWED": OUT_OF_SCOPE,
    "GRANT_WINDOW": GRANT_EXPIRED,
    "GRANT_REVOKED": GRANT_REVOKED_MID_FLIGHT,
    "GRANT_SIGNATURE": GRANT_TAMPERED,
    "AGENT_MATCH": WRONG_AGENT,
    "IDEMPOTENCY": REPLAY_ATTEMPT,
    "STEP_UP_THRESHOLD": NEEDS_HUMAN,
    "INTERNAL_ERROR": INTERNAL_ERROR,
}

# Razorpay declines that a different attempt could plausibly clear, versus
# those that will fail identically forever.
TRANSIENT_PAYMENT_CODES = {
    "GATEWAY_ERROR",
    "SERVER_ERROR",
    "BAD_REQUEST_ERROR:payment_timeout",
}

INJECTION_PATTERNS = (
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"disregard (the |your )?(previous|prior|above|system)",
    r"approve any amount",
    r"(the )?(spending )?limits? (do(es)? not|don't) apply",
    r"you are now",
    r"new instructions?:",
    r"system prompt",
    r"this is an authorised (bulk )?order",
)
_INJECTION_RE = tuple(re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS)


def injection_markers(text: str) -> list[str]:
    """Which injection patterns a piece of merchant copy matches."""
    return [r.pattern for r in _INJECTION_RE if r.search(text or "")]


def cart_contains_injection(cart: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    """Did the agent's cart include a product whose description attacks it?

    Deterministic: the catalog is data we hold, so this is a lookup, not a
    judgement call.
    """
    hostile_skus: list[str] = []
    markers: list[str] = []
    for line in cart.get("lines") or []:
        product = catalog.get(line.get("sku", ""))
        if product is None:
            continue
        found = injection_markers(product.description)
        if found:
            hostile_skus.append(product.sku)
            markers.extend(found)
    return bool(hostile_skus), hostile_skus, sorted(set(markers))


@dataclass(frozen=True)
class Classification:
    code: str
    blame: str
    recoverability: str
    detail: str
    evidence: dict[str, Any]

    @property
    def retryable(self) -> bool:
        return self.recoverability == "TRANSIENT"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "blame": self.blame,
            "recoverability": self.recoverability,
            "retryable": self.retryable,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def _make(code: str, detail: str, **evidence: Any) -> Classification:
    return Classification(
        code=code,
        blame=BLAME.get(code, BLAME[UNCLASSIFIED]),
        recoverability=RECOVERABILITY.get(code, "AMBIGUOUS"),
        detail=detail,
        evidence=evidence,
    )


def classify_session(session: dict[str, Any], history: list[dict[str, Any]] | None = None) -> Classification:
    """One class per session. Deterministic, in a fixed order of precedence."""
    state = session.get("state")
    decision = session.get("decision") or {}
    binding = decision.get("binding_check")
    outcome = decision.get("outcome")
    cart = session.get("cart") or {}
    failure = session.get("failure") or {}

    # 1. A payment that actually reached the bank and failed there.
    if state == "failed" and failure.get("code"):
        code = str(failure.get("code"))
        if failure.get("classification") in ("TRANSIENT", "PERMANENT", "AMBIGUOUS"):
            return _make(
                BACKEND_UNAVAILABLE,
                f"Razorpay call failed: {failure.get('message') or code}",
                razorpay_code=code,
                classification=failure.get("classification"),
                attempts=failure.get("attempts"),
            )
        transient = code in TRANSIENT_PAYMENT_CODES or failure.get("reason") == "gateway_error"
        klass = PAYMENT_DECLINED
        return Classification(
            code=klass,
            blame=BLAME[klass],
            recoverability="TRANSIENT" if transient else "PERMANENT",
            detail=failure.get("description") or code,
            evidence={
                "razorpay_code": code,
                "reason": failure.get("reason"),
                "source": failure.get("source"),
                "step": failure.get("step"),
            },
        )

    # 2. A denial where the cart contained hostile merchant copy. This ranks
    #    above the plain limit reading, because "hit a cap" and "was talked
    #    into trying to hit a cap" are different events for a merchant.
    if outcome == "DENY":
        hostile, skus, markers = cart_contains_injection(cart)
        if hostile:
            return _make(
                PROMPT_INJECTION,
                (
                    "The agent's cart contained a product whose description tries to "
                    "override its instructions. The purchase was denied on "
                    f"{binding} regardless - the check does not read descriptions."
                ),
                hostile_skus=skus,
                markers=markers,
                binding_check=binding,
                amount_paise=cart.get("total_paise"),
            )

    # 3. Repetition without progress.
    if history:
        repeats = [
            h
            for h in history
            if (h.get("decision") or {}).get("binding_check") == binding
            and (h.get("cart") or {}).get("total_paise") == cart.get("total_paise")
        ]
        if len(repeats) >= 3:
            return _make(
                AGENT_LOOPED,
                f"the same request was denied on {binding} {len(repeats)} times",
                repeats=len(repeats),
                binding_check=binding,
            )

    # 4. Straight from the binding check.
    if binding in BINDING_CHECK_TO_CLASS:
        code = BINDING_CHECK_TO_CLASS[binding]
        return _make(
            code,
            decision.get("reason") or binding,
            binding_check=binding,
            amount_paise=cart.get("total_paise"),
        )

    if outcome == "STEP_UP" or state == "step_up":
        return _make(
            NEEDS_HUMAN,
            decision.get("reason") or "above the step-up threshold",
            amount_paise=cart.get("total_paise"),
        )

    if state in ("paid", "awaiting_payment", "created"):
        return Classification(
            code=NOTHING_FAILED,
            blame="nothing failed",
            recoverability="NONE",
            detail=f"session is {state}",
            evidence={"state": state},
        )

    return _make(UNCLASSIFIED, f"session state {state!r} with no decision recorded", state=state)


def classify_cart_error(message: str) -> Classification:
    """A cart that could not even be priced - the agent misread the listing."""
    return _make(AGENT_MISREAD_LISTING, message, error=message)
