"""Razorpay test-mode client, with the self-anneal rules built in.

Retry thresholds, backoff and error classification come from
`skills/self-anneal/SKILL.md` rather than being invented per call site. The
rules that matter most here:

* **Reads retry 3 times, writes retry twice.** A money write is never
  blind-retried.
* **Every money write carries an idempotency key**, so a retry cannot create a
  second order.
* **`POLICY` is never retried.** A denied purchase is the system working
  correctly, and retrying it would be a security bug wearing a resilience
  costume. BOUND denials never reach this module at all - by the time anything
  here runs, the decision was already ALLOW.

Plain `requests` against the REST API, matching `execution/probe_testmode.py`.
The official SDK would be one more dependency for no gain at this size.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

BASE_URL = "https://api.razorpay.com/v1"
TIMEOUT_SECONDS = 30

TRANSIENT = "TRANSIENT"
PERMANENT = "PERMANENT"
POLICY = "POLICY"
AMBIGUOUS = "AMBIGUOUS"

READ_BACKOFF = (1.0, 2.0, 4.0)
WRITE_BACKOFF = (2.0, 6.0)


class RazorpayError(RuntimeError):
    """A failed Razorpay call, carrying its self-anneal classification."""

    def __init__(
        self,
        message: str,
        *,
        classification: str,
        status: int | None = None,
        code: str | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.status = status
        self.code = code
        self.attempts = attempts

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "classification": self.classification,
            "status": self.status,
            "code": self.code,
            "attempts": self.attempts,
        }


def classify(status: int | None, body: dict[str, Any] | None) -> str:
    """One class per failure. The class decides what happens next."""
    if status is None:
        return TRANSIENT  # a timeout or a dropped connection
    if status >= 500 or status == 429:
        return TRANSIENT
    if status in (408, 409):
        return TRANSIENT
    if 400 <= status < 500:
        error = (body or {}).get("error") or {}
        # Razorpay marks genuinely retryable 4xx cases; everything else is a
        # business rule and will fail again identically.
        if error.get("reason") in ("server_error", "gateway_error"):
            return TRANSIENT
        if error.get("code"):
            return PERMANENT
        return AMBIGUOUS
    return AMBIGUOUS


@dataclass
class CallOutcome:
    ok: bool
    status: int | None
    body: dict[str, Any]
    attempts: int
    classification: str | None = None


class RazorpayClient:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not key_id or not key_secret:
            raise ValueError("Razorpay credentials are missing")
        self.auth = (key_id, key_secret)
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep

    # -- transport --------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        write: bool = False,
    ) -> dict[str, Any]:
        backoff = WRITE_BACKOFF if write else READ_BACKOFF
        max_attempts = len(backoff) if write else len(backoff)
        headers: dict[str, str] = {}
        if idempotency_key:
            # A retry must never create a second order.
            headers["X-Razorpay-Idempotency-Key"] = idempotency_key

        last: RazorpayError | None = None
        for attempt in range(1, max_attempts + 1):
            status: int | None = None
            body: dict[str, Any] = {}
            try:
                response = requests.request(
                    method,
                    f"{self.base_url}{path}",
                    auth=self.auth,
                    json=payload,
                    headers=headers or None,
                    timeout=TIMEOUT_SECONDS,
                )
                status = response.status_code
                try:
                    body = response.json()
                except ValueError:
                    body = {"raw": response.text[:500]}
                if 200 <= status < 300:
                    return body
            except requests.RequestException as exc:
                body = {"exception": type(exc).__name__}

            classification = classify(status, body)
            error = (body or {}).get("error") or {}
            last = RazorpayError(
                error.get("description") or f"{method} {path} failed",
                classification=classification,
                status=status,
                code=error.get("code"),
                attempts=attempt,
            )

            if classification != TRANSIENT or attempt == max_attempts:
                raise last

            delay = backoff[attempt - 1] + random.uniform(0, 0.4)
            self._sleep(delay)

        raise last or RazorpayError(
            f"{method} {path} failed", classification=AMBIGUOUS, attempts=max_attempts
        )

    # -- the calls Ambit actually makes -----------------------------------
    def create_order(
        self,
        amount_paise: int,
        receipt: str,
        notes: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/orders",
            payload={
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt[:40],
                "notes": notes or {},
            },
            idempotency_key=idempotency_key or receipt,
            write=True,
        )

    def create_payment_link(
        self,
        amount_paise: int,
        description: str,
        reference_id: str,
        *,
        customer: dict[str, str] | None = None,
        notes: dict[str, Any] | None = None,
        callback_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "amount": amount_paise,
            "currency": "INR",
            "description": description[:2048],
            "reference_id": reference_id,
            "notes": notes or {},
        }
        if customer:
            payload["customer"] = customer
            payload["notify"] = {"sms": False, "email": False}
        if callback_url:
            payload["callback_url"] = callback_url
            payload["callback_method"] = "get"
        return self._request(
            "POST",
            "/payment_links",
            payload=payload,
            idempotency_key=idempotency_key or reference_id,
            write=True,
        )

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/orders/{order_id}")

    def fetch_payments_for_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/orders/{order_id}/payments")

    def fetch_payment_link(self, plink_id: str) -> dict[str, Any]:
        return self._request("GET", f"/payment_links/{plink_id}")


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Razorpay signs the raw body with HMAC-SHA256.

    Compared with `compare_digest`, and the raw bytes are used rather than a
    re-serialised dict - re-encoding JSON changes the bytes and the signature
    would never match.
    """
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
