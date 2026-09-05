"""What to do about a failure - and when to stop trying.

The stopping rule is the part that matters, and Track 01 names it explicitly.
An agent that retries until something works is not resilient, it is a way to
lose money slowly. So:

* **At most two automated attempts.** Then a human is told, and the system
  stops on its own.
* **Escalation is terminal, not a pause.** Nothing auto-resumes once a human
  has been pulled in.
* **A POLICY denial is never retried, not even once.** A denied purchase is
  the system working correctly. Retrying it would be a security bug wearing a
  resilience costume, and it is the single easiest mistake to make here.

Rules come from `skills/self-anneal/SKILL.md` rather than being invented per
call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ambit.explain.classify import Classification

MAX_AUTOMATED_ATTEMPTS = 2

# What the system does next.
RETRY = "RETRY"
ESCALATE = "ESCALATE"
STOP = "STOP"
NONE = "NONE"


@dataclass(frozen=True)
class RecoveryPlan:
    action: str
    reason: str
    attempts_made: int
    attempts_remaining: int
    terminal: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "attempts_made": self.attempts_made,
            "attempts_remaining": self.attempts_remaining,
            "terminal": self.terminal,
        }


def plan(classification: Classification, attempts_made: int = 0) -> RecoveryPlan:
    """Decide what happens next. Deterministic; no model involved."""
    recoverability = classification.recoverability

    if recoverability == "NONE":
        return RecoveryPlan(NONE, "nothing failed", attempts_made, 0, terminal=True)

    if recoverability == "POLICY":
        # The most important branch in the file. Never RETRY here.
        return RecoveryPlan(
            STOP,
            (
                f"{classification.code} is a policy decision, not a failure. "
                "It is never retried - the answer would be identical, and trying "
                "to get a different one is an attack, not a recovery."
            ),
            attempts_made,
            0,
            terminal=True,
        )

    if recoverability == "PERMANENT":
        return RecoveryPlan(
            ESCALATE,
            f"{classification.code} will fail identically on a retry; a human has to look",
            attempts_made,
            0,
            terminal=True,
        )

    if recoverability == "TRANSIENT":
        remaining = MAX_AUTOMATED_ATTEMPTS - attempts_made
        if remaining > 0:
            return RecoveryPlan(
                RETRY,
                f"{classification.code} may clear on another attempt",
                attempts_made,
                remaining,
                terminal=False,
            )
        return RecoveryPlan(
            ESCALATE,
            (
                f"{MAX_AUTOMATED_ATTEMPTS} automated attempts already made. "
                "Stopping and escalating rather than trying again."
            ),
            attempts_made,
            0,
            terminal=True,
        )

    # AMBIGUOUS: treated as PERMANENT and flagged, never retried hopefully.
    return RecoveryPlan(
        ESCALATE,
        "could not be classified from the record; treated as permanent and flagged",
        attempts_made,
        0,
        terminal=True,
    )
