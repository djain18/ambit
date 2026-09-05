"""The grant - a signed, immutable statement of spending authority.

Money is in **paise, as integers, everywhere**. Never floats: floating-point
rupees is how a rounding bug gets shipped into a payment system.

A signed grant is immutable. That has a consequence worth stating plainly,
because it is easy to get wrong: `limits.spent_paise` and `revoked` are the
values *at issue time*, not live state. Debiting spend or revoking a grant
cannot rewrite the signed document without breaking its own signature, so live
spend and revocations live in the store (`bound/store.py`) and the checks read
them from there. The grant says what was granted; the store says what has
happened since.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ambit.canonical import canonical_bytes

SCHEMA_VERSION = 1
SIGNATURE_PREFIX = "ed25519:"


def new_grant_id() -> str:
    return "gnt_" + secrets.token_hex(8)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    """Parse an ISO-8601 instant and force it into UTC.

    A naive timestamp is treated as UTC, not local time. Every comparison in
    the checks happens in UTC; a grant near expiry is denied, never rounded.
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Limits:
    per_transaction_paise: int
    total_paise: int
    spent_paise: int = 0
    max_transactions_per_day: int = 5
    velocity_window_seconds: int = 3600
    max_transactions_per_window: int = 2

    def as_dict(self) -> dict[str, int]:
        return {
            "per_transaction_paise": self.per_transaction_paise,
            "total_paise": self.total_paise,
            "spent_paise": self.spent_paise,
            "max_transactions_per_day": self.max_transactions_per_day,
            "velocity_window_seconds": self.velocity_window_seconds,
            "max_transactions_per_window": self.max_transactions_per_window,
        }


@dataclass(frozen=True)
class Grant:
    grant_id: str
    principal: str
    agent_id: str
    limits: Limits
    allow_merchants: tuple[str, ...]
    allow_categories: tuple[str, ...]
    deny_merchants: tuple[str, ...]
    deny_categories: tuple[str, ...]
    step_up_above_paise: int
    not_before: str
    expires_at: str
    revoked: bool = False
    issuer_key_id: str = "ambit-demo-issuer"
    schema_version: int = SCHEMA_VERSION
    signature: str | None = None

    # -- serialisation ---------------------------------------------------
    def signing_payload(self) -> dict[str, Any]:
        """Every field except `signature`. This is exactly what gets signed."""
        return {
            "schema_version": self.schema_version,
            "grant_id": self.grant_id,
            "principal": self.principal,
            "agent_id": self.agent_id,
            "issuer_key_id": self.issuer_key_id,
            "limits": self.limits.as_dict(),
            "allow": {
                "merchants": list(self.allow_merchants),
                "categories": list(self.allow_categories),
            },
            "deny": {
                "merchants": list(self.deny_merchants),
                "categories": list(self.deny_categories),
            },
            "step_up_above_paise": self.step_up_above_paise,
            "not_before": self.not_before,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.signing_payload()
        payload["signature"] = self.signature
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Grant":
        allow = data.get("allow") or {}
        deny = data.get("deny") or {}
        return cls(
            grant_id=data["grant_id"],
            principal=data["principal"],
            agent_id=data["agent_id"],
            limits=Limits(**data["limits"]),
            allow_merchants=tuple(allow.get("merchants") or ()),
            allow_categories=tuple(allow.get("categories") or ()),
            deny_merchants=tuple(deny.get("merchants") or ()),
            deny_categories=tuple(deny.get("categories") or ()),
            step_up_above_paise=data["step_up_above_paise"],
            not_before=data["not_before"],
            expires_at=data["expires_at"],
            revoked=bool(data.get("revoked", False)),
            issuer_key_id=data.get("issuer_key_id", "ambit-demo-issuer"),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            signature=data.get("signature"),
        )

    # -- signing ---------------------------------------------------------
    def signed(self, private_key: Ed25519PrivateKey) -> "Grant":
        raw = private_key.sign(canonical_bytes(self.signing_payload()))
        return replace(self, signature=SIGNATURE_PREFIX + base64.b64encode(raw).decode("ascii"))

    def verify(self, public_key: Ed25519PublicKey) -> bool:
        if not self.signature or not self.signature.startswith(SIGNATURE_PREFIX):
            return False
        try:
            raw = base64.b64decode(self.signature[len(SIGNATURE_PREFIX):], validate=True)
            public_key.verify(raw, canonical_bytes(self.signing_payload()))
        except InvalidSignature:
            return False
        except Exception:
            # Fail closed on anything unexpected. Never fail open.
            return False
        return True


# -- key handling --------------------------------------------------------
def generate_keypair(private_path: Path, public_path: Path) -> Ed25519PrivateKey:
    """Create an Ed25519 keypair on disk. The private key stays gitignored."""
    private_path = Path(private_path)
    public_path = Path(public_path)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return key


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"{path} is not an Ed25519 private key")
    return key


def load_public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError(f"{path} is not an Ed25519 public key")
    return key
