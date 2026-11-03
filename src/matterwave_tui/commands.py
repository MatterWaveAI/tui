"""Interactive TUI composer slash-command definitions and metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommandSpec:
    """Metadata for an interactive TUI composer slash command."""

    name: str
    usage: str
    description: str


PI_SLASH_COMMAND_SPECS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec("/pi", "/pi <prompt>", "Run prompt with Pi 1.0 agent in transcript"),
    SlashCommandSpec(
        "/pi-engine",
        "/pi-engine",
        "Inspect or switch agent engine (auto, pi, builtin)",
    ),
    SlashCommandSpec(
        "/pi-model",
        "/pi-model [provider/]model",
        "Switch or display the active Pi 1.0 model",
    ),
    SlashCommandSpec(
        "/pi-fork",
        "/pi-fork [entry_id]",
        "Fork the current Pi 1.0 session branch",
    ),
    SlashCommandSpec(
        "/pi-compact",
        "/pi-compact [instructions]",
        "Compact the Pi 1.0 RPC session context",
    ),
    SlashCommandSpec(
        "/pi-status",
        "/pi-status",
        "Inspect Pi 1.0 binary, engine, and session status",
    ),
    SlashCommandSpec("/pi-new", "/pi-new", "Reset the Pi 1.0 RPC session"),
)

SLASH_COMMANDS: tuple[str, ...] = (
    "/role",
    "/shell",
    "/temp",
    "/pi",
    "/pi-engine",
    "/agent-engine",
    "/pi-model",
    "/pi-fork",
    "/pi-compact",
    "/pi-status",
    "/pi-new",
)

SLASH_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "/role": "Apply or manage named system prompt roles",
    "/shell": "Toggle shell assistant mode",
    "/temp": "Set or inspect conversation sampling temperature",
    "/agent-engine": "Inspect or switch agent engine (auto, pi, builtin)",
    **{spec.name: spec.description for spec in PI_SLASH_COMMAND_SPECS},
}
