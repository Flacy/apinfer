"""
CLI entry point for apinfer.

Three commands:
- ``apinfer status``  — quick overview of all observed endpoints
- ``apinfer report``  — inferred schema per endpoint as a field table
- ``apinfer diff``    — drift changes between the two most recent snapshots

Entry point registered in pyproject.toml as ``apinfer = "apinfer.output.cli:app"``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from apinfer.core.detector import detect_latest_drift
from apinfer.core.inferrer import PRESENCE_RATE_KEY
from apinfer.core.models import (
    ChangeKind,
    DriftChange,
    DriftSeverity,
    EndpointKey,
    HttpMethod,
    Snapshot,
)
from apinfer.core.storage import DEFAULT_DB_FILENAME, SnapshotStorage, default_db_path

__all__ = ["app"]

app = typer.Typer(name="apinfer", no_args_is_help=True, add_completion=False)
console = Console()
err_console = Console(stderr=True)

DbPath = Annotated[
    Path | None,
    typer.Option(
        "--db",
        help=f"Path to apinfer.db (default: ./{DEFAULT_DB_FILENAME})",
        show_default=False,
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_storage(db: Path | None) -> SnapshotStorage:
    """Resolve db path, verify it exists, return a SnapshotStorage.

    Exits with code 1 and a helpful message if the file is not found.
    Must check existence *before* constructing SnapshotStorage, because
    sqlite-utils creates the file on construction.
    """
    path = db if db is not None else default_db_path()
    if not path.exists():
        err_console.print(
            f"[red]No database found at [bold]{path}[/bold].[/]\n"
            "[dim]Run your application with the apinfer middleware first, "
            "then try again.[/]"
        )
        raise typer.Exit(code=1)
    return SnapshotStorage(path)


def _relative_time(ts: datetime) -> str:
    """Return a human-readable relative time string, e.g. '3h ago'."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = datetime.now(tz=UTC) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _severity_style(severity: DriftSeverity) -> str:
    match severity:
        case DriftSeverity.BREAKING:
            return "bold red"
        case DriftSeverity.NON_BREAKING:
            return "yellow"
        case DriftSeverity.INFORMATIONAL:
            return "dim"


def _severity_icon(change: DriftChange) -> str:
    match change.kind:
        case (
            ChangeKind.FIELD_REMOVED
            | ChangeKind.STATUS_CODE_REMOVED
            | ChangeKind.FIELD_TYPE_CHANGED
        ):
            return "✗"
        case ChangeKind.FIELD_ADDED | ChangeKind.STATUS_CODE_ADDED:
            return "+"
        case _:
            return "~"


def _method_style(method: HttpMethod) -> str:
    match method:
        case HttpMethod.GET:
            return "green"
        case HttpMethod.POST:
            return "blue"
        case HttpMethod.PUT | HttpMethod.PATCH:
            return "yellow"
        case HttpMethod.DELETE:
            return "red"
        case _:
            return "white"


def _status_code_style(code: int) -> str:
    if 200 <= code < 300:
        return "green"
    if 300 <= code < 400:
        return "cyan"
    if 400 <= code < 500:
        return "yellow"
    if code >= 500:
        return "red"
    return "white"


def _extract_type_label(schema: dict) -> str:  # type: ignore[type-arg]
    """Return a short, readable type string for a JSON Schema node."""
    if not isinstance(schema, dict):
        return "any"
    if "properties" in schema:
        return "object"
    if "items" in schema:
        items = schema["items"]
        item_label = _extract_type_label(items) if isinstance(items, dict) else "any"
        return f"array[{item_label}]"
    match schema:
        case {"type": str(t)}:
            return t
        case {"type": [*types]}:
            return " | ".join(str(t) for t in types)
        case {"anyOf": [*branches]}:
            non_null = [
                b for b in branches if isinstance(b, dict) and b.get("type") != "null"
            ]
            labels = (
                [_extract_type_label(b) for b in non_null] if non_null else ["null"]
            )
            return " | ".join(labels)
        case _:
            return "any"


