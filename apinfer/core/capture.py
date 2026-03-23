"""
Reservoir-sampling buffer for captured HTTP exchanges.

:class:`ReservoirBuffer` holds at most *max_size* exchanges per endpoint using
Knuth's Algorithm R, guaranteeing a uniform random sample regardless of
traffic volume.  Memory overhead is constant once the buffer is full.

The buffer has no I/O and no async code.  It is safe to call from within a
single asyncio event loop without additional locking because all mutations are
synchronous and complete within a single event-loop tick.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apinfer.core.models import CapturedExchange, EndpointKey

__all__ = ["ReservoirBuffer"]


class ReservoirBuffer:
    """Reservoir-sampling buffer of captured HTTP exchanges per endpoint.

    Exchanges are keyed by :class:`~apinfer.core.models.EndpointKey`.  Once
    the buffer for an endpoint reaches *max_size*, new exchanges replace
    existing entries using Algorithm R, preserving a statistically
    representative sample.

    :param max_size: Maximum number of exchanges retained per endpoint.
        Defaults to ``100``.
    """

    def __init__(self, max_size: int = 100) -> None:
        self._max_size = max_size
        self._buffers: dict[EndpointKey, list[CapturedExchange]] = {}
        self._counts: dict[EndpointKey, int] = {}

    def add(self, exchange: CapturedExchange) -> None:
        """Add *exchange* to the reservoir buffer using Algorithm R.

        If the buffer for this endpoint has fewer than *max_size* entries,
        the exchange is appended.  Otherwise it replaces a uniformly random
        existing entry with probability ``max_size / n``, where *n* is the
        total number of exchanges ever seen for this endpoint.

        :param exchange: Exchange to buffer.
        """
        key = exchange.endpoint_key
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        buf = self._buffers.setdefault(key, [])
        if len(buf) < self._max_size:
            buf.append(exchange)
        else:
            j = random.randint(0, count - 1)
            if j < self._max_size:
                buf[j] = exchange

    def get(self, key: EndpointKey) -> list[CapturedExchange]:
        """Return a snapshot of the buffer for *key*.

        Returns a shallow copy; mutating the result does not affect the buffer.

        :param key: Endpoint whose buffer to retrieve.
        :return: Current buffered exchanges (at most *max_size*).
        """
        return list(self._buffers.get(key, []))

    def total_count(self, key: EndpointKey) -> int:
        """Return the total number of exchanges ever seen for *key*.

        Always ≥ ``len(self.get(key))``.  Used to decide when to trigger a
        new snapshot (modulo check against *snapshot_every*).

        :param key: Endpoint to query.
        :return: Lifetime exchange count (not capped by *max_size*).
        """
        return self._counts.get(key, 0)

    def keys(self) -> list[EndpointKey]:
        """Return all endpoint keys with at least one buffered exchange.

        :return: List of :class:`~apinfer.core.models.EndpointKey` objects.
        """
        return list(self._buffers)
