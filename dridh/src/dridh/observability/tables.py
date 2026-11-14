"""The observability report as readable tables, for ``dridh observe`` and the OpenCode panel alike.

``collect()`` gathers raw figures; this module turns them into small tables with plain values: "3 min ago" instead of
seconds, "1.2M" tokens, "$0.42", and "–" where there is nothing to show, never ``None`` or ``{}``. The same tables are
printed in a terminal and sent to the panel as JSON (rows plus the text already laid out), so both always agree.
"""

from __future__ import annotations

from typing import Any

DASH = "–"
NOT_COUNTED = ("The Coordinator's own conversation runs in OpenCode, not through Dridh, so it is not counted here; "
               "`opencode stats --project .` shows it.")


def ago(seconds: int | float | None) -> str:
    """``95`` -> ``1 min ago``; None -> ``–``."""
    if seconds is None:
        return DASH
    return "just now" if seconds < 60 else f"{duration(seconds)} ago"


def duration(seconds: int | float | None) -> str:
    """``42`` -> ``42s``, ``3900`` -> ``1 h 5 min``, ``200000`` -> ``2 days``."""
    if seconds is None:
        return DASH
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    return f"{hours // 24} days"


def tokens(value: int | float | None) -> str:
    """``1234567`` -> ``1.2M``, ``34500`` -> ``34.5k``, ``812`` -> ``812``."""
    if not value:
        return "0"
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if value >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(value))


def cost(value: float | None) -> str:
    """Cheap models cost fractions of a cent, so four decimals below a dollar: ``$0.0123``, ``$1.25``."""
    if not value:
        return "$0"
    if value >= 1:
        return f"${value:.2f}"
    text = f"{value:.4f}".rstrip("0")
    return "$" + (text if len(text.split(".")[1]) >= 2 else f"{value:.2f}")


def _label(value: str) -> str:
    """``IN_PROGRESS`` -> ``in progress``."""
    return value.replace("_", " ").lower()


def _counts_row(counts: dict[str, int] | None) -> str:
    return ", ".join(f"{_label(k)} {v}" for k, v in (counts or {}).items()) or DASH


def table(title: str, columns: list[str], rows: list[list[Any]], *, numeric: tuple[int, ...] = (), note: str = "") -> dict[str, Any]:
    """One table: cells as text, the numeric columns right-aligned when laid out."""
    cells = [[DASH if cell in (None, "") else str(cell) for cell in row] for row in rows]
    return {"title": title, "columns": columns, "rows": cells, "numeric": list(numeric), "note": note}


def layout(item: dict[str, Any]) -> list[str]:
    """A table as aligned text lines: a header, a rule and one line per row (fits a terminal and the panel)."""
    columns, rows, numeric = item["columns"], item["rows"], set(item["numeric"])
    widths = [max(len(str(value)) for value in [column, *(row[i] for row in rows)]) for i, column in enumerate(columns)]

    def line(values: list[str]) -> str:
        return "  ".join(value.rjust(widths[i]) if i in numeric else value.ljust(widths[i]) for i, value in enumerate(values)).rstrip()

    out = [line(columns), "  ".join("─" * width for width in widths)]
    out += [line(row) for row in rows] if rows else ["(nothing yet)"]
    return out


def _usage_tables(usage: dict[str, Any]) -> list[dict[str, Any]]:
    if "unavailable" in usage:
        return [table("Model usage", ["Status"], [["unavailable: " + str(usage["unavailable"])]])]
    if not usage.get("recorded"):
        return [table("Model usage", ["Status"], [["no agent runs recorded yet"]], note=NOT_COUNTED)]
    columns = ["Runs", "Failed", "Tokens", "Input", "Output", "Cache read", "Cost", "Time"]

    def row(name: str, bucket: dict[str, Any]) -> list[Any]:
        return [name, bucket["invocations"], bucket["failed"], tokens(bucket["total_tokens"]), tokens(bucket["input"]),
                tokens(bucket["output"]), tokens(bucket["cache_read"]), cost(bucket["cost"]), duration(bucket["duration_seconds"])]

    numeric = tuple(range(1, 9))
    by_stage = [row(_label(name), bucket) for name, bucket in usage["by_stage"].items()] + [row("all agent runs", usage)]
    tables = [table("Model usage by stage", ["Stage", *columns], by_stage, numeric=numeric, note=NOT_COUNTED)]
    if usage["by_model"]:
        tables.append(table("Model usage by model", ["Model", *columns], [row(name, b) for name, b in usage["by_model"].items()], numeric=numeric))
    if usage["by_story"]:
        tables.append(table("Model usage by story", ["Story", *columns], [row(name, b) for name, b in usage["by_story"].items()], numeric=numeric))
    return tables


