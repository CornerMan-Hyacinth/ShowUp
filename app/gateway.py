import os
import random
import time
import uuid


class GatewayError(Exception):
    pass


class MockGateway:
    """In-code simulation of a payment gateway: 200ms latency, 1% random failure."""

    def __init__(self, delay=0.2, failure_rate=0.01):
        self.delay = delay
        self.failure_rate = failure_rate

    def _maybe_fail(self):
        time.sleep(self.delay)
        if random.random() < self.failure_rate:
            raise GatewayError("simulated gateway failure")

    def charge(self, amount: float, idempotency_key: str) -> str:
        self._maybe_fail()
        return f"mock_ch_{uuid.uuid4().hex[:12]}"

    def refund(self, gateway_ref: str, amount: float) -> str:
        self._maybe_fail()
        return f"mock_re_{uuid.uuid4().hex[:12]}"


gateway = MockGateway(delay=float(os.getenv("GATEWAY_DELAY", "0.2")),
                      failure_rate=float(os.getenv("GATEWAY_FAILURE_RATE", "0.01")))
