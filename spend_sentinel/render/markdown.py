"""Markdown verdict renderer (R20). A self-contained, PR-comment-ready report.

Takes the finished :class:`~spend_sentinel.core.models.Verdict` only — no
business logic here (Modularity notes), which keeps the output golden-file
testable.

Security (spec, Markdown injection): every plan/state-derived string placed in
the document (addresses, types, attributes, values, rule messages, meta
strings) passes through :func:`_escape`, which neutralizes pipes, backticks,
and HTML-significant characters and replaces control characters, so a crafted
resource name cannot inject markup, break a table, or spoof the verdict line.
Sensitive drift values arrive already masked as ``(sensitive)`` from the
detector and render as-is.

Size (R20): the cost breakdown truncates past 50 rows with an "…and N more"
line; the unpriced, drift, skipped, and error lists get the same 50-row cap so
a 500-change plan stays well under the 65,536-character budget.
"""

from __future__ import annotations

import json
from typing import Any

from spend_sentinel.core.models import DriftStatus, Verdict

_MAX_ROWS = 50
_MAX_VALUE_CHARS = 120

_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "|": "&#124;",
    "`": "&#96;",
}


def _escape(text: str) -> str:
    """Neutralize Markdown/HTML-significant and control characters."""
    escaped = []
    for ch in text:
        if ch in _ESCAPES:
            escaped.append(_ESCAPES[ch])
        elif ch.isprintable():
            escaped.append(ch)
        else:
            escaped.append(" ")
    return "".join(escaped)


def _value(value: Any) -> str:
    """Render a drift value compactly, escaped and length-capped."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > _MAX_VALUE_CHARS:
        text = text[: _MAX_VALUE_CHARS - 1] + "…"
    return _escape(text)


def _truncation_line(total: int, shown: int) -> list[str]:
    if total > shown:
        return [f"…and {total - shown} more"]
    return []


def render_md(verdict: Verdict) -> str:
    """Render the R20 PR comment (trailing newline)."""
    lines: list[str] = []

    # One-line, emoji-free verdict header (R20).
    lines.append(f"Verdict: {verdict.verdict.value}")
    lines.append("")

    s = verdict.summary
    lines.append(
        f"Changes: {s.created} created, {s.deleted} deleted, "
        f"{s.updated} updated, {s.replaced} replaced ({s.changed} total)"
    )
    lines.append("")

    # Cost table (R20) and unpriced list (R7).
    lines.append("## Cost")
    lines.append("")
    lines.append(f"Monthly delta: ${verdict.cost.monthly_delta_usd:.2f}")
    lines.append("")
    if verdict.cost.breakdown:
        lines.append("| Resource | Action | Monthly delta (USD) |")
        lines.append("| --- | --- | ---: |")
        shown = verdict.cost.breakdown[:_MAX_ROWS]
        for line in shown:
            lines.append(
                f"| {_escape(line.address)} | {line.action.value} "
                f"| {line.monthly_delta_usd:.2f} |"
            )
        lines.extend(_truncation_line(len(verdict.cost.breakdown), len(shown)))
        lines.append("")
    if verdict.cost.unpriced:
        lines.append(f"{len(verdict.cost.unpriced)} unpriced resources:")
        lines.append("")
        shown_unpriced = verdict.cost.unpriced[:_MAX_ROWS]
        for u in shown_unpriced:
            lines.append(f"- {_escape(u.address)} ({_escape(u.type)}): {u.reason.value}")
        lines.extend(_truncation_line(len(verdict.cost.unpriced), len(shown_unpriced)))
        lines.append("")

    # Drift table only when drift ran (R20).
    if verdict.drift.status is DriftStatus.RAN:
        lines.append("## Drift")
        lines.append("")
        if verdict.drift.drifts:
            lines.append("| Resource | Kind | Attribute | State value | Live value |")
            lines.append("| --- | --- | --- | --- | --- |")
            shown_drifts = verdict.drift.drifts[:_MAX_ROWS]
            for d in shown_drifts:
                attribute = _escape(d.attribute) if d.attribute is not None else "-"
                lines.append(
                    f"| {_escape(d.address)} | {d.kind.value} | {attribute} "
                    f"| {_value(d.state_value)} | {_value(d.live_value)} |"
                )
            lines.extend(_truncation_line(len(verdict.drift.drifts), len(shown_drifts)))
        else:
            lines.append("No drift detected.")
        lines.append("")
        if verdict.drift.skipped:
            lines.append(f"{len(verdict.drift.skipped)} resource(s) skipped:")
            lines.append("")
            shown_skipped = verdict.drift.skipped[:_MAX_ROWS]
            for sk in shown_skipped:
                lines.append(f"- {_escape(sk.address)} ({_escape(sk.type)}): {_escape(sk.reason)}")
            lines.extend(_truncation_line(len(verdict.drift.skipped), len(shown_skipped)))
            lines.append("")
        if verdict.drift.errors:
            lines.append(f"{len(verdict.drift.errors)} read error(s):")
            lines.append("")
            shown_errors = verdict.drift.errors[:_MAX_ROWS]
            for e in shown_errors:
                lines.append(f"- {_escape(e.address)}: {_escape(e.error)}")
            lines.extend(_truncation_line(len(verdict.drift.errors), len(shown_errors)))
            lines.append("")

    # Policy results table (R20).
    lines.append("## Policy")
    lines.append("")
    lines.append("| Rule | Result | Detail |")
    lines.append("| --- | --- | --- |")
    for r in verdict.policy:
        lines.append(f"| {_escape(r.name)} | {r.result.value} | {_escape(r.message)} |")
    lines.append("")

    m = verdict.meta
    lines.append(
        f"spend-sentinel {_escape(m.tool_version)} — pricing snapshot "
        f"{_escape(m.pricing_snapshot_version)} ({_escape(m.pricing_snapshot_date)}) "
        f"— region {_escape(m.region)}"
    )
    return "\n".join(lines) + "\n"
