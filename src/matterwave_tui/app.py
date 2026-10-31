"""The Textual app: conversation list, transcript, composer, status line."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from matterwave_sdk import frames as fr
from matterwave_sdk.errors import APIConnectionError, APIError, ConflictError
from matterwave_sdk.models import Attachment, Conversation, ConversationMessage, UsageBalance
from textual import on, work
from textual.app import App, ComposeResult, get_system_commands_provider
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static, TextArea

from . import images
from .api import NOTIFICATIONS_PAGE, ChatAPI, Cut, Opened, StreamItem
from .dictation import MEDIA_TYPES, MicRecorder, media_type_for, mic_available
from .export import conversation_markdown, export_filename
from .failures import Failure, classify, retry_in
from .models import is_image_filename, model_accepts_images, model_window, vision_capable_model_ids
from .screens import (
    Accounts,
    Confirm,
    DeviceLogin,
    DeviceLoginFn,
    Dictation,
    FeedbackDialog,
    InstructionsEditor,
    Library,
    LibraryState,
    ModelPicker,
    Notifications,
    Prompt,
    Shares,
)
from .thread import Divider, Message, Thread
from .widgets import DividerView, ImageView, MessageView, ThumbClicked

# How often a stream redraws the answer in progress. A redraw per delta is
# hundreds a second on a fast model and buys nothing a person can see.
LIVE_REDRAW_S = 1 / 15
OFFLINE_PROBE_S = 5.0
SEARCH_DEBOUNCE_S = 0.3
# How often the image layer re-checks where the pictures it has drawn
# should sit (the transcript scrolls under them).
IMAGE_REFRESH_S = 0.25
# The id of the answer being streamed, before the server has named it.
LIVE_ID = "_live"
# The sidebar list's slices, in the order the `List` key cycles them. The
# archived ones are only reachable here, which is what makes unarchiving
# possible at all.
LIST_FILTERS = ("default", "pinned", "archived", "all")
# What a temporary chat is, in one line, shown while one is open.
TEMPORARY_NOTICE = (
    "Temporary chat. It won't appear in your history or search, can't be shared, "
    "and is deleted 24 hours after the last message."
)
# Importing a ChatGPT or Claude export: the API's cap and the poll, as the
# web app's Data pane mirrors them (web/src/apps/settings/importApi.ts).
IMPORT_MAX_BYTES = 100 * 1024 * 1024
IMPORT_POLL_S = 1.5
# Ten minutes of polls before the watch gives up saying so (the worker
# keeps the row on the server, so nothing is lost, only unwatched).
IMPORT_POLLS_MAX = 400
# The sources an export names, as they are said to the person.
IMPORT_SOURCE_NAMES = {"chatgpt": "ChatGPT", "claude": "Claude"}
# The account export: a server job like an import, polled the same way;
# the bytes then land in a file beside the terminal, named as the API's
# own content-disposition names them.
EXPORT_POLL_S = 1.5
EXPORT_POLLS_MAX = 400


# --- per-device state (dismissed announcement ids) ----------------------------


def _state_path() -> Path:
    """Where the app keeps its per-device state. MATTERWAVE_STATE_DIR moves
    it, which is also how the tests point it at a scratch directory."""
    root = os.environ.get("MATTERWAVE_STATE_DIR")
    if not root:
        xdg = os.environ.get("XDG_STATE_HOME")
        root = xdg if xdg else str(Path.home() / ".local" / "state")
    return Path(root) / "matterwave-tui" / "dismissed-announcements.json"


def _read_dismissed(path: Path) -> set[str]:
    """The announcement ids dismissed on this device. Anything unreadable
    reads as none dismissed, which only re-shows a notice."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(row) for row in rows if row} if isinstance(rows, list) else set()


