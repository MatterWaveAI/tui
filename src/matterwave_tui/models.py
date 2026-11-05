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

GRT_HF_ORG = "MatterWaveAI"
GRT_HF_ORG_URL = "https://huggingface.co/MatterWaveAI"

GRT_HF_MODELS: tuple[dict[str, Any], ...] = (
    {
        "repo_id": "MatterWaveAI/grt-30m",
        "alias": "grt-30m",
        "model_type": "grt",
        "auto_class": "GRTForCausalLM",
        "pipeline_tag": "text-generation",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m",
        "weight_sha256": "205f76f3ed59fee0ed54bb8133ea1b1a64527994c68776066057b4707f1b2456",
        "config_sha256": "5ffa4facb3cfba2b39512bbc54c7b7ecda637c70f40fd18ede13396cd8e5a86b",
    },
    {
        "repo_id": "MatterWaveAI/grt-30m-chat",
        "alias": "grt-30m-chat",
        "model_type": "grt",
        "auto_class": "GRTForCausalLM",
        "pipeline_tag": "text-generation",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m-chat",
        "weight_sha256": "ee613803cb7b2c618def7e6c79476e1aab4e887cc08e234204b6bad12ad772bc",
        "config_sha256": "c2b264a05e11914a8e41c1610f3c81e15d38151c459a1c66caaaa0381d804685",
    },
    {
        "repo_id": "MatterWaveAI/grt-30m-mor",
        "alias": "grt-30m-mor",
        "model_type": "grt_mor",
        "auto_class": "GRTForCausalLM",
        "pipeline_tag": "text-generation",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m-mor",
        "weight_sha256": "5633a578073e808d18fe3e52fb0fbf0005dc015b6bb10a1bff339f5d83225a07",
        "config_sha256": "bf652df2d4107c7e7ef3c23d0854436aedb0030f0821f97939ed6ed858254013",
    },
    {
        "repo_id": "MatterWaveAI/grt-30m-embedding",
        "alias": "grt-30m-embedding",
        "model_type": "grt",
        "auto_class": "GRTModel",
        "pipeline_tag": "feature-extraction",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m-embedding",
        "weight_sha256": "5cdab387c5e2ee9bb3907acaccfccfaa7cc10060c21e3f265ce981a4a7d38226",
        "config_sha256": "c987cec1b71deba4a1749a939032309921872ae2a6c21378f8160edb29b210df",
    },
    {
        "repo_id": "MatterWaveAI/grt-30m-reranker",
        "alias": "grt-30m-reranker",
        "model_type": "grt",
        "auto_class": "GRTForSequenceClassification",
        "pipeline_tag": "text-classification",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m-reranker",
        "weight_sha256": "6d090decc4426a1c21a372c1bd718a6b80626e2c7b460c9ab79b713d84320ef9",
        "config_sha256": "6f401b7ed6af93ed7fc93eedc6de44ad67f2f19ecdc5b3c96dcc92ee564750fc",
    },
    {
        "repo_id": "MatterWaveAI/grt-30m-vl",
        "alias": "grt-30m-vl",
        "model_type": "grt_vl",
        "auto_class": "GRTVLForConditionalGeneration",
        "pipeline_tag": "image-text-to-text",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m-vl",
        "weight_sha256": "b21667c9d39c67513671af4fe4480551f26ac738b23821902fd78ae55a4f1dfe",
        "config_sha256": "0fd2f34d415723412d54055ecb174bcbb60cccaf43235af52b6c13826e6bb859",
    },
    {
        "repo_id": "MatterWaveAI/grt-30m-tts",
        "alias": "grt-30m-tts",
        "model_type": "grt_tts",
        "auto_class": "GRTTTSForConditionalGeneration",
        "pipeline_tag": "text-to-speech",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m-tts",
        "weight_sha256": "9df117cf2d9fdd14a972495293628bfea4ab503ee4fbcb270ffb667763e314f4",
        "config_sha256": "74dca6d26f2726ae96033446d5d418a152aef8368a8e49aa49bb15f6364ca75a",
    },
    {
        "repo_id": "MatterWaveAI/grt-30m-omni",
        "alias": "grt-30m-omni",
        "model_type": "grt_omni",
        "auto_class": "GRTOmniForConditionalGeneration",
        "pipeline_tag": "any-to-any",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m-omni",
        "weight_sha256": "b314996ce883a0c875de18da67f5d23103c507c79b7421b8b8c52cef95d2fbf7",
        "config_sha256": "5bc88f708534280522872f046e89deade537f286a78bc261c1e039d03fa6d0cf",
    },
    {
        "repo_id": "MatterWaveAI/grt-30m-tokenizer",
        "alias": "grt-30m-tokenizer",
        "model_type": "grt",
        "auto_class": "AutoTokenizer",
        "pipeline_tag": "feature-extraction",
        "hf_url": "https://huggingface.co/MatterWaveAI/grt-30m-tokenizer",
        "weight_sha256": None,
        "config_sha256": "4c7ed24fcbaf5b1c62ec160296e682eb7b700ea19d88f0a8e01e363b125d505f",
    },
)


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
