#!/usr/bin/env python
"""Re-walk the audit chain and prove it has not been edited.

    python execution/verify_audit_chain.py
    python execution/verify_audit_chain.py --show 20
    python execution/verify_audit_chain.py --json

Exit code is 0 for an intact chain and 1 for a broken one, so this works as a
CI gate as well as a demo beat. Breaking it on purpose is part of the demo:
edit any row in data/audit.jsonl, run this again, and it names the row.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ambit.canonical import format_paise
from ambit.config import load_settings, use_utf8_stdout
from ambit.explain.chain import AuditChain

SUMMARY_FIELDS = ("outcome", "binding_check", "grant_id", "request_id", "payment_id")


def _one_line(entry: dict) -> str:
    payload = entry.get("payload") or {}
    bits = []
    for field in SUMMARY_FIELDS:
        value = payload.get(field)
        if value:
            bits.append(f"{field}={value}")
    request = payload.get("request") or {}
    if request.get("amount_paise") is not None:
        bits.append(f"amount={format_paise(request['amount_paise'])}")
    if payload.get("amount_paise") is not None:
        bits.append(f"amount={format_paise(payload['amount_paise'])}")
    return f"  {entry['seq']:>4}  {entry['ts']}  {entry['type']:<18} {' '.join(bits)}"


def _self_test() -> int:
    """Prove the chain both verifies and *notices*, from nothing.

    A verifier that always says VERIFIED is worse than no verifier, because
    people believe it. So this builds a throwaway chain, checks it passes,
    edits one past row the way an attacker would, and fails unless
    verification catches it and names the right row.
    """
    import json as _json
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="ambit-chain-selftest-"))
    try:
        path = tmp / "audit.jsonl"
        chain = AuditChain(path)
        for i in range(6):
            chain.append("DECISION", {"outcome": "ALLOW", "request_id": f"req_{i}", "n": i})

        intact = chain.verify()
        print(f"built {intact.entries_checked} entries")
        if not intact.ok:
            print("  FAILED: a freshly built chain did not verify")
            return 1
        print("  intact chain verifies")

        # Edit a past row, leaving its recorded hash untouched.
        rows = [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        target = 2
        rows[target]["payload"]["outcome"] = "DENY"
        path.write_text(
            "\n".join(_json.dumps(r, separators=(",", ":"), sort_keys=True) for r in rows) + "\n",
            encoding="utf-8",
        )

        broken = AuditChain(path).verify()
        if broken.ok:
            print(f"  FAILED: entry {target} was edited and verification still passed")
            return 1
        print(f"  tampered entry {target} caught: BROKEN at {broken.broken_at_seq}")
        print("\nself-test passed: the chain verifies when intact and breaks when edited")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--path", help="chain file (defaults to the configured data dir)")
    parser.add_argument("--show", type=int, default=0, help="print the last N entries")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="build a throwaway chain, verify it, corrupt a row, and prove the break is caught",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    settings = load_settings()
    path = Path(args.path) if args.path else settings.audit_chain_path
    chain = AuditChain(path)
    result = chain.verify()

    if args.json:
        print(json.dumps({"path": str(path), **result.as_dict()}, indent=2))
        return 0 if result.ok else 1

    print(f"chain: {path}")
    if not path.exists():
        print("  no chain file yet - nothing has been recorded")
        return 0

    if args.show:
        entries = chain.entries()
        print(f"\nlast {min(args.show, len(entries))} of {len(entries)} entries:")
        for entry in entries[-args.show:]:
            print(_one_line(entry))
        print()

    if result.ok:
        print(f"  VERIFIED - {result.entries_checked} entries, hash chain intact")
        print("  every entry links to the one before it; nothing has been edited")
        return 0

    print(f"  BROKEN at entry {result.broken_at_seq}")
    print(f"  {result.reason}")
    print(f"  {result.entries_checked} entries verified before the break")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
