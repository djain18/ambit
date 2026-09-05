"""Session timelines and the honest numbers.

Two jobs. The timeline turns one session into the ordered story of what the
agent did - what it looked at, what it was told, where it stopped. The metrics
turn all of them into figures a merchant would actually check, including the
one most dashboards leave out: what is still unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ambit.canonical import format_paise
from ambit.explain.classify import NOTHING_FAILED, classify_session
from ambit.explain.recovery import plan

# How each session event reads in a timeline, in plain language.
EVENT_LABELS = {
    "session_created": "opened a checkout session",
    "preview": "asked whether the purchase would be allowed",
    "decision": "the ten checks ran",
    "order_created": "order created at Razorpay",
    "payment_link_created": "payment link issued",
    "payment_captured": "payment captured",
    "payment_failed": "payment failed",
    "razorpay_failed": "the Razorpay call failed",
    "step_up_resolved": "a human answered the step-up",
}


@dataclass(frozen=True)
class TimelineStep:
    at: str
    label: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "label": self.label, "detail": self.detail}


def timeline(session: dict[str, Any]) -> list[dict[str, Any]]:
    steps = []
    for event in session.get("events") or []:
        name = event.get("event", "")
        steps.append(
            TimelineStep(
                at=event.get("at", ""),
                label=EVENT_LABELS.get(name, name.replace("_", " ")),
                detail=event.get("detail") or {},
            ).as_dict()
        )
    return steps


def explain_session(session: dict[str, Any], history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One session, fully explained: what happened, why, and what happens next."""
    classification = classify_session(session, history)
    attempts = sum(
        1 for e in (session.get("events") or []) if e.get("event") in ("decision", "razorpay_failed")
    )
    recovery = plan(classification, attempts_made=max(0, attempts - 1))
    decision = session.get("decision") or {}

    return {
        "session_id": session.get("session_id"),
        "agent_id": session.get("agent_id"),
        "grant_id": session.get("grant_id"),
        "state": session.get("state"),
        "total": format_paise(session.get("cart", {}).get("total_paise", 0)),
        "total_paise": session.get("cart", {}).get("total_paise", 0),
        "outcome": decision.get("outcome"),
        "binding_check": decision.get("binding_check"),
        "reason": decision.get("reason"),
        "checks": decision.get("checks", []),
        "classification": classification.as_dict(),
        "recovery": recovery.as_dict(),
        "timeline": timeline(session),
        "cart": session.get("cart"),
    }


def metrics(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """The numbers, including the ones that are inconvenient."""
    attempted = len(sessions)
    outcomes = {"ALLOW": 0, "STEP_UP": 0, "DENY": 0}
    by_binding: dict[str, int] = {}
    by_class: dict[str, int] = {}
    money_moved = 0
    money_stopped = 0
    unresolved: list[dict[str, Any]] = []

    for session in sessions:
        decision = session.get("decision") or {}
        outcome = decision.get("outcome")
        if outcome in outcomes:
            outcomes[outcome] += 1

        binding = decision.get("binding_check")
        if binding:
            by_binding[binding] = by_binding.get(binding, 0) + 1

        classification = classify_session(session)
        if classification.code != NOTHING_FAILED:
            by_class[classification.code] = by_class.get(classification.code, 0) + 1

        total = int(session.get("cart", {}).get("total_paise", 0))
        if session.get("state") == "paid":
            money_moved += total
        elif outcome == "DENY":
            money_stopped += total

        # Anything a human still has to deal with.
        if session.get("state") in ("step_up", "failed"):
            unresolved.append(
                {
                    "session_id": session.get("session_id"),
                    "state": session.get("state"),
                    "total_paise": total,
                    "classification": classification.code,
                    "blame": classification.blame,
                }
            )

    return {
        "attempted": attempted,
        "allowed": outcomes["ALLOW"],
        "stepped_up": outcomes["STEP_UP"],
        "denied": outcomes["DENY"],
        "money_moved_paise": money_moved,
        "money_moved": format_paise(money_moved),
        "money_stopped_paise": money_stopped,
        "money_stopped": format_paise(money_stopped),
        "denials_by_check": dict(sorted(by_binding.items(), key=lambda kv: -kv[1])),
        "failures_by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "note": (
            "money_stopped is the total of what was asked for and refused. It is not "
            "a claim about fraud prevented - it is what the limits actually blocked."
        ),
    }
