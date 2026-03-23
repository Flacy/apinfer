"""
Core data models for the apinfer pipeline.

All dataclasses and enumerations used throughout the system are defined here.
Nothing in this module may import from web frameworks — the core must remain
framework-agnostic so that the transport layer (ASGI/WSGI) is the only
framework-aware component.

Data flows bottom-up through five layers:

1. **Transport** — intercepts HTTP exchanges, produces :class:`CapturedExchange`.
2. **Capture** — buffers exchanges via reservoir sampling.
3. **Core** — infers JSON Schema from buffered exchanges; persists :class:`Snapshot`
   to SQLite; yields :class:`Snapshot` on retrieval.
4. **Detector** — compares two :class:`Snapshot` objects; produces :class:`DriftReport`.
5. **Output** — renders :class:`DriftReport` to the terminal, HTML, or pytest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, computed_field

__all__ = [
    "CapturedExchange",
    "ChangeKind",
    "DriftChange",
    "DriftReport",
    "DriftSeverity",
    "EndpointKey",
    "HttpMethod",
    "JsonBody",
    "JsonSchema",
    "Snapshot",
]

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

JsonSchema: TypeAlias = dict[str, Any]
"""A JSON Schema object as a plain Python dictionary."""

JsonBody: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None
"""Any value that can appear as a decoded JSON HTTP body, including ``null``."""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class HttpMethod(StrEnum):
    """HTTP request methods as defined by :rfc:`9110`.

    Values are uppercase strings so that serialized output matches the
    wire format without custom serializers.
    """

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    TRACE = "TRACE"


class DriftSeverity(StrEnum):
    """Severity classification for a detected schema change.

    Severity is assigned by the detector based on the nature of the change
    and its potential impact on consumers of the API.

    - ``BREAKING`` — a consumer relying on the previous contract will break:
      a required field disappeared, a field type changed, or a status code
      was removed.
    - ``NON_BREAKING`` — new surface area was added that does not break
      existing consumers: a new optional field appeared or a new status code
      was added.
    - ``INFORMATIONAL`` — the change is noteworthy but unlikely to break
      consumers: a field became optional, or its observed presence rate
      changed across samples.
    """

    BREAKING = "breaking"
    NON_BREAKING = "non_breaking"
    INFORMATIONAL = "informational"


class ChangeKind(StrEnum):
    """Structural category of a detected schema change.

    :class:`ChangeKind` is orthogonal to :class:`DriftSeverity`: the same
    kind of change may be breaking or non-breaking depending on direction
    (e.g. a field added is non-breaking; a field removed is breaking).
    The detector owns the mapping from kind + direction → severity.
    """

    FIELD_TYPE_CHANGED = "field_type_changed"
    FIELD_REMOVED = "field_removed"
    FIELD_ADDED = "field_added"
    FIELD_OPTIONALITY_CHANGED = "field_optionality_changed"
    FIELD_PRESENCE_RATE_CHANGED = "field_presence_rate_changed"
    STATUS_CODE_REMOVED = "status_code_removed"
    STATUS_CODE_ADDED = "status_code_added"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class EndpointKey(BaseModel):
    """Immutable identifier for a specific endpoint variant.

    Groups captured exchanges and schema snapshots by the tuple
    ``(method, path pattern, status code)``. Used as a dictionary key
    in the sampler buffer and as a lookup key in storage.

    Because this model is frozen, it is hashable and safe to use as a
    ``dict`` key or in a ``set``.

    :param method: HTTP method for this endpoint.
    :param path: Normalized path pattern with path parameters abstracted,
        e.g. ``/users/{id}``. Path normalization is the responsibility of
        the transport layer, not enforced here.
    :param status_code: HTTP response status code.
    """

    model_config = ConfigDict(frozen=True)

    method: HttpMethod = Field(description="HTTP method for this endpoint.")
    path: str = Field(
        description="Normalized path pattern with path parameters abstracted, "
        "e.g. '/users/{id}'."
    )
    status_code: int = Field(description="HTTP response status code.")


class CapturedExchange(BaseModel):
    """A single normalized, framework-agnostic HTTP request/response pair.

    Produced by the transport layer (ASGI or WSGI middleware) after
    intercepting and normalizing a live HTTP exchange. The core pipeline
    consumes only :class:`CapturedExchange` objects — it never sees
    framework-specific request/response types.

    The model is frozen (immutable) because exchanges are append-only
    data flowing through the pipeline; nothing downstream should mutate
    a captured exchange.

    :param method: HTTP method of the request.
    :param path: Normalized path pattern with path parameters abstracted.
    :param path_raw: Actual path as received by the server, kept for
        debugging and future exact-match analysis.
    :param status_code: HTTP response status code.
    :param request_body: Decoded request body, or ``None`` if absent or
        not JSON-decodable.
    :param response_body: Decoded response body, or ``None`` if absent or
        not JSON-decodable.
    :param timestamp: UTC timestamp of when the exchange was captured.
        Defaults to the current UTC time at instantiation.
    """

    model_config = ConfigDict(frozen=True)

    method: HttpMethod = Field(description="HTTP method of the request.")
    path: str = Field(
        description="Normalized path pattern with path parameters abstracted."
    )
    path_raw: str = Field(description="Actual path as received by the server.")
    status_code: int = Field(description="HTTP response status code.")
    request_body: JsonBody | None = Field(
        default=None,
        description="Decoded request body, or None if absent or non-JSON.",
    )
    response_body: JsonBody | None = Field(
        default=None,
        description="Decoded response body, or None if absent or non-JSON.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        description="UTC timestamp of when the exchange was captured.",
    )

    @property
    def endpoint_key(self) -> EndpointKey:
        """Return the :class:`EndpointKey` that identifies this exchange's endpoint.

        This is a plain ``@property``, not a ``@computed_field``, so it is
        excluded from :meth:`~pydantic.BaseModel.model_dump` output.

        :return: An :class:`EndpointKey` derived from ``method``, ``path``,
            and ``status_code``.
        :rtype: EndpointKey
        """
        return EndpointKey(
            method=self.method,
            path=self.path,
            status_code=self.status_code,
        )


class Snapshot(BaseModel):
    """A point-in-time JSON Schema representation of an endpoint's contract.

    Each :class:`Snapshot` is derived from a set of :class:`CapturedExchange`
    objects for a single endpoint. The schema captures field types, presence
    rates, and nullability across all observed samples.

    Snapshots are immutable (frozen). Storage assigns an integer ``id``
    after persisting; callers should use
    :meth:`~pydantic.BaseModel.model_copy` to produce an updated copy::

        persisted = snapshot.model_copy(update={"id": row_id})

    :param key: The endpoint this snapshot describes.
    :param inferred_schema: JSON Schema object inferred from captured exchanges.
    :param content_hash: SHA-256 hex digest of the canonical
        JSON-serialized schema (``sort_keys=True``, no whitespace).
        Use :meth:`build` to compute this automatically.
    :param timestamp: UTC timestamp of when this snapshot was created.
    :param sample_count: Number of exchanges that contributed to this
        schema.
    :param id: Storage-assigned integer ID. ``None`` before the snapshot
        is persisted to the database.
    """

    model_config = ConfigDict(frozen=True)

    key: EndpointKey = Field(description="Endpoint this snapshot describes.")
    inferred_schema: JsonSchema = Field(
        description="JSON Schema object inferred from captured exchanges."
    )
    content_hash: str = Field(
        description="SHA-256 hex digest of the canonical JSON-serialized schema."
    )
    timestamp: datetime = Field(description="UTC timestamp of snapshot creation.")
    sample_count: int = Field(
        description="Number of exchanges that contributed to this schema."
    )
    id: int | None = Field(
        default=None,
        description=(
            "Storage-assigned integer ID. None before the snapshot is persisted."
        ),
    )

    @classmethod
    def build(
        cls,
        key: EndpointKey,
        schema: JsonSchema,
        sample_count: int,
    ) -> Snapshot:
        """Construct a :class:`Snapshot`, computing hash and timestamp automatically.

        The ``content_hash`` is the SHA-256 hex digest of the schema serialized
        as canonical JSON (``sort_keys=True``, no whitespace). This ensures the
        hash is stable regardless of Python dict insertion order or formatting.

        :param key: The endpoint this snapshot describes.
        :param schema: The JSON Schema object inferred from captured exchanges.
        :param sample_count: Number of exchanges that contributed to this schema.
        :return: A new :class:`Snapshot` with ``content_hash`` and ``timestamp``
            computed automatically. ``id`` is ``None`` until persisted.
        :rtype: Snapshot
        """
        canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        return cls(
            key=key,
            inferred_schema=schema,
            content_hash=content_hash,
            timestamp=datetime.now(tz=UTC),
            sample_count=sample_count,
        )


class DriftChange(BaseModel):
    """A single detected change between two schema snapshots.

    Produced by the drift detector when comparing a baseline
    :class:`Snapshot` against a current one. Each change records what
    changed, where, the severity, and the before/after values for display.

    The model is frozen. To acknowledge a change, produce an updated copy::

        acked = change.model_copy(update={"acknowledged": True})

    :param field_path: JSONPath-like location of the changed element,
        e.g. ``$.properties.name.type``.
    :param kind: Structural category of the change.
    :param severity: Severity classification of this change.
    :param description: Human-readable explanation of what changed.
    :param before: Previous value of the changed element, or ``None``
        when a field or status code was added (no prior value exists).
    :param after: New value of the changed element, or ``None`` when a
        field or status code was removed.
    :param acknowledged: Whether the user has explicitly accepted this
        change via the CLI. Defaults to ``False``.
    """

    model_config = ConfigDict(frozen=True)

    field_path: str = Field(
        description="JSONPath-like location of the changed element, "
        "e.g. '$.properties.name.type'."
    )
    kind: ChangeKind = Field(description="Structural category of the change.")
    severity: DriftSeverity = Field(
        description="Severity classification of this change."
    )
    description: str = Field(description="Human-readable explanation of what changed.")
    before: Any = Field(
        default=None,
        description="Previous value of the changed element, or None for additions.",
    )
    after: Any = Field(
        default=None,
        description="New value of the changed element, or None for removals.",
    )
    acknowledged: bool = Field(
        default=False,
        description="Whether the user has explicitly accepted this change.",
    )


class DriftReport(BaseModel):
    """The result of comparing two :class:`Snapshot` objects for the same endpoint.

    Produced by the drift detector and consumed by the output layer
    (CLI, pytest plugin, HTML report). The ``changes`` list can be
    replaced after construction — for example, to apply acknowledgements
    — without violating the model's contract, because :class:`DriftReport`
    is not frozen.

    Computed fields :attr:`has_breaking_changes` and :attr:`is_clean`
    reflect the current state of ``changes`` dynamically and are included
    in :meth:`~pydantic.BaseModel.model_dump` output for serialization.

    :param endpoint_key: Endpoint this report covers.
    :param baseline_id: Storage ID of the previous :class:`Snapshot`,
        or ``None`` if no prior snapshot exists for this endpoint.
    :param current_id: Storage ID of the current :class:`Snapshot`.
    :param changes: All detected changes between baseline and current
        snapshot. Defaults to an empty list (no changes = clean).
    :param timestamp: UTC timestamp of when this report was generated.
        Defaults to the current UTC time at instantiation.
    """

    endpoint_key: EndpointKey = Field(description="Endpoint this report covers.")
    baseline_id: int | None = Field(
        description=(
            "Storage ID of the previous Snapshot, or None if no prior snapshot exists."
        )
    )
    current_id: int | None = Field(description="Storage ID of the current Snapshot.")
    changes: list[DriftChange] = Field(
        default_factory=list,
        description="All detected changes between baseline and current snapshot.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        description="UTC timestamp of when this report was generated.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_breaking_changes(self) -> bool:
        """Whether any change in this report has :attr:`~DriftSeverity.BREAKING`
        severity.

        Re-evaluated on every access so that replacing ``changes`` in place
        is immediately reflected.

        :return: ``True`` if at least one change has
            :attr:`DriftSeverity.BREAKING` severity.
        :rtype: bool
        """
        return any(c.severity is DriftSeverity.BREAKING for c in self.changes)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_clean(self) -> bool:
        """Whether this report contains no detected changes at all.

        :return: ``True`` if ``changes`` is empty.
        :rtype: bool
        """
        return len(self.changes) == 0
