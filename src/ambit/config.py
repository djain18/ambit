"""One place that knows where things live and refuses to run in live mode.

The test-mode rail is deliberately load-bearing: `AMBIT_REQUIRE_TEST_MODE`
defaults to on, and with it on the process will not start against a key that
does not begin with `rzp_test_`. Nothing in this project should ever touch
real money, and that is enforced rather than documented.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_KEY_PREFIX = "rzp_test_"


def use_utf8_stdout() -> None:
    """Windows consoles default to cp1252 and mangle the rupee sign.

    Called by the CLI entry points so a demo recorded on Windows does not show
    mojibake where it should show an amount.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


class LiveModeRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    require_test_mode: bool
    host: str
    port: int
    public_url: str
    signing_key_path: Path
    public_key_path: Path
    data_dir: Path

    @property
    def audit_chain_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def has_razorpay_credentials(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    def redacted(self) -> dict[str, object]:
        """Safe to print, log, or show on a video. Never returns a secret."""
        return {
            "razorpay_key_id": (
                self.razorpay_key_id[:12] + "..." if self.razorpay_key_id else "(unset)"
            ),
            "razorpay_key_secret": "(set)" if self.razorpay_key_secret else "(unset)",
            "razorpay_webhook_secret": "(set)" if self.razorpay_webhook_secret else "(unset)",
            "require_test_mode": self.require_test_mode,
            "host": self.host,
            "port": self.port,
            "public_url": self.public_url or "(unset)",
            "data_dir": str(self.data_dir),
        }


def _bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or (REPO_ROOT / ".env"), override=False)

    key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
    require_test_mode = _bool(os.getenv("AMBIT_REQUIRE_TEST_MODE"), True)

    if require_test_mode and key_id and not key_id.startswith(TEST_KEY_PREFIX):
        raise LiveModeRefused(
            "RAZORPAY_KEY_ID does not start with 'rzp_test_'. Ambit refuses to run "
            "against a live key. Set a test key, or set AMBIT_REQUIRE_TEST_MODE=false "
            "if you genuinely know what you are doing."
        )

    data_dir = Path(os.getenv("AMBIT_DATA_DIR", str(REPO_ROOT / "data"))).resolve()
    signing_key_path = Path(
        os.getenv("AMBIT_GRANT_SIGNING_KEY_PATH", "keys/grant_signing_key.pem")
    )
    if not signing_key_path.is_absolute():
        signing_key_path = REPO_ROOT / signing_key_path

    return Settings(
        razorpay_key_id=key_id,
        razorpay_key_secret=os.getenv("RAZORPAY_KEY_SECRET", "").strip(),
        razorpay_webhook_secret=os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip(),
        require_test_mode=require_test_mode,
        host=os.getenv("AMBIT_HOST", "127.0.0.1"),
        port=int(os.getenv("AMBIT_PORT", "8000")),
        public_url=os.getenv("AMBIT_PUBLIC_URL", "").strip().rstrip("/"),
        signing_key_path=signing_key_path,
        public_key_path=signing_key_path.with_suffix(".pub"),
        data_dir=data_dir,
    )
