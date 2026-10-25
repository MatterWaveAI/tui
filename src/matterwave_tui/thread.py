"""The conversation tree held in memory, and the path the transcript shows.

A port of the web app's `branches.ts` (path to the leaf, siblings in
creation order, descend to the youngest leaf) plus the compaction view: a
`summary` message on the path hides the ancestors it covers behind a divider,
and expanding the divider reads them from this same tree -- nothing is
fetched to show them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from matterwave_sdk.models import ConversationMessage

# The transcript's unit, under the name the rest of the client uses.
Message = ConversationMessage


def summary_part(message: Message) -> dict[str, Any] | None:
    return message.summary if message.role == "system" else None


@dataclass
class Divider:
    """Where a summary sits on the path, and the originals it stands for."""

    summary: Message
    covered: list[Message]
    expanded: bool = False

    @property
    def text(self) -> str:
        part = summary_part(self.summary) or {}
        return str(part.get("text") or "")


@dataclass
class Thread:
    messages: dict[str, Message] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    leaf: str | None = None
    # Summary ids whose originals the reader asked to see.
    expanded: set[str] = field(default_factory=set)

    @classmethod
    def from_tree(cls, tree: list[Message], recorded_leaf: str | None) -> Thread:
        thread = cls()
        for message in sorted(tree, key=lambda m: (m.created_at, m.id)):
            thread.add(message)
        thread.leaf = thread.default_leaf(recorded_leaf)
        return thread

    def add(self, message: Message) -> None:
        if message.id not in self.messages:
            self.order.append(message.id)
        self.messages[message.id] = message

    def children(self, parent: str | None) -> list[Message]:
        return [self.messages[i] for i in self.order if self.messages[i].parent_id == parent]

    def descend(self, start: str) -> str:
        current, seen = start, {start}
        while True:
            kids = self.children(current)
            if not kids or kids[-1].id in seen:
                return current
            current = kids[-1].id
            seen.add(current)

    def default_leaf(self, recorded: str | None) -> str | None:
        if recorded and recorded in self.messages:
            return recorded
        roots = self.children(None)
        return self.descend(roots[-1].id) if roots else None

    def path(self) -> list[Message]:
        out: list[Message] = []
        seen: set[str] = set()
        cursor = self.messages.get(self.leaf) if self.leaf else None
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            out.append(cursor)
            cursor = self.messages.get(cursor.parent_id) if cursor.parent_id else None
        out.reverse()
        return out

    def position(self, message: Message) -> tuple[int, int] | None:
        """(index, total) among siblings, or None when it has none."""
        siblings = self.children(message.parent_id)
        if len(siblings) < 2:
            return None
        ids = [s.id for s in siblings]
        return (ids.index(message.id), len(ids)) if message.id in ids else None

    def sibling_leaf(self, message: Message, delta: int) -> str | None:
        ids = [s.id for s in self.children(message.parent_id)]
        if len(ids) < 2 or message.id not in ids:
            return None
        target = ids.index(message.id) + delta
        if target < 0 or target >= len(ids):
            return None
        return self.descend(ids[target])

    def covered_by(self, summary: Message) -> list[Message]:
        """The originals a summary stands for: its ancestors from `covers_to`
        back to `covers_from`, oldest first."""
        part = summary_part(summary) or {}
        start = part.get("covers_from")
        end = part.get("covers_to") or summary.parent_id
        out: list[Message] = []
        cursor = self.messages.get(str(end)) if end else None
        seen: set[str] = set()
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            out.append(cursor)
            if cursor.id == start:
                break
            cursor = self.messages.get(cursor.parent_id) if cursor.parent_id else None
        out.reverse()
        return out

    def view(self) -> list[Message | Divider]:
        """The path as the transcript draws it: covered messages folded into
        their summary's divider unless the reader expanded it."""
        path = self.path()
        hidden: dict[str, str] = {}
        dividers: dict[str, Divider] = {}
        for message in path:
            if summary_part(message) is None:
                continue
            covered = self.covered_by(message)
            dividers[message.id] = Divider(message, covered, expanded=message.id in self.expanded)
            for original in covered:
                hidden.setdefault(original.id, message.id)
        out: list[Message | Divider] = []
        for message in path:
            if message.id in dividers:
                divider = dividers[message.id]
                if divider.expanded:
                    out.extend(divider.covered)
                out.append(divider)
            elif message.id not in hidden:
                out.append(message)
        return out

    def last(self, role: str) -> Message | None:
        for message in reversed(self.path()):
            if message.role == role:
                return message
        return None
