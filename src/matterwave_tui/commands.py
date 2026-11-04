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

WAVE_SLASH_COMMAND_SPECS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec(
        "/search",
        "/search <query>",
        "Search the web and render citations and snippets into the transcript",
    ),
    SlashCommandSpec(
        "/rag",
        "/rag [collection_id] <query>",
        "Query a RAG collection, or list RAG collections when invoked with no arguments",
    ),
    SlashCommandSpec(
        "/mcp",
        "/mcp [server_id]",
        "List registered MCP servers and their tools",
    ),
    SlashCommandSpec(
        "/runs",
        "/runs [status]",
        "List recent Agent Command Center runs with status, agent ID, and run ID",
    ),
    SlashCommandSpec(
        "/connectors",
        "/connectors [category]",
        "List providers from the Universal Connector Catalog",
    ),
    SlashCommandSpec(
        "/trail",
        "/trail <run_id>",
        "Render the decision trail nodes and confidence scores for a run",
    ),
    SlashCommandSpec(
        "/replay",
        "/replay <run_id> [mode]",
        "Replay a run (simulated, strict, or counterfactual) and display differences",
    ),
    SlashCommandSpec(
        "/checkpoints",
        "/checkpoints <run_id> [resume <checkpoint_id>]",
        "List checkpoints for a run or resume a run from a checkpoint",
    ),
    SlashCommandSpec(
        "/reliability",
        "/reliability [agent_id]",
        "Display the 7 ia_modules reliability metrics (SR, CR, PC, HIR, MA, TCL, WCT)",
    ),
    SlashCommandSpec(
        "/guardrails",
        "/guardrails <text>",
        "Evaluate text against the platform Guardrails engine",
    ),
    SlashCommandSpec(
        "/help",
        "/help",
        "Show keybindings and interactive composer slash commands",
    ),
)

SLASH_COMMAND_SPECS: tuple[SlashCommandSpec, ...] = (
    *PI_SLASH_COMMAND_SPECS,
    *WAVE_SLASH_COMMAND_SPECS,
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
    "/search",
    "/rag",
    "/mcp",
    "/runs",
    "/connectors",
    "/trail",
    "/replay",
    "/checkpoints",
    "/reliability",
    "/guardrails",
    "/help",
)

SLASH_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "/role": "Apply or manage named system prompt roles",
    "/shell": "Toggle shell assistant mode",
    "/temp": "Set or inspect conversation sampling temperature",
    "/agent-engine": "Inspect or switch agent engine (auto, pi, builtin)",
    **{spec.name: spec.description for spec in SLASH_COMMAND_SPECS},
}