def _write_dismissed(path: Path, ids: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    except OSError:
        pass


def estimate_tokens(text: str) -> int:
    """The server's own rough count (characters / 4), so the meter and the
    server's compaction decision agree to within the estimate."""
    return (len(text) + 3) // 4


def _read_attachment(path_text: str) -> tuple[bytes, str]:
    """The file's bytes and its name, off the event loop (it may be large)."""
    path = Path(path_text).expanduser()
    return path.read_bytes(), path.name


class ConversationItem(ListItem):
    def __init__(self, conversation: Conversation, *, library: str = "") -> None:
        self.conversation = conversation
        # The caption's tail naming the folder and tags, as the library
        # holds them; "" until the library answers.
        self.library = library
        super().__init__(Label(self.caption(), markup=False))

    def caption(self) -> str:
        c = self.conversation
        marks = ("📌 " if c.pinned_at else "") + ("🗄 " if c.archived_at else "")
        return marks + (c.title or "New chat") + self.library

    def retitle(self, title: str) -> None:
        self.conversation = replace(self.conversation, title=title)
        self.query_one(Label).update(self.caption())

    def replace_conversation(self, conversation: Conversation) -> None:
        """The conversation changed on the server (pin, archive): redraw the row."""
        self.conversation = conversation
        self.query_one(Label).update(self.caption())

    def set_library(self, library: str) -> None:
        """The conversation's folder or tags changed: redraw the row."""
        self.library = library
        self.query_one(Label).update(self.caption())


class Transcript(VerticalScroll):
    """The active path. Focusable so the branch and edit keys apply to the
    selected message rather than to whatever the composer holds."""

    can_focus = True
    BINDINGS = [
        Binding("up,k", "move(-1)", "Prev", show=False),
        Binding("down,j", "move(1)", "Next", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.selected: int | None = None

    def items(self) -> list[MessageView | DividerView]:
        return [w for w in self.children if isinstance(w, MessageView | DividerView)]

    def select(self, index: int | None) -> None:
        items = self.items()
        for widget in items:
            widget.remove_class("selected")
        if index is None or not items:
            self.selected = None
            return
        index = max(0, min(index, len(items) - 1))
        self.selected = index
        items[index].add_class("selected")
        items[index].scroll_visible()

    def action_move(self, delta: int) -> None:
        items = self.items()
        if not items:
            return
        start = len(items) if self.selected is None else self.selected
        self.select(start + delta)

    def selected_widget(self) -> MessageView | DividerView | None:
        items = self.items()
        if self.selected is None or self.selected >= len(items):
            return items[-1] if items else None
        return items[self.selected]


class TuiCommands(Provider):
    """The command palette's TUI-specific entries, beside Textual's system
    commands: the actions that have no key of their own."""

    ENTRIES: tuple[tuple[str, str, str], ...] = (
        ("Switch account…", "who is signed in here, and make another one acting",
         "action_switch_account"),
        ("Import a ChatGPT or Claude export…", "bring an exported history into this account",
         "action_import"),
        ("Export this account's data…", "every conversation and rating the account holds, "
         "as one JSON file", "action_export_account"),
        ("Dictate audio…", "transcribe the microphone or an audio file into the composer",
         "action_dictate"),
    )

    async def discover(self) -> Hits:
        for name, help_text, action in self.ENTRIES:
            yield DiscoveryHit(name, getattr(self.app, action), help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, action in self.ENTRIES:
            if (match := matcher.match(name)) > 0:
                yield Hit(match, matcher.highlight(name), getattr(self.app, action),
                          help=help_text)


class MatterWaveApp(App[None]):
    TITLE = "MatterWave"
    # Textual's system commands, plus the TUI's own (accounts, import,
    # dictation): the palette's entries.
    COMMANDS = {get_system_commands_provider, TuiCommands}
    CSS = """
    #sidebar { width: 32; border-right: solid $panel; }
    #search { margin: 0 0 1 0; }
    #main { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; }
    #failure { display: none; background: $error 20%; color: $text; padding: 0 1; }
    #failure.shown { display: block; }
    #announcements { display: none; background: $accent 20%; color: $text; padding: 0 1; }
    #announcements.shown { display: block; }
    #temporary { display: none; background: $warning 20%; color: $text; padding: 0 1; }
    #temporary.shown { display: block; }
    #composer { height: 6; border: round $panel; }
    #composer:focus { border: round $accent; }
    #status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    """
    BINDINGS = [
        Binding("ctrl+enter,ctrl+j,ctrl+s", "send", "Send", priority=True),
        Binding("n", "new", "New"),
        Binding("slash", "search", "Search"),
        Binding("m", "pick_model", "Model"),
        Binding("e", "edit", "Edit"),
        Binding("r", "regenerate", "Regenerate"),
        Binding("left_square_bracket", "branch(-1)", "Prev branch"),
        Binding("right_square_bracket", "branch(1)", "Next branch"),
        Binding("x", "toggle_originals", "Originals"),
        Binding("c", "compact", "Compact"),
        Binding("t", "instructions", "Instructions"),
        Binding("b", "feedback", "Feedback"),
        Binding("f2", "rename", "Rename"),
        Binding("p", "toggle_pin", "Pin"),
        Binding("a", "toggle_archive", "Archive"),
        Binding("d", "delete_conversation", "Delete"),
        Binding("f", "fork", "Fork"),
        Binding("s", "share", "Share"),
        Binding("S", "my_shares", "Shares"),
        Binding("l", "library", "Library"),
        Binding("R", "report", "Report"),
        Binding("N", "notifications", "Inbox"),
        Binding("A", "dismiss_announcements", "Dismiss"),
        Binding("w", "export_markdown", "Export"),
        Binding("i", "attach", "Attach"),
        Binding("v", "dictate", "Dictate"),
        Binding("o", "cycle_filter", "List"),
        Binding("escape", "leave_composer", "Transcript", show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, api: ChatAPI, *, relogin: DeviceLoginFn | None = None) -> None:
        super().__init__()
        self.api = api
        self.relogin_fn = relogin
        self.conversations: list[Conversation] = []
        self.conversation: Conversation | None = None
        self.thread = Thread()
        self.drafts: dict[str, str] = {}
        self.models_list: list[dict[str, Any]] = []
        self.default_model: str | None = None
        self.model: str | None = None
        self.editing: str | None = None
        self.streaming = False
        # The turn now streaming into the open conversation; `_consume` drops
        # anything from a turn that is no longer this one.
        self._turn: object | None = None
        self.live: Message | None = None
        self._live_dirty = False
        self.failure: Failure | None = None
        self.last_error_request_id: str | None = None
        self.connection = "online"
        self.context_used: int | None = None
        self.context_window: int | None = None
        # The org's balance this period, for the status line; None until the
        # usage route has answered (or forever, when it is not there).
        self.usage_balance: UsageBalance | None = None
        # Files uploaded but not sent yet; their ids go with the next turn.
        self.pending_attachments: list[Attachment] = []
        # The open conversation is temporary: created so in this session, so
        # no share or fork, and a notice standing in for the usual history.
        self.temporary_chat = False
        # The caller's own ratings in the open conversation, by message id.
        self.feedback: dict[str, dict[str, Any]] = {}
        # Which slice of the conversations the sidebar shows.
        self.list_filter = "default"
        # The caller's notifications (the page GET /api/me/notifications
        # answers) and the unread count the badge shows; None until the
        # route has answered (or forever, when it is not there). What the
        # inbox last asked for -- the unread filter and the page's length
        # -- is kept, so reopening it shows the same view.
        self.notification_rows: list[dict[str, Any]] = []
        self.unread_notifications: int | None = None
        self.notifications_unread_only = False
        self.notifications_limit = NOTIFICATIONS_PAGE
        # The library's folders, tags and per-conversation tag
        # assignments, for the `l` screen and the sidebar captions; empty
        # until GET /api/library answers (or forever, when it is not
        # there). Which conversation is in which folder rides the
        # conversations themselves (`folder_id`).
        self.library_folders: list[dict[str, Any]] = []
        self.library_tags: list[dict[str, Any]] = []
        self.library_assignments: dict[str, list[str]] = {}
        # The announcements in their window that this device has not
        # dismissed; a critical one is never dismissible.
        self.announcements: list[dict[str, Any]] = []
        # The org's budget (GET /api/budget), for the overage line; None
        # until the route has answered.
        self.budget: dict[str, Any] | None = None
        # The caller's share links, as the shares screen last saw them.
        self.share_rows: list[dict[str, Any]] = []
        # When the server's `Retry-After` runs out, on the monotonic clock.
        self.rate_limited_until: float | None = None
        # Terminal graphics: which protocol this terminal speaks (None is
        # the plain placeholder), what the layer has drawn, and the writes
        # waiting for the next refresh to carry them out.
        self.image_backend: str | None = images.detect_backend(os.environ)
        self._image_placements: dict[str, images.Placement] = {}
        self._image_ops: list[str] = []
        self._image_seq = 0
        # Attachment bytes already fetched for the transcript's image parts,
        # so a redraw hands a rebuilt view its picture without a re-fetch.
        self._image_data: dict[str, bytes] = {}
        self._search_seq = 0

    # --- layout ----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Input(placeholder="Search (/)", id="search")
                yield ListView(id="conversations")
            with Vertical(id="main"):
                yield Static("", id="announcements", markup=False)
                yield Transcript(id="transcript")
                yield Static("", id="failure", markup=False)
                yield Static("", id="temporary", markup=False)
                yield TextArea(id="composer", placeholder="Message… (Ctrl+Enter sends)")
                yield Static("", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(LIVE_REDRAW_S, self._flush_live)
        self.set_interval(1.0, self._tick_rate_limit)
        if self.image_backend is not None:
            self.set_interval(IMAGE_REFRESH_S, self._refresh_images)
        self.render_status()
        self.query_one("#composer", TextArea).focus()
        self.load_initial()

    async def on_unmount(self) -> None:
        await self.api.aclose()

    def post_display_hook(self) -> None:
        """Carry the image layer's pending writes out, right after a
        refresh: this is the one moment the terminal is not mid-redraw. A
        headless test writes nowhere."""
        if not self._image_ops:
            return
        driver = self._driver
        if driver is not None and not self.is_headless:
            for sequence in self._image_ops:
                driver.write(sequence)
            driver.flush()
        self._image_ops.clear()

    # --- status line -----------------------------------------------------------

    def status_text(self) -> str:
        bits = [f"model {self.model or self.default_model or '—'}"]
        window = self.context_window or model_window(self.models_list, self.model)
        if window:
            draft = estimate_tokens(self.query_one("#composer", TextArea).text)
            used = (self.context_used or 0) + draft
            pct = min(100, round(100 * used / window))
            bits.append(f"context {used:,}/{window:,} ({pct}%)")
        balance = self.usage_balance
        if balance is not None:
            if balance.exhausted:
                bits.append("budget used up")
            else:
                warn = "⚠ " if balance.warn else ""
                bits.append(f"{warn}usage {balance.used:,}/{balance.included:,} "
                            f"({round(balance.percent)}%)")
        budget = self.budget
        if budget is not None and budget.get("overage_enabled"):
            overage = int(budget.get("overage_micros") or 0)
            cap = int(budget.get("overage_cap_micros") or 0)
            if cap:
                warn = "⚠ " if overage >= cap else ""
                bits.append(f"{warn}overage ${overage / 1e6:,.2f} of ${cap / 1e6:,.2f}")
        if self.unread_notifications:
            bits.append(f"✉ {self.unread_notifications} unread")
        if self.pending_attachments:
            names = ", ".join(a.filename for a in self.pending_attachments)
            bits.append(f"attached: {names}")
        if self.streaming:
            bits.append("streaming… (Esc stops)")
        bits.append(self.connection)
        wait = self.rate_limit_remaining()
        if wait is not None:
            bits.append(f"rate limited · retry in {retry_in(wait)}")
        if self.last_error_request_id:
            bits.append(f"last error ref {self.last_error_request_id}")
        if self.editing:
            bits.append("editing (Esc cancels)")
        return " │ ".join(bits)

    def render_status(self) -> None:
        self.query_one("#status", Static).update(self.status_text())

    def rate_limit_remaining(self) -> float | None:
        if self.rate_limited_until is None:
            return None
        left = self.rate_limited_until - time.monotonic()
        return left if left > 0 else None

    def _tick_rate_limit(self) -> None:
        if self.rate_limited_until is None:
            return
        if self.rate_limit_remaining() is None:
            self.rate_limited_until = None
        self.render_status()

    @on(TextArea.Changed, "#composer")
    def _draft_changed(self) -> None:
        self.render_status()

    def show_failure(self, failure: Failure | None) -> None:
        self.failure = failure
        banner = self.query_one("#failure", Static)
        if failure is None:
            banner.remove_class("shown")
            banner.update("")
            return
        if failure.request_id:
            self.last_error_request_id = failure.request_id
        if failure.kind == "rate_limited":
            self.rate_limited_until = time.monotonic() + (failure.retry_after or 0)
        banner.update(failure.render())
        banner.add_class("shown")
        self.render_status()

    def fail_from_exception(self, exc: Exception) -> None:
        if isinstance(exc, APIConnectionError):
            self.go_offline()
            self.show_failure(Failure("network", None))
        elif isinstance(exc, APIError):
            kind = classify(exc.message, exc.status_code, exc.code)
            self.show_failure(Failure(kind, exc.request_id, retry_after=exc.retry_after))
        else:
            self.show_failure(Failure("unknown", None))

    # --- temporary chat ---------------------------------------------------------

    def set_temporary_chat(self, temporary: bool) -> None:
        """Marks the open conversation temporary (or not) and re-derives
        everything that depends on it: the notice and the share/fork keys."""
        self.temporary_chat = temporary
        banner = self.query_one("#temporary", Static)
        if temporary:
            banner.update(TEMPORARY_NOTICE)
            banner.add_class("shown")
        else:
            banner.remove_class("shown")
            banner.update("")
        # The key panel hides a command whose action just stopped existing.
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Share and fork do not exist for a temporary conversation: while
        one is open the keys are dead and the commands off the footer. The
        announcements banner offers its dismiss key only while it stands."""
        if action == "dismiss_announcements":
            return bool(self.announcements)
        return not (self.temporary_chat and action in ("share", "fork"))

    def go_offline(self) -> None:
        if self.connection != "offline":
            self.connection = "offline"
            self.render_status()
            self.probe_online()

    @work(group="probe", exclusive=True)
    async def probe_online(self) -> None:
        while self.connection == "offline":
            await asyncio.sleep(OFFLINE_PROBE_S)
            try:
                await self.api.me()
            except APIConnectionError:
                continue
            except APIError:
                pass
            self.connection = "online"
            self.render_status()

    # --- loading -----------------------------------------------------------------

    @work(group="load")
    async def load_initial(self) -> None:
        await self.load_models()
        await self.refresh_conversations()
        self.load_usage()
        self.load_budget()
        self.load_notifications()
        self.load_announcements()
        self.load_library()
        if self.conversations:
            await self.open_conversation(self.conversations[0].id)
        self.render_status()

    async def load_models(self) -> bool:
        try:
            models = await self.api.models()
        except Exception as exc:
            self.fail_from_exception(exc)
            return False
        data = models.get("data")
        self.models_list = [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []
        default = models.get("default_model")
        self.default_model = str(default) if default else None
        return True

    async def refresh_conversations(self, query: str = "") -> None:
        try:
            if query:
                found = await self.api.search_conversations(query)
            else:
                found = await self.api.list_conversations(filter=self.list_filter)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        if not query:
            self.conversations = found
        view = self.query_one("#conversations", ListView)
        await view.clear()
        await view.extend(
            [ConversationItem(c, library=self._library_suffix(c)) for c in found]
        )

    @work(group="usage", exclusive=True)
    async def load_usage(self) -> None:
        try:
            balance = await self.api.usage()
        except Exception:
            # No usage route on this deployment, or a key without the scope:
            # no line, like a model with no context window.
            return
        self.usage_balance = balance
        self.render_status()

    @work(group="budget", exclusive=True)
    async def load_budget(self) -> None:
        """The org's budget with the overage cap in force, for the status
        line's overage bit."""
        try:
            body = await self.api.budget()
        except Exception:
            # No budget route on this deployment: the usage line stands alone.
            return
        self.budget = body if isinstance(body, dict) else None
        self.render_status()

    @work(group="notifications", exclusive=True)
    async def load_notifications(self) -> None:
        """The bell's page of rows and the unread count the status line
        badges, as the inbox last asked for them."""
        try:
            await self.fetch_notifications()
        except Exception:
            # No notifications route on this deployment: no badge, no rows.
            return

    async def fetch_notifications(self) -> None:
        body = await self.api.notifications(
            unread=self.notifications_unread_only, limit=self.notifications_limit
        )
        rows = body.get("data")
        self.notification_rows = (
            [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        )
        count = body.get("unread_count")
        self.unread_notifications = count if isinstance(count, int) else None
        self.render_status()

    @work(group="announcements", exclusive=True)
    async def load_announcements(self) -> None:
        """The announcements in their window, minus the ids this device has
        dismissed. A critical one is never dismissible, the platform's own
        rule, so it shows however often it is dismissed here."""
        try:
            body = await self.api.announcements()
        except Exception:
            # No announcements route on this deployment: no banner.
            return
        rows = body.get("data")
        active = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        dismissed = _read_dismissed(_state_path())
        self.announcements = [
            row
            for row in active
            if not row.get("dismissible", True) or str(row.get("id")) not in dismissed
        ]
        self.render_announcements()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self._search_seq += 1
        self.run_search(event.value.strip(), self._search_seq)

    @work(group="search")
    async def run_search(self, query: str, seq: int) -> None:
        await asyncio.sleep(SEARCH_DEBOUNCE_S)
        if seq == self._search_seq:
            await self.refresh_conversations(query)

    @on(ListView.Selected, "#conversations")
    def _picked(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ConversationItem):
            self.open_worker(event.item.conversation.id)

    @work(group="load", exclusive=True)
    async def open_worker(self, conversation_id: str) -> None:
        await self.open_conversation(conversation_id)

    async def open_conversation(self, conversation_id: str) -> None:
        if self.streaming and (self.conversation is None or self.conversation.id != conversation_id):
            self._detach_turn()
        composer = self.query_one("#composer", TextArea)
        if self.conversation is not None:
            self.drafts[self.conversation.id] = composer.text
        try:
            tree = await self.api.tree(conversation_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.conversation = replace(tree, messages=[])
        self.thread = Thread.from_tree(tree.messages, tree.current_leaf_message_id)
        self.model = tree.default_model or self.model
        self.editing = None
        self.feedback = {}
        # A conversation read from the list is never the temporary one that
        # was just created: the server keeps those out of every list and
        # every search, so only the creation itself can open one.
        self.set_temporary_chat(False)
        self.show_failure(None)
        composer.text = self.drafts.get(conversation_id, "")
        self.context_used, self.context_window = None, None
        await self.render_thread()
        self.load_context(conversation_id)
        self.load_feedback(conversation_id)
        self.render_status()

    @work(group="context", exclusive=True)
    async def load_context(self, conversation_id: str) -> None:
        try:
            ctx = await self.api.context(conversation_id)
        except Exception:
            # An API without the route, or a model with no window: no meter.
            return
        self.context_used, self.context_window = ctx.used_tokens, ctx.context_window
        self.render_status()

    # --- the transcript ----------------------------------------------------------

    async def render_thread(self, *, keep_selection: bool = False) -> None:
        transcript = self.query_one("#transcript", Transcript)
        selected = transcript.selected if keep_selection else None
        await transcript.remove_children()
        widgets: list[Widget] = []
        for item in self.thread.view():
            if isinstance(item, Divider):
                widgets.append(DividerView(item))
            else:
                widgets.append(
                    MessageView(item, position=self.thread.position(item),
                                feedback=self.feedback.get(item.id),
                                image_backend=self.image_backend)
                )
        if self.live is not None:
            widgets.append(MessageView(self.live, streaming=True))
        await transcript.mount_all(widgets)
        transcript.select(selected)
        transcript.scroll_end(animate=False)
        self.load_images()

    # --- terminal images ---------------------------------------------------------

    def _image_views(self) -> list[ImageView]:
        views: list[ImageView] = []
        for widget in self.query_one("#transcript", Transcript).children:
            if isinstance(widget, MessageView):
                views.extend(view for view in widget.children if isinstance(view, ImageView))
        return views

    @work(group="images", exclusive=True)
    async def load_images(self) -> None:
        """Fetch the bytes for every image part the transcript is showing
        and the terminal can draw. One download at a time, each failure
        noted on its view and never retried here; bytes already fetched for
        an attachment are handed over without a second fetch."""
        if self.image_backend is None:
            return
        for view in self._image_views():
            if not view.wants_bytes():
                continue
            cached = self._image_data.get(view.attachment_id)
            if cached is not None:
                view.set_data(cached)
                continue
            try:
                data = await self.api.download_attachment(view.attachment_id)
            except Exception:
                view.set_failed()
                continue
            self._image_data[view.attachment_id] = data
            view.set_data(data)

    def _refresh_images(self) -> None:
        """Where the loaded images should sit now. The transcript scrolls
        under them, so a picture whose line has moved is deleted and drawn
        again at the new spot; one off screen comes down until it scrolls
        back."""
        if self.image_backend is None:
            return
        try:
            transcript = self.query_one("#transcript", Transcript)
        except Exception:
            return
        placements: dict[str, images.Placement] = {}
        ops: list[str] = []
        for view in self._image_views():
            if view.data is None or not view.region.overlaps(transcript.region):
                continue
            at = view.region
            key = f"image-{id(view)}"
            old = self._image_placements.get(key)
            if old is not None and (old.x, old.y) == (at.x, at.y):
                placements[key] = old
                continue
            # The picture is where it was no longer: take that one down
            # before the new one goes up.
            if old is not None and old.remove:
                ops.append(old.remove)
            self._image_seq += 1
            image_id = images.new_image_id(self._image_seq)
            if self.image_backend == "kitty":
                sequences = images.kitty_placement(
                    image_id, at.x + 1, at.y + 1, max(at.width, 8), max(at.height, 2), view.data
                )
                if not sequences:
                    view.set_failed()
                    continue
                ops.extend(sequences)
                placements[key] = images.Placement(
                    image_id, at.x, at.y, "", images.kitty_delete(image_id)
                )
            else:
                placement = images.sixel_placement(image_id, at.x + 1, at.y + 1, view.data)
                if placement is None:
                    view.set_failed()
                    continue
                ops.append(placement.sequence)
                placements[key] = placement
        for key, placement in self._image_placements.items():
            if key not in placements and placement.remove:
                ops.append(placement.remove)
        self._image_placements = placements
        self._image_ops.extend(ops)

    def _flush_live(self) -> None:
        if self._live_dirty and self.live is not None:
            self._live_dirty = False
            self.run_worker(self._redraw_live(), group="live", exclusive=True)

    async def _redraw_live(self) -> None:
        transcript = self.query_one("#transcript", Transcript)
        views = [w for w in transcript.children if isinstance(w, MessageView) and w.streaming]
        if views and self.live is not None:
            await views[-1].update_message(self.live)
            transcript.scroll_end(animate=False)

    def selected_message(self) -> Message | None:
        widget = self.query_one("#transcript", Transcript).selected_widget()
        return widget.message if widget is not None else None

    # --- actions -----------------------------------------------------------------

    def action_leave_composer(self) -> None:
        if self.streaming:
            self.stop_stream()
        if self.editing:
            self.editing = None
            self.query_one("#composer", TextArea).text = ""
            self.render_status()
        self.query_one("#transcript", Transcript).focus()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_new(self) -> None:
        """A new chat. Temporary is decided here or not at all: the flag is
        fixed at creation and no route flips it afterwards."""

        def chosen(temporary: bool | None) -> None:
            self._new_conversation(bool(temporary))

        self.push_screen(
            Confirm(
                "Start a temporary chat? It won't appear in your history or search, can't be "
                "shared, and is deleted 24 hours after the last message.",
                danger=False,
                hint="Enter = temporary, Esc = a regular chat.",
            ),
            chosen,
        )

    @work(group="action")
    async def _new_conversation(self, temporary: bool) -> None:
        try:
            conv = await self.api.create_conversation(temporary=temporary)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        # The sidebar is the server's list, and the server keeps a temporary
        # conversation out of every one of them, so the creation itself adds
        # nothing there (web/src/shell/useShellState.ts never inserts it
        # either). The next refresh reads whatever the server now lists.
        # Focus stays where it was: an empty thread has nothing to read, and
        # the conversation keys stay live for the chat just opened.
        await self.open_conversation(conv.id)
        self.set_temporary_chat(temporary)

    @work(group="action")
    async def action_pick_model(self) -> None:
        # The list is read again first: a plan change since the app started
        # would otherwise let a model be picked that the turn then refuses.
        if not await self.load_models():
            return

        def chosen(model_id: str | None) -> None:
            if model_id:
                self.apply_model(model_id)

        self.push_screen(ModelPicker(self.models_list, self.model), chosen)

    def model_allowed(self, model_id: str) -> bool:
        """Whether `/api/models` lists it as one the plan includes."""
        for model in self.models_list:
            if model.get("id") == model_id:
                return model.get("included", True) is not False
        return False

    @work(group="action")
    async def apply_model(self, model_id: str) -> None:
        """The picker's choice: checked against the list, then kept on the
        conversation so the next turn, here or in the web app, uses it."""
        if not self.model_allowed(model_id):
            self.notify(f"{model_id} is not a model you can use here.", severity="warning")
            return
        if self.conversation is not None:
            try:
                updated = await self.api.set_default_model(self.conversation.id, model_id)
            except Exception as exc:
                self.fail_from_exception(exc)
                return
            self.conversation = replace(self.conversation, default_model=updated.default_model)
        self.model = model_id
        self.render_status()

    def action_send(self) -> None:
        composer = self.query_one("#composer", TextArea)
        text = composer.text.strip()
        if not text or self.streaming:
            return
        attachment_ids = [a.id for a in self.pending_attachments] or None
        if self.conversation is None:
            self.run_worker(self._create_then_send(text, attachment_ids), group="stream",
                            exclusive=True)
            return
        cid = self.conversation.id
        if self.editing:
            stream = self.api.edit(cid, self.editing, text, self.model)
        else:
            stream = self.api.send(cid, text, self.model, attachment_ids=attachment_ids)
        composer.text = ""
        self.drafts.pop(cid, None)
        self.editing = None
        self.pending_attachments = []
        self.consume(stream)

    async def _create_then_send(self, text: str, attachment_ids: list[str] | None = None) -> None:
        try:
            conv = await self.api.create_conversation(model=self.model)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        await self.refresh_conversations()
        await self.open_conversation(conv.id)
        self.query_one("#composer", TextArea).text = ""
        self.pending_attachments = []
        await self._consume(self.api.send(conv.id, text, self.model, attachment_ids=attachment_ids))

    def action_edit(self) -> None:
        message = self.selected_message()
        if message is not None and message.role == "assistant":
            message = self.thread.messages.get(message.parent_id or "")
        if message is None or message.role != "user":
            message = self.thread.last("user")
        if message is None:
            return
        self.editing = message.id
        composer = self.query_one("#composer", TextArea)
        composer.text = message.content
        composer.focus()
        self.render_status()

    def action_regenerate(self) -> None:
        if self.conversation is None or self.streaming:
            return
        message = self.selected_message()
        if message is None or message.role != "assistant":
            message = self.thread.last("assistant")
        if message is None:
            return
        self.consume(self.api.regenerate(self.conversation.id, message.id, self.model))

    @work(group="action")
    async def action_branch(self, delta: int) -> None:
        message = self.selected_message()
        if message is None or self.conversation is None or self.streaming:
            return
        leaf = self.thread.sibling_leaf(message, delta)
        if leaf is None:
            self.notify("No other branch that way.")
            return
        try:
            await self.api.set_active_leaf(self.conversation.id, leaf)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.thread.leaf = leaf
        await self.render_thread(keep_selection=True)

    @work(group="action")
    async def action_toggle_originals(self) -> None:
        widget = self.query_one("#transcript", Transcript).selected_widget()
        target: str | None = None
        if isinstance(widget, DividerView):
            target = widget.divider.summary.id
        else:
            dividers = [i for i in self.thread.view() if isinstance(i, Divider)]
            if dividers:
                target = dividers[-1].summary.id
        if target is None:
            return
        self.thread.expanded ^= {target}
        await self.render_thread()

    @work(group="action")
    async def action_compact(self) -> None:
        if self.conversation is None or self.streaming:
            return
        try:
            summary = await self.api.compact(self.conversation.id)
        except ConflictError as exc:
            # 409 "Nothing to compact yet.": a state, not a failed answer, so
            # it is said in passing rather than in the failure banner.
            self.notify(exc.message or "Nothing to compact yet.")
            return
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.thread.add(summary)
        self.thread.leaf = summary.id
        await self.render_thread()
        self.load_context(self.conversation.id)

    # --- conversation management -------------------------------------------------

    def _sync_conversation(self, updated: Conversation) -> None:
        """The server's state of one conversation, applied to the sidebar and
        the open conversation without a refetch."""
        self.conversations = [updated if c.id == updated.id else c for c in self.conversations]
        if self.conversation is not None and self.conversation.id == updated.id:
            self.conversation = updated
        for child in self.query_one("#conversations", ListView).children:
            if isinstance(child, ConversationItem) and child.conversation.id == updated.id:
                child.replace_conversation(updated)

    def action_rename(self) -> None:
        if self.conversation is None:
            return

        def chosen(title: str | None) -> None:
            if title:
                self.apply_rename(title)

        self.push_screen(Prompt("Rename conversation", self.conversation.title), chosen)

    @work(group="action")
    async def apply_rename(self, title: str) -> None:
        if self.conversation is None:
            return
        try:
            updated = await self.api.rename_conversation(self.conversation.id, title)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self._retitle(updated.id, updated.title)
        self.notify("Renamed.")

    @work(group="action")
    async def action_toggle_pin(self) -> None:
        if self.conversation is None:
            return
        pinned = self.conversation.pinned_at is None
        try:
            updated = await self.api.set_pinned(self.conversation.id, pinned)
        except ConflictError as exc:
            # A temporary conversation cannot be kept, the server says why;
            # the detail is the message, not a failure banner.
            self.notify(exc.message or "This conversation cannot be pinned.")
            return
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self._sync_conversation(updated)
        self.notify("Pinned." if pinned else "Unpinned.")

    @work(group="action")
    async def action_toggle_archive(self) -> None:
        if self.conversation is None:
            return
        archived = self.conversation.archived_at is None
        try:
            updated = await self.api.set_archived(self.conversation.id, archived)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self._sync_conversation(updated)
        # An archived conversation leaves the slice the sidebar shows (and an
        # unarchived one returns to it), so the list is read again.
        await self.refresh_conversations()
        self.notify("Archived." if archived else "Unarchived.")

    def action_delete_conversation(self) -> None:
        if self.conversation is None:
            return
        target = self.conversation

        def chosen(confirmation: bool | None) -> None:
            if confirmation:
                self.apply_delete(target.id)

        self.push_screen(Confirm(f"Delete “{target.title}”? This cannot be undone."), chosen)

    @work(group="action")
    async def apply_delete(self, conversation_id: str) -> None:
        try:
            await self.api.delete_conversation(conversation_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        if self.conversation is not None and self.conversation.id == conversation_id:
            if self.streaming:
                self._detach_turn()
            self.conversation = None
            self.thread = Thread()
            self.editing = None
            self.live = None
            await self.render_thread()
        self.conversations = [c for c in self.conversations if c.id != conversation_id]
        view = self.query_one("#conversations", ListView)
        await view.clear()
        await view.extend(
            [ConversationItem(c, library=self._library_suffix(c)) for c in self.conversations]
        )
        self.notify("Deleted.")
        self.render_status()

    @work(group="action")
    async def action_fork(self) -> None:
        if self.conversation is None or self.streaming:
            return
        message = self.selected_message() or self.thread.messages.get(self.thread.leaf or "")
        if message is None:
            self.notify("Nothing to fork yet.")
            return
        try:
            forked = await self.api.fork_conversation(self.conversation.id, message.id)
        except ConflictError as exc:
            self.notify(exc.message or "This conversation cannot be forked.")
            return
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        await self.refresh_conversations()
        await self.open_conversation(forked.id)
        self.notify(f"Forked as “{forked.title}”.")

    @work(group="action")
    async def action_share(self) -> None:
        if self.conversation is None:
            return
        try:
            url = await self.api.share_conversation(self.conversation.id)
        except ConflictError as exc:
            self.notify(exc.message or "This conversation cannot be shared.")
            return
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.notify(f"Share link: {url}")

    @work(group="action")
    async def action_export_markdown(self) -> None:
        if self.conversation is None:
            return
        conversation, thread = self.conversation, self.thread
        path = Path.cwd() / export_filename(conversation)
        document = conversation_markdown(conversation, thread)

        def write() -> None:
            path.write_text(document, encoding="utf-8")

        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            self.notify(f"Could not write {path.name}: {exc}", severity="error")
            return
        self.notify(f"Exported to {path}")

    def action_attach(self) -> None:
        def chosen(path_text: str | None) -> None:
            if path_text:
                self.apply_attach(path_text)

        self.push_screen(Prompt("Attach a file (its path):"), chosen)

    @work(group="action")
    async def apply_attach(self, path_text: str) -> None:
        try:
            data, filename = await asyncio.to_thread(_read_attachment, path_text)
        except OSError as exc:
            self.notify(f"Could not read {path_text}: {exc}", severity="error")
            return
        # The vision gate, before the upload: a model the list says cannot
        # see is told by name, with the ones that can. An unknown model is
        # allowed through; the server's 422 remains the backstop.
        model_id = self.model or self.default_model
        if is_image_filename(filename) and not model_accepts_images(self.models_list, model_id):
            capable = vision_capable_model_ids(self.models_list)
            names = ", ".join(capable) if capable else "none of your models"
            self.notify(
                f"{model_id or 'This model'} cannot read images. Vision-capable models: {names}.",
                severity="warning",
            )
            return
        conversation_id = self.conversation.id if self.conversation else None
        try:
            attachment = await self.api.upload_attachment(
                data, filename, conversation_id=conversation_id
            )
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.pending_attachments.append(attachment)
        self.notify(f"Attached {attachment.filename}.")
        self.render_status()

    @work(group="action")
    async def action_cycle_filter(self) -> None:
        index = LIST_FILTERS.index(self.list_filter)
        self.list_filter = LIST_FILTERS[(index + 1) % len(LIST_FILTERS)]
        await self.refresh_conversations()
        self.notify(f"Showing {self.list_filter} conversations.")

    # --- notifications and announcements -----------------------------------------

    def action_notifications(self) -> None:
        self.push_screen(
            Notifications(
                list(self.notification_rows),
                mark_read=self.apply_notification_read,
                mark_all=self.apply_notifications_read_all,
                load_page=self.apply_notifications_page,
                unread_only=self.notifications_unread_only,
            )
        )

    async def apply_notifications_page(self, unread_only: bool, limit: int) -> list[dict[str, Any]]:
        """The page the inbox asked for: `unread=1`, or a longer limit for
        the pages past the loaded ones. What comes back is what the server
        now lists, and the badge follows the count it reports."""
        self.notifications_unread_only = unread_only
        self.notifications_limit = limit
        try:
            await self.fetch_notifications()
        except Exception as exc:
            self.fail_from_exception(exc)
        return self.notification_rows

    async def apply_notification_read(self, notification_id: str) -> list[dict[str, Any]]:
        """One row read: the panel redraws from the rows this hands back,
        and the status line's badge follows the unread count."""
        try:
            row = await self.api.read_notification(notification_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return self.notification_rows
        self.notification_rows = [
            row if r.get("id") == notification_id else r for r in self.notification_rows
        ]
        count = row.get("unread_count")
        self.unread_notifications = count if isinstance(count, int) else self.unread_notifications
        self.render_status()
        return self.notification_rows

    async def apply_notifications_read_all(self) -> list[dict[str, Any]]:
        try:
            await self.api.read_all_notifications()
        except Exception as exc:
            self.fail_from_exception(exc)
            return self.notification_rows
        read_at = datetime.now(UTC).isoformat()
        self.notification_rows = [
            {**row, "read_at": row.get("read_at") or read_at} for row in self.notification_rows
        ]
        self.unread_notifications = 0
        self.render_status()
        return self.notification_rows

    def render_announcements(self) -> None:
        banner = self.query_one("#announcements", Static)
        if not self.announcements:
            banner.remove_class("shown")
            banner.update("")
            return
        lines = []
        for row in self.announcements:
            prefix = {"critical": "⛔ ", "warning": "⚠ "}.get(str(row.get("severity")), "ℹ ")
            lines.append(prefix + str(row.get("message") or ""))
        lines.append("A dismisses these; a critical notice stays until the operator ends it.")
        banner.update("\n".join(lines))
        banner.add_class("shown")

    def action_dismiss_announcements(self) -> None:
        """Hide the banner and remember which ids this device dismissed, so
        they do not show again; the critical ones stay."""
        dismissible = {
            str(row.get("id"))
            for row in self.announcements
            if row.get("dismissible", True) and row.get("id")
        }
        path = _state_path()
        _write_dismissed(path, _read_dismissed(path) | dismissible)
        self.announcements = [
            row for row in self.announcements if not row.get("dismissible", True)
        ]
        self.render_announcements()
        # The footer's Dismiss goes away with the banner.
        self.refresh_bindings()

    # --- reporting ------------------------------------------------------------------

    def action_report(self) -> None:
        """A report about the open conversation — or about one reply, when
        a reply is what the transcript has selected, as the web app's
        report-this-reply does. What kind, an optional note, then the
        confirm. The platform routes `unsafe` to review and `unhelpful` to
        the product backlog; the filed id is the one support can be
        quoted."""
        if self.conversation is None:
            return
        target = self.conversation
        # A reply the reader moved to is the report's subject, as the web
        # app's report-this-reply is; whatever the transcript is resting on
        # by default is not a selection, and reports the conversation.
        transcript = self.query_one("#transcript", Transcript)
        selected = (
            transcript.selected_widget() if transcript.selected is not None else None
        )
        message_id = (
            selected.message.id
            if isinstance(selected, MessageView) and selected.message.role == "assistant"
            else None
        )

        def kind_chosen(kind: str | None) -> None:
            if kind is None:
                return
            if kind not in ("unsafe", "unhelpful"):
                self.notify("A report is filed as unsafe or unhelpful.", severity="warning")
                return

            def note_chosen(note: str | None) -> None:
                def confirmed(filing: bool | None) -> None:
                    if filing:
                        self.apply_report(target.id, kind, note, message_id)

                self.push_screen(
                    Confirm(
                        f"File a {kind} report about "
                        + ("the selected reply" if message_id else f"“{target.title}”")
                        + "?",
                        danger=False,
                        hint="Enter files it, Esc cancels.",
                    ),
                    confirmed,
                )

            self.push_screen(
                Prompt("Add a note for the reviewers (optional; Esc files without one):"),
                note_chosen,
            )

        self.push_screen(
            Prompt("Report this conversation — kind? (unsafe or unhelpful):"), kind_chosen
        )

    @work(group="action")
    async def apply_report(
        self, conversation_id: str, kind: str, note: str | None, message_id: str | None = None
    ) -> None:
        try:
            filed = await self.api.report_conversation(
                conversation_id, kind, note=note, message_id=message_id
            )
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        if filed.id:
            self.notify(f"Report {filed.id} filed as {filed.kind}.")
        else:
            self.notify(f"Report filed as {kind}.")

    # --- share management -------------------------------------------------------------

    @work(group="shares", exclusive=True)
    async def action_my_shares(self) -> None:
        try:
            rows = await self.api.my_shares()
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.share_rows = rows
        self.push_screen(Shares(list(rows), revoke=self.apply_share_revoke))

    async def apply_share_revoke(self, share_id: str) -> list[dict[str, Any]]:
        try:
            await self.api.revoke_share(share_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return self.share_rows
        # The server keeps the row and marks it revoked, and so does the list.
        self.share_rows = [
            {**row, "status": "revoked"} if row.get("id") == share_id else row
            for row in self.share_rows
        ]
        self.notify("Share revoked.")
        return self.share_rows

    # --- accounts ---------------------------------------------------------------------

    @work(group="action")
    async def action_switch_account(self) -> None:
        """The account switcher: read who is signed in, offer the parked
        ones, and make the chosen slot acting. Everything the open
        conversation showed belonged to the old account, so it is dropped
        and loaded again from the new one."""
        try:
            body = await self.api.accounts()
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        rows = body.get("accounts")
        offered = (
            [row for row in rows if isinstance(row, dict)]
            if isinstance(rows, list)
            else []
        )
        # The acting account is not offered (switching to it would be a
        # no-op); with nothing parked there is nothing to choose.
        if not offered or all(
            row.get("current") is True or row.get("slot") == 0 for row in offered
        ):
            self.notify("No other account is signed in on this client.")
            return

        def chosen(row: dict[str, Any] | None) -> None:
            if row is not None:
                self.apply_switch_account(int(row.get("slot") or 0))

        self.push_screen(Accounts(offered), chosen)

    @work(group="action")
    async def apply_switch_account(self, slot: int) -> None:
        try:
            switched = await self.api.switch_account(slot)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        who = str(switched.get("email") or switched.get("subject") or "the other account")
        self.notify(f"Switched to {who}.")
        # The drafts, ratings, notifications and sidebar were the old
        # account's; the new account's state is read back whole.
        self.drafts.clear()
        self.feedback = {}
        self.notification_rows = []
        self.unread_notifications = None
        if self.streaming:
            self._detach_turn()
        self.conversation = None
        self.thread = Thread()
        self.editing = None
        self.live = None
        await self.render_thread()
        await self.refresh_conversations()
        self.load_usage()
        self.load_budget()
        self.load_notifications()
        self.load_announcements()
        if self.conversations:
            await self.open_conversation(self.conversations[0].id)
        self.render_status()

    # --- importing --------------------------------------------------------------------

    def action_import(self) -> None:
        """An exported ChatGPT or Claude history, brought into this
        account: the file's path, then the upload, then a watch on the job
        while it runs."""

        def chosen(path_text: str | None) -> None:
            if path_text:
                self.apply_import(path_text)

        self.push_screen(Prompt("Import a ChatGPT or Claude export (its file path):"), chosen)

    @work(group="action")
    async def apply_import(self, path_text: str) -> None:
        try:
            data, filename = await asyncio.to_thread(_read_attachment, path_text)
        except OSError as exc:
            self.notify(f"Could not read {path_text}: {exc}", severity="error")
            return
        if len(data) > IMPORT_MAX_BYTES:
            self.notify("That file is larger than the 100 MB limit.", severity="warning")
            return
        self.notify("Import starting…")
        try:
            row = await self.api.start_import(data, filename)
        except APIError as exc:
            # The route's own refusals say what was wrong (an unsupported
            # export, an import already running): they are the message.
            self.notify(exc.message or "The import could not be started.", severity="error")
            return
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        # A worker handing over to another: not awaited (a @work call is a
        # Worker, not a coroutine), the watch polls on its own group.
        self._watch_import(str(row.get("id") or ""))

    @work(group="import", exclusive=True)
    async def _watch_import(self, import_id: str) -> None:
        """The import is a job on the server: poll its row until it is done
        or failed, then say what it did. The row is the record either way,
        so a watch that gives up has lost nothing."""
        if not import_id:
            return
        row: dict[str, Any] = {}
        for _ in range(IMPORT_POLLS_MAX):
            try:
                row = await self.api.import_status(import_id)
            except Exception as exc:
                self.fail_from_exception(exc)
                return
            status = str(row.get("status") or "")
            if status == "done":
                self.notify(self._import_summary(row))
                await self.refresh_conversations()
                return
            if status == "failed":
                error = str(row.get("error") or "The import failed.")
                self.notify(f"Import failed: {error}", severity="error")
                return
            await asyncio.sleep(IMPORT_POLL_S)
        self.notify("The import is still running; it will appear when it finishes.")

    @staticmethod
    def _import_summary(row: dict[str, Any]) -> str:
        """What the import did, in one line, the web result card's numbers
        (web/src/apps/settings/importApi.ts, summarize)."""
        source = IMPORT_SOURCE_NAMES.get(str(row.get("source")), str(row.get("source") or "export"))
        created = int(row.get("conversations_created") or 0)
        messages = int(row.get("messages_created") or 0)
        summary = f"Imported {created} conversations, {messages} messages from {source}"
        skipped = int(row.get("conversations_skipped") or 0)
        if skipped:
            summary += f"; {skipped} skipped"
        return summary + "."

    # --- account export -----------------------------------------------------------------

    @work(group="action")
    async def action_export_account(self) -> None:
        """The whole account as one JSON document, built on the server:
        the job is queued, watched the way an import is, and the bytes
        land in a file beside the terminal."""
        self.notify("Export starting…")
        try:
            row = await self.api.start_export()
        except APIError as exc:
            # The route's own refusals say what was wrong (one already
            # building, the hourly limit): they are the message.
            self.notify(exc.message or "The export could not be started.", severity="error")
            return
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self._watch_export(str(row.get("id") or ""))

    @work(group="export", exclusive=True)
    async def _watch_export(self, export_id: str) -> None:
        """The export is a job on the server: poll its row until it is
        ready or failed, then fetch the bytes behind the fresh signed
        link and write the file. A watch that gives up has lost nothing —
        the row stands, and the server mails its link too."""
        if not export_id:
            return
        row: dict[str, Any] = {}
        for _ in range(EXPORT_POLLS_MAX):
            try:
                row = await self.api.export_status(export_id)
            except Exception as exc:
                self.fail_from_exception(exc)
                return
            status = str(row.get("status") or "")
            if status == "ready":
                await self._save_export(row)
                return
            if status in ("failed", "expired"):
                self.notify(
                    "The account export failed; ask for another when you like.",
                    severity="error",
                )
                return
            await asyncio.sleep(EXPORT_POLL_S)
        self.notify("The export is still building; it will be saved when it finishes.")

    async def _save_export(self, row: dict[str, Any]) -> None:
        link = str(row.get("download_url") or "")
        if not link:
            self.notify("The export finished without a download link.", severity="error")
            return
        try:
            data = await self.api.export_download(link)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        created = str(row.get("created_at") or "")
        path = Path.cwd() / f"matterwave-export-{created[:10] or 'account'}.json"

        def write() -> None:
            path.write_bytes(data)

        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            self.notify(f"Could not write {path.name}: {exc}", severity="error")
            return
        self.notify(f"Account export saved to {path} ({len(data)} bytes).")

    # --- dictation --------------------------------------------------------------------

    def action_dictate(self) -> None:
        """Words into the composer without the keyboard: the microphone
        where the `dictation` extra provides it, an audio file's path
        otherwise. The transcript lands at the caret, as text to read,
        correct and send."""
        if mic_available():
            self.push_screen(Dictation(MicRecorder()), self._dictated)
        else:
            self.notify(f"No microphone support installed (pip install matterwave-tui[dictation]); "
                        f"naming an audio file instead ({', '.join(sorted(MEDIA_TYPES))}).")

            def chosen(path_text: str | None) -> None:
                if path_text:
                    self.apply_transcribe_file(path_text)

            self.push_screen(Prompt("Transcribe an audio file (its path):"), chosen)

    def _dictated(self, clip: tuple[bytes, str] | None) -> None:
        if clip is not None:
            self.apply_dictation(clip[0], clip[1])

    @work(group="action")
    async def apply_transcribe_file(self, path_text: str) -> None:
        try:
            data, filename = await asyncio.to_thread(_read_attachment, path_text)
        except OSError as exc:
            self.notify(f"Could not read {path_text}: {exc}", severity="error")
            return
        content_type = media_type_for(filename)
        if content_type is None:
            self.notify(
                f"Send an audio clip ({', '.join(sorted(MEDIA_TYPES))}).", severity="warning"
            )
            return
        self._transcribe(data, filename, content_type)

    @work(group="action")
    async def apply_dictation(self, clip: bytes, content_type: str) -> None:
        if not clip:
            self.notify("Nothing was captured.", severity="warning")
            return
        self._transcribe(clip, "dictation.wav", content_type)

    @work(group="action")
    async def _transcribe(self, data: bytes, filename: str, content_type: str) -> None:
        try:
            text = await self.api.transcribe(data, filename, content_type)
        except APIError as exc:
            self.notify(exc.message or "The transcription failed.", severity="error")
            return
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        text = text.strip()
        if not text:
            self.notify("Nothing was heard.")
            return
        self._insert_into_composer(text)

    def _insert_into_composer(self, text: str) -> None:
        """The transcript at the caret, in place of the typing."""
        composer = self.query_one("#composer", TextArea)
        composer.focus()
        composer.insert(text)
        self.render_status()

    # --- the library (folders and tags) ---------------------------------------------

    def _library_suffix(self, conversation: Conversation) -> str:
        """The caption's tail naming the conversation's folder and tags:
        ` — 📁 Work · #draft`, from the library the server answered.
        Nothing when the conversation is in no folder and carries no tag."""
        folder_names = {
            str(folder.get("id")): str(folder.get("name") or "")
            for folder in self.library_folders
            if folder.get("id")
        }
        tag_names = {
            str(tag.get("id")): str(tag.get("name") or "") for tag in self.library_tags if tag.get("id")
        }
        bits: list[str] = []
        folder = folder_names.get(conversation.folder_id or "")
        if folder:
            bits.append(f"📁 {folder}")
        bits.extend(
            f"#{tag_names.get(tag_id, tag_id)}"
            for tag_id in self.library_assignments.get(conversation.id, [])
        )
        return (" — " + " · ".join(bits)) if bits else ""

    def _refresh_library_captions(self) -> None:
        """The sidebar captions carry the folder and tag names: after a
        move or a tagging they are drawn again."""
        for child in self.query_one("#conversations", ListView).children:
            if isinstance(child, ConversationItem):
                child.set_library(self._library_suffix(child.conversation))

    def _adopt_library(self, body: dict[str, Any]) -> None:
        """The library as the server now holds it: the folders, the tags,
        and which conversation carries which tag. The captions are
        redrawn, the names being theirs."""
        folders = body.get("folders")
        tags = body.get("tags")
        assignments = body.get("assignments")
        self.library_folders = (
            [folder for folder in folders if isinstance(folder, dict)]
            if isinstance(folders, list)
            else []
        )
        self.library_tags = (
            [tag for tag in tags if isinstance(tag, dict)] if isinstance(tags, list) else []
        )
        self.library_assignments = (
            {
                str(conversation_id): [str(tag_id) for tag_id in tag_ids]
                for conversation_id, tag_ids in assignments.items()
                if isinstance(tag_ids, list)
            }
            if isinstance(assignments, dict)
            else {}
        )
        self._refresh_library_captions()

    @work(group="library", exclusive=True)
    async def load_library(self) -> None:
        try:
            body = await self.api.library()
        except Exception:
            # No library route on this deployment: captions without
            # folders or tags.
            return
        self._adopt_library(body)

    def _library_state(self, folder_id: str | None) -> LibraryState:
        """What the library screen redraws from after a write: the
        folders, the tags, the assignments, and the open conversation's
        folder as the app now holds them."""
        return (
            list(self.library_folders),
            list(self.library_tags),
            {cid: list(tag_ids) for cid, tag_ids in self.library_assignments.items()},
            folder_id,
        )

    def _open_folder_id(self) -> str | None:
        return self.conversation.folder_id if self.conversation else None

    @work(group="library", exclusive=True)
    async def action_library(self) -> None:
        """The folders and tags, and the open conversation's place among
        them. The library is read first: a folder made in the web app
        since the app started should be there to file into."""
        if self.conversation is None:
            return
        try:
            body = await self.api.library()
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self._adopt_library(body)
        self.push_screen(
            Library(
                list(self.library_folders),
                list(self.library_tags),
                dict(self.library_assignments),
                self.conversation.id,
                self.conversation.folder_id,
                new_folder=self.apply_folder_create,
                new_tag=self.apply_tag_create,
                move_to=self.apply_folder_move,
                set_tags=self.apply_tags_set,
            )
        )

    async def apply_folder_create(self, name: str) -> LibraryState | None:
        """A folder of that name. A duplicate is the server's 409, and its
        detail is the message, not a failure banner."""
        try:
            folder = await self.api.create_folder(name)
        except ConflictError as exc:
            self.notify(exc.message or "That folder name is taken.")
            return None
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.library_folders = [*self.library_folders, folder]
        return self._library_state(self._open_folder_id())

    async def apply_tag_create(self, name: str) -> LibraryState | None:
        """A tag of that name; the server answers the one already there
        when the name is taken, so a write is never refused for that."""
        try:
            tag = await self.api.create_tag(name)
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.library_tags = [*self.library_tags, tag]
        return self._library_state(self._open_folder_id())

    async def apply_folder_move(self, folder_id: str | None) -> LibraryState | None:
        """The open conversation filed in a folder (None: back in the main
        list). The sidebar row and the open conversation follow."""
        if self.conversation is None:
            return None
        try:
            await self.api.move_to_folder(self.conversation.id, folder_id)
        except ConflictError as exc:
            self.notify(exc.message or "This conversation cannot be filed.")
            return None
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self._sync_conversation(replace(self.conversation, folder_id=folder_id))
        self._refresh_library_captions()
        self.notify("Filed." if folder_id else "Back in the main list.")
        return self._library_state(folder_id)

    async def apply_tags_set(self, tag_ids: list[str]) -> LibraryState | None:
        """The open conversation's tags become exactly these."""
        if self.conversation is None:
            return None
        try:
            answer = await self.api.set_tags(self.conversation.id, tag_ids)
        except ConflictError as exc:
            self.notify(exc.message or "This conversation cannot be tagged.")
            return None
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        kept = answer.get("tag_ids")
        self.library_assignments = {
            **self.library_assignments,
            self.conversation.id: (
                [str(tag_id) for tag_id in kept] if isinstance(kept, list) else list(tag_ids)
            ),
        }
        self._refresh_library_captions()
        return self._library_state(self._open_folder_id())

    # --- custom instructions -----------------------------------------------------

    @work(group="action")
    async def action_instructions(self) -> None:
        """The thread's own instructions: read the levels in force, then edit
        the conversation's own text against the effective one as reference."""
        if self.conversation is None:
            return
        try:
            instructions = await self.api.conversation_instructions(self.conversation.id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        text = instructions.get("conversation")
        effective = instructions.get("effective")

        def chosen(edited: str | None) -> None:
            if edited is not None:
                self.apply_instructions(edited)

        self.push_screen(
            InstructionsEditor(
                text if isinstance(text, str) else "",
                effective=effective if isinstance(effective, str) else None,
            ),
            chosen,
        )

    @work(group="action")
    async def apply_instructions(self, text: str) -> None:
        if self.conversation is None:
            return
        # "" clears: the thread's prompt is lifted and the user's saved
        # instructions apply again. Over 2000 characters the server
        # refuses, and that refusal is what shows.
        try:
            await self.api.set_conversation_instructions(self.conversation.id, text or None)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.notify("Instructions saved.")

    # --- reply feedback ------------------------------------------------------------

    @work(group="feedback", exclusive=True)
    async def load_feedback(self, conversation_id: str) -> None:
        """The caller's own ratings in this conversation, to mark the
        transcript's thumbs with as it opens."""
        try:
            rows = await self.api.conversation_feedback(conversation_id)
        except Exception:
            # An API without the route: the transcript opens unmarked.
            return
        self.feedback = {
            str(row.get("message_id") or ""): row for row in rows if row.get("message_id")
        }
        await self.render_thread(keep_selection=True)

    @on(ThumbClicked)
    def _thumb_clicked(self, event: ThumbClicked) -> None:
        self.toggle_feedback(event.message_id, event.rating)

    def _view_of(self, message_id: str) -> MessageView | None:
        """The transcript's view of one message, when that message is on the
        active path."""
        for widget in self.query_one("#transcript", Transcript).items():
            if isinstance(widget, MessageView) and widget.message.id == message_id:
                return widget
        return None

    async def _show_feedback(self, message_id: str) -> None:
        """Mark one message's thumbs with the caller's rating: in place when
        the message is on screen, a transcript rebuild when it is not."""
        view = self._view_of(message_id)
        if view is not None and view.message.role == "assistant" and not view.streaming:
            view.apply_feedback(self.feedback.get(message_id))
            return
        await self.render_thread(keep_selection=True)

    @work(group="action")
    async def toggle_feedback(self, message_id: str, rating: int) -> None:
        """A thumb click is one vote: the same thumb again takes it back.
        The PUT carries no reasons — the dialog is where those go."""
        current = self.feedback.get(message_id)
        try:
            if current is not None and current.get("rating") == rating:
                await self.api.delete_feedback(message_id)
                self.feedback.pop(message_id, None)
                self.notify("Feedback withdrawn.")
            else:
                await self.api.set_feedback(message_id, rating)
                self.feedback[message_id] = {
                    "message_id": message_id,
                    "rating": rating,
                    "reasons": [],
                    "comment": None,
                }
                self.notify("Feedback sent.")
        except ConflictError as exc:
            self.notify(exc.message or "The rating was not kept.")
            return
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        await self._show_feedback(message_id)

    def action_feedback(self) -> None:
        """Reasons and an optional comment for the selected (or last)
        reply's rating. The reply's id goes into the dialog and comes back
        in its answer, so the PUT that follows names exactly the message
        the dialog was opened on."""
        message = self.selected_message()
        if message is None or message.role != "assistant":
            message = self.thread.last("assistant")
        if message is None:
            return
        row = self.feedback.get(message.id) or {}
        rating = row.get("rating")
        reasons = row.get("reasons")
        comment = row.get("comment")

        def chosen(result: tuple[str, int, list[str], str | None] | None) -> None:
            if result is not None:
                self.apply_feedback(*result)

        self.push_screen(
            FeedbackDialog(
                message.id,
                rating if rating in (-1, 1) else -1,
                list(reasons) if isinstance(reasons, list) else [],
                comment if isinstance(comment, str) else "",
            ),
            chosen,
        )

    @work(group="action")
    async def apply_feedback(
        self, message_id: str, rating: int, reasons: list[str], comment: str | None
    ) -> None:
        try:
            await self.api.set_feedback(message_id, rating, reasons=reasons, comment=comment)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.feedback[message_id] = {
            "message_id": message_id,
            "rating": rating,
            "reasons": list(reasons),
            "comment": comment,
        }
        await self._show_feedback(message_id)
        self.notify("Feedback sent.")

    # --- the stream --------------------------------------------------------------

    def consume(self, stream: Any) -> None:
        self.run_worker(self._consume(stream), group="stream", exclusive=True)

    def stop_stream(self) -> None:
        """Esc while an answer streams: close the stream. The server keeps
        what it had sent as a truncated turn, which `_consume` reads back."""
        self.workers.cancel_group(self, "stream")

    def _detach_turn(self) -> None:
        """Leaving a conversation mid-answer: stop its stream (the server
        keeps what arrived as a cut answer) and let go of it, so none of its
        frames reach the conversation opened next."""
        self._turn = None
        self.streaming = False
        self.live = None
        self.stop_stream()

    def _new_live(self) -> Message:
        return ConversationMessage(
            id=LIVE_ID,
            conversation_id=self.conversation.id if self.conversation else "",
            role="assistant",
            parts=[],
            content="",
            created_at="",
            model=self.model,
        )

    async def _consume(self, stream: AsyncIterator[StreamItem]) -> None:
        turn = object()
        self._turn = turn
        self.streaming = True
        self.show_failure(None)
        self.live = self._new_live()
        await self.render_thread()
        self.render_status()
        request_id: str | None = None
        terminal = False
        stopped = False
        try:
            async for item in stream:
                if self._turn is not turn:
                    break
                request_id = await self._apply(item, request_id)
                if isinstance(item, (*fr.TERMINAL_FRAMES, Cut)):
                    terminal = True
        except asyncio.CancelledError:
            stopped = True
            raise
        except Exception as exc:
            if self._turn is turn:
                self.fail_from_exception(exc)
        finally:
            # Both adapters stream from an async generator; closing it is
            # what releases the connection when the turn is stopped early.
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
            if self._turn is turn:
                await self._settle_turn(stopped=stopped, terminal=terminal, request_id=request_id)

    async def _settle_turn(self, *, stopped: bool, terminal: bool, request_id: str | None) -> None:
        self._turn = None
        self.streaming = False
        partial = self.live
        self.live = None
        if stopped:
            await self._resync()
            self.notify("Stopped.")
        elif not terminal and partial and partial.parts and self.failure is None:
            self.show_failure(Failure("truncated", request_id))
        await self.render_thread()
        self.render_status()

    async def _apply(self, item: StreamItem, request_id: str | None) -> str | None:
        live = self.live
        if isinstance(item, Opened):
            self.connection = "online"
            return item.request_id
        if isinstance(item, Cut):
            await self._resync()
            self.show_failure(Failure("truncated", item.request_id or request_id))
        elif isinstance(item, fr.CompactionFrame):
            if item.message.id:
                self.thread.add(item.message)
                self.thread.leaf = item.message.id
            if item.tokens_after:
                self.context_used = item.tokens_after
            await self.render_thread()
        elif isinstance(item, fr.MessageFrame):
            self.thread.add(item.message)
            self.thread.leaf = item.message.id
            await self.render_thread()
        elif isinstance(item, fr.PartStartFrame) and live is not None:
            parts = live.parts
            while len(parts) <= item.index:
                parts.append({"type": "text", "text": ""})
            parts[item.index] = _part_from(item)
            self._live_dirty = True
        elif isinstance(item, fr.DeltaFrame) and live is not None:
            parts = live.parts
            while len(parts) <= item.index:
                parts.append({"type": "reasoning" if item.reasoning else "text", "text": ""})
            parts[item.index]["text"] = (
                str(parts[item.index].get("text", "")) + (item.content or "") + (item.reasoning or "")
            )
            self._live_dirty = True
        elif isinstance(item, fr.TitleFrame) and self.conversation is not None:
            self._retitle(self.conversation.id, item.title)
        elif isinstance(item, fr.ContextFrame):
            self.context_used = (item.prompt_tokens or 0) + (item.completion_tokens or 0)
            if item.context_window:
                self.context_window = item.context_window
            self.render_status()
        elif isinstance(item, fr.NoticeFrame):
            if item.detail:
                self.notify(item.detail)
        elif isinstance(item, fr.DoneFrame):
            self.live = None
            self.thread.add(item.message)
            self.thread.leaf = item.message.id
            await self.render_thread()
            if item.message.truncated:
                self.show_failure(Failure("truncated", request_id))
        elif isinstance(item, fr.ModerationBlockFrame):
            self.live = None
            self.show_failure(Failure("moderation", item.request_id or request_id))
        elif isinstance(item, fr.ErrorFrame):
            await self._resync()
            self.show_failure(Failure(classify(item.detail, None, item.code), request_id))
        return request_id

    def _retitle(self, conversation_id: str, title: str) -> None:
        if self.conversation is not None and self.conversation.id == conversation_id:
            self.conversation = replace(self.conversation, title=title)
        self.conversations = [
            replace(c, title=title) if c.id == conversation_id else c for c in self.conversations
        ]
        for child in self.query_one("#conversations", ListView).children:
            if isinstance(child, ConversationItem) and child.conversation.id == conversation_id:
                child.retitle(title)

    async def _resync(self) -> None:
        """After a failed or stopped turn, read the tree back: the server
        persisted the partial answer (marked truncated) and that is what
        should show."""
        self.live = None
        if self.conversation is None:
            return
        try:
            tree = await self.api.tree(self.conversation.id)
        except Exception:
            return
        for message in tree.messages:
            self.thread.add(message)
        self.thread.leaf = self.thread.default_leaf(tree.current_leaf_message_id)

    # --- re-login ------------------------------------------------------------------

    async def relogin(self) -> str | None:
        """Called by the API adapter on a 401 it could not fix by re-reading
        the keyring. Shows the device code in a modal and waits."""
        if self.relogin_fn is None:
            return None
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

        def done(token: str | None) -> None:
            if not future.done():
                future.set_result(token)

        self.push_screen(DeviceLogin(self.relogin_fn), done)
        return await future


def _part_from(frame: fr.PartStartFrame) -> dict[str, Any]:
    """A `part_start` as the part the renderer reads: tool parts arrive
    whole in the frame, text and reasoning fill in by delta."""
    part: dict[str, Any] = {"type": frame.type, "text": ""}
    if frame.call_id is not None:
        part["id"] = frame.call_id
    if frame.name is not None:
        part["name"] = frame.name
    if frame.arguments is not None:
        part["arguments"] = frame.arguments
    if frame.output is not None:
        part["output"] = frame.output
    if frame.type == "tool_result":
        part["is_error"] = frame.is_error
    return part
