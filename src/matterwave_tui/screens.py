"""Modal screens: the model picker, the in-app device sign-in, the one-line
prompt and yes/no the conversation actions share, the per-thread custom
instructions editor, the reply-feedback dialog, the notifications inbox,
the share-links list, the library of folders and tags, the prompt roles
library and its editor, the files waiting for the next message, the account
switcher, the person's own account (keys, sessions, preferences,
instructions, plan) and the microphone clip recorder."""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from matterwave_sdk import FEEDBACK_REASONS
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Collapsible, DirectoryTree, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from . import markers
from .api import NOTIFICATIONS_LIMIT_MAX, NOTIFICATIONS_PAGE
from .auth import DeviceCode, LoginError
from .dictation import MAX_RECORDING_S, MicRecorder
from .models import PickerRow, picker_rows

# The per-thread instructions budget: shown as a counter, never enforced
# here -- the server is authoritative and refuses more with a 422.
INSTRUCTIONS_MAX = 2000
FEEDBACK_COMMENT_MAX = 2000
REASONS_HINT = "Reasons, comma-separated from: " + ", ".join(FEEDBACK_REASONS)

# What a library write hands back to the screen that asked for it: the
# folders, the tags, which conversation carries which tags, and the folder
# the open conversation is in -- as the app now holds them. None means the
# write did not land, and the screen keeps what it had.
LibraryState = tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]], str | None]


