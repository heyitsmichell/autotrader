"""Broker exception types."""

from __future__ import annotations


class BrokerError(RuntimeError):
    """A broker call failed.

    transport=True means we never got an answer (timeout, dead process). For
    order placement that means the order may or may not exist at Webull.
    """

    def __init__(self, message: str, *, transport: bool = False) -> None:
        super().__init__(message)
        self.transport = transport


class OrderGuardError(BrokerError):
    """The broker-level order guard refused an order before anything was sent."""
