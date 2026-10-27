from __future__ import annotations

from matterwave_sdk.models import ConversationMessage

from matterwave_tui.export import conversation_markdown, export_filename
from matterwave_tui.thread import Thread

from .fake_api import conversation, message


def thread_of(messages: list[ConversationMessage], leaf: str | None = None) -> Thread:
    return Thread.from_tree(messages, leaf)


def test_the_active_path_exports_with_roles_and_content():
    thread = thread_of(
        [
            message("u1", "user", "What is TUI?"),
            message("a1", "assistant", "A terminal UI.", parent="u1", created_at="2",
                    model="grt-nano"),
        ]
    )
    text = conversation_markdown(conversation(title="Demo"), thread)
    assert text == (
        "# Demo\n"
        "\n"
        "## You\n"
        "\n"
        "What is TUI?\n"
        "\n"
        "## Assistant · grt-nano\n"
        "\n"
        "A terminal UI.\n"
    )


def test_a_summary_folds_into_a_quote_exactly_where_the_transcript_folds_it():
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
    thread = thread_of(tree, "u2")
    text = conversation_markdown(conversation(title="Folded"), thread)
    assert "> **Summary** — They talked." in text
    assert "q" not in text.replace("after", "")  # the covered originals stay folded
    assert "## You\n\nafter" in text


def test_tool_parts_and_truncated_answers_are_recorded():
    tree = [
        message("u1", "user", "weather?"),
        message(
            "a1", "assistant", "", parent="u1", created_at="2", truncated=True,
            parts=[
                {"type": "tool_call", "name": "web_search", "arguments": "{\"q\": \"oslo\"}"},
                {"type": "tool_result", "output": "raining"},
                {"type": "text", "text": "It rains."},
            ],
        ),
    ]
    text = conversation_markdown(conversation(), thread_of(tree))
    assert "> ⚙ `web_search` called" in text
    assert "> ↳ raining" in text
    assert "It rains." in text
    assert "*Cut off — this is not the whole answer.*" in text


def test_images_and_documents_export_as_placeholders():
    tree = [
        message(
            "u1", "user", "", created_at="1",
            parts=[{"type": "image", "filename": "picture.png"}, {"type": "text", "text": "look"}],
        ),
    ]
    text = conversation_markdown(conversation(), thread_of(tree))
    assert "> *[image: picture.png]*" in text
    assert "look" in text


def test_the_filename_slugs_the_title_and_keeps_the_id():
    assert export_filename(conversation(cid="abcdefghij", title="Héllo, World!")) == (
        "matterwave-h-llo-world-abcdefgh.md"
    )
    assert export_filename(conversation(cid="short", title="")) == (
        "matterwave-conversation-short.md"
    )
