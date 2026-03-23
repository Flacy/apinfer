"""Drift detection: compare two :class:`~apinfer.core.models.Snapshot` objects.

The detector is a pure function layer — it takes two snapshots and returns a
:class:`~apinfer.core.models.DriftReport` describing every detected change,
its structural category (:class:`~apinfer.core.models.ChangeKind`), and its
severity (:class:`~apinfer.core.models.DriftSeverity`).

Public surface::

    from apinfer.core.detector import detect_drift, detect_latest_drift
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apinfer.core.inferrer import PRESENCE_RATE_KEY
from apinfer.core.models import (
    ChangeKind,
    DriftChange,
    DriftReport,
    DriftSeverity,
    JsonSchema,
    Snapshot,
)

if TYPE_CHECKING:
    from apinfer.core.models import EndpointKey
    from apinfer.core.storage import SnapshotStorage

__all__ = [
    "detect_drift",
    "detect_latest_drift",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_drift(baseline: Snapshot, current: Snapshot) -> DriftReport:
    """Compare two snapshots for the same endpoint and produce a :class:`DriftReport`.

    The comparison is purely structural: schemas are walked recursively and
    each detected difference is recorded as a :class:`~apinfer.core.models.DriftChange`
    with an appropriate :class:`~apinfer.core.models.ChangeKind` and
    :class:`~apinfer.core.models.DriftSeverity`.

    If both snapshots share the same ``content_hash`` no comparison is
    performed and a clean (empty) report is returned immediately.

    :param baseline: The older snapshot — the reference contract.
    :param current: The newer snapshot — the observed state to compare against.
    :return: A :class:`DriftReport` containing all detected changes.
    :rtype: DriftReport
    """
    if baseline.content_hash == current.content_hash:
        return DriftReport(
            endpoint_key=current.key,
            baseline_id=baseline.id,
            current_id=current.id,
            changes=[],
        )

    changes = _compare_schemas(
        baseline.inferred_schema,
        current.inferred_schema,
        path="$",
    )
    return DriftReport(
        endpoint_key=current.key,
        baseline_id=baseline.id,
        current_id=current.id,
        changes=changes,
    )


def detect_latest_drift(
    key: EndpointKey,
    storage: SnapshotStorage,
) -> DriftReport | None:
    """Detect drift between the two most recent snapshots for *key*.

    Fetches the latest and the immediately preceding snapshot from *storage*
    and delegates to :func:`detect_drift`.  Returns ``None`` when fewer than
    two snapshots exist for the endpoint (i.e. there is no baseline to
    compare against).

    :param key: Endpoint identifier to look up in storage.
    :param storage: An open :class:`~apinfer.core.storage.SnapshotStorage`
        instance.
    :return: A :class:`DriftReport`, or ``None`` if no prior baseline exists.
    :rtype: DriftReport | None
    """
    current = storage.get_latest(key)
    if current is None or current.id is None:
        return None

    baseline = storage.get_previous(key, before_id=current.id)
    if baseline is None:
        return None

    return detect_drift(baseline, current)


# ---------------------------------------------------------------------------
# Private helpers — schema comparison
# ---------------------------------------------------------------------------


def _is_structural(schema: JsonSchema) -> bool:
    """Return ``True`` if *schema* describes a nested structure (object or array).

    A schema is considered structural when it carries a ``"properties"`` key
    (object) or an ``"items"`` key (array).  Pure primitives and ``anyOf``
    unions without these keys are non-structural.

    :param schema: A JSON Schema node dict.
    :return: ``True`` if the schema has ``"properties"`` or ``"items"``.
    :rtype: bool
    """
    return "properties" in schema or "items" in schema


def _extract_types(schema: JsonSchema) -> frozenset[str]:
    """Normalize a JSON Schema node to a frozenset of type strings.

    Handles all forms produced by genson + our annotation layer:

    - ``{"type": "string", ...}``  →  ``frozenset({"string"})``
    - ``{"type": ["string", "null"], ...}``  →  ``frozenset({"string", "null"})``
    - ``{"anyOf": [{"type": "string"}, {"type": "null"}], ...}``  →
      ``frozenset({"string", "null"})``
    - ``{}``  →  ``frozenset()``

    :param schema: A JSON Schema node dict.
    :return: A frozenset of JSON Schema primitive type strings.
    :rtype: frozenset[str]
    """
    match schema:
        case {"type": str(t)}:
            return frozenset({t})
        case {"type": [*types]}:
            return frozenset(t for t in types if isinstance(t, str))
        case {"anyOf": list(branches)}:
            result: set[str] = set()
            for branch in branches:
                if isinstance(branch, dict):
                    result |= _extract_types(branch)
            return frozenset(result)
        case _:
            return frozenset()


def _compare_schemas(
    baseline: JsonSchema,
    current: JsonSchema,
    path: str,
) -> list[DriftChange]:
    """Recursively compare two JSON Schema nodes and collect
    :class:`DriftChange` objects.

    Checks are applied in order:

    1. **Structural mismatch** — one side is an object/array, the other is not.
    2. **Primitive type change** — neither side is structural, but their
       normalized type sets differ.
    3. **Object recursion** — both sides have ``"properties"``.
    4. **Array recursion** — both sides have ``"items"``.
    5. **Presence-rate drift** — always attempted; returns ``[]`` if the rate
       key is absent from either side.

    :param baseline: The older schema node.
    :param current: The newer schema node.
    :param path: JSONPath-like string for this node, e.g. ``"$.properties.user"``.
    :return: List of :class:`DriftChange` objects detected at this level and below.
    :rtype: list[DriftChange]
    """
    changes: list[DriftChange] = []

    baseline_structural = _is_structural(baseline)
    current_structural = _is_structural(current)

    # 1. Structural nature changed (e.g. object → string)
    if baseline_structural != current_structural:
        b_label = _structural_label(baseline)
        c_label = _structural_label(current)
        changes.append(
            DriftChange(
                field_path=path,
                kind=ChangeKind.FIELD_TYPE_CHANGED,
                severity=DriftSeverity.BREAKING,
                description=(f"Field at '{path}' changed from {b_label} to {c_label}"),
                before=b_label,
                after=c_label,
            )
        )
        return changes

    # 2. Both non-structural — compare normalized type sets
    if not baseline_structural:
        b_types = _extract_types(baseline)
        c_types = _extract_types(current)
        if b_types and c_types and b_types != c_types:
            changes.append(
                DriftChange(
                    field_path=path,
                    kind=ChangeKind.FIELD_TYPE_CHANGED,
                    severity=DriftSeverity.BREAKING,
                    description=(
                        f"Field at '{path}' changed type: "
                        f"{sorted(b_types)} → {sorted(c_types)}"
                    ),
                    before=sorted(b_types),
                    after=sorted(c_types),
                )
            )
            return changes  # presence rate no longer meaningful

    # 3. Object recursion
    if "properties" in baseline and "properties" in current:
        changes.extend(_compare_object_properties(baseline, current, path))

    # 4. Array recursion
    elif "items" in baseline and "items" in current:
        changes.extend(_compare_array_items(baseline, current, path))

    # 5. Presence-rate drift (applies to every node where types match)
    changes.extend(_compare_presence_rate(baseline, current, path))

    return changes


def _compare_object_properties(
    baseline: JsonSchema,
    current: JsonSchema,
    path: str,
) -> list[DriftChange]:
    """Compare the ``properties`` dicts of two object schema nodes.

    Emits a change for every field that was added, removed, or recursively
    differs between the two schemas.

    :param baseline: Baseline object schema node (has ``"properties"``).
    :param current: Current object schema node (has ``"properties"``).
    :param path: JSONPath-like path to this object node.
    :return: List of :class:`DriftChange` objects.
    :rtype: list[DriftChange]
    """
    baseline_props: dict[str, JsonSchema] = baseline.get("properties", {})
    current_props: dict[str, JsonSchema] = current.get("properties", {})
    baseline_required: set[str] = set(baseline.get("required", []))

    baseline_keys = set(baseline_props)
    current_keys = set(current_props)
    changes: list[DriftChange] = []

    # Fields present in baseline but absent in current (removed)
    for field in sorted(baseline_keys - current_keys):
        prop_schema = baseline_props[field]
        prop_path = f"{path}.properties.{field}"
        rate = prop_schema.get(PRESENCE_RATE_KEY)
        was_required = field in baseline_required or rate == 1.0
        changes.append(
            DriftChange(
                field_path=prop_path,
                kind=ChangeKind.FIELD_REMOVED,
                severity=(
                    DriftSeverity.BREAKING
                    if was_required
                    else DriftSeverity.NON_BREAKING
                ),
                description=_describe_removed(field, path, was_required),
                before=prop_schema,
                after=None,
            )
        )

    # Fields absent in baseline but present in current (added)
    for field in sorted(current_keys - baseline_keys):
        prop_schema = current_props[field]
        prop_path = f"{path}.properties.{field}"
        changes.append(
            DriftChange(
                field_path=prop_path,
                kind=ChangeKind.FIELD_ADDED,
                severity=DriftSeverity.NON_BREAKING,
                description=_describe_added(field, path),
                before=None,
                after=prop_schema,
            )
        )

    # Fields present in both — recurse
    for field in sorted(baseline_keys & current_keys):
        prop_path = f"{path}.properties.{field}"
        changes.extend(
            _compare_schemas(
                baseline_props[field],
                current_props[field],
                path=prop_path,
            )
        )

    return changes


def _compare_array_items(
    baseline: JsonSchema,
    current: JsonSchema,
    path: str,
) -> list[DriftChange]:
    """Recurse into the ``items`` sub-schema of two array schema nodes.

    :param baseline: Baseline array schema node (has ``"items"``).
    :param current: Current array schema node (has ``"items"``).
    :param path: JSONPath-like path to this array node.
    :return: List of :class:`DriftChange` objects from the items sub-schema.
    :rtype: list[DriftChange]
    """
    baseline_items = baseline.get("items")
    current_items = current.get("items")

    # Tuple-validation schemas (items as list) are not produced by genson — skip
    if not isinstance(baseline_items, dict) or not isinstance(current_items, dict):
        return []

    return _compare_schemas(
        baseline_items,
        current_items,
        path=f"{path}[]",
    )


def _compare_presence_rate(
    baseline: JsonSchema,
    current: JsonSchema,
    path: str,
) -> list[DriftChange]:
    """Detect :attr:`~ChangeKind.FIELD_PRESENCE_RATE_CHANGED` or
    :attr:`~ChangeKind.FIELD_OPTIONALITY_CHANGED` between two schema nodes.

    Returns an empty list when either node lacks the ``x-apinfer-presence-rate``
    annotation or when both rates are equal.

    :param baseline: Baseline schema node.
    :param current: Current schema node.
    :param path: JSONPath-like path to this node.
    :return: Zero or one :class:`DriftChange` for the presence rate.
    :rtype: list[DriftChange]
    """
    baseline_rate: float | None = baseline.get(PRESENCE_RATE_KEY)
    current_rate: float | None = current.get(PRESENCE_RATE_KEY)

    if baseline_rate is None or current_rate is None:
        return []
    if baseline_rate == current_rate:
        return []

    baseline_full = baseline_rate == 1.0
    current_full = current_rate == 1.0

    if baseline_full != current_full:
        direction = "became optional" if baseline_full else "became required"
        return [
            DriftChange(
                field_path=path,
                kind=ChangeKind.FIELD_OPTIONALITY_CHANGED,
                severity=DriftSeverity.INFORMATIONAL,
                description=(
                    f"Field at '{path}' {direction} "
                    f"(presence rate: {baseline_rate} → {current_rate})"
                ),
                before=baseline_rate,
                after=current_rate,
            )
        ]

    return [
        DriftChange(
            field_path=path,
            kind=ChangeKind.FIELD_PRESENCE_RATE_CHANGED,
            severity=DriftSeverity.INFORMATIONAL,
            description=(
                f"Field at '{path}' presence rate changed: "
                f"{baseline_rate} → {current_rate}"
            ),
            before=baseline_rate,
            after=current_rate,
        )
    ]


# ---------------------------------------------------------------------------
# Private helpers — descriptions and labels
# ---------------------------------------------------------------------------


def _structural_label(schema: JsonSchema) -> str:
    """Return a human-readable type label for a structural or primitive schema.

    Used when generating descriptions for :attr:`~ChangeKind.FIELD_TYPE_CHANGED`
    events involving structural mismatches.

    :param schema: A JSON Schema node dict.
    :return: A label string such as ``"object"``, ``"array"``, or
        ``"['integer', 'string']"``.
    :rtype: str
    """
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    types = _extract_types(schema)
    return str(sorted(types)) if types else "unknown"


def _describe_removed(field: str, parent_path: str, was_required: bool) -> str:
    """Human-readable description for a removed field.

    :param field: Name of the removed property.
    :param parent_path: JSONPath to the parent object.
    :param was_required: Whether the field was required in the baseline.
    :return: Description string.
    :rtype: str
    """
    req_label = "Required" if was_required else "Optional"
    return f"{req_label} field '{field}' was removed from '{parent_path}'"


def _describe_added(field: str, parent_path: str) -> str:
    """Human-readable description for an added field.

    :param field: Name of the added property.
    :param parent_path: JSONPath to the parent object.
    :return: Description string.
    :rtype: str
    """
    return f"New field '{field}' appeared in '{parent_path}'"
