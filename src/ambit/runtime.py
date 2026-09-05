"""One place that wires the pieces together.

Everything that needs a store, a chain, an engine or a Razorpay client gets it
from here, so there is exactly one audit chain and one ledger per process
rather than several that disagree with each other.
"""

from __future__ import annotations

from functools import lru_cache

from ambit.bound.decide import BoundEngine
from ambit.bound.grant import load_public_key
from ambit.bound.store import BoundStore
from ambit.config import Settings, load_settings
from ambit.explain.chain import AuditChain
from ambit.open.sessions import SessionStore
from ambit.razorpay_client import RazorpayClient


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = BoundStore(settings.data_dir)
        self.chain = AuditChain(settings.audit_chain_path)
        self.sessions = SessionStore(settings.data_dir)

        public_key = None
        if settings.public_key_path.exists():
            public_key = load_public_key(settings.public_key_path)
        self.public_key = public_key
        self.engine = BoundEngine(self.store, self.chain, public_key)

        self._razorpay: RazorpayClient | None = None

    @property
    def razorpay(self) -> RazorpayClient | None:
        """None when credentials are absent.

        OPEN degrades to a readable, un-buyable shop rather than pretending a
        payment happened - the self-anneal fallback order, rule 1.
        """
        if self._razorpay is None and self.settings.has_razorpay_credentials:
            self._razorpay = RazorpayClient(
                self.settings.razorpay_key_id, self.settings.razorpay_key_secret
            )
        return self._razorpay

    @property
    def signing_key_present(self) -> bool:
        return self.public_key is not None


@lru_cache(maxsize=1)
def get_runtime() -> Runtime:
    return Runtime(load_settings())
