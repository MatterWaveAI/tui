"""The model picker's rows, grouped the way the web picker groups them.

Pure data so the gating -- a model the plan does not include is shown with
its upgrade path and cannot be chosen -- is testable without a screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The web picker's headings (web/src/lib/billing.ts, ModelPicker.tsx).
GRT_HEADING = "GRT models"
PARTNER_HEADING = "Partner models"
PREVIEW_HEADING = "Research preview — not for real use"


@dataclass(frozen=True)
class PickerRow:
    """A heading or a model. Only a model row with `selectable` can be chosen."""

    label: str
    model_id: str | None = None
    selectable: bool = False

    @property
    def is_heading(self) -> bool:
        return self.model_id is None


def format_context_window(window: Any) -> str | None:
    """`32768` -> `32k`, `2000000` -> `2M`; unknown stays unknown."""
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        return None
    if window >= 1_000_000 and window % 1_000_000 == 0:
        return f"{window // 1_000_000}M"
    if window >= 1_000:
        return f"{round(window / 1_000)}k"
    return str(window)


def group_models(
    models: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """GRT, partner, research preview -- each in the order the API gave.
    A research preview goes in its group whatever else it is."""
    grt: list[dict[str, Any]] = []
    partners: list[dict[str, Any]] = []
    previews: list[dict[str, Any]] = []
    for model in models:
        if model.get("research_preview"):
            previews.append(model)
        elif model.get("partner"):
            partners.append(model)
        else:
            grt.append(model)
    return grt, partners, previews


def _row(model: dict[str, Any]) -> PickerRow:
    model_id = str(model.get("id", ""))
    label = str(model.get("label") or model_id)
    window = format_context_window(model.get("context_window"))
    text = f"{label}  ({window} context)" if window else label
    # `included` absent means an API that predates plan gating: everything is.
    included = model.get("included", True) is not False
    if not included:
        upgrade = model.get("upgrade_to")
        text += f"  — on {upgrade}" if upgrade else "  — not on your plan"
    return PickerRow(text, model_id, included)


def picker_rows(models: list[dict[str, Any]]) -> list[PickerRow]:
    rows: list[PickerRow] = []
    for heading, group in zip(
        (GRT_HEADING, PARTNER_HEADING, PREVIEW_HEADING), group_models(models), strict=True
    ):
        if not group:
            continue
        rows.append(PickerRow(heading))
        rows.extend(_row(m) for m in group)
    return rows


def model_window(models: list[dict[str, Any]], model_id: str | None) -> int | None:
    for model in models:
        if model.get("id") == model_id:
            window = model.get("context_window")
            return window if isinstance(window, int) else None
    return None
