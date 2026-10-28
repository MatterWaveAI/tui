from __future__ import annotations

import re
from datetime import UTC, datetime

from matterwave_sdk.models import ConversationMessage

from matterwave_tui.export import conversation_markdown, export_filename
from matterwave_tui.thread import Thread

from .fake_api import conversation, message

NOW = datetime(2026, 10, 27, 9, 30, 0, tzinfo=UTC)


def thread_of(messages: list[ConversationMessage], leaf: str | None = None) -> Thread:
    return Thread.from_tree(messages, leaf)


def test_the_document_matches_the_web_exporter():
    thread = thread_of(
        [
            message("u1", "user", "What is TUI?"),
            message("a1", "assistant", "A terminal UI.", parent="u1", created_at="2",
                    model="grt-nano"),
        ]
    )
    text = conversation_markdown(conversation(cid="c1", title="Demo"), thread, now=NOW)
    assert text == (
        "# Demo\n"
        "\n"
        "Exported from MatterWave on 2026-10-27T09:30:00Z. Conversation `c1`.\n"
        "\n"
        "## You\n"
        "\n"
        "What is TUI?\n"
        "\n"
        "## Assistant (grt-nano)\n"
        "\n"
        "A terminal UI.\n"
        "\n"
    )


def test_the_export_stamp_is_the_current_time_without_milliseconds():
    text = conversation_markdown(conversation(cid="c1"), thread_of([message("u1", "user", "hi")]))
    stamp = re.search(r"Exported from MatterWave on (\S+)\.", text)
    assert stamp is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp.group(1))


def test_reasoning_is_dropped_and_tool_parts_take_the_web_shape():
    tree = [
        message("u1", "user", "weather?"),
        message(
            "a1", "assistant", "It rains.", parent="u1", created_at="2", model="grt-nano",
            parts=[
                {"type": "reasoning", "text": "the user asks about weather"},
                {"type": "tool_call", "name": "web_search", "arguments": "{\"q\": \"oslo\"}"},
                {"type": "tool_result", "output": "raining"},
                {"type": "tool_result", "output": "boom", "is_error": True},
                {"type": "text", "text": "It rains."},
            ],
        ),
    ]
    text = conversation_markdown(conversation(cid="c1"), thread_of(tree), now=NOW)
    assert text == (
        "# New chat\n"
        "\n"
        "Exported from MatterWave on 2026-10-27T09:30:00Z. Conversation `c1`.\n"
        "\n"
        "## You\n"
        "\n"
        "weather?\n"
        "\n"
        "## Assistant (grt-nano)\n"
        "\n"
        "*Called `web_search`*\n"
        "\n"
        "```json\n"
        "{\"q\": \"oslo\"}\n"
        "```\n"
        "\n"
        "*Tool result:*\n"
        "\n"
        "```\n"
        "raining\n"
        "```\n"
        "\n"
        "*Tool result:*\n"
        "\n"
        "```\n"
        "boom\n"
        "``` (error)\n"
        "\n"
        "It rains.\n"
        "\n"
    )
    assert "reasoning" not in text and "oslo weather" not in text


def test_attachments_export_as_the_web_placeholders_and_unknown_as_its_type():
    tree = [
        message(
            "u1", "user", "", created_at="1",
            parts=[
                {"type": "image", "filename": "picture.png"},
                {"type": "document", "filename": "notes.pdf"},
                {"type": "weird", "text": "?"},
                {"type": "text", "text": "look"},
            ],
        ),
    ]
    text = conversation_markdown(conversation(cid="c1"), thread_of(tree), now=NOW)
    assert "*Attached image: picture.png*" in text
    assert "*Attached file: notes.pdf*" in text
    assert "*(unsupported content: weird)*" in text
    assert "look" in text


def test_summaries_and_system_messages_are_omitted():
    tree = [
        message("u1", "user", "q"),
        message("a1", "assistant", "one", parent="u1", created_at="2"),
        message(
            "s1", "system", "", parent="a1", created_at="3",
            parts=[{"type": "summary", "text": "They talked.", "covers_from": "u1",
                    "covers_to": "a1"}],
        ),
        message("u2", "user", "after", parent="s1", created_at="4"),
    ]
    text = conversation_markdown(conversation(cid="c1"), thread_of(tree, "u2"), now=NOW)
    assert "Summary" not in text and "They talked." not in text and "q" not in text
    assert "## You\n\nafter" in text
    # An assistant answer without a model is a bare heading.
    assert "## Assistant (" not in text


def test_the_filename_is_the_web_exporters_derivation():
    cases = [
        ("Héllo, World!", "Héllo, World!.md"),
        ("", "conversation.md"),
        ("   ", "conversation.md"),
        ("a/b:c*d?e\"f<g>h|i\\j*k", "a b c d e f g h i j k.md"),
        ("control\x01\x02chars", "control chars.md"),  # controls become spaces, then collapse
        ("full-width ＡＢＣ", "full-width ABC.md"),  # NFKC folds the width
        ("...dots...", "dots.md"),
        ("con", "con conversation.md"),
        ("CON", "CON conversation.md"),
        ("Lpt4", "Lpt4 conversation.md"),
        ("com0", "com0.md"),  # com1-9 only
        ("x" * 100, "x" * 80 + ".md"),
        ("trailing dots... ", "trailing dots.md"),
    ]
    for title, expected in cases:
        assert export_filename(conversation(cid="c1", title=title)) == expected, title
