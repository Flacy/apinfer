"""apinfer — passive API schema observation from live traffic."""

from __future__ import annotations

from apinfer.transport.asgi import ApinferASGIMiddleware

# Canonical user-facing name.
# Currently points to the ASGI variant.
# Will auto-select ASGI/WSGI after build order #7.
ApinferMiddleware = ApinferASGIMiddleware

__all__ = [
    "ApinferASGIMiddleware",
    "ApinferMiddleware",
    "__version__",
]
__version__ = "0.1.0"