class ModelPicker(ModalScreen[str | None]):
    """Grouped like the web picker; a model the plan lacks is shown, not chosen.
    When a preset fixes the conversation's model, the header and current badge
    say so, and selecting a row hands back its id so the caller can refuse with
    its reason."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]
    DEFAULT_CSS = """
    ModelPicker { align: center middle; }
    ModelPicker > Vertical { width: 80%; max-width: 90; height: auto; max-height: 80%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(
        self,
        models: list[dict[str, Any]],
        current: str | None,
        *,
        fixed_by: str | None = None,
    ) -> None:
        super().__init__()
        rows = picker_rows(models)
        if current and all(r.model_id != current for r in rows):
            rows = [PickerRow(current, current, selectable=True), *rows]
        self.rows = rows
        self.current = current
        self.fixed_by = fixed_by

    def compose(self) -> ComposeResult:
        options: list[Option] = []
        for i, row in enumerate(self.rows):
            if row.is_heading:
                options.append(Option(f"— {row.label} —", id=f"heading-{i}", disabled=True))
            else:
                badge = markers.badge("current")
                if self.fixed_by and row.model_id == self.current:
                    badge = f"{badge} · set by {self.fixed_by}"
                label = row.label + (f"  ({badge})" if row.model_id == self.current else "")
                options.append(Option(label, id=f"model-{i}", disabled=not row.selectable))
        heading = (
            f"Model fixed to {self.current} by the {self.fixed_by} preset (Esc closes)"
            if self.fixed_by
            else "Choose a model (Enter selects, Esc cancels)"
        )
        with Vertical():
            yield Static(heading, markup=False)
            yield OptionList(*options, id="models")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id or ""
        if not option_id.startswith("model-"):
            return
        row = self.rows[int(option_id.removeprefix("model-"))]
        # The OptionList already refuses a disabled row; this is the second
        # lock, so a plan's exclusion never depends on one widget's behaviour.
        if row.selectable:
            self.dismiss(row.model_id)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class PresetPicker(ModalScreen[str | None]):
    """The org's live presets (`GET /api/org/presets`), 'No preset' to clear,
    and — when the open conversation is on a deleted preset — that one row so
    it is named and kept without being offered to any other conversation.
    Dismisses with `""` for No preset, the preset id for a pick, or `None` on
    Esc."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]
    DEFAULT_CSS = """
    PresetPicker { align: center middle; }
    PresetPicker > Vertical { width: 80%; max-width: 90; height: auto; max-height: 80%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(
        self,
        presets: list[dict[str, Any]],
        current: str | None,
        *,
        retired: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.current = current
        rows: list[tuple[str, str]] = [("", "No preset — Your own model and instructions.")]
        listed_ids: set[str] = set()
        for preset in presets:
            pid = str(preset.get("id") or "")
            if not pid:
                continue
            listed_ids.add(pid)
            name = str(preset.get("name") or pid)
            desc = (
                str(preset.get("description") or "").strip()
                or f"Answers with {preset.get('model') or 'its model'}."
            )
            rows.append((pid, f"{name} — {desc}"))
        if current and current not in listed_ids and retired is not None:
            rname = str(retired.get("name") or current)
            rows.append((current, f"{rname} — No longer offered; this conversation keeps it."))
        self.rows = rows

    def compose(self) -> ComposeResult:
        active = self.current or ""
        options: list[Option] = []
        for i, (pid, text) in enumerate(self.rows):
            label = text + (f"  ({markers.badge('current')})" if pid == active else "")
            options.append(Option(label, id=f"preset-{i}"))
        with Vertical():
            yield Static("Choose a preset (Enter selects, Esc cancels)", markup=False)
            yield OptionList(*options, id="presets")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id or ""
        if not option_id.startswith("preset-"):
            return
        pid, _ = self.rows[int(option_id.removeprefix("preset-"))]
        self.dismiss(pid)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


DeviceLoginFn = Callable[[Callable[[DeviceCode], None]], Awaitable[str]]


class Prompt(ModalScreen[str | None]):
    """One line of text: a new title, a file's path to attach. Enter hands
    the value back, Esc hands back None."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]
    DEFAULT_CSS = """
    Prompt { align: center middle; }
    Prompt > Vertical { width: 60; height: auto; border: round $accent;
        background: $surface; padding: 1 2; }
    """

    def __init__(self, title: str, text: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, markup=False)
            yield Input(value=self.initial, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen[bool]):
    """A yes or no: destructive by default, or a plain choice (`danger=False`)
    such as the new-chat temporary toggle. Enter or y confirms, Esc or n
    cancels."""

    BINDINGS = [
        Binding("escape,n", "no", "Cancel"),
        Binding("enter,y", "yes", "Confirm"),
    ]
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 60; height: auto; border: round $error;
        background: $surface; padding: 1 2; }
    Confirm.safe > Vertical { border: round $accent; }
    """

    def __init__(self, message: str, *, danger: bool = True, hint: str | None = None) -> None:
        super().__init__(classes="" if danger else "safe")
        self.message = message
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.message, markup=False)
            yield Static(self.hint or "Enter confirms, Esc cancels.", classes="hint", markup=False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class NewChat(ModalScreen[str | None]):
    """What kind of chat `n` starts: Enter a regular one, `t` a temporary
    one, Esc none. The temporary kind is the one that deletes itself, so it
    takes the deliberate key and the natural one keeps what is written."""

    BINDINGS = [
        Binding("enter", "choose('saved')", "Regular chat"),
        Binding("t", "choose('temporary')", "Temporary chat"),
        Binding("escape", "choose('')", "Cancel"),
    ]
    DEFAULT_CSS = """
    NewChat { align: center middle; }
    NewChat > Vertical { width: 64; height: auto; border: round $accent;
        background: $surface; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Start a new chat.", markup=False)
            yield Static(
                "Enter: a regular chat, kept in your history.\n"
                "t: a temporary chat. It won't appear in your history or search, can't be "
                "shared, and is deleted 24 hours after the last message.\n"
                "Esc: cancel.",
                classes="hint",
                markup=False,
            )

    def action_choose(self, kind: str) -> None:
        self.dismiss(kind or None)


class DeviceLogin(ModalScreen[str | None]):
    """Sign in again without leaving the TUI: the URL and code, then wait."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]
    DEFAULT_CSS = """
    DeviceLogin { align: center middle; }
    DeviceLogin > Vertical { width: 70; height: auto; border: round $warning;
        background: $surface; padding: 1 2; }
    """

    def __init__(self, login: DeviceLoginFn) -> None:
        super().__init__()
        self.login = login

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Your session needs signing in again.", markup=False)
            yield Static("Starting device sign-in…", id="device-code", markup=False)

    def on_mount(self) -> None:
        self.run_login()

    @work(exclusive=True)
    async def run_login(self) -> None:
        label = self.query_one("#device-code", Static)

        def show(code: DeviceCode) -> None:
            url = code.verification_uri_complete or code.verification_uri
            label.update(f"Open {url}\nand enter the code {code.user_code}\n\nWaiting…")

        try:
            token = await self.login(show)
        except LoginError as exc:
            label.update(str(exc))
            return
        self.dismiss(token)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class EffectiveInstructions(Collapsible):
    """The effective instructions, with the text as a direct child ahead of
    the title: a type query reads a screen breadth first, so the title —
    itself a Static — would otherwise be found before the text. Collapsed,
    how it opens, the text is hidden and it looks exactly like a titled
    Collapsible."""

    DEFAULT_CSS = """
    EffectiveInstructions.-collapsed > Static { display: none; }
    """

    def compose(self) -> ComposeResult:
        yield from self._contents_list
        yield self._title


class InstructionsEditor(ModalScreen[str | None]):
    """The thread's own instructions, with the character budget as a
    counter. Ctrl+T hands the text back ("" clears, so the user's saved
    instructions apply again), Esc hands back None."""

    BINDINGS = [
        Binding("ctrl+t", "save", "Save"),
        Binding("escape", "dismiss_none", "Cancel"),
    ]
    DEFAULT_CSS = """
    InstructionsEditor { align: center middle; }
    InstructionsEditor > Vertical { width: 70; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 1 2; }
    InstructionsEditor TextArea { height: 8; }
    """

    def __init__(self, text: str, *, effective: str | None = None) -> None:
        super().__init__()
        self.initial = text
        self.effective = effective

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "Instructions for this conversation — the model reads these "
                "instead of your saved ones.",
                markup=False,
            )
            yield TextArea(self.initial, id="instructions")
            yield Static(
                self._count(self.initial), id="instructions-count", classes="hint", markup=False
            )
            if self.effective:
                yield EffectiveInstructions(
                    Static(self.effective, markup=False),
                    title="Effective instructions (what the model sees)",
                    collapsed=True,
                )
            yield Static("Ctrl+T saves, Esc cancels.", classes="hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#instructions", TextArea).focus()

    @staticmethod
    def _count(text: str) -> str:
        return f"{len(text)} / {INSTRUCTIONS_MAX} characters"

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "instructions":
            self.query_one("#instructions-count", Static).update(self._count(event.text_area.text))

    def action_save(self) -> None:
        self.dismiss(self.query_one("#instructions", TextArea).text)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class FeedbackDialog(ModalScreen[tuple[str, int, list[str], str | None]]):
    """Optional reasons and comment for one reply's rating (already set, or
    being set now). The reply's `message_id` rides the dialog like the
    rating does, so what the dialog hands back is what the API call names.
    Reasons come from the platform's fixed vocabulary; the comment's
    2000-character budget is a counter, the server being authoritative.
    Ctrl+T hands back (message_id, rating, reasons, comment), Esc None."""

    BINDINGS = [
        Binding("ctrl+t", "save", "Save"),
        Binding("escape", "dismiss_none", "Cancel"),
    ]
    DEFAULT_CSS = """
    FeedbackDialog { align: center middle; }
    FeedbackDialog > Vertical { width: 70; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 1 2; }
    FeedbackDialog TextArea { height: 6; }
    """

    def __init__(self, message_id: str, rating: int, reasons: list[str], comment: str) -> None:
        super().__init__()
        self.message_id = message_id
        self.rating = rating
        self.initial_reasons = reasons
        self.initial_comment = comment

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"Rate this reply ({'up' if self.rating == 1 else 'down'}) — "
                "reasons and a comment are optional.",
                markup=False,
            )
            yield Static(REASONS_HINT, markup=False)
            yield Input(value=", ".join(self.initial_reasons), id="feedback-reasons")
            yield Static("Comment", markup=False)
            yield TextArea(self.initial_comment, id="feedback-comment")
            yield Static(
                self._count(self.initial_comment), id="comment-count", classes="hint", markup=False
            )
            yield Static("Ctrl+T saves, Esc cancels.", classes="hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#feedback-reasons", Input).focus()

    @staticmethod
    def _count(text: str) -> str:
        return f"{len(text)} / {FEEDBACK_COMMENT_MAX} characters"

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "feedback-comment":
            self.query_one("#comment-count", Static).update(self._count(event.text_area.text))

    def action_save(self) -> None:
        raw = self.query_one("#feedback-reasons", Input).value
        reasons = [reason.strip() for reason in raw.split(",") if reason.strip()]
        unknown = [reason for reason in reasons if reason not in FEEDBACK_REASONS]
        if unknown:
            self.query_one("#feedback-reasons", Input).focus()
            self.notify(f"Not a reason the server knows: {', '.join(unknown)}", severity="warning")
            return
        comment = self.query_one("#feedback-comment", TextArea).text
        self.dismiss((self.message_id, self.rating, reasons, comment or None))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class RoleEditor(ModalScreen[str | None]):
    """The system text of one prompt role. Ctrl+T hands the text back
    ("" keeps the name with an empty prompt, which is rarely wanted, so
    the screen refuses it), Esc hands back None."""

    BINDINGS = [
        Binding("ctrl+t", "save", "Save"),
        Binding("escape", "dismiss_none", "Cancel"),
    ]
    DEFAULT_CSS = """
    RoleEditor { align: center middle; }
    RoleEditor > Vertical { width: 70; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 1 2; }
    RoleEditor TextArea { height: 8; }
    """

    def __init__(self, name: str, text: str = "") -> None:
        super().__init__()
        self.role_name = name
        self.initial = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"System text for the role “{self.role_name}” — applied to a conversation "
                "as its instructions.",
                markup=False,
            )
            yield TextArea(self.initial, id="role-text")
            yield Static("Ctrl+T saves, Esc cancels.", classes="hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#role-text", TextArea).focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one("#role-text", TextArea).text)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class Roles(ModalScreen[str | None]):
    """The prompt library: the named system texts on this device. Enter
    hands the highlighted role's name back to the app to apply it to the
    open conversation, `a` makes one (a name, then its system text), `d`
    deletes the highlighted one after a confirm, Esc closes. Every write
    answers with the library as the app now holds it, so the list redraws
    from that."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("enter", "apply", "Apply"),
        Binding("a", "add", "Add"),
        Binding("d", "delete", "Delete"),
    ]
    DEFAULT_CSS = """
    Roles { align: center middle; }
    Roles > Vertical { width: 70; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(
        self,
        roles: dict[str, str],
        *,
        add: Callable[[str, str], Awaitable[dict[str, str]]],
        delete: Callable[[str], Awaitable[dict[str, str]]],
    ) -> None:
        super().__init__()
        self.roles = roles
        self._add = add
        self._delete = delete

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._headline(), id="roles-head", markup=False)
            yield OptionList(*self._options(), id="role-rows")

    def on_mount(self) -> None:
        self.query_one("#role-rows", OptionList).focus()

    def _headline(self) -> str:
        return (
            f"Prompt roles — {len(self.roles)} saved "
            "(Enter applies the highlighted one to this chat, a makes one, "
            "d deletes it, Esc closes)"
        )

    def _options(self) -> list[Option]:
        return [
            Option(f"{name} — {self._one_line(text)}", id=f"role-{i}")
            for i, (name, text) in enumerate(sorted(self.roles.items()))
        ]

    @staticmethod
    def _one_line(text: str, limit: int = 60) -> str:
        flat = " ".join(text.split())
        return flat if len(flat) <= limit else flat[: limit - 1] + "…"

    def _redraw(self) -> None:
        option_list = self.query_one("#role-rows", OptionList)
        keep = option_list.highlighted
        self.query_one("#roles-head", Static).update(self._headline())
        option_list.set_options(self._options())
        if keep is not None and option_list.option_count:
            option_list.highlighted = min(keep, option_list.option_count - 1)

    def _highlighted_name(self) -> str | None:
        option_list = self.query_one("#role-rows", OptionList)
        index = option_list.highlighted
        if index is None:
            return None
        option_id = option_list.get_option_at_index(index).id or ""
        if not option_id.startswith("role-"):
            return None
        i = int(option_id.removeprefix("role-"))
        names = sorted(self.roles)
        return names[i] if i < len(names) else None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter on the list is the same as Enter anywhere else: the
        highlighted role applies."""
        self.action_apply()

    def action_apply(self) -> None:
        name = self._highlighted_name()
        if name is None:
            self.notify("Highlight a role to apply.")
            return
        self.dismiss(name)

    def action_add(self) -> None:
        def named(name: str | None) -> None:
            if not name:
                return
            if name in self.roles:
                self.notify(f"A role named {name} is already there; it is open for editing.")
            editor = RoleEditor(name, self.roles.get(name, ""))

            def written(text: str | None) -> None:
                if text is None:
                    return
                self.run_worker(self._role_written(name, text), exclusive=True)

            self.app.push_screen(editor, written)

        self.app.push_screen(Prompt("Name the role:"), named)

    async def _role_written(self, name: str, text: str) -> None:
        self.roles = await self._add(name, text)
        self._redraw()

    def action_delete(self) -> None:
        name = self._highlighted_name()
        if name is None:
            return

        def chosen(confirmation: bool | None) -> None:
            if confirmation:
                self.run_worker(self._role_deleted(name), exclusive=True)

        self.app.push_screen(Confirm(f"Delete the role “{name}”?", danger=False), chosen)

    async def _role_deleted(self, name: str) -> None:
        self.roles = await self._delete(name)
        self._redraw()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class PendingAttachments(ModalScreen[str | None]):
    """The files waiting for the open conversation's next message: Enter
    or `x` hands the highlighted one's id back to be taken off, Esc hands
    back None."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("x,delete", "remove", "Remove"),
    ]
    DEFAULT_CSS = """
    PendingAttachments { align: center middle; }
    PendingAttachments > Vertical { width: 70; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        super().__init__()
        # (attachment id, the label the status line gives it)
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "Attached to the next message (Enter or x removes the highlighted one, "
                "Esc cancels)",
                markup=False,
            )
            yield OptionList(
                *(Option(label, id=f"attachment-{i}") for i, (_, label) in enumerate(self.rows)),
                id="attachment-rows",
            )

    def on_mount(self) -> None:
        self.query_one("#attachment-rows", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_remove()

    def action_remove(self) -> None:
        index = self.query_one("#attachment-rows", OptionList).highlighted
        if index is not None and index < len(self.rows):
            self.dismiss(self.rows[index][0])

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class Accounts(ModalScreen[dict[str, Any] | None]):
    """The signed-in accounts, the acting one marked: Enter makes the
    highlighted parked account the acting one (the acting row itself is
    not offered), Esc hands back None. The app reloads what it had open,
    which now belongs to the other account."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]
    DEFAULT_CSS = """
    Accounts { align: center middle; }
    Accounts > Vertical { width: 70; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Signed-in accounts (Enter switches, Esc cancels)", markup=False)
            yield OptionList(*self._options(), id="account-rows")

    def on_mount(self) -> None:
        self.query_one("#account-rows", OptionList).focus()

    def _options(self) -> list[Option]:
        options: list[Option] = []
        for i, row in enumerate(self.rows):
            who = str(row.get("email") or row.get("name") or row.get("subject") or "(unknown)")
            org = row.get("org_name")
            if org:
                who += f" — {org}"
            # Slot 0 is the acting account; switching to it would be a
            # no-op, so it is marked and disabled rather than offered.
            current = row.get("current") is True or row.get("slot") == 0
            options.append(
                Option(
                    who + (f"  ({markers.badge('current')})" if current else ""),
                    id=f"account-{i}",
                    disabled=current,
                )
            )
        return options

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id or ""
        if not option_id.startswith("account-"):
            return
        index = int(option_id.removeprefix("account-"))
        if index < len(self.rows):
            self.dismiss(self.rows[index])

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class AccountState:
    """What the account screen draws: the summary lines (plan, billing,
    moderation), the API keys, the browser sessions, the preferences and
    the person's own instructions, with the listed prefix of this
    terminal's own key."""

    summary: list[str]
    keys: list[dict[str, Any]]
    sessions: list[dict[str, Any]]
    preferences: dict[str, Any]
    instructions: dict[str, str]
    own_prefix: str | None