def _flatten_schema(
    schema: dict,  # type: ignore[type-arg]
    prefix: str = "",
    _depth: int = 0,
) -> list[tuple[str, str, str]]:
    """Recursively flatten a JSON Schema into (field_path, type, presence%) rows."""
    if _depth > 10 or not isinstance(schema, dict) or "properties" not in schema:
        return []

    rows: list[tuple[str, str, str]] = []
    for field_name, field_schema in schema.get("properties", {}).items():
        if not isinstance(field_schema, dict):
            continue
        path = f"{prefix}.{field_name}" if prefix else field_name
        type_str = _extract_type_label(field_schema)
        rate = field_schema.get(PRESENCE_RATE_KEY)
        presence = f"{rate * 100:.0f}%" if isinstance(rate, float) else "—"
        rows.append((path, type_str, presence))

        # Recurse into nested objects
        if "properties" in field_schema:
            rows.extend(_flatten_schema(field_schema, prefix=path, _depth=_depth + 1))
        elif (
            "items" in field_schema
            and isinstance(field_schema["items"], dict)
            and "properties" in field_schema["items"]
        ):
            rows.extend(
                _flatten_schema(
                    field_schema["items"], prefix=f"{path}[]", _depth=_depth + 1
                )
            )

    return rows


def _resolve_keys(
    storage: SnapshotStorage,
    method: str | None,
    path: str | None,
    status: int | None,
) -> list[EndpointKey]:
    """Return the list of keys to operate on.

    If any of method/path/status is provided, all three must be provided.
    Otherwise, returns all keys sorted by (path, method, status_code).
    """
    given = sum(x is not None for x in [method, path, status])
    if 0 < given < 3:
        err_console.print(
            "[red]--method, --path, and --status must all be provided together.[/]"
        )
        raise typer.Exit(code=1)

    if given == 3:
        assert method is not None and path is not None and status is not None
        try:
            key = EndpointKey(
                method=HttpMethod(method.upper()), path=path, status_code=status
            )
        except ValueError as e:
            err_console.print(f"[red]Invalid HTTP method: {method!r}[/]")
            raise typer.Exit(code=1) from e
        if storage.get_latest(key) is None:
            err_console.print(
                f"[red]No data found for {method.upper()} {path} {status}.[/]"
            )
            raise typer.Exit(code=1)
        return [key]

    return sorted(
        storage.list_keys(),
        key=lambda k: (k.path, k.method.value, k.status_code),
    )


def _endpoint_rule(key: EndpointKey, snapshot: Snapshot | None = None) -> None:
    """Print a Rich rule with endpoint details as the section header."""
    title = (
        f"[bold {_method_style(key.method)}]{key.method.value}[/]  "
        f"[bold]{key.path}[/]  "
        f"[{_status_code_style(key.status_code)}]{key.status_code}[/]"
    )
    if snapshot is not None:
        n = snapshot.sample_count
        title += (
            f"  [dim]·  {n} sample{'s' if n != 1 else ''}  ·  "
            f"{_relative_time(snapshot.timestamp)}[/]"
        )
    console.rule(title, align="left")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def status(db: DbPath = None) -> None:
    """Show a quick overview of all observed endpoints."""
    storage = _open_storage(db)
    keys = sorted(
        storage.list_keys(),
        key=lambda k: (k.path, k.method.value, k.status_code),
    )

    if not keys:
        console.print(
            "[dim]No endpoints observed yet. "
            "Run your application with the middleware first.[/]"
        )
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True, highlight=False)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Method", no_wrap=True)
    table.add_column("Path")
    table.add_column("Status", justify="right", no_wrap=True)
    table.add_column("Samples", justify="right", no_wrap=True)
    table.add_column("Last seen", justify="right", no_wrap=True)
    table.add_column("Drift", no_wrap=True)

    for key in keys:
        snapshot = storage.get_latest(key)
        drift_report = detect_latest_drift(key, storage)

        if drift_report is None:
            icon = Text("—", style="dim")
            drift_cell = Text("no history", style="dim")
        elif drift_report.is_clean:
            icon = Text("✓", style="green")
            drift_cell = Text("clean", style="green")
        elif drift_report.has_breaking_changes:
            icon = Text("✗", style="bold red")
            n = len(drift_report.changes)
            drift_cell = Text(f"{n} change{'s' if n != 1 else ''}", style="bold red")
        else:
            icon = Text("⚠", style="yellow")
            n = len(drift_report.changes)
            drift_cell = Text(f"{n} change{'s' if n != 1 else ''}", style="yellow")

        method_cell = Text(key.method.value, style=_method_style(key.method))
        status_cell = Text(
            str(key.status_code), style=_status_code_style(key.status_code)
        )

        if snapshot is not None:
            samples = str(snapshot.sample_count)
            last_seen = _relative_time(snapshot.timestamp)
        else:
            samples = "—"
            last_seen = "—"

        table.add_row(
            icon, method_cell, key.path, status_cell, samples, last_seen, drift_cell
        )

    console.print(table)


