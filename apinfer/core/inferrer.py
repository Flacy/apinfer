"""
JSON Schema inference from observed HTTP payloads.

Given a batch of JSON payloads captured from a live endpoint, this module infers:

- Field types and nullability (via :pypi:`genson`)
- Required vs optional fields — genson marks a field ``required`` only when it
  appeared in every sample
- **Conditional presence rates** — the ``x-apinfer-presence-rate`` JSON Schema
  extension is added to every property.  The denominator is the number of
  parent-level samples that contained the property's enclosing object, giving
  accurate "when does this field appear?" semantics independent of parent
  optionality.

Public surface:

- :func:`infer_schema` — pure inference from a list of JSON values
- :func:`infer_response_schema` — convenience wrapper over
  :class:`~apinfer.core.models.CapturedExchange`
- :func:`build_snapshot` — full pipeline: infer schema → build
  :class:`~apinfer.core.models.Snapshot`
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genson import SchemaBuilder

from apinfer.core.models import Snapshot

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any, Final

    from apinfer.core.models import (
        CapturedExchange,
        EndpointKey,
        JsonBody,
        JsonSchema,
    )

__all__ = [
    "PRESENCE_RATE_KEY",
    "build_snapshot",
    "infer_response_schema",
    "infer_schema",
]

PRESENCE_RATE_KEY: Final = "x-apinfer-presence-rate"
"""JSON Schema extension key written to every annotated property.

The value is a ``float`` in ``[0.0, 1.0]``, rounded to 4 decimal places.
The drift detector and output layer read this key via :data:`PRESENCE_RATE_KEY`
rather than the bare string to keep them in sync with any future rename.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def infer_schema(payloads: Sequence[JsonBody]) -> JsonSchema:
    """Infer a JSON Schema from a sequence of observed JSON payloads.

    Uses genson for base type inference (field types, nullability, and the
    ``required`` array), then layers conditional presence rates on every
    object property via the ``x-apinfer-presence-rate`` extension key.

    :param payloads: Sequence of decoded JSON values from observed responses.
        ``None`` entries are silently skipped — they represent responses with
        no body or a non-JSON-decodable body.
    :return: JSON Schema ``dict``.  Returns ``{}`` when no valid (non-``None``)
        payloads remain after filtering.
    :rtype: JsonSchema
    """
    valid: list[JsonBody] = [p for p in payloads if p is not None]
    if not valid:
        return {}
    schema = _build_genson_schema(valid)
    return _annotate_schema(schema, valid)


def infer_response_schema(exchanges: Sequence[CapturedExchange]) -> JsonSchema:
    """Infer a JSON Schema from the response bodies of captured exchanges.

    Thin wrapper around :func:`infer_schema` that extracts
    ``exchange.response_body`` from each exchange.  ``None`` bodies (absent
    or non-JSON responses) are silently ignored by the inner call.

    :param exchanges: Captured HTTP exchanges for a single endpoint.
    :return: JSON Schema dict.  Returns ``{}`` if no exchange had a
        decodable response body.
    :rtype: JsonSchema
    """
    return infer_schema([e.response_body for e in exchanges])


def build_snapshot(
    key: EndpointKey,
    exchanges: Sequence[CapturedExchange],
) -> Snapshot | None:
    """Infer a schema from exchanges and build a ready-to-persist
    :class:`~apinfer.core.models.Snapshot`.

    Convenience function combining :func:`infer_schema` with
    :meth:`~apinfer.core.models.Snapshot.build`.  The ``sample_count`` on the
    returned snapshot equals the number of exchanges that had a non-``None``
    response body.

    :param key: Endpoint identifier (method, normalized path, status code).
    :param exchanges: Captured exchanges for this endpoint, typically drawn
        from the reservoir sampler buffer.
    :return: A :class:`~apinfer.core.models.Snapshot` with ``id=None``
        (not yet persisted to storage), or ``None`` if every exchange had
        an empty or non-JSON response body.
    :rtype: Snapshot | None
    """
    valid_bodies: list[JsonBody] = [
        e.response_body for e in exchanges if e.response_body is not None
    ]
    if not valid_bodies:
        return None
    schema = infer_schema(valid_bodies)
    return Snapshot.build(key=key, schema=schema, sample_count=len(valid_bodies))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_genson_schema(payloads: Sequence[JsonBody]) -> JsonSchema:
    """Run genson over all payloads and return a clean JSON Schema dict.

    :param payloads: Non-empty sequence of non-``None`` JSON values.
    :return: Raw JSON Schema produced by genson, with ``"$schema"`` stripped.
    :rtype: JsonSchema
    """
    builder = SchemaBuilder(schema_uri=None)  # suppress "$schema" key
    for payload in payloads:
        builder.add_object(payload)
    raw: dict[str, Any] = dict(builder.to_schema())
    raw.pop("$schema", None)  # defensive strip against genson version differences
    return raw


