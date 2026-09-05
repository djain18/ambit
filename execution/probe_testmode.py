"""Milestone 0 — test-mode capability probe.

Answers one question before any real code gets written: which Razorpay
endpoints actually work with a test key? Finding a gap here on day 1 costs
two hours. Finding it on day 6 costs the submission.

Re-runnable. Creates only test-mode objects (no real money can move).
Never prints the API secret.

    python scripts/probe_testmode.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://api.razorpay.com/v1"
TIMEOUT = 30


def die(msg: str) -> None:
    print(f"\n  FATAL: {msg}\n")
    sys.exit(1)


def load_auth() -> tuple[str, str]:
    load_dotenv(ROOT / ".env")
    key_id = (os.getenv("RAZORPAY_KEY_ID") or "").strip()
    secret = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()

    if not key_id or not secret:
        die("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET missing from ambit/.env")
    if "xxxx" in key_id:
        die("RAZORPAY_KEY_ID is still the placeholder from env.example")

    require_test = (os.getenv("AMBIT_REQUIRE_TEST_MODE", "true").lower() == "true")
    if require_test and not key_id.startswith("rzp_test_"):
        die(
            f"key id starts with {key_id[:9]!r}, not 'rzp_test_'. "
            "Refusing to run. Flip the Razorpay Dashboard toggle to Test."
        )
    return key_id, secret


class Probe:
    def __init__(self, auth: tuple[str, str]) -> None:
        self.auth = auth
        self.results: list[dict] = []
        self.artifacts: dict[str, str] = {}

    def call(
        self,
        label: str,
        method: str,
        path: str,
        payload: dict | None = None,
        capture: str | None = None,
    ) -> dict | None:
        """Run one endpoint check. Records outcome, never raises."""
        url = f"{BASE}{path}"
        try:
            resp = requests.request(
                method, url, auth=self.auth, json=payload, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            self.results.append(
                {"check": label, "method": method, "path": path,
                 "status": None, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            )
            print(f"  [NET ] {label:<34} {method} {path}  ({type(exc).__name__})")
            return None

        ok = 200 <= resp.status_code < 300
        entry = {
            "check": label, "method": method, "path": path,
            "status": resp.status_code, "ok": ok,
        }

        body: dict = {}
        try:
            body = resp.json()
        except ValueError:
            entry["error"] = "non-JSON response"

        if not ok:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            entry["error"] = err.get("description") or resp.text[:200]
            entry["error_code"] = err.get("code")

        self.results.append(entry)

        flag = "PASS" if ok else "FAIL"
        detail = "" if ok else f"  -> {entry.get('error', '')[:70]}"
        print(f"  [{flag}] {label:<34} {method} {path} [{resp.status_code}]{detail}")

        if ok and capture and isinstance(body, dict) and body.get("id"):
            self.artifacts[capture] = body["id"]
        return body if ok else None


def main() -> int:
    key_id, secret = load_auth()
    stamp = datetime.now(timezone.utc)

    print("\n" + "=" * 78)
    print("  AMBIT — Milestone 0: Razorpay test-mode capability probe")
    print("=" * 78)
    print(f"  key id : {key_id[:13]}…{key_id[-4:]}   (secret not shown)")
    print(f"  mode   : TEST")
    print(f"  time   : {stamp.isoformat()}")
    print("=" * 78 + "\n")

    p = Probe((key_id, secret))
    receipt = f"ambit_m0_{int(stamp.timestamp())}"

    # --- 1. Auth + read paths -------------------------------------------
    print("-- auth & reads " + "-" * 62)
    p.call("auth / list payments", "GET", "/payments?count=1")
    p.call("list orders", "GET", "/orders?count=1")
    p.call("list payment links", "GET", "/payment_links")
    p.call("list settlements", "GET", "/settlements?count=1")
    p.call("list refunds", "GET", "/refunds?count=1")

    # --- 2. Orders — the OPEN feature depends on this --------------------
    print("\n-- orders (OPEN depends on these) " + "-" * 44)
    p.call(
        "create order", "POST", "/orders",
        {
            "amount": 50000,  # paise = Rs 500
            "currency": "INR",
            "receipt": receipt,
            "notes": {"source": "ambit-m0-probe", "purpose": "capability check"},
        },
        capture="order_id",
    )
    if oid := p.artifacts.get("order_id"):
        p.call("fetch order by id", "GET", f"/orders/{oid}")
        p.call("fetch payments for order", "GET", f"/orders/{oid}/payments")

    # --- 3. Payment Links — how an agent actually pays -------------------
    print("\n-- payment links (agent checkout) " + "-" * 44)
    p.call(
        "create payment link", "POST", "/payment_links",
        {
            "amount": 50000,
            "currency": "INR",
            "description": "Ambit M0 probe",
            "reference_id": receipt,
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {"source": "ambit-m0-probe"},
        },
        capture="plink_id",
    )
    if pid := p.artifacts.get("plink_id"):
        p.call("fetch payment link", "GET", f"/payment_links/{pid}")

    # --- 4. Customers — used to attribute agent sessions -----------------
    print("\n-- customers " + "-" * 65)
    p.call(
        "create customer", "POST", "/customers",
        {"name": "Ambit Probe", "fail_existing": "0",
         "notes": {"source": "ambit-m0-probe"}},
        capture="customer_id",
    )

    # --- summary ---------------------------------------------------------
    passed = [r for r in p.results if r["ok"]]
    failed = [r for r in p.results if not r["ok"]]

    print("\n" + "=" * 78)
    print(f"  {len(passed)}/{len(p.results)} checks passed")
    if failed:
        print("\n  UNAVAILABLE / BROKEN — these constrain the build:")
        for r in failed:
            print(f"    - {r['check']}: {r.get('error', 'unknown')}")
    print("=" * 78)

    if p.artifacts:
        print("\n  Test objects created (visible in Dashboard, Test mode):")
        for k, v in p.artifacts.items():
            print(f"    {k:<12} {v}")

    out = ROOT / ".tmp" / "m0_probe.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {"timestamp": stamp.isoformat(), "key_id_prefix": key_id[:13],
             "passed": len(passed), "total": len(p.results),
             "results": p.results, "artifacts": p.artifacts},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  Full results: {out.relative_to(ROOT)}\n")

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