@app.command()
def report(
    db: DbPath = None,
    method: Annotated[
        str | None,
        typer.Option("--method", "-m", help="HTTP method (e.g. GET)"),
    ] = None,
    path: Annotated[
        str | None,
        typer.Option("--path", "-p", help="Path pattern (e.g. /users/{id})"),
    ] = None,
    status: Annotated[
        int | None,
        typer.Option("--status", "-s", help="HTTP status code (e.g. 200)"),
    ] = None,
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Print raw JSON schema instead of field table"),
    ] = False,
) -> None:
    """Show inferred schema for all (or one) endpoint(s)."""
    storage = _open_storage(db)
    keys = _resolve_keys(storage, method, path, status)

    if not keys:
        console.print(
            "[dim]No endpoints observed yet. "
            "Run your application with the middleware first.[/]"
        )
        return

    for key in keys:
        snapshot = storage.get_latest(key)
        _endpoint_rule(key, snapshot)

        if snapshot is None:
            console.print("  [dim]No snapshot yet.[/]")
            console.print()
            continue

        if raw:
            console.print(
                Syntax(
                    json.dumps(snapshot.inferred_schema, indent=2),
                    "json",
                    theme="ansi_dark",
                )
            )
            console.print()
            continue

        rows = _flatten_schema(snapshot.inferred_schema)

        if not rows:
            console.print("  [dim]Empty schema (no JSON response body observed).[/]")
            console.print()
            continue

        tbl = Table(box=box.SIMPLE, show_edge=False, show_header=True, padding=(0, 1))
        tbl.add_column("Field")
        tbl.add_column("Type")
        tbl.add_column("Presence", justify="right")

        for field_path, type_str, presence in rows:
            style = "dim" if presence not in ("100%", "—") else ""
            tbl.add_row(field_path, type_str, presence, style=style)

        console.print(tbl)
        console.print()


@app.command()
def diff(
    db: DbPath = None,
    method: Annotated[
        str | None,
        typer.Option("--method", "-m", help="HTTP method (e.g. GET)"),
    ] = None,
    path: Annotated[
        str | None,
        typer.Option("--path", "-p", help="Path pattern (e.g. /users/{id})"),
    ] = None,
    status: Annotated[
        int | None,
        typer.Option("--status", "-s", help="HTTP status code (e.g. 200)"),
    ] = None,
) -> None:
    """Show drift changes between the two most recent snapshots."""
    storage = _open_storage(db)
    keys = _resolve_keys(storage, method, path, status)

    if not keys:
        console.print(
            "[dim]No endpoints observed yet. "
            "Run your application with the middleware first.[/]"
        )
        return

    for key in keys:
        snapshot = storage.get_latest(key)
        _endpoint_rule(key, snapshot)

        drift_report = detect_latest_drift(key, storage)

        if drift_report is None:
            console.print("  [dim]no history yet — need at least 2 snapshots[/]")
            console.print()
            continue

        if drift_report.is_clean:
            console.print("  [green]✓ clean[/]")
            console.print()
            continue

        for change in drift_report.changes:
            icon = _severity_icon(change)
            style = _severity_style(change.severity)
            line = Text()
            line.append(f"  {icon}  ", style=style)
            line.append(change.field_path, style=style)
            console.print(line)
            console.print(f"     [dim]{change.description}[/]")

        console.print()