# The preference switches the screen offers, by their place in the shape.
PREFERENCE_SWITCHES: tuple[tuple[tuple[str, str], str], ...] = (
    (("email", "product_updates"), "Product update emails"),
    (("email", "usage_alerts"), "Usage alert emails"),
    (("notifications", "budget_warn"), "Budget warning notifications"),
)


@dataclass(frozen=True)
class AccountActions:
    """The account screen's writes; each answers the state to redraw from,
    or None when it did not land (the app has said why)."""

    revoke_key: Callable[[str], Awaitable[AccountState | None]]
    rotate_key: Callable[[str], Awaitable[tuple[AccountState, str] | None]]
    revoke_session: Callable[[str], Awaitable[AccountState | None]]
    set_preferences: Callable[[dict[str, Any]], Awaitable[AccountState | None]]
    set_instructions: Callable[[str, str], Awaitable[AccountState | None]]


class Account(ModalScreen[None]):
    """The person's own account: the plan and billing state, moderation,
    API keys, browser sessions, preferences and instructions. Enter
    toggles a preference or edits the locale or an instruction, `x`
    revokes the highlighted key or session after a confirm, `o` rotates
    the highlighted key and shows its new secret once, Esc closes. This
    terminal's own key is marked and left alone: `logout` is how it
    goes."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("x,delete", "revoke", "Revoke"),
        Binding("o", "rotate", "Rotate"),
    ]
    DEFAULT_CSS = """
    Account { align: center middle; }
    Account > Vertical { width: 90; height: auto; max-height: 90%;
        border: round $accent; background: $surface; padding: 0 1; }
    Account #account-summary { color: $text-muted; }
    """

    def __init__(self, state: AccountState, actions: AccountActions) -> None:
        super().__init__()
        self.state = state
        self.actions = actions
        self.rows: list[tuple[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._summary(), id="account-summary", markup=False)
            yield OptionList(*self._options(), id="account-rows")
            yield Static(
                "Enter toggles or edits, x revokes a key or session, o rotates a key, Esc closes.",
                classes="hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#account-rows", OptionList).focus()

    def _summary(self) -> str:
        return "\n".join(self.state.summary)

    def _options(self) -> list[Option]:
        self.rows = []
        options: list[Option] = []

        def heading(title: str) -> None:
            self.rows.append(("head", None))
            options.append(Option(f"— {title} —", id=f"row-{len(self.rows)}", disabled=True))

        def row(kind: str, payload: Any, label: str) -> None:
            self.rows.append((kind, payload))
            options.append(Option(label, id=f"row-{len(self.rows)}"))

        heading("API keys")
        for key in self.state.keys:
            mine = self.state.own_prefix and key.get("prefix") == self.state.own_prefix
            state = "revoked" if key.get("revoked_at") else str(key.get("kind") or "user")
            used = str(key.get("last_used_at") or "never")[:10]
            label = f"{key.get('name') or key.get('prefix')} · {key.get('prefix')} · {state}"
            label += f" · last used {used}" + (" · this terminal" if mine else "")
            row("key", key, label)
        heading("Signed-in browsers")
        for session in self.state.sessions:
            agent = str(session.get("user_agent") or "unknown browser")
            label = f"{agent[:50]} · {session.get('ip') or '?'}"
            label += f" · last seen {str(session.get('last_seen_at') or '')[:10]}"
            row("session", session, label)
        heading("Preferences")
        prefs = self.state.preferences
        for path, name in PREFERENCE_SWITCHES:
            group = prefs.get(path[0]) if isinstance(prefs.get(path[0]), dict) else {}
            on = bool(group.get(path[1])) if isinstance(group, dict) else False
            row("switch", path, f"{name}: {'on' if on else 'off'}")
        row("locale", None, f"Locale: {prefs.get('locale') or 'not set'}")
        heading("Your instructions")
        for field_name, name in (("about", "About you"), ("style", "How to answer")):
            text = self.state.instructions.get(field_name) or "(empty)"
            flat = " ".join(text.split())
            row(
                "instructions", field_name, f"{name}: {flat[:60] + ('…' if len(flat) > 60 else '')}"
            )
        return options

    def _redraw(self, state: AccountState | None) -> None:
        if state is None:
            return
        self.state = state
        option_list = self.query_one("#account-rows", OptionList)
        keep = option_list.highlighted
        self.query_one("#account-summary", Static).update(self._summary())
        option_list.set_options(self._options())
        if keep is not None and option_list.option_count:
            option_list.highlighted = min(keep, option_list.option_count - 1)

    def _highlighted(self) -> tuple[str, Any]:
        index = self.query_one("#account-rows", OptionList).highlighted
        if index is None or index >= len(self.rows):
            return ("head", None)
        return self.rows[index]

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        kind, payload = self._highlighted()
        if kind == "switch":
            self.run_worker(self._toggle(payload), exclusive=True)
        elif kind == "locale":
            self._edit_locale()
        elif kind == "instructions":
            self._edit_instructions(payload)
        elif kind in ("key", "session"):
            self.notify("x revokes it" + (", o rotates it." if kind == "key" else "."))

    async def _toggle(self, path: tuple[str, str]) -> None:
        prefs = copy.deepcopy(self.state.preferences)
        group = prefs.setdefault(path[0], {})
        group[path[1]] = not bool(group.get(path[1]))
        self._redraw(await self.actions.set_preferences(prefs))

    def _edit_locale(self) -> None:
        def chosen(locale: str | None) -> None:
            if locale is None:
                return
            prefs = copy.deepcopy(self.state.preferences)
            prefs["locale"] = locale.strip() or None
            self.run_worker(self._saved_preferences(prefs), exclusive=True)

        self.app.push_screen(
            Prompt(
                "Locale, as a language tag (en, en-GB); empty clears it:",
                str(self.state.preferences.get("locale") or ""),
            ),
            chosen,
        )

    async def _saved_preferences(self, prefs: dict[str, Any]) -> None:
        self._redraw(await self.actions.set_preferences(prefs))

    def _edit_instructions(self, field_name: str) -> None:
        def written(text: str | None) -> None:
            if text is None:
                return
            pair = dict(self.state.instructions)
            pair[field_name] = text
            self.run_worker(
                self._saved_instructions(pair.get("about", ""), pair.get("style", "")),
                exclusive=True,
            )

        title = "About you" if field_name == "about" else "How to answer"
        self.app.push_screen(
            TextEditor(
                f"{title} — your instructions, in every conversation that has none of its own.",
                self.state.instructions.get(field_name, ""),
            ),
            written,
        )

    async def _saved_instructions(self, about: str, style: str) -> None:
        self._redraw(await self.actions.set_instructions(about, style))

    def _is_own(self, key: dict[str, Any]) -> bool:
        return bool(self.state.own_prefix) and key.get("prefix") == self.state.own_prefix

    def action_revoke(self) -> None:
        kind, payload = self._highlighted()
        if kind == "key":
            if self._is_own(payload):
                self.notify("That is this terminal's own key: `matterwave-tui logout` revokes it.")
                return
            if payload.get("revoked_at"):
                self.notify("That key is already revoked.")
                return
            key_id = str(payload.get("id") or "")
            question = f"Revoke the key “{payload.get('name') or payload.get('prefix')}”? "
            question += "Everything using it stops working at once."

            def chosen(confirmation: bool | None) -> None:
                if confirmation:
                    self.run_worker(self._revoked_key(key_id), exclusive=True)

            self.app.push_screen(Confirm(question), chosen)
        elif kind == "session":
            session_id = str(payload.get("id") or "")

            def signed_out(confirmation: bool | None) -> None:
                if confirmation:
                    self.run_worker(self._revoked_session(session_id), exclusive=True)

            self.app.push_screen(Confirm("Sign that browser out?"), signed_out)
        else:
            self.notify("Highlight a key or a browser to revoke it.")

    async def _revoked_key(self, key_id: str) -> None:
        self._redraw(await self.actions.revoke_key(key_id))

    async def _revoked_session(self, session_id: str) -> None:
        self._redraw(await self.actions.revoke_session(session_id))

    def action_rotate(self) -> None:
        kind, payload = self._highlighted()
        if kind != "key":
            self.notify("Highlight a key to rotate it.")
            return
        if self._is_own(payload):
            self.notify("That is this terminal's own key; sign in again to replace it.")
            return
        if payload.get("revoked_at"):
            self.notify("A revoked key cannot be rotated.")
            return
        key_id = str(payload.get("id") or "")

        def chosen(confirmation: bool | None) -> None:
            if confirmation:
                self.run_worker(self._rotated(key_id), exclusive=True)

        self.app.push_screen(
            Confirm(
                f"Rotate “{payload.get('name') or payload.get('prefix')}”? It gets a new secret; "
                "the old one keeps working for a grace period."
            ),
            chosen,
        )

    async def _rotated(self, key_id: str) -> None:
        rotated = await self.actions.rotate_key(key_id)
        if rotated is None:
            return
        state, secret = rotated
        self._redraw(state)
        self.app.push_screen(SecretShown(secret))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class SecretShown(ModalScreen[None]):
    """A new key's secret, shown the one time the server sends it: `y`
    copies it, Esc closes and it is gone."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("y", "copy", "Copy"),
    ]
    DEFAULT_CSS = """
    SecretShown { align: center middle; }
    SecretShown > Vertical { width: 80; height: auto; border: round $warning;
        background: $surface; padding: 1 2; }
    """

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("The new secret — copy it now; it is not shown again:", markup=False)
            yield Static(self.secret, id="secret", markup=False)
            yield Static("y copies it, Esc closes.", classes="hint", markup=False)

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.secret)
        self.notify("Copied.")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class TextEditor(ModalScreen[str | None]):
    """A few lines of text under a title: Ctrl+T hands them back, Esc
    hands back None."""

    BINDINGS = [
        Binding("ctrl+t", "save", "Save"),
        Binding("escape", "dismiss_none", "Cancel"),
    ]
    DEFAULT_CSS = """
    TextEditor { align: center middle; }
    TextEditor > Vertical { width: 70; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 1 2; }
    TextEditor TextArea { height: 8; }
    """

    def __init__(self, title: str, text: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, markup=False)
            yield TextArea(self.initial, id="text")
            yield Static("Ctrl+T saves, Esc cancels.", classes="hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#text", TextArea).focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one("#text", TextArea).text)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Every key, as the bindings have them: the key, what it does, and
    the name the config file's [keys] table overrides it by. Esc, `?` or
    F1 closes."""

    BINDINGS = [Binding("escape,question_mark,f1", "dismiss_none", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical { width: 90; height: auto; max-height: 90%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        width = max((len(keys) for keys, _, _ in self.rows), default=0)
        with Vertical():
            yield Static("Keys (letters apply outside the composer; Esc gets there)", markup=False)
            yield OptionList(
                *(
                    Option(f"{keys.ljust(width)}  {does}" + (f"  [{name}]" if name else ""))
                    for keys, does, name in self.rows
                ),
                id="help-rows",
            )
            yield Static(
                "In the composer: /role, /shell, /temp, /pi and /pi-engine are commands; "
                '// sends a leading slash. Change a key in the config file: [keys] name = "ctrl+n".',
                classes="hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#help-rows", OptionList).focus()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class FilePicker(ModalScreen[list[str] | None]):
    """Files from a tree of a directory: Enter on a file attaches it (with
    any marked ones), Space marks or unmarks the highlighted file, Esc
    cancels."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        # Over the tree's own Space (open a folder): Enter does that too.
        Binding("space", "mark", "Mark", priority=True),
    ]
    DEFAULT_CSS = """
    FilePicker { align: center middle; }
    FilePicker > Vertical { width: 90; height: 80%;
        border: round $accent; background: $surface; padding: 0 1; }
    FilePicker DirectoryTree { height: 1fr; }
    """

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.marked: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"Attach from {self.root} (Enter attaches, Space marks several, Esc cancels)",
                markup=False,
            )
            yield DirectoryTree(self.root, id="files")
            yield Static("", id="marked", markup=False)

    def on_mount(self) -> None:
        self.query_one("#files", DirectoryTree).focus()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        chosen = str(event.path)
        self.dismiss([*self.marked, *([] if chosen in self.marked else [chosen])])

    def action_mark(self) -> None:
        node = self.query_one("#files", DirectoryTree).cursor_node
        data = node.data if node is not None else None
        path = getattr(data, "path", None)
        if path is None or not Path(path).is_file():
            return
        name = str(path)
        if name in self.marked:
            self.marked.remove(name)
        else:
            self.marked.append(name)
        names = ", ".join(Path(m).name for m in self.marked)
        self.query_one("#marked", Static).update(f"Marked: {names}" if names else "")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class Dictation(ModalScreen[tuple[bytes, str] | None]):
    """A clip from the microphone, for the transcribe route. It records
    while it is up: Enter stops and hands back (wav bytes, media type), Esc
    hands back None. It stops itself at the clip length the web app's
    recorder uses. Needs the `dictation` extra; where it is missing the app
    never pushes this screen and asks for a file instead."""

    BINDINGS = [
        Binding("enter", "finish", "Transcribe"),
        Binding("escape", "cancel", "Cancel"),
    ]
    DEFAULT_CSS = """
    Dictation { align: center middle; }
    Dictation > Vertical { width: 60; height: auto; border: round $error;
        background: $surface; padding: 1 2; }
    """

    def __init__(self, recorder: MicRecorder) -> None:
        super().__init__()
        self.recorder = recorder
        self._finished = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("● Recording the microphone.", markup=False)
            yield Static(
                f"Enter stops and transcribes (up to {MAX_RECORDING_S}s); Esc cancels.",
                classes="hint",
                markup=False,
            )
            yield Static("", id="dictation-note", markup=False)

    def on_mount(self) -> None:
        self.run_worker(self._start(), exclusive=True)
        # The web recorder's own cap: sixty seconds, then it stops itself.
        self.set_timer(MAX_RECORDING_S, self._cap_reached)

    async def _start(self) -> None:
        try:
            await asyncio.to_thread(self.recorder.start)
        except Exception as exc:
            self.query_one("#dictation-note", Static).update(f"Microphone unavailable: {exc}")

    def _cap_reached(self) -> None:
        if not self._finished:
            self.run_worker(self.action_finish(), exclusive=True)

    async def action_finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            clip = await asyncio.to_thread(self.recorder.stop)
        except Exception:
            clip = b""
        # An empty clip is handed over too (Esc is the cancel): the app is
        # the one that says nothing was captured.
        self.dismiss((clip, "audio/wav"))

    async def action_cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        # The recording is abandoned: whatever closing the microphone says,
        # the screen still closes.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.recorder.discard)
        self.dismiss(None)


