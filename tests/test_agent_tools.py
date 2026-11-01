"""The seven contract tools on a real folder: confinement refuses before
anything is read or written, search honours .gitignore, .matterwaveignore
and the deny list, an edit is worked out (and its diff shown) before it is
written, and a protected path is worked out to be asked about."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from matterwave_sdk import agent as contract

from matterwave_tui.agent.tools import LocalTools, PendingWrite, ToolOutcome


def tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret outside", encoding="utf-8")
    return tree(root, {
        "README.md": "# demo\n",
        "src/app.py": "def main():\n    return 1\n",
        "src/util.py": "VALUE = 2\n",
        ".env": "KEY=1\n",
        "keys/deploy.pem": "-----BEGIN-----\n",
        "build/out.py": "generated = True\n",
        "private/notes.md": "private\n",
        ".gitignore": "build/\n*.log\n!keep.log\n",
        ".matterwaveignore": "private/\n",
        "debug.log": "noise\n",
        "keep.log": "kept\n",
        "pkg/.gitignore": "local_only.py\n",
        "pkg/local_only.py": "x = 1\n",
        "pkg/shared.py": "y = 2\n",
    })


def read(tools: LocalTools, name: str, **args) -> ToolOutcome:
    import asyncio

    return asyncio.run(tools.read(name, args))


# --- confinement --------------------------------------------------------------------


def test_a_path_through_dotdot_is_refused(workspace: Path):
    out = read(LocalTools(workspace), "read_file", path="../outside.txt")
    assert out.is_error and "goes through '..'" in out.text
    assert "secret outside" not in out.text


def test_an_absolute_path_outside_the_root_is_refused(workspace: Path):
    outside = str(workspace.parent / "outside.txt")
    out = read(LocalTools(workspace), "read_file", path=outside)
    assert out.is_error and "outside the workspace root" in out.text


def test_an_absolute_path_inside_the_root_is_allowed(workspace: Path):
    out = read(LocalTools(workspace), "read_file", path=str(workspace / "src" / "util.py"))
    assert not out.is_error and "VALUE = 2" in out.text


def test_reading_dotenv_is_refused_and_nothing_is_sent(workspace: Path):
    out = read(LocalTools(workspace), "read_file", path=".env")
    assert out.is_error and "is protected" in out.text
    assert "KEY=1" not in out.text


def test_a_key_file_and_a_matterwaveignore_entry_are_refused(workspace: Path):
    tools = LocalTools(workspace)
    assert read(tools, "read_file", path="keys/deploy.pem").is_error
    assert read(tools, "read_file", path="private/notes.md").is_error


def _symlink(target: Path, link: Path, *, directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links need admin or developer mode here ({exc})")


def test_a_symlink_out_of_the_root_is_refused(workspace: Path):
    _symlink(workspace.parent / "outside.txt", workspace / "escape.txt", directory=False)
    out = read(LocalTools(workspace), "read_file", path="escape.txt")
    assert out.is_error and "link that leads outside" in out.text
    tools = LocalTools(workspace)
    pending = tools.prepare_write("write_file", {"path": "escape.txt", "content": "x"})
    assert isinstance(pending, ToolOutcome) and pending.is_error
    assert (workspace.parent / "outside.txt").read_text(encoding="utf-8") == "secret outside"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'")
def test_a_junction_out_of_the_root_is_refused(workspace: Path):
    import _winapi

    outside = workspace.parent / "elsewhere"
    outside.mkdir()
    (outside / "data.txt").write_text("outside data", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(workspace / "jump"))
    out = read(LocalTools(workspace), "read_file", path="jump/data.txt")
    assert out.is_error and "link that leads outside" in out.text
    listed = read(LocalTools(workspace), "glob", pattern="**/*.txt")
    assert "data.txt" not in listed.text


def test_writes_are_confined_too(workspace: Path):
    tools = LocalTools(workspace)
    for args in (
        {"path": "../outside.txt", "content": "x"},
        {"path": str(workspace.parent / "outside.txt"), "content": "x"},
        {"path": ".env", "content": "x"},
    ):
        result = tools.prepare_write("write_file", args)
        assert isinstance(result, ToolOutcome) and result.is_error, args
    assert (workspace.parent / "outside.txt").read_text(encoding="utf-8") == "secret outside"
    assert (workspace / ".env").read_text(encoding="utf-8") == "KEY=1\n"


def test_a_protected_path_is_worked_out_to_be_asked_about_not_refused(workspace: Path):
    tools = LocalTools(workspace)
    for path in (".matterwaveignore", "AGENTS.md", "docs/agents.md", ".matterwave/settings.json",
                 ".bashrc", "home/me/.ssh/config", "me/.config/matterwave/settings.json"):
        pending = tools.prepare_write("write_file", {"path": path, "content": "x"})
        assert isinstance(pending, PendingWrite) and pending.protected, path
        # Without the user's approval the SDK refuses the write itself.
        with pytest.raises(contract.PathRefused) as refused:
            tools.confine(path, write=True)
        assert refused.value.reason == "protected"
    plain = tools.prepare_write("write_file", {"path": "src/new.py", "content": "x"})
    assert isinstance(plain, PendingWrite) and not plain.protected
    assert (workspace / ".matterwaveignore").read_text(encoding="utf-8") == "private/\n"


# --- reading and searching ---------------------------------------------------------------


def test_read_file_numbers_lines_and_takes_a_range(workspace: Path):
    tools = LocalTools(workspace)
    whole = read(tools, "read_file", path="src/app.py")
    assert whole.text == "1\tdef main():\n2\t    return 1"
    second = read(tools, "read_file", path="src/app.py", start_line=2, end_line=2)
    assert second.text == "2\t    return 1"
    past = read(tools, "read_file", path="src/app.py", start_line=9)
    assert past.is_error and past.text == "start_line 9 is past the end of src/app.py (2 lines)."
    backwards = read(tools, "read_file", path="src/app.py", start_line=2, end_line=1)
    assert backwards.is_error and backwards.text == "end_line 1 is before start_line 2."
    typed = read(tools, "read_file", path="src/app.py", start_line="2")
    assert typed.is_error and "must be an integer" in typed.text


def test_list_dir_leaves_out_ignored_and_denied_entries_and_shows_protected_ones(workspace: Path):
    out = read(LocalTools(workspace), "list_dir")
    names = out.text.splitlines()
    assert names[0].endswith("/")  # folders first
    assert "src/" in names and "pkg/" in names
    for hidden in (".env", "build/", "private/", "debug.log"):
        assert hidden not in names
    assert "keep.log" in names  # negated in .gitignore
    assert ".matterwaveignore" in names  # protected: it can be read, so it is listed


def test_glob_honours_gitignore_nested_gitignore_and_the_deny_list(workspace: Path):
    out = read(LocalTools(workspace), "glob", pattern="**/*.py")
    found = out.text.splitlines()
    assert found == ["pkg/shared.py", "src/app.py", "src/util.py"]
    scoped = read(LocalTools(workspace), "glob", pattern="*.py", path="src")
    assert scoped.text.splitlines() == ["src/app.py", "src/util.py"]


def test_grep_finds_matches_and_skips_ignored_files(workspace: Path):
    tools = LocalTools(workspace)
    out = read(tools, "grep", pattern=r"=\s*\d")
    lines = out.text.splitlines()
    assert "src/util.py:1: VALUE = 2" in lines
    assert "pkg/shared.py:1: y = 2" in lines
    assert not any(line.startswith(("build/", "pkg/local_only.py", ".env")) for line in lines)
    only_src = read(tools, "grep", pattern="return", glob="*.py", path="src")
    assert only_src.text == "src/app.py:2:     return 1"
    bad = read(tools, "grep", pattern="(")
    assert bad.is_error


def test_the_search_rules_follow_gits_rules_and_ignore_case(tmp_path: Path):
    tree(tmp_path, {".gitignore": "*.tmp\n/top.txt\nlogs/\n!important.tmp\n", "a/.gitignore": "!x.tmp\n"})
    rules = LocalTools(tmp_path).search_ignore()
    assert rules.ignored("x.tmp")
    assert rules.ignored("deep/y.tmp")
    assert rules.ignored("DEEP/Y.TMP")  # on every platform
    assert not rules.ignored("important.tmp")
    assert rules.ignored("top.txt") and not rules.ignored("sub/top.txt")
    assert rules.ignored("logs", is_dir=True) and not rules.ignored("logs")
    assert not rules.ignored("a/x.tmp")  # the nested file un-ignores it
    assert rules.ignored(".git", is_dir=True)


def test_glob_matches_names_at_any_depth_and_a_path_pattern_under_path(workspace: Path):
    tools = LocalTools(workspace)
    found = read(tools, "glob", pattern="*.PY").text.splitlines()
    assert found == ["pkg/shared.py", "src/app.py", "src/util.py"]
    assert read(tools, "glob", pattern="src/*.py").text.splitlines() == ["src/app.py", "src/util.py"]
    # A grep glob with a / is matched against the path under `path`.
    assert read(tools, "grep", pattern="return", glob="src/*.py").text == "src/app.py:2:     return 1"
    assert read(tools, "grep", pattern="return", glob="src/*.py", path="src").text == "No matches for return."
    assert read(tools, "grep", pattern="return", glob="*.py", path="src").text == "src/app.py:2:     return 1"


# --- writes --------------------------------------------------------------------------------


def test_an_edit_is_worked_out_with_its_diff_before_anything_is_written(workspace: Path):
    tools = LocalTools(workspace)
    before = (workspace / "src/app.py").read_bytes()
    pending = tools.prepare_write(
        "edit", {"path": "src/app.py", "old_string": "return 1", "new_string": "return 42"}
    )
    assert isinstance(pending, PendingWrite)
    assert "-    return 1" in pending.diff and "+    return 42" in pending.diff
    assert pending.diff.startswith("--- a/src/app.py\n+++ b/src/app.py\n")
    assert (workspace / "src/app.py").read_bytes() == before
    applied = tools.apply_write(pending)
    assert not applied.is_error
    assert (workspace / "src/app.py").read_text(encoding="utf-8") == "def main():\n    return 42\n"


def test_edit_failures_leave_the_file_alone(workspace: Path):
    tools = LocalTools(workspace)
    (workspace / "dup.txt").write_text("a\na\n", encoding="utf-8")
    missing = tools.prepare_write("edit", {"path": "nope.py", "old_string": "a", "new_string": "b"})
    assert isinstance(missing, ToolOutcome) and "does not exist" in missing.text
    not_found = tools.prepare_write("edit", {"path": "src/app.py", "old_string": "zzz", "new_string": "b"})
    assert isinstance(not_found, ToolOutcome) and "was not found" in not_found.text
    twice = tools.prepare_write("edit", {"path": "dup.txt", "old_string": "a", "new_string": "b"})
    assert isinstance(twice, ToolOutcome) and "occurs 2 times" in twice.text
    assert (workspace / "dup.txt").read_text(encoding="utf-8") == "a\na\n"


def test_an_edit_keeps_crlf_and_the_rest_of_the_bytes(workspace: Path):
    path = workspace / "win.txt"
    path.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
    tools = LocalTools(workspace)
    pending = tools.prepare_write("edit", {"path": "win.txt", "old_string": "two\nthree", "new_string": "2\n3"})
    assert isinstance(pending, PendingWrite)
    tools.apply_write(pending)
    assert path.read_bytes() == b"\xef\xbb\xbfone\r\n2\r\n3\r\n"


def test_write_file_creates_folders_and_shows_a_new_file_diff(workspace: Path):
    tools = LocalTools(workspace)
    pending = tools.prepare_write("write_file", {"path": "new/dir/mod.py", "content": "x = 1\n"})
    assert isinstance(pending, PendingWrite) and pending.created
    assert pending.diff.startswith("--- /dev/null\n+++ b/new/dir/mod.py\n")
    tools.apply_write(pending)
    assert (workspace / "new/dir/mod.py").read_text(encoding="utf-8") == "x = 1\n"
