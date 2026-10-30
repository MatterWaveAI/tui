"""Modal screens: the model picker, the in-app device sign-in, the one-line
prompt and yes/no the conversation actions share, the per-thread custom
instructions editor, the reply-feedback dialog, the notifications inbox,
the share-links list, the library of folders and tags, the account switcher
and the microphone clip recorder."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from matterwave_sdk import FEEDBACK_REASONS
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .api import NOTIFICATIONS_LIMIT_MAX, NOTIFICATIONS_PAGE
from .auth import DeviceCode, LoginError
from .dictation import MAX_RECORDING_S, MicRecorder
from .models import picker_rows

# The per-thread instructions budget: shown as a counter, never enforced
# here -- the server is authoritative and refuses more with a 422.
INSTRUCTIONS_MAX = 2000
FEEDBACK_COMMENT_MAX = 2000
REASONS_HINT = "Reasons, comma-separated from: " + ", ".join(FEEDBACK_REASONS)

# What a library write hands back to the screen that asked for it: the
# folders, the tags, which conversation carries which tags, and the folder
# the open conversation is in -- as the app now holds them. None means the
# write did not land, and the screen keeps what it had.
LibraryState = tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]], str | None
]


class ModelPicker(ModalScreen[str | None]):
    """Grouped like the web picker; a model the plan lacks is shown, not chosen."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]
    DEFAULT_CSS = """
    ModelPicker { align: center middle; }
    ModelPicker > Vertical { width: 80%; max-width: 90; height: auto; max-height: 80%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(self, models: list[dict[str, Any]], current: str | None) -> None:
        super().__init__()
        self.rows = picker_rows(models)
        self.current = current

    def compose(self) -> ComposeResult:
        options: list[Option] = []
        for i, row in enumerate(self.rows):
            if row.is_heading:
                options.append(Option(f"— {row.label} —", id=f"heading-{i}", disabled=True))
            else:
                mark = "● " if row.model_id == self.current else "  "
                options.append(
                    Option(mark + row.label, id=f"model-{i}", disabled=not row.selectable)
                )
        with Vertical():
            yield Static("Choose a model (Enter selects, Esc cancels)", markup=False)
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
            yield Static(
                self.hint or "Enter confirms, Esc cancels.", classes="hint", markup=False
            )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


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
            yield Static(
                "Signed-in accounts (Enter switches, Esc cancels)", markup=False
            )
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
                Option(("● " if current else "  ") + who, id=f"account-{i}", disabled=current)
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
        return (f"Notifications — {unread} unread of {len(self.rows)} loaded, "
                f"page {self.page} of {self._pages()} "
                "(r reads the highlighted one, a reads all, u unread only, "
                "n and p turn pages, Esc closes)")

    def _options(self) -> list[Option]:
        options: list[Option] = []
        for i, row in enumerate(self._slice()):
            mark = "● " if not row.get("read_at") else "  "
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


class Library(ModalScreen[None]):
    """The folders and tags, and the open conversation's place among them:
    `f` makes a folder, `t` makes a tag, `m` files the conversation in the
    highlighted folder (`m` again takes it back out), Space toggles the
    highlighted tag on it, Esc closes. Every write answers with the
    library as the app now holds it, and the sidebar's captions follow."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("f", "new_folder", "Folder"),
        Binding("t", "new_tag", "Tag"),
        Binding("m", "move", "Move"),
        Binding("space", "toggle_tag", "Tag it"),
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

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._headline(), id="library-head", markup=False)
            yield OptionList(*self._options(), id="library-rows")

    def on_mount(self) -> None:
        self.query_one("#library-rows", OptionList).focus()

    def _headline(self) -> str:
        mine = self.assignments.get(self.conversation_id, [])
        return (f"Library — {len(self.folders)} folders, {len(self.tags)} tags, "
                f"{len(mine)} on this chat "
                "(f makes a folder, t a tag, m files this chat in the highlighted "
                "folder, Space toggles the highlighted tag, Esc closes)")

    def _options(self) -> list[Option]:
        options: list[Option] = [Option("— Folders —", id="head-folders", disabled=True)]
        for i, folder in enumerate(self.folders):
            mark = "● " if folder.get("id") == self.folder_id else "  "
            options.append(Option(mark + str(folder.get("name") or "(unnamed)"), id=f"folder-{i}"))
        options.append(Option("— Tags —", id="head-tags", disabled=True))
        mine = self.assignments.get(self.conversation_id, [])
        for i, tag in enumerate(self.tags):
            mark = "● " if tag.get("id") in mine else "  "
            options.append(Option(mark + str(tag.get("name") or "(unnamed)"), id=f"tag-{i}"))
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

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
