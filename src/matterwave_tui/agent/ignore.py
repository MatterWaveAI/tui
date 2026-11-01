"""What search leaves out: `.gitignore` files (the root's and nested ones),
plus the deny list (the contract's defaults and `.matterwaveignore`).

The deny list is the SDK's `DenyList`; this module adds only `.gitignore`,
which the SDK does not read: negation (`!`), and a nested file's patterns
being relative to its own folder. Glob syntax is the SDK's
`glob_to_regex`, so a pattern means the same thing in both lists.

Search (`glob`, `grep`, `list_dir`) honours both; `read_file` honours only
the deny list, since an ignored file (a build output, a log) is still the
user's to show the model.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from matterwave_sdk import agent as contract

_FLAGS = re.IGNORECASE if sys.platform in ("win32", "darwin") else 0


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool
    anchored: bool
    base: str  # the folder the .gitignore sits in, relative to the root ("" for the root)


def _parse(line: str, base: str) -> _Rule | None:
    text = line.rstrip("\n").rstrip("\r")
    # Trailing spaces are dropped unless escaped.
    while text.endswith(" ") and not text.endswith("\\ "):
        text = text[:-1]
    if not text or text.startswith("#"):
        return None
    negate = False
    if text.startswith("!"):
        negate = True
        text = text[1:]
    elif text.startswith(("\\#", "\\!")):
        text = text[1:]
    dir_only = text.endswith("/")
    text = text.rstrip("/")
    anchored = "/" in text
    text = text.lstrip("/")
    if not text:
        return None
    pattern = contract.glob_to_regex(text.replace("\\ ", " "))
    return _Rule(re.compile(pattern, _FLAGS), negate, dir_only, anchored, base)


class GitIgnore:
    """Every `.gitignore` under a root, loaded as the walk reaches its
    folder. `ignored(rel, is_dir)` is git's answer for one path, given that
    no folder above it is ignored (the walker never enters one that is)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._rules: dict[str, list[_Rule]] = {}

    def _rules_for(self, folder: str) -> list[_Rule]:
        cached = self._rules.get(folder)
        if cached is not None:
            return cached
        path = self.root / folder / ".gitignore" if folder else self.root / ".gitignore"
        rules: list[_Rule] = []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            rule = _parse(line, folder)
            if rule is not None:
                rules.append(rule)
        self._rules[folder] = rules
        return rules

    def ignored(self, rel: str, is_dir: bool) -> bool:
        segments = rel.split("/")
        folders = [""] + ["/".join(segments[: i + 1]) for i in range(len(segments) - 1)]
        verdict = False
        for folder in folders:
            local = rel[len(folder) + 1 :] if folder else rel
            name = segments[-1]
            for rule in self._rules_for(folder):
                if rule.dir_only and not is_dir:
                    continue
                subject = local if rule.anchored else name
                if rule.regex.fullmatch(subject):
                    verdict = not rule.negate
        return verdict


class SearchFilter:
    """Gitignore plus the deny list plus the contract's protected paths:
    what search, listing and globbing leave out."""

    def __init__(self, root: Path, deny: contract.DenyList | None = None) -> None:
        self.root = root
        self.deny = deny if deny is not None else contract.DenyList.from_root(root)
        self.protected = contract.DenyList(contract.PROTECTED)
        self.gitignore = GitIgnore(root)

    def hidden(self, rel: str, is_dir: bool) -> bool:
        if self.deny.matches(rel, is_dir=is_dir) or self.protected.matches(rel, is_dir=is_dir):
            return True
        return self.gitignore.ignored(rel, is_dir)

    def walk(self, start: str = "") -> Iterator[tuple[str, bool]]:
        """Every entry under `start` (relative, `/`-separated; "" for the
        root) that search may see, depth first in name order, as
        `(relative path, is_dir)`. Links are not followed into folders, and
        a linked file whose target is outside the root is left out."""
        real_root = os.path.realpath(self.root)
        stack = [start]
        while stack:
            folder = stack.pop()
            absolute = self.root / folder if folder else self.root
            try:
                with os.scandir(absolute) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            subfolders: list[str] = []
            for entry in entries:
                rel = f"{folder}/{entry.name}" if folder else entry.name
                try:
                    is_link = entry.is_symlink() or _is_junction(entry.path)
                    is_dir = entry.is_dir(follow_symlinks=False) and not is_link
                except OSError:
                    continue
                if is_link:
                    target = os.path.realpath(entry.path)
                    if not _within(target, real_root):
                        continue
                    target_rel = os.path.relpath(target, real_root).replace(os.sep, "/")
                    if self.hidden(target_rel, os.path.isdir(target)):
                        # A link to a denied or ignored file: its contents
                        # would leave by another name.
                        continue
                    if os.path.isdir(target):
                        # A link to a folder inside the root: it is listed,
                        # not walked, so a cycle cannot loop the search.
                        if not self.hidden(rel, True):
                            yield rel, True
                        continue
                if self.hidden(rel, is_dir):
                    continue
                yield rel, is_dir
                if is_dir:
                    subfolders.append(rel)
            stack.extend(reversed(subfolders))


def _is_junction(path: str) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction(path)) if isjunction is not None else False


def _within(child: str, parent: str) -> bool:
    c = os.path.normcase(os.path.normpath(child))
    p = os.path.normcase(os.path.normpath(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)
