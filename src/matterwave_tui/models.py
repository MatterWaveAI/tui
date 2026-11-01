"""The model picker's rows, grouped the way the web picker groups them.

Pure data so the gating -- a model the plan does not include is shown with
its upgrade path and cannot be chosen -- is testable without a screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from matterwave_sdk.models import vision_of

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


# --- what an answer cost ----------------------------------------------------------


def answer_usage(
    models: list[dict[str, Any]],
    model_id: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> str | None:
    """The line under an answer: tokens in and out, and what they come to
    at the model's list price per million (`/api/models`), when the list
    has one. None when the turn reported no tokens."""
    if prompt_tokens is None and completion_tokens is None:
        return None
    tokens_in, tokens_out = prompt_tokens or 0, completion_tokens or 0
    line = f"{tokens_in:,} tokens in · {tokens_out:,} out"
    row = next((m for m in models if m.get("id") == model_id), None)
    price_in = row.get("input_usd_per_m") if row else None
    price_out = row.get("output_usd_per_m") if row else None
    priced = isinstance(price_in, int | float) and isinstance(price_out, int | float)
    if priced and (price_in or price_out):
        cost = (tokens_in * float(price_in or 0) + tokens_out * float(price_out or 0)) / 1_000_000
        line += f" · ${cost:,.4f} at list price"
    return line


# --- vision ---------------------------------------------------------------------
#
# Which models accept images, from the same `GET /api/models` rows the
# picker reads. The tri-state is the API's `supports_vision`:
# true | false | null, where null (and a model the list does not name)
# means the gateway did not say, which is not a refusal.


def model_accepts_images(models: list[dict[str, Any]], model_id: str | None) -> bool:
    """Whether an image may be attached while `model_id` is the model. Only
    an explicit `supports_vision: false` refuses; the server's 422 stays
    the backstop for everything the list does not settle."""
    return vision_of(models, model_id) != "no"


def vision_capable_model_ids(models: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """Up to `limit` model ids the list marks as vision-capable, in the
    order the API gave them."""
    return [
        str(model["id"])
        for model in models
        if model.get("supports_vision") is True and model.get("id")
    ][:limit]


IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif", ".avif", ".tif", ".tiff"}
)


def is_image_filename(filename: str) -> bool:
    """Whether an upload of this name would be an image, which is what the
    vision gate asks before it refuses one."""
    dot = filename.rfind(".")
    return dot >= 0 and filename[dot:].lower() in IMAGE_SUFFIXES
