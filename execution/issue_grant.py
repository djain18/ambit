#!/usr/bin/env python
"""Issue, inspect, list and revoke spending grants.

    python execution/issue_grant.py keygen
    python execution/issue_grant.py issue --agent agt_shopper_01 --per-txn 500000 \
        --total 2000000 --step-up 200000 --merchants mrc_demo_store \
        --categories groceries,software --hours 24
    python execution/issue_grant.py list
    python execution/issue_grant.py inspect gnt_abc123
    python execution/issue_grant.py revoke gnt_abc123 --reason "spending looked wrong"

Amounts are in **paise**. 500000 paise is 5,000 rupees.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ambit.canonical import format_paise
from ambit.config import load_settings, use_utf8_stdout
from ambit.bound.grant import (
    Grant,
    Limits,
    generate_keypair,
    load_private_key,
    load_public_key,
    new_grant_id,
    to_iso,
    utc_now,
)
from ambit.bound.store import BoundStore
from ambit.explain.chain import AuditChain


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def cmd_keygen(args, settings) -> int:
    if settings.signing_key_path.exists() and not args.force:
        print(f"key already exists at {settings.signing_key_path}")
        print("pass --force to replace it (every existing grant stops verifying)")
        return 1
    generate_keypair(settings.signing_key_path, settings.public_key_path)
    print(f"private key -> {settings.signing_key_path}  (gitignored, never commit)")
    print(f"public key  -> {settings.public_key_path}")
    return 0


def cmd_issue(args, settings) -> int:
    if not settings.signing_key_path.exists():
        print("no signing key yet. Run: python execution/issue_grant.py keygen")
        return 1

    private = load_private_key(settings.signing_key_path)
    now = utc_now()
    grant = Grant(
        grant_id=new_grant_id(),
        principal=args.principal,
        agent_id=args.agent,
        limits=Limits(
            per_transaction_paise=args.per_txn,
            total_paise=args.total,
            spent_paise=0,
            max_transactions_per_day=args.max_per_day,
            velocity_window_seconds=args.window_seconds,
            max_transactions_per_window=args.max_per_window,
        ),
        allow_merchants=_csv(args.merchants),
        allow_categories=_csv(args.categories),
        deny_merchants=_csv(args.deny_merchants),
        deny_categories=_csv(args.deny_categories),
        step_up_above_paise=args.step_up,
        not_before=to_iso(now),
        expires_at=to_iso(now + timedelta(hours=args.hours)),
    ).signed(private)

    store = BoundStore(settings.data_dir)
    path = store.save_grant(grant)
    AuditChain(settings.audit_chain_path).append(
        "GRANT_ISSUED",
        {
            "grant_id": grant.grant_id,
            "agent_id": grant.agent_id,
            "principal": grant.principal,
            "per_transaction_paise": grant.limits.per_transaction_paise,
            "total_paise": grant.limits.total_paise,
            "expires_at": grant.expires_at,
        },
    )

    print(f"issued {grant.grant_id}")
    print(f"  agent        {grant.agent_id}")
    print(f"  per txn      {format_paise(grant.limits.per_transaction_paise)}")
    print(f"  total        {format_paise(grant.limits.total_paise)}")
    print(f"  step up above {format_paise(grant.step_up_above_paise)}")
    print(f"  merchants    {', '.join(grant.allow_merchants) or '(none)'}")
    print(f"  categories   {', '.join(grant.allow_categories) or '(none)'}")
    print(f"  expires      {grant.expires_at}")
    print(f"  saved to     {path}")
    return 0


def _summarise(grant, store, settings) -> dict:
    captured = store.captured_paise(grant.grant_id)
    revocation = store.revocation(grant.grant_id)
    verified = None
    if settings.public_key_path.exists():
        verified = grant.verify(load_public_key(settings.public_key_path))
    return {
        "grant_id": grant.grant_id,
        "agent_id": grant.agent_id,
        "principal": grant.principal,
        "signature_verifies": verified,
        "revoked": bool(revocation) or grant.revoked,
        "expires_at": grant.expires_at,
        "per_transaction_paise": grant.limits.per_transaction_paise,
        "total_paise": grant.limits.total_paise,
        "captured_paise": captured,
        "remaining_paise": max(0, grant.limits.total_paise - captured),
    }


def cmd_list(args, settings) -> int:
    store = BoundStore(settings.data_dir)
    grants = store.list_grants()
    if not grants:
        print("no grants issued yet")
        return 0
    for grant in grants:
        s = _summarise(grant, store, settings)
        flag = "REVOKED" if s["revoked"] else ("ok" if s["signature_verifies"] else "BAD SIG")
        print(
            f"{s['grant_id']}  {flag:8}  {s['agent_id']:18}"
            f"  spent {format_paise(s['captured_paise'])} of {format_paise(s['total_paise'])}"
            f"  expires {s['expires_at']}"
        )
    return 0


def cmd_inspect(args, settings) -> int:
    store = BoundStore(settings.data_dir)
    grant = store.load_grant(args.grant_id)
    if grant is None:
        print(f"no such grant: {args.grant_id}")
        return 1
    summary = _summarise(grant, store, settings)
    print(json.dumps(summary, indent=2))
    print()
    print("transactions:")
    entries = store.ledger_for(grant.grant_id)
    if not entries:
        print("  (none)")
    for entry in entries:
        print(
            f"  {entry.ts}  {entry.state:11}  {format_paise(entry.amount_paise):>12}"
            f"  {entry.merchant_id}  {entry.request_id}"
        )
    return 0


def cmd_revoke(args, settings) -> int:
    store = BoundStore(settings.data_dir)
    if store.load_grant(args.grant_id) is None:
        print(f"no such grant: {args.grant_id}")
        return 1
    record = store.revoke(args.grant_id, args.reason)
    AuditChain(settings.audit_chain_path).append(
        "GRANT_REVOKED", {"grant_id": args.grant_id, **record}
    )
    print(f"revoked {args.grant_id} at {record['revoked_at']}")
    print(f"  reason: {record['reason']}")
    print("  every future decision on this grant now denies on GRANT_REVOKED")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="create the Ed25519 issuer keypair")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("issue", help="issue a signed grant")
    p.add_argument("--principal", default="user_daksh")
    p.add_argument("--agent", default="agt_shopper_01")
    p.add_argument("--per-txn", type=int, default=500_000, help="per-transaction cap, paise")
    p.add_argument("--total", type=int, default=2_000_000, help="total budget, paise")
    p.add_argument("--step-up", type=int, default=200_000, help="human approval above, paise")
    p.add_argument("--merchants", default="mrc_demo_store")
    p.add_argument("--categories", default="groceries,software")
    p.add_argument("--deny-merchants", default="")
    p.add_argument("--deny-categories", default="gambling")
    p.add_argument("--max-per-day", type=int, default=5)
    p.add_argument("--max-per-window", type=int, default=2)
    p.add_argument("--window-seconds", type=int, default=3600)
    p.add_argument("--hours", type=int, default=24, help="validity in hours")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("list", help="list every issued grant")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("inspect", help="show one grant and its transactions")
    p.add_argument("grant_id")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("revoke", help="revoke a grant, immediately")
    p.add_argument("grant_id")
    p.add_argument("--reason", default="revoked from the command line")
    p.set_defaults(func=cmd_revoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    use_utf8_stdout()
    args = build_parser().parse_args(argv)
    settings = load_settings()
    return args.func(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
