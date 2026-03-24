"""
ASGI middleware for passive HTTP schema observation.

:class:`ApinferASGIMiddleware` intercepts every HTTP request/response pair,
normalizes it into a framework-agnostic
:class:`~apinfer.core.models.CapturedExchange`, and accumulates exchanges in
a :class:`~apinfer.core.capture.ReservoirBuffer`.  Once *snapshot_every*
exchanges have been seen for an endpoint, the middleware builds a
:class:`~apinfer.core.models.Snapshot` from the buffered sample and persists
it to SQLite — entirely off the request path via :func:`asyncio.create_task`.

This module is a **pure ASGI implementation**: it imports nothing from any
web framework and works with FastAPI, Starlette, Litestar, and any other
ASGI server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apinfer.core.capture import ReservoirBuffer
from apinfer.core.inferrer import build_snapshot
from apinfer.core.models import CapturedExchange, HttpMethod
from apinfer.core.storage import SnapshotStorage, default_db_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping
    from pathlib import Path
    from typing import Any

    from apinfer.core.models import EndpointKey, JsonBody

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["ApinferASGIMiddleware"]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path normalisation helpers
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_INT_RE = re.compile(r"^\d+$")


def _normalize_path(scope: Scope, raw_path: str) -> str:
    """Return a normalized path pattern for the given ASGI scope.

    Resolution order:

    1. ``scope["route"].path`` — set by Starlette / FastAPI after routing,
       provides the exact template (e.g. ``/users/{user_id}``).
    2. Heuristic fallback — replaces UUID v4 and pure-integer segments
       with the placeholder ``{id}``.

    :param scope: ASGI scope dict for the current request.
    :param raw_path: Literal path as received from the client.
    :return: Normalized path pattern string.
    """
    route = scope.get("route")
    if route is not None:
        path_attr = getattr(route, "path", None)
        if isinstance(path_attr, str):
            return path_attr

    parts = raw_path.split("/")
    normalized: list[str] = []
    for seg in parts:
        if seg and (_UUID_RE.fullmatch(seg) or _INT_RE.fullmatch(seg)):
            normalized.append("{id}")
        else:
            normalized.append(seg)
    return "/".join(normalized)


def _parse_json_body(raw: bytes) -> JsonBody | None:
    """Attempt to decode *raw* bytes as JSON.

    :param raw: Raw HTTP body bytes.
    :return: Decoded JSON value, or ``None`` if *raw* is empty or invalid.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ApinferASGIMiddleware:
    """ASGI middleware that passively observes HTTP traffic for schema inference.

    Add to any ASGI application with one line::

        from apinfer import ApinferMiddleware

        app.add_middleware(ApinferMiddleware)

    Schema inference and storage writes happen in background tasks — the
    response is sent to the client before any inference work begins.

    :param app: The inner ASGI application to wrap.
    :param db_path: Path to the SQLite database file.  Defaults to
        ``apinfer.db`` in the current working directory.
    :param buffer_size: Maximum exchanges retained per endpoint in the
        reservoir buffer.  Defaults to ``100``.
    :param snapshot_every: Persist a new snapshot every *N* exchanges for
        an endpoint.  Defaults to ``10``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        db_path: Path | None = None,
        buffer_size: int = 100,
        snapshot_every: int = 10,
    ) -> None:
        self._app = app
        self._buffer = ReservoirBuffer(max_size=buffer_size)
        self._snapshot_every = snapshot_every
        self._storage = SnapshotStorage(
            db_path if db_path is not None else default_db_path()
        )
        # Retain strong references so asyncio does not GC pending tasks.
        self._tasks: set[asyncio.Task[None]] = set()

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """ASGI entry point.

        Non-HTTP scopes (``lifespan``, ``websocket``) are forwarded without
        interception.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        await self._handle_http(scope, receive, send)

    async def _handle_http(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        request_chunks: list[bytes] = []
        response_chunks: list[bytes] = []
        # Single-item list used as a mutable container for the closure.
        response_status: list[int] = [0]

        async def wrapped_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                chunk: bytes = message.get("body", b"")
                if chunk:
                    request_chunks.append(chunk)
            return message

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_status[0] = message["status"]
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    response_chunks.append(chunk)
            await send(message)

        await self._app(scope, wrapped_receive, wrapped_send)

        # Assemble raw bodies before scheduling so the chunk lists are not
        # mutated by a concurrent request sharing the same closure.
        task = asyncio.create_task(
            self._process_exchange(
                scope=scope,
                method_str=str(scope.get("method", "GET")).upper(),
                raw_path=str(scope.get("path", "/")),
                response_status=response_status[0],
                request_body_raw=b"".join(request_chunks),
                response_body_raw=b"".join(response_chunks),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_exchange(
        self,
        *,
        scope: Scope,
        method_str: str,
        raw_path: str,
        response_status: int,
        request_body_raw: bytes,
        response_body_raw: bytes,
    ) -> None:
        try:
            if response_status == 0:
                # http.response.start was never sent (e.g. unhandled exception
                # raised before the framework could produce a response).
                return

            try:
                method = HttpMethod(method_str)
            except ValueError:
                return  # Non-standard method (CONNECT, PROPFIND, …); skip.

            exchange = CapturedExchange(
                method=method,
                path=_normalize_path(scope, raw_path),
                path_raw=raw_path,
                status_code=response_status,
                request_body=_parse_json_body(request_body_raw),
                response_body=_parse_json_body(response_body_raw),
                timestamp=datetime.now(tz=UTC),
            )
            self._buffer.add(exchange)

            key = exchange.endpoint_key
            count = self._buffer.total_count(key)
            if count % self._snapshot_every == 0:
                await self._save_snapshot(key)

        except Exception:
            _logger.exception("apinfer: unhandled error processing exchange")

    async def _save_snapshot(self, key: EndpointKey) -> None:
        try:
            exchanges = self._buffer.get(key)
            if not exchanges:
                return
            snapshot = build_snapshot(key, exchanges)
            if snapshot is None:
                return
            self._storage.save(snapshot)
        except Exception:
            _logger.exception("apinfer: failed to persist snapshot for %s", key)