def build(report: dict[str, Any]) -> dict[str, Any]:
    """``{summary, tables}``: a one-line summary for the panel's overview and every table, in reading order."""
    tables: list[dict[str, Any]] = []
    life = report.get("lifecycle")
    if isinstance(life, list):
        tables.append(table("Features", ["Feature", "Stage", "Status", "In this stage since", "Moves"],
                            [[f["slug"], _label(f["stage"]), _label(f["status"]), ago(f["seconds_in_stage"]), f["transitions"]] for f in life],
                            numeric=(4,)))
    ex = report.get("execution") or {}
    if "unavailable" not in ex:
        stories = ex.get("stories") or {}
        total = sum(stories.values())
        tables.append(table("Execution", ["Measure", "Value"], [
            ["Stories", f"{stories.get('COMPLETED', 0)} of {total} completed" if total else DASH],
            ["By status", _counts_row(stories)],
            ["Repairs", ex.get("repairs", 0)],
            ["Runs", _counts_row(ex.get("runs"))],
            ["Checkpoint lots", _counts_row(ex.get("lots"))],
        ]))
    tables += _usage_tables(report.get("usage") or {})
    rv, de, ledger, jev = (report.get(k) or {} for k in ("reviews", "delivery", "ledger", "jev"))
    quality = []
    if "unavailable" not in rv:
        quality += [["Reviews pending", rv.get("pending", 0)], ["Reviews approved", rv.get("approved", 0)],
                    ["Stale documents", rv.get("stale_documents", 0)]]
    if "unavailable" not in ledger:
        quality += [["Ledger entries", _counts_row(ledger.get("entries")) if ledger.get("imported") else "no ledger yet"],
                    ["Deviations", ledger.get("deviations", 0)]]
    if "unavailable" not in jev:
        quality += [["Jev evaluations", _counts_row(jev.get("evaluations"))], ["Jev fallbacks", jev.get("fallbacks", 0)]]
    if "unavailable" not in de:
        quality += [["Pull requests", _counts_row(de.get("pull_requests"))], ["Blocked delivery gates", de.get("blocked_dependency_gates", 0)]]
    tables.append(table("Reviews, deviations and delivery", ["Measure", "Value"], quality))
    cr, bridge = report.get("code_reacher") or {}, report.get("bridge") or {}
    system = []
    if "unavailable" not in cr:
        if cr.get("indexed"):
            system += [["Code index", f"{cr['indexed_files']} of {cr['files']} files, built in {duration(cr['index_seconds'])}"],
                       ["Indexed", ago(cr["age_seconds"]) + (" (stale: run `dridh index`)" if cr.get("stale") else "")],
                       ["Failed index runs", cr["failed_runs"]]]
        else:
            system.append(["Code index", "not indexed"])
    if "unavailable" not in bridge:
        latency = bridge.get("latency_ms") or {}
        system += [["Bridge calls", f"{bridge.get('sampled', 0)} ({bridge.get('failed', 0)} failed)"],
                   ["Bridge latency", f"p50 {latency.get('p50')} ms, p95 {latency.get('p95')} ms" if latency.get("p50") is not None else DASH]]
    tables.append(table("System", ["Measure", "Value"], system))
    missing = [[_label(name), "unavailable: " + str(section["unavailable"])] for name, section in report.items()
               if isinstance(section, dict) and "unavailable" in section]
    if missing:  # a section that could not be computed is reported, never silently left out
        tables.append(table("Unavailable", ["Section", "Why"], missing))
    for item in tables:
        item["lines"] = layout(item)
    return {"summary": summary(report), "tables": tables}


def summary(report: dict[str, Any]) -> str:
    """One line for the panel: stories, repairs, agent runs, tokens and cost; empty when there is nothing yet."""
    parts = []
    stories = (report.get("execution") or {}).get("stories") or {}
    if stories:
        parts.append(f"{stories.get('COMPLETED', 0)}/{sum(stories.values())} stories done")
        repairs = (report.get("execution") or {}).get("repairs", 0)
        parts.append(f"{repairs} repair{'s' if repairs != 1 else ''}")
    usage = report.get("usage") or {}
    if usage.get("recorded"):
        parts += [f"{usage['invocations']} agent run{'s' if usage['invocations'] != 1 else ''}", f"{tokens(usage['total_tokens'])} tokens",
                  cost(usage["cost"])]
    return " · ".join(parts)


def render(report: dict[str, Any]) -> str:
    """The whole report as text tables, for a terminal."""
    built = build(report)
    head = f"Dridh {report['dridh_version']} · workspace {report['workspace']['id'][:8]} ({report['workspace']['mode']})"
    out = [head + (f"\n{built['summary']}" if built["summary"] else "")]
    for item in built["tables"]:
        out.append(f"\n{item['title']}")
        out += ["  " + line for line in item["lines"]]
        if item["note"]:
            out.append(f"  {item['note']}")
    return "\n".join(out)
