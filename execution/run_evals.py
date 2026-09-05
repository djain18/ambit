"""Run the labelled decision scenarios in `evals/scenarios.json`.

The claim this backs up is narrow and total: **the decision layer is
deterministic, so accuracy against a labelled expectation should be 100%.**
Anything less is a bug rather than variance, so a single miss exits 1 and the
CI gate fails.

It checks two things per scenario, not one. Getting DENY for the wrong reason
is still wrong: an audit trail whose reason codes cannot be trusted is worse
than no audit trail, because someone will believe it. So the expected
`binding_check` is asserted alongside the expected outcome.

No network, no API keys, no model. Every scenario gets a fresh store and chain
in a temp directory, so no scenario can see another one's ledger.

    python execution/run_evals.py                # human-readable report
    python execution/run_evals.py --json out.json
    python execution/run_evals.py --quiet        # CI: exit code only
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ambit.bound.checks import PurchaseRequest  # noqa: E402
from ambit.bound.decide import BoundEngine  # noqa: E402
from ambit.bound.grant import (  # noqa: E402
    Grant,
    Limits,
    generate_keypair,
    load_public_key,
    to_iso,
)
from ambit.bound.store import BoundStore, LedgerEntry  # noqa: E402
from ambit.explain.chain import AuditChain  # noqa: E402

BASE_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
SCENARIOS = ROOT / "evals" / "scenarios.json"

BASE_LIMITS = {
    "per_transaction_paise": 500_000,
    "total_paise": 2_000_000,
    "spent_paise": 0,
    "max_transactions_per_day": 5,
    "velocity_window_seconds": 3600,
    "max_transactions_per_window": 2,
}
LIMIT_FIELDS = set(BASE_LIMITS)


def build_grant(private_key, overrides: dict[str, Any]) -> Grant:
    """The baseline grant, with per-scenario overrides applied before signing."""
    limits = dict(BASE_LIMITS)
    top: dict[str, Any] = {}
    for key, value in (overrides or {}).items():
        if key in LIMIT_FIELDS:
            limits[key] = value
        else:
            top[key] = value

    fields: dict[str, Any] = {
        "grant_id": "gnt_eval_0001",
        "principal": "user_daksh",
        "agent_id": "agt_shopper_01",
        "allow_merchants": ("mrc_demo_store",),
        "allow_categories": ("groceries", "software"),
        "deny_merchants": ("mrc_blocked_store",),
        "deny_categories": ("gambling",),
        "step_up_above_paise": 200_000,
        "not_before": to_iso(BASE_NOW - timedelta(days=1)),
        "expires_at": to_iso(BASE_NOW + timedelta(days=7)),
    }
    fields.update(top)
    for name in ("allow_merchants", "allow_categories", "deny_merchants", "deny_categories"):
        fields[name] = tuple(fields[name])

    return Grant(limits=Limits(**limits), **fields).signed(private_key)


def tamper(grant: Grant, field: str, value: Any) -> Grant:
    """Change a signed grant without re-signing it, the way an attacker would."""
    if field.startswith("limits."):
        name = field.split(".", 1)[1]
        limits = dataclasses.replace(grant.limits, **{name: value})
        return dataclasses.replace(grant, limits=limits)
    if field in {"allow_merchants", "allow_categories", "deny_merchants", "deny_categories"}:
        value = tuple(value)
    return dataclasses.replace(grant, **{field: value})


def make_request(spec: dict[str, Any], default_id: str) -> PurchaseRequest:
    return PurchaseRequest(
        request_id=spec.get("request_id", default_id),
        agent_id=spec.get("agent_id", "agt_shopper_01"),
        merchant_id=spec.get("merchant_id", "mrc_demo_store"),
        category=spec.get("category", "groceries"),
        amount_paise=spec["amount_paise"],
        now=BASE_NOW + timedelta(seconds=spec.get("offset_seconds", 0)),
    )


def run_one(scenario: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Set the world up, ask for one decision, compare it to the label."""
    private = generate_keypair(workdir / "k.pem", workdir / "k.pub")
    public = load_public_key(workdir / "k.pub")
    store = BoundStore(workdir / "data")
    chain = AuditChain(workdir / "data" / "audit.jsonl")
    engine = BoundEngine(store, chain, public)

    grant = build_grant(private, scenario.get("grant", {}))
    store.save_grant(grant)

    for step in scenario.get("setup", []):
        op = step["op"]
        if op == "revoke":
            store.revoke(grant.grant_id, step.get("reason", "revoked"))
        elif op == "tamper":
            grant = tamper(grant, step["field"], step["value"])
        elif op == "ledger":
            at = BASE_NOW - timedelta(minutes=step["minutes_ago"])
            store.append_ledger(
                LedgerEntry(
                    request_id=f"req_prior_{step['minutes_ago']}_{step['amount_paise']}",
                    grant_id=grant.grant_id,
                    agent_id="agt_shopper_01",
                    merchant_id="mrc_demo_store",
                    category="groceries",
                    amount_paise=step["amount_paise"],
                    decision="ALLOW",
                    state=step.get("state", "authorised"),
                    ts=to_iso(at),
                )
            )
        elif op == "prior_request":
            # A real first decision under the id the scenario will reuse.
            engine.decide(grant, make_request(step, "req_same"))
        else:  # pragma: no cover - guards a typo in the scenario file
            raise ValueError(f"unknown setup op: {op}")

    decision = engine.decide(grant, make_request(scenario["request"], scenario["id"]))

    expect = scenario["expect"]
    outcome_ok = decision.outcome == expect["outcome"]
    binding_ok = decision.binding_check == expect["binding_check"]
    replay_ok = True
    if "replay" in expect:
        replay_ok = bool(decision.replay_of) == bool(expect["replay"])

    return {
        "id": scenario["id"],
        "family": scenario.get("family", "other"),
        "name": scenario["name"],
        "expected": {"outcome": expect["outcome"], "binding_check": expect["binding_check"]},
        "actual": {"outcome": decision.outcome, "binding_check": decision.binding_check},
        "reason": decision.reason,
        "checks_run": len(decision.checks),
        "passed": outcome_ok and binding_ok and replay_ok,
        "why_failed": (
            None
            if outcome_ok and binding_ok and replay_ok
            else (
                "wrong outcome"
                if not outcome_ok
                else "right outcome, wrong binding check"
                if not binding_ok
                else "replay handling differed"
            )
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Ambit's labelled decision scenarios.")
    ap.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    ap.add_argument("--quiet", action="store_true", help="print nothing, exit code only")
    args = ap.parse_args()

    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    scenarios = data["scenarios"]

    ids = [s["id"] for s in scenarios]
    duplicates = [i for i, n in Counter(ids).items() if n > 1]
    if duplicates:
        print(f"duplicate scenario ids: {duplicates}", file=sys.stderr)
        return 2

    results = []
    tmp = Path(tempfile.mkdtemp(prefix="ambit-evals-"))
    try:
        for scenario in scenarios:
            workdir = tmp / scenario["id"]
            workdir.mkdir(parents=True)
            try:
                results.append(run_one(scenario, workdir))
            except Exception as exc:  # noqa: BLE001 - a crash is a failed scenario
                results.append(
                    {
                        "id": scenario["id"],
                        "family": scenario.get("family", "other"),
                        "name": scenario["name"],
                        "expected": scenario["expect"],
                        "actual": None,
                        "passed": False,
                        "why_failed": f"{type(exc).__name__}: {exc}",
                    }
                )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = [r for r in results if not r["passed"]]
    accuracy = passed / total if total else 0.0

    by_family: dict[str, dict[str, int]] = {}
    for r in results:
        row = by_family.setdefault(r["family"], {"total": 0, "passed": 0})
        row["total"] += 1
        row["passed"] += int(r["passed"])

    report = {
        "generated_at": to_iso(datetime.now(timezone.utc)),
        "total": total,
        "passed": passed,
        "failed": len(failed),
        "decision_accuracy": round(accuracy, 4),
        "target": 1.0,
        "note": (
            "The decision layer is deterministic, so the target is 100%. "
            "A scenario passes only if BOTH the outcome and the binding check match: "
            "the right answer for the wrong reason is still a bug."
        ),
        "by_family": by_family,
        "unresolved": [
            {"id": r["id"], "name": r["name"], "why": r["why_failed"]} for r in failed
        ],
        "results": results,
    }

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not args.quiet:
        print(f"\nAmbit decision evals: {passed}/{total} correct "
              f"({accuracy:.1%}, target 100%)\n")
        width = max(len(f) for f in by_family) if by_family else 10
        for family in sorted(by_family):
            row = by_family[family]
            mark = "ok  " if row["passed"] == row["total"] else "FAIL"
            print(f"  {mark}  {family:<{width}}  {row['passed']}/{row['total']}")
        if failed:
            print("\nUnresolved:")
            for r in failed:
                exp, act = r["expected"], r.get("actual")
                print(f"\n  {r['id']}  {r['name']}")
                print(f"      why      {r['why_failed']}")
                print(f"      expected {exp['outcome']} / {exp['binding_check']}")
                if act:
                    print(f"      actual   {act['outcome']} / {act['binding_check']}")
        else:
            print("\nNothing unresolved.")
        print()

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
