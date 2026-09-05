"""ALLOW / STEP_UP / DENY.

The decision is deterministic. Same grant, same request, same store state,
same answer, every time - which is why the target on the scenario suite is
100% and not "high". Anything less is a bug, not variance.

Three rules govern this file and none of them are negotiable:

* **No model call, anywhere in this path.** A language model can be argued
  into approving a hundred thousand rupees. A policy check cannot be argued
  with at all.
* **Record before attempting.** The decision is appended to the audit chain
  before any Razorpay call happens, so a crash mid-flight still leaves
  evidence of what was intended.
* **Fail closed.** Any unexpected error becomes a DENY that is logged. There
  is no path through this file that turns an error into an approval.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ambit.bound.checks import CheckResult, PurchaseRequest, run_checks
from ambit.bound.grant import Grant, to_iso
from ambit.bound.store import BoundStore, LedgerEntry
from ambit.explain.chain import AuditChain

ALLOW = "ALLOW"
STEP_UP = "STEP_UP"
DENY = "DENY"

STEP_UP_THRESHOLD = "STEP_UP_THRESHOLD"
INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class Decision:
    outcome: str
    binding_check: str | None
    reason: str
    grant_id: str
    request: dict[str, Any]
    checks: list[CheckResult] = field(default_factory=list)
    decided_at: str = ""
    replay_of: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "binding_check": self.binding_check,
            "reason": self.reason,
            "grant_id": self.grant_id,
            "request": self.request,
            "checks": [c.as_dict() for c in self.checks],
            "decided_at": self.decided_at,
            "replay_of": self.replay_of,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        return cls(
            outcome=data["outcome"],
            binding_check=data.get("binding_check"),
            reason=data.get("reason", ""),
            grant_id=data.get("grant_id", ""),
            request=data.get("request", {}),
            checks=[CheckResult(**c) for c in data.get("checks", [])],
            decided_at=data.get("decided_at", ""),
            replay_of=data.get("replay_of"),
        )


class BoundEngine:
    """Binds a store, an audit chain and an issuer key into one decision path."""

    def __init__(
        self,
        store: BoundStore,
        chain: AuditChain,
        public_key: Ed25519PublicKey | None,
    ) -> None:
        self.store = store
        self.chain = chain
        self.public_key = public_key

    # -- the decision ----------------------------------------------------
    def decide(self, grant: Grant, req: PurchaseRequest) -> Decision:
        try:
            return self._decide(grant, req)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            # Fail closed. An error is never an approval.
            decision = Decision(
                outcome=DENY,
                binding_check=INTERNAL_ERROR,
                reason=f"denied because the check itself failed: {type(exc).__name__}",
                grant_id=getattr(grant, "grant_id", "unknown"),
                request=req.as_dict(),
                checks=[],
                decided_at=to_iso(datetime.now(timezone.utc)),
            )
            self.chain.append(
                "DECISION_ERROR",
                {
                    "decision": decision.as_dict(),
                    "error": type(exc).__name__,
                    "traceback": traceback.format_exc(limit=3),
                },
            )
            return decision

    def _decide(self, grant: Grant, req: PurchaseRequest) -> Decision:
        fingerprint = req.fingerprint(grant.grant_id)
        prior = self.store.recall_decision(req.request_id)

        # An honest retry returns the original answer verbatim and never
        # re-executes. A replay with altered parameters is not a retry, so it
        # falls through to the checks and gets denied on IDEMPOTENCY.
        if prior is not None and prior.get("fingerprint") == fingerprint:
            original = Decision.from_dict(prior["decision"])
            replayed = Decision(
                outcome=original.outcome,
                binding_check=original.binding_check,
                reason=original.reason,
                grant_id=original.grant_id,
                request=original.request,
                checks=original.checks,
                decided_at=original.decided_at,
                replay_of=req.request_id,
            )
            self.chain.append(
                "DECISION_REPLAYED",
                {"request_id": req.request_id, "outcome": original.outcome},
            )
            return replayed

        checks = run_checks(grant, req, self.store, self.public_key)
        failed = [c for c in checks if not c.passed]

        if failed:
            # The binding check is the first failure in the fixed order. Every
            # other check still ran and is still in the trace.
            binding = failed[0]
            decision = Decision(
                outcome=DENY,
                binding_check=binding.code,
                reason=binding.message,
                grant_id=grant.grant_id,
                request=req.as_dict(),
                checks=checks,
                decided_at=to_iso(req.now),
            )
        elif req.amount_paise > grant.step_up_above_paise:
            decision = Decision(
                outcome=STEP_UP,
                binding_check=STEP_UP_THRESHOLD,
                reason=(
                    f"all ten checks passed, but {req.amount_paise} paise is above the "
                    f"step-up threshold of {grant.step_up_above_paise} paise - "
                    "a human has to approve this one"
                ),
                grant_id=grant.grant_id,
                request=req.as_dict(),
                checks=checks,
                decided_at=to_iso(req.now),
            )
        else:
            decision = Decision(
                outcome=ALLOW,
                binding_check=None,
                reason="all ten checks passed and the amount is under the step-up threshold",
                grant_id=grant.grant_id,
                request=req.as_dict(),
                checks=checks,
                decided_at=to_iso(req.now),
            )

        # Record before attempting - the chain entry is written before the
        # caller is allowed to touch the Razorpay API.
        self.chain.append("DECISION", decision.as_dict())
        self.store.remember_decision(req.request_id, fingerprint, decision.as_dict())

        if decision.outcome == ALLOW:
            self._authorise(grant, req, decision)

        return decision

    def preview(self, grant: Grant, req: PurchaseRequest) -> Decision:
        """Run the same ten checks, change nothing.

        An agent gets to ask "would this be allowed?" before committing to it.
        The preview burns no idempotency record, books no ledger row and holds
        no budget - so browsing cannot exhaust a grant. It is still written to
        the audit chain, because what an agent *considered* buying is part of
        explaining what it did.
        """
        try:
            checks = run_checks(grant, req, self.store, self.public_key)
            failed = [c for c in checks if not c.passed]
            if failed:
                outcome, binding, reason = DENY, failed[0].code, failed[0].message
            elif req.amount_paise > grant.step_up_above_paise:
                outcome, binding, reason = (
                    STEP_UP,
                    STEP_UP_THRESHOLD,
                    "would need a human to approve this amount",
                )
            else:
                outcome, binding, reason = ALLOW, None, "would be allowed"
            decision = Decision(
                outcome=outcome,
                binding_check=binding,
                reason=reason,
                grant_id=grant.grant_id,
                request=req.as_dict(),
                checks=checks,
                decided_at=to_iso(req.now),
            )
        except Exception as exc:  # noqa: BLE001
            decision = Decision(
                outcome=DENY,
                binding_check=INTERNAL_ERROR,
                reason=f"preview failed: {type(exc).__name__}",
                grant_id=getattr(grant, "grant_id", "unknown"),
                request=req.as_dict(),
                checks=[],
                decided_at=to_iso(datetime.now(timezone.utc)),
            )
        self.chain.append("DECISION_PREVIEW", decision.as_dict())
        return decision

    def _authorise(self, grant: Grant, req: PurchaseRequest, decision: Decision) -> None:
        """Reserve the slot. Budget is only truly spent on capture."""
        self.store.append_ledger(
            LedgerEntry(
                request_id=req.request_id,
                grant_id=grant.grant_id,
                agent_id=req.agent_id,
                merchant_id=req.merchant_id,
                category=req.category,
                amount_paise=req.amount_paise,
                decision=decision.outcome,
                state="authorised",
                ts=to_iso(req.now),
            )
        )

    # -- what happens after the decision ---------------------------------
    def approve_step_up(self, grant: Grant, req: PurchaseRequest, approver: str) -> Decision:
        """Turn a STEP_UP into an ALLOW after a human says yes.

        The checks are re-run, not waived. A human approving an amount does not
        approve an expired grant, a revoked one, or an exhausted budget - only
        the step-up threshold is satisfied by the approval.
        """
        checks = run_checks(grant, req, self.store, self.public_key)
        failed = [c for c in checks if not c.passed and c.code != "IDEMPOTENCY"]
        if failed:
            decision = Decision(
                outcome=DENY,
                binding_check=failed[0].code,
                reason=f"approval arrived but {failed[0].message}",
                grant_id=grant.grant_id,
                request=req.as_dict(),
                checks=checks,
                decided_at=to_iso(req.now),
            )
        else:
            decision = Decision(
                outcome=ALLOW,
                binding_check=None,
                reason=f"step-up approved by {approver}; all ten checks re-run and passed",
                grant_id=grant.grant_id,
                request=req.as_dict(),
                checks=checks,
                decided_at=to_iso(req.now),
            )

        self.chain.append(
            "STEP_UP_RESOLVED",
            {"approver": approver, "decision": decision.as_dict()},
        )
        self.store.remember_decision(
            req.request_id, req.fingerprint(grant.grant_id), decision.as_dict()
        )
        if decision.outcome == ALLOW:
            self._authorise(grant, req, decision)
        return decision

    def record_capture(self, request_id: str, payment_id: str, amount_paise: int) -> bool:
        """Budget debits here, on payment.captured - not on ALLOW.

        An allowed purchase that never gets paid must not consume budget.
        """
        changed = self.store.set_state(request_id, "captured")
        self.chain.append(
            "PAYMENT_CAPTURED",
            {
                "request_id": request_id,
                "payment_id": payment_id,
                "amount_paise": amount_paise,
                "ledger_updated": changed,
            },
        )
        return changed

    def record_failure(self, request_id: str, code: str, description: str) -> bool:
        changed = self.store.set_state(request_id, "failed")
        self.chain.append(
            "PAYMENT_FAILED",
            {
                "request_id": request_id,
                "code": code,
                "description": description,
                "ledger_updated": changed,
            },
        )
        return changed

    def revoke(self, grant_id: str, reason: str) -> dict[str, Any]:
        record = self.store.revoke(grant_id, reason)
        self.chain.append("GRANT_REVOKED", {"grant_id": grant_id, **record})
        return record
