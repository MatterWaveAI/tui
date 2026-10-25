from __future__ import annotations

from matterwave_tui.thread import Divider, Thread

from .fake_api import message


def msg(mid, parent, role="user", t="2026-10-18T10:00:00", **extra):
    return message(mid, role, mid, parent, created_at=t, **extra)


TREE = [
    msg("u1", None, t="1"),
    msg("a1", "u1", "assistant", t="2"),
    msg("a1b", "u1", "assistant", t="3"),
    msg("u2", "a1", t="4"),
    msg("a2", "u2", "assistant", t="5"),
]


def test_path_and_sibling_switching():
    thread = Thread.from_tree(TREE, "a2")
    assert [m.id for m in thread.path()] == ["u1", "a1", "u2", "a2"]
    assert thread.position(thread.messages["a1"]) == (0, 2)
    assert thread.position(thread.messages["u2"]) is None
    assert thread.sibling_leaf(thread.messages["a1"], 1) == "a1b"
    assert thread.sibling_leaf(thread.messages["a1b"], -1) == "a2"
    assert thread.sibling_leaf(thread.messages["a1"], -1) is None


def test_no_recorded_leaf_descends_to_the_youngest_branch():
    # The web app's fallback: the youngest child at every level.
    assert Thread.from_tree(TREE, None).leaf == "a1b"
    assert Thread.from_tree(TREE, "gone").leaf == "a1b"


def test_a_summary_folds_its_originals_and_x_brings_them_back():
    summary = msg(
        "s1",
        "a2",
        "system",
        t="6",
        parts=[{"type": "summary", "text": "sum", "covers_from": "u1", "covers_to": "a2"}],
    )
    thread = Thread.from_tree([*TREE, summary, msg("u3", "s1", t="7")], "u3")
    view = thread.view()
    assert isinstance(view[0], Divider)
    assert [m.id for m in view[0].covered] == ["u1", "a1", "u2", "a2"]
    assert view[1].id == "u3"
    thread.expanded.add("s1")
    ids = [i.summary.id if isinstance(i, Divider) else i.id for i in thread.view()]
    assert ids == ["u1", "a1", "u2", "a2", "s1", "u3"]