def _annotate_schema(schema: JsonSchema, payloads: list[JsonBody]) -> JsonSchema:
    """Dispatch presence-rate annotation based on schema structure.

    Routes to the appropriate annotator:

    - ``"properties"`` present → :func:`_annotate_object`
    - ``"items"`` present → :func:`_annotate_array`
    - ``"anyOf"`` present → :func:`_annotate_anyof`
    - Scalar / unrecognised structure → returned unchanged

    The ``"properties"`` check takes priority over ``"anyOf"`` because some
    genson versions may include both keys on the same schema node.

    :param schema: JSON Schema dict to annotate.
    :param payloads: JSON values that produced this schema.
    :return: Annotated copy of ``schema`` (or the original if no annotation
        applies).
    :rtype: JsonSchema
    """
    if "properties" in schema:
        obj_payloads: list[dict[str, Any]] = [
            p for p in payloads if isinstance(p, dict)
        ]
        return _annotate_object(schema, obj_payloads)
    if "items" in schema:
        arr_payloads: list[list[Any]] = [p for p in payloads if isinstance(p, list)]
        return _annotate_array(schema, arr_payloads)
    if "anyOf" in schema:
        return _annotate_anyof(schema, payloads)
    return schema


def _annotate_object(
    schema: JsonSchema,
    object_payloads: list[dict[str, Any]],
) -> JsonSchema:
    """Annotate object properties with conditional ``x-apinfer-presence-rate``.

    For each property the presence rate is::

        count(samples where property is present) / len(object_payloads)

    The denominator is the number of samples *at this nesting level* — i.e.
    the count of parent objects seen at this point in the recursion — not the
    global sample count.  This gives conditional semantics: "when the parent
    object is present, how often does this child field appear?"

    Recurses into nested structures via :func:`_annotate_schema`, passing only
    the *values* of each property as the next level's ``payloads``.  Each
    recursion therefore obtains its own conditional denominator.

    :param schema: JSON Schema dict with a ``"properties"`` key.
    :param object_payloads: Dict payloads matching this object schema.
    :return: Copy of ``schema`` with ``"properties"`` fully annotated.
    :rtype: JsonSchema
    """
    total = len(object_payloads)
    if total == 0 or "properties" not in schema:
        return schema

    annotated_properties: dict[str, JsonSchema] = {}

    for prop_name, prop_schema in schema["properties"].items():
        samples_with_prop = [p for p in object_payloads if prop_name in p]
        presence_rate = round(len(samples_with_prop) / total, 4)

        annotated_prop: JsonSchema = {**prop_schema, PRESENCE_RATE_KEY: presence_rate}

        # Values of this property become the next level's payload set.
        # Their count is the conditional denominator for the level below.
        nested_values: list[JsonBody] = [p[prop_name] for p in samples_with_prop]
        annotated_prop = _annotate_schema(annotated_prop, nested_values)

        annotated_properties[prop_name] = annotated_prop

    return {**schema, "properties": annotated_properties}


def _annotate_array(
    schema: JsonSchema,
    array_payloads: list[list[Any]],
) -> JsonSchema:
    """Annotate an array schema's items with conditional presence rates.

    All array elements from all response samples are flattened into a single
    pool.  The denominator for item-level presence rates is the total element
    count across all samples (not the number of responses).

    Example: three responses returning ``[{id:1}, {id:2}]``, ``[{id:3}]``,
    and ``[{id:4, name:"X"}]`` yield four items total.  ``id`` rate = 1.0,
    ``name`` rate = 0.25.

    :param schema: JSON Schema dict with an ``"items"`` key.
    :param array_payloads: List payloads at this nesting level.
    :return: Copy of ``schema`` with ``"items"`` annotated.
    :rtype: JsonSchema
    """
    items_schema = schema.get("items")
    if not items_schema or not isinstance(items_schema, dict):
        return schema

    all_items: list[JsonBody] = [item for arr in array_payloads for item in arr]
    if not all_items:
        return schema

    annotated_items = _annotate_schema(items_schema, all_items)
    return {**schema, "items": annotated_items}


def _annotate_anyof(
    schema: JsonSchema,
    payloads: list[JsonBody],
) -> JsonSchema:
    """Best-effort presence-rate annotation of ``anyOf`` schema branches.

    Each branch is annotated independently with the full payload set.
    Object branches (those with ``"properties"``) receive annotation;
    scalar and ``null`` branches have no ``"properties"`` and pass through
    unchanged.

    This is an approximation: payloads are not partitioned by which branch
    they satisfy.  For the most common genson ``anyOf`` case — an object
    type alongside a ``null`` type — the approximation is exact because the
    ``null`` branch has nothing to annotate.

    :param schema: JSON Schema dict with an ``"anyOf"`` key.
    :param payloads: Payload set that produced this schema.
    :return: Copy of ``schema`` with ``"anyOf"`` branches annotated.
    :rtype: JsonSchema
    """
    sub_schemas = schema.get("anyOf", [])
    if not isinstance(sub_schemas, list):
        return schema

    annotated: list[JsonSchema] = [
        _annotate_schema(sub, payloads) if isinstance(sub, dict) else sub
        for sub in sub_schemas
    ]
    return {**schema, "anyOf": annotated}