class Notifications(ModalScreen[None]):
    """The bell's rows, newest first, a page at a time: `r` marks the
    highlighted one read, `a` reads them all, `u` shows only the unread
    ones, `n` and `p` turn pages, Esc closes. The route has no cursor, so
    the page after the loaded ones is a longer limit, capped at the
    route's own maximum. What stays unread rides the status line. The app
    hands in the rows and the writes; they answer the rows as the server
    now holds them, so the list redraws from that."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("r", "read", "Read"),
        Binding("a", "read_all", "Read all"),
        Binding("u", "toggle_unread", "Unread only"),
        Binding("n", "next_page", "Next page"),
        Binding("p", "previous_page", "Previous page"),
    ]
    DEFAULT_CSS = """
    Notifications { align: center middle; }
    Notifications > Vertical { width: 80; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        mark_read: Callable[[str], Awaitable[list[dict[str, Any]]]],
        mark_all: Callable[[], Awaitable[list[dict[str, Any]]]],
        load_page: Callable[[bool, int], Awaitable[list[dict[str, Any]]]],
        unread_only: bool = False,
        page_size: int = NOTIFICATIONS_PAGE,
    ) -> None:
        super().__init__()
        self.rows = rows
        self._mark_read = mark_read
        self._mark_all = mark_all
        self._load_page = load_page
        self.unread_only = unread_only
        self.page_size = page_size
        self.page = 1

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._headline(), id="notifications-head", markup=False)
            yield OptionList(*self._options(), id="notification-rows")

    def on_mount(self) -> None:
        self.query_one("#notification-rows", OptionList).focus()

    @staticmethod
    def _one_line(text: str, limit: int = 100) -> str:
        flat = " ".join(text.split())
        return flat if len(flat) <= limit else flat[: limit - 1] + "…"

    def _pages(self) -> int:
        return max(1, -(-len(self.rows) // self.page_size))

    def _slice(self) -> list[dict[str, Any]]:
        """The page the list shows: the loaded rows, cut to this page."""
        start = (self.page - 1) * self.page_size
        return self.rows[start : start + self.page_size]

    def _headline(self) -> str:
        if not self.rows:
            return "No unread notifications." if self.unread_only else "No notifications."
        unread = sum(1 for row in self.rows if not row.get("read_at"))
        return (
            f"Notifications — {unread} unread of {len(self.rows)} loaded, "
            f"page {self.page} of {self._pages()} "
            "(r reads the highlighted one, a reads all, u unread only, "
            "n and p turn pages, Esc closes)"
        )

    def _options(self) -> list[Option]:
        options: list[Option] = []
        for i, row in enumerate(self._slice()):
            mark = f"{markers.badge('unread')} · " if not row.get("read_at") else ""
            label = mark + str(row.get("title") or "(no title)")
            body = str(row.get("body") or "")
            if body:
                label += f" — {self._one_line(body)}"
            options.append(Option(label, id=f"notification-{i}"))
        return options

    def _redraw(self, keep: int | None) -> None:
        self.query_one("#notifications-head", Static).update(self._headline())
        option_list = self.query_one("#notification-rows", OptionList)
        option_list.set_options(self._options())
        if keep is not None and option_list.option_count:
            option_list.highlighted = min(keep, option_list.option_count - 1)

    @work(exclusive=True)
    async def action_read(self) -> None:
        option_list = self.query_one("#notification-rows", OptionList)
        index = option_list.highlighted
        slice_rows = self._slice()
        if index is None or index >= len(slice_rows):
            return
        notification_id = str(slice_rows[index].get("id") or "")
        if not notification_id:
            return
        self.rows = await self._mark_read(notification_id)
        self._redraw(index)

    @work(exclusive=True)
    async def action_read_all(self) -> None:
        self.rows = await self._mark_all()
        self._redraw(self.query_one("#notification-rows", OptionList).highlighted)

    @work(exclusive=True)
    async def action_toggle_unread(self) -> None:
        """Only what is unread: the fetch carries `unread=1` and the page
        starts over, the loaded set being a different one now."""
        self.unread_only = not self.unread_only
        self.rows = await self._load_page(self.unread_only, self.page_size)
        self.page = 1
        self._redraw(0)

    @work(exclusive=True)
    async def action_next_page(self) -> None:
        if self.page >= self._pages():
            # Every loaded row is on show: the next page is a longer limit.
            wanted = min(self.page_size * (self.page + 1), NOTIFICATIONS_LIMIT_MAX)
            self.rows = await self._load_page(self.unread_only, wanted)
            if len(self.rows) <= self.page_size * self.page:
                self.notify("No more notifications.")
                self._redraw(self.query_one("#notification-rows", OptionList).highlighted)
                return
        self.page += 1
        self._redraw(0)

    @work(exclusive=True)
    async def action_previous_page(self) -> None:
        if self.page <= 1:
            return
        self.page -= 1
        self._redraw(0)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class Shares(ModalScreen[None]):
    """Every link the caller has made, the one place to see what is out
    there and take it back: `x` revokes the highlighted live link, after a
    confirm; Esc closes. A revoked link stops answering at once."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("x", "revoke", "Revoke"),
    ]
    DEFAULT_CSS = """
    Shares { align: center middle; }
    Shares > Vertical { width: 80; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        revoke: Callable[[str], Awaitable[list[dict[str, Any]]]],
    ) -> None:
        super().__init__()
        self.rows = rows
        self._revoke = revoke

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._headline(), id="shares-head", markup=False)
            yield OptionList(*self._options(), id="share-rows")

    def on_mount(self) -> None:
        self.query_one("#share-rows", OptionList).focus()

    def _headline(self) -> str:
        if not self.rows:
            return "You have made no share links."
        live = sum(1 for row in self.rows if row.get("status") == "active")
        return f"Your share links — {live} live (x revokes the highlighted one, Esc closes)"

    def _options(self) -> list[Option]:
        options: list[Option] = []
        for i, row in enumerate(self.rows):
            status = str(row.get("status") or "active")
            mark = {"active": "● ", "expired": "… ", "revoked": "✗ "}.get(status, "  ")
            label = mark + str(row.get("title") or "(untitled)")
            views = row.get("view_count")
            if isinstance(views, int):
                label += f" — {views} views"
            options.append(Option(f"{label} — {status}", id=f"share-{i}"))
        return options

    def _redraw(self, keep: int | None) -> None:
        self.query_one("#shares-head", Static).update(self._headline())
        option_list = self.query_one("#share-rows", OptionList)
        option_list.set_options(self._options())
        if keep is not None:
            option_list.highlighted = keep

    @work(exclusive=True)
    async def action_revoke(self) -> None:
        option_list = self.query_one("#share-rows", OptionList)
        index = option_list.highlighted
        if index is None or index >= len(self.rows):
            return
        row = self.rows[index]
        share_id = str(row.get("id") or "")
        if not share_id or row.get("status") != "active":
            self.notify("That link is not live.")
            return
        title = str(row.get("title") or "this link")

        def chosen(confirmation: bool | None) -> None:
            if confirmation:
                self.run_worker(self._revoke_row(index, share_id), exclusive=True)

        self.app.push_screen(Confirm(f"Revoke “{title}”? Its link stops working."), chosen)

    async def _revoke_row(self, index: int, share_id: str) -> None:
        self.rows = await self._revoke(share_id)
        self._redraw(min(index, max(len(self.rows) - 1, 0)))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# How long a new share link lives, as the dialog offers it: (label, seconds,
# None for until revoked). The platform takes any span from a minute to a
# year.
SHARE_EXPIRIES: tuple[tuple[str, int | None], ...] = (
    ("never (until revoked)", None),
    ("in 1 hour", 60 * 60),
    ("in 1 day", 24 * 60 * 60),
    ("in 7 days", 7 * 24 * 60 * 60),
    ("in 30 days", 30 * 24 * 60 * 60),
)


class ShareDialog(ModalScreen[None]):
    """Sharing the open conversation: Enter on "Expires" cycles how long a
    new link lives, on "Attachments" toggles whether readers can open the
    files, on "Create the link" makes it -- the link is shown and copied
    -- and `x` revokes the highlighted one of the links made for this
    conversation, after a confirm. Esc closes."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("x,delete", "revoke", "Revoke"),
    ]
    DEFAULT_CSS = """
    ShareDialog { align: center middle; }
    ShareDialog > Vertical { width: 80; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(
        self,
        title: str,
        rows: list[dict[str, Any]],
        *,
        create: Callable[[int | None, bool], Awaitable[tuple[str, list[dict[str, Any]]] | None]],
        revoke: Callable[[str], Awaitable[list[dict[str, Any]]]],
    ) -> None:
        super().__init__()
        self.title_text = title
        self.rows = rows
        self._create = create
        self._revoke = revoke
        self.expiry = 0
        self.include_attachments = False
        self.link = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Share “{self.title_text}”", markup=False)
            yield OptionList(*self._options(), id="share-options")
            yield Static("", id="share-link", markup=False)
            yield Static(
                "Enter changes or creates, x revokes a link, Esc closes.",
                classes="hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#share-options", OptionList).focus()

    def _options(self) -> list[Option]:
        options = [
            Option(f"Expires: {SHARE_EXPIRIES[self.expiry][0]}", id="expiry"),
            Option(
                "Attachments: "
                + ("readers can open them" if self.include_attachments else "names only"),
                id="attachments",
            ),
            Option("Create the link", id="create"),
            Option("— Links to this conversation —", id="head", disabled=True),
        ]
        for i, row in enumerate(self.rows):
            status = str(row.get("status") or "active")
            views = row.get("view_count")
            label = f"{str(row.get('created_at') or '')[:10]} · {status}"
            if isinstance(views, int):
                label += f" · {views} views"
            if row.get("expires_at"):
                label += f" · expires {str(row.get('expires_at'))[:10]}"
            options.append(Option(label, id=f"share-{i}"))
        return options

    def _redraw(self) -> None:
        option_list = self.query_one("#share-options", OptionList)
        keep = option_list.highlighted
        option_list.set_options(self._options())
        if keep is not None and option_list.option_count:
            option_list.highlighted = min(keep, option_list.option_count - 1)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id or ""
        if option_id == "expiry":
            self.expiry = (self.expiry + 1) % len(SHARE_EXPIRIES)
            self._redraw()
        elif option_id == "attachments":
            self.include_attachments = not self.include_attachments
            self._redraw()
        elif option_id == "create":
            self.run_worker(self._created(), exclusive=True)

    async def _created(self) -> None:
        made = await self._create(SHARE_EXPIRIES[self.expiry][1], self.include_attachments)
        if made is None:
            return
        self.link, self.rows = made
        self.query_one("#share-link", Static).update(f"Link (copied): {self.link}")
        self._redraw()

    def action_revoke(self) -> None:
        option_list = self.query_one("#share-options", OptionList)
        index = option_list.highlighted
        option_id = option_list.get_option_at_index(index).id if index is not None else ""
        if not option_id or not option_id.startswith("share-"):
            self.notify("Highlight a link to revoke it.")
            return
        row = self.rows[int(option_id.removeprefix("share-"))]
        share_id = str(row.get("id") or "")
        if row.get("status") != "active" or not share_id:
            self.notify("That link is not live.")
            return

        def chosen(confirmation: bool | None) -> None:
            if confirmation:
                self.run_worker(self._revoked(share_id), exclusive=True)

        self.app.push_screen(Confirm("Revoke this link? It stops working at once."), chosen)

    async def _revoked(self, share_id: str) -> None:
        self.rows = await self._revoke(share_id)
        self._redraw()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class Library(ModalScreen[tuple[str, str] | None]):
    """The folders and tags, and the open conversation's place among them:
    `f` makes a folder, `t` makes a tag, `m` files the conversation in the
    highlighted folder (`m` again takes it back out), Space toggles the
    highlighted tag on it, `r` renames the highlighted folder, `x` deletes
    the highlighted folder or tag after a confirm, Enter hands back
    ("folder" | "tag", id) for the sidebar to show only that, Esc closes.
    Every write answers with the library as the app now holds it, and the
    sidebar's captions follow."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("f", "new_folder", "Folder"),
        Binding("t", "new_tag", "Tag"),
        Binding("m", "move", "Move"),
        Binding("space", "toggle_tag", "Tag it"),
        Binding("r", "rename", "Rename"),
        Binding("x,delete", "delete", "Delete"),
        Binding("enter", "show_only", "Show only"),
    ]
    DEFAULT_CSS = """
    Library { align: center middle; }
    Library > Vertical { width: 70; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(
        self,
        folders: list[dict[str, Any]],
        tags: list[dict[str, Any]],
        assignments: dict[str, list[str]],
        conversation_id: str,
        folder_id: str | None,
        *,
        new_folder: Callable[[str], Awaitable[LibraryState | None]],
        new_tag: Callable[[str], Awaitable[LibraryState | None]],
        move_to: Callable[[str | None], Awaitable[LibraryState | None]],
        set_tags: Callable[[list[str]], Awaitable[LibraryState | None]],
        rename_folder: Callable[[str, str], Awaitable[LibraryState | None]],
        delete_folder: Callable[[str], Awaitable[LibraryState | None]],
        delete_tag: Callable[[str], Awaitable[LibraryState | None]],
    ) -> None:
        super().__init__()
        self.folders = folders
        self.tags = tags
        self.assignments = assignments
        self.conversation_id = conversation_id
        self.folder_id = folder_id
        self._new_folder = new_folder
        self._new_tag = new_tag
        self._move_to = move_to
        self._set_tags = set_tags
        self._rename_folder = rename_folder
        self._delete_folder = delete_folder
        self._delete_tag = delete_tag

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._headline(), id="library-head", markup=False)
            yield OptionList(*self._options(), id="library-rows")

    def on_mount(self) -> None:
        self.query_one("#library-rows", OptionList).focus()

    def _headline(self) -> str:
        mine = self.assignments.get(self.conversation_id, [])
        return (
            f"Library — {len(self.folders)} folders, {len(self.tags)} tags, "
            f"{len(mine)} on this chat (f makes a folder, t a tag, m files this chat in the "
            "highlighted folder, Space toggles the highlighted tag, r renames a folder, "
            "x deletes, Enter shows only that one in the sidebar, Esc closes)"
        )

    def _options(self) -> list[Option]:
        options: list[Option] = [Option("— Folders —", id="head-folders", disabled=True)]
        for i, folder in enumerate(self.folders):
            here = folder.get("id") == self.folder_id
            name = str(folder.get("name") or "(unnamed)")
            options.append(Option(name + ("  (this chat)" if here else ""), id=f"folder-{i}"))
        options.append(Option("— Tags —", id="head-tags", disabled=True))
        mine = self.assignments.get(self.conversation_id, [])
        for i, tag in enumerate(self.tags):
            on = tag.get("id") in mine
            name = str(tag.get("name") or "(unnamed)")
            options.append(Option(name + ("  (on this chat)" if on else ""), id=f"tag-{i}"))
        return options

    def _redraw(self) -> None:
        option_list = self.query_one("#library-rows", OptionList)
        keep = option_list.highlighted
        self.query_one("#library-head", Static).update(self._headline())
        option_list.set_options(self._options())
        if keep is not None and option_list.option_count:
            option_list.highlighted = min(keep, option_list.option_count - 1)

    def _highlighted(self) -> tuple[str, int] | None:
        """The kind and index of the highlighted row: a folder or a tag,
        never a heading. None when the highlight is nowhere usable."""
        option_list = self.query_one("#library-rows", OptionList)
        index = option_list.highlighted
        if index is None:
            return None
        option_id = option_list.get_option_at_index(index).id or ""
        for kind in ("folder", "tag"):
            if option_id.startswith(f"{kind}-"):
                return kind, int(option_id.removeprefix(f"{kind}-"))
        return None

    def _adopt(self, state: LibraryState | None) -> None:
        """The library as the write left it; a write that did not land
        leaves the screen as it was."""
        if state is not None:
            self.folders, self.tags, self.assignments, self.folder_id = state
            self._redraw()

    def action_new_folder(self) -> None:
        def chosen(name: str | None) -> None:
            if name:
                self.run_worker(self._folder_made(name), exclusive=True)

        self.app.push_screen(Prompt("Name the new folder:"), chosen)

    async def _folder_made(self, name: str) -> None:
        self._adopt(await self._new_folder(name))

    def action_new_tag(self) -> None:
        def chosen(name: str | None) -> None:
            if name:
                self.run_worker(self._tag_made(name), exclusive=True)

        self.app.push_screen(Prompt("Name the new tag:"), chosen)

    async def _tag_made(self, name: str) -> None:
        self._adopt(await self._new_tag(name))

    @work(exclusive=True)
    async def action_move(self) -> None:
        """Into the highlighted folder; `m` on the folder the chat is
        already in takes it back to the main list."""
        found = self._highlighted()
        if found is None or found[0] != "folder":
            self.notify("Highlight a folder to file this chat in.")
            return
        folder_id = str(self.folders[found[1]].get("id") or "")
        # The folder the chat is already in takes it back out: m toggles.
        target = None if not folder_id or folder_id == self.folder_id else folder_id
        self._adopt(await self._move_to(target))

    @work(exclusive=True)
    async def action_toggle_tag(self) -> None:
        found = self._highlighted()
        if found is None or found[0] != "tag":
            self.notify("Highlight a tag to toggle it on this chat.")
            return
        tag_id = str(self.tags[found[1]].get("id") or "")
        mine = list(self.assignments.get(self.conversation_id, []))
        if tag_id in mine:
            mine.remove(tag_id)
        else:
            mine.append(tag_id)
        self._adopt(await self._set_tags(mine))

    def action_rename(self) -> None:
        found = self._highlighted()
        if found is None or found[0] != "folder":
            self.notify("Highlight a folder to rename it.")
            return
        folder = self.folders[found[1]]
        folder_id = str(folder.get("id") or "")

        def chosen(name: str | None) -> None:
            if name and folder_id:
                self.run_worker(self._renamed(folder_id, name), exclusive=True)

        self.app.push_screen(Prompt("Rename the folder:", str(folder.get("name") or "")), chosen)

    async def _renamed(self, folder_id: str, name: str) -> None:
        self._adopt(await self._rename_folder(folder_id, name))

    def action_delete(self) -> None:
        """The highlighted folder (its conversations go back to the main
        list) or tag (it comes off every conversation), after a confirm."""
        found = self._highlighted()
        if found is None:
            self.notify("Highlight a folder or a tag to delete it.")
            return
        kind, index = found
        row = (self.folders if kind == "folder" else self.tags)[index]
        row_id = str(row.get("id") or "")
        name = str(row.get("name") or "")
        question = (
            f"Delete the folder “{name}”? Its conversations go back to the main list."
            if kind == "folder"
            else f"Delete the tag “{name}”? It comes off every conversation."
        )

        def chosen(confirmation: bool | None) -> None:
            if confirmation and row_id:
                self.run_worker(self._deleted(kind, row_id), exclusive=True)

        self.app.push_screen(Confirm(question), chosen)

    async def _deleted(self, kind: str, row_id: str) -> None:
        remove = self._delete_folder if kind == "folder" else self._delete_tag
        self._adopt(await remove(row_id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_show_only()

    def action_show_only(self) -> None:
        found = self._highlighted()
        if found is None:
            return
        kind, index = found
        row = (self.folders if kind == "folder" else self.tags)[index]
        if row.get("id"):
            self.dismiss((kind, str(row["id"])))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
