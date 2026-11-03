"""The Textual app: conversation list, transcript, composer, status line."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from matterwave_sdk import frames as fr
from matterwave_sdk.errors import (
    APIConnectionError,
    APIError,
    ConflictError,
    ConnectorConversationError,
)
from matterwave_sdk.models import (
    Attachment,
    Conversation,
    ConversationMessage,
    ConversationSource,
    UsageBalance,
)
from textual import on, work
from textual.app import App, ComposeResult, get_system_commands_provider
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static, TextArea

from . import __version__, images, markers
from .agent.session import SessionOptions

if TYPE_CHECKING:
    from .agent.screen import AgentScreen
from .api import DEFAULT_BASE_URL, NOTIFICATIONS_PAGE, ChatAPI, Cut, Opened, StreamItem
from .dictation import MEDIA_TYPES, MicRecorder, media_type_for, mic_available
from .export import conversation_markdown, export_filename, free_path
from .failures import Failure, classify, retry_in
from .models import (
    answer_usage,
    is_image_filename,
    model_accepts_images,
    model_window,
    vision_capable_model_ids,
)
from .paths import migrate_legacy_state, state_dir
from .pi_runner import (
    PiRpcBridge,
    PiRpcEvent,
    build_pi_argv,
    build_pi_launch_env,
    find_pi_binary,
)
from .roles import load_roles, roles_path, save_roles
from .screens import (
    Account,
    AccountActions,
    Accounts,
    AccountState,
    Confirm,
    DeviceLogin,
    DeviceLoginFn,
    Dictation,
    FeedbackDialog,
    FilePicker,
    HelpScreen,
    InstructionsEditor,
    Library,
    LibraryState,
    ModelPicker,
    NewChat,
    Notifications,
    PendingAttachments,
    PresetPicker,
    Prompt,
    RoleEditor,
    Roles,
    ShareDialog,
    Shares,
)
from .shell import detect_shell, run_shell_command, shell_command, shell_request
from .thread import Divider, Message, Thread
from .update import STATE_FILE as UPDATE_STATE_FILE
from .update import check_for_update
from .widgets import DividerView, ImageView, MessageView, ThumbClicked, code_blocks

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
# An upload's malware scan, waited on the way the web composer waits on it
# (web/src/apps/chat/attachments.ts): a deployment that scans after the
# upload answers `scanning`, and a turn naming the file is refused until it
# is `clean`. Ninety seconds of polls, then the person is asked to attach
# it again; the server keeps scanning, and sweeps a row nothing names.
SCAN_CLEAN = "clean"
SCAN_REJECTED = frozenset({"infected", "scan_failed", "failed"})
SCAN_POLL_S = 1.5
SCAN_POLLS_MAX = 60
# What the app remembers between runs beside the roles: the theme.
SETTINGS_FILE = "settings.json"
# Coding-agent backends and interactive composer slash commands.
AGENT_ENGINES: tuple[str, ...] = ("auto", "pi", "builtin")
SLASH_COMMANDS: tuple[str, ...] = (
    "/role",
    "/shell",
    "/temp",
    "/pi",
    "/pi-engine",
    "/agent-engine",
    "/pi-compact",
    "/pi-status",
    "/pi-new",
)


# --- per-device state (dismissed announcement ids) ----------------------------


def _state_path() -> Path:
    """Where the announcements this device dismissed are kept."""
    return state_dir() / "dismissed-announcements.json"


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


def scan_pending(scan_status: str) -> bool:
    """Whether an upload is still waiting on its scan. An empty status is an
    API that predates scanning, which never refuses a turn over it."""
    return bool(scan_status) and scan_status != SCAN_CLEAN and scan_status not in SCAN_REJECTED


def scan_refusal(filename: str, scan_status: str) -> str:
    """Why an upload left the message, naming the file. The scanner's
    signature name is never said: it is a hint about what got through."""
    if scan_status == "infected":
        return f"{filename} was removed by the malware scan."
    if scan_status in SCAN_REJECTED:
        return f"{filename} could not be scanned; attach it again."
    return f"Scanning {filename} is taking longer than expected; attach it again."


def typed_path(path_text: str) -> Path:
    """A path as it was typed or pasted: the quotes Windows' "Copy as path"
    (or a shell's drag and drop) puts round it come off, and `~` expands."""
    text = path_text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return Path(text).expanduser()


def expand_paths(path_text: str) -> list[str] | None:
    """The files a glob matches, sorted; None when the text is a plain
    path rather than a pattern."""
    path = typed_path(path_text)
    if not glob.has_magic(str(path)):
        return None
    return sorted(p for p in glob.glob(str(path)) if Path(p).is_file())


def editor_command(env: Mapping[str, str]) -> list[str]:
    """`$VISUAL`, else `$EDITOR`, else the platform's plain editor, as an
    argument list (Windows paths keep their backslashes)."""
    named = env.get("VISUAL") or env.get("EDITOR")
    if not named:
        return ["notepad"] if os.name == "nt" else ["vi"]
    return shlex.split(named, posix=os.name != "nt")


def load_saved_theme() -> str | None:
    """The theme last chosen in the app, from the state directory."""
    try:
        saved = json.loads((state_dir() / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    theme = saved.get("theme") if isinstance(saved, dict) else None
    return theme if isinstance(theme, str) and theme else None


def save_theme(theme: str) -> None:
    path = state_dir() / SETTINGS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"theme": theme}), encoding="utf-8")
    except OSError:
        return


def human_size(size: int) -> str:
    """Bytes as a person reads them: 512 B, 3.2 KB, 20 MB."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" or value >= 10 else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def attachment_label(attachment: Attachment) -> str:
    """A pending file as the status line and the detach list name it."""
    return attachment.filename + (" (scanning…)" if scan_pending(attachment.scan_status) else "")


def _read_attachment(path_text: str) -> tuple[bytes, str]:
    """The file's bytes and its name, off the event loop (it may be large)."""
    path = typed_path(path_text)
    return path.read_bytes(), path.name


def source_label(source: ConversationSource) -> str:
    """`Slack #support`: the connector by name, the channel as the server
    labels it (with its own `#`); the connector alone when the channel is
    unknown."""
    name = (source.connector[:1].upper() + source.connector[1:]) or "its connector"
    return f"{name} {source.channel_label}" if source.channel_label else name


def connector_notice(source: ConversationSource) -> str:
    """What stands in for a send in a conversation a connector opened:
    where it is answered, the thread's link when the connector gave one,
    and the fork as the way to continue here."""
    text = f"Answered in {source_label(source)} — reply in the thread"
    if source.url:
        text += f": {source.url}"
    return text + ". Press f to fork and continue here."


class ConversationItem(ListItem):
    def __init__(self, conversation: Conversation, *, library: str = "") -> None:
        self.conversation = conversation
        # The caption's tail naming the folder and tags, as the library
        # holds them; "" until the library answers.
        self.library = library
        super().__init__(Label(self.caption(), markup=False))

    def caption(self) -> str:
        c = self.conversation
        marks = [
            markers.badge(name)
            for name, on in (("pinned", c.pinned_at), ("archived", c.archived_at))
            if on
        ]
        # A connector's conversation says where it is answered, ahead of
        # the library tail.
        source = f" — {source_label(c.source)}" if c.source is not None else ""
        return " · ".join([*marks, c.title or "New chat"]) + source + self.library

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


class MoreResults(ListItem):
    """The last row of a search that has another page: selecting it reads
    that page in its place."""

    def __init__(self) -> None:
        super().__init__(Label("More results…", markup=False))


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
    commands: the actions that have no key of their own, plus the model and
    preset pickers."""

    ENTRIES: tuple[tuple[str, str, str], ...] = (
        ("Choose model…", "a model for this conversation", "action_pick_model"),
        (
            "Choose preset…",
            "an agent preset for this conversation, or none",
            "action_pick_preset",
        ),
        (
            "Account…",
            "API keys, signed-in browsers, preferences, your instructions and the plan",
            "action_account",
        ),
        (
            "Switch account…",
            "who is signed in here, and make another one acting",
            "action_switch_account",
        ),
        (
            "Import a ChatGPT or Claude export…",
            "bring an exported history into this account",
            "action_import",
        ),
        (
            "Export this account's data…",
            "every conversation and rating the account holds, as one JSON file",
            "action_export_account",
        ),
        (
            "Dictate audio…",
            "transcribe the microphone or an audio file into the composer",
            "action_dictate",
        ),
        ("Prompt roles…", "named system prompts to apply to this conversation", "action_roles"),
        (
            "Coding agent in this folder…",
            "read, edit and run commands here, with approvals",
            "action_open_agent",
        ),
    )

    def _offered(self, action: str) -> bool:
        if action == "action_pick_preset" and isinstance(self.app, MatterWaveApp):
            return self.app.has_preset_picker()
        return True

    async def discover(self) -> Hits:
        for name, help_text, action in self.ENTRIES:
            if self._offered(action):
                yield DiscoveryHit(name, getattr(self.app, action), help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, action in self.ENTRIES:
            if self._offered(action) and (match := matcher.match(name)) > 0:
                yield Hit(match, matcher.highlight(name), getattr(self.app, action), help=help_text)


# The chat's own key actions, switched off while the agent screen is in front
# (its composer takes the letters; elsewhere they would act on the chat
# hidden behind it).
CHAT_ACTIONS = frozenset(
    {
        "new",
        "search",
        "pick_model",
        "pick_preset",
        "edit",
        "regenerate",
        "branch",
        "toggle_originals",
        "compact",
        "instructions",
        "feedback",
        "rename",
        "toggle_pin",
        "toggle_archive",
        "delete_conversation",
        "fork",
        "share",
        "my_shares",
        "library",
        "roles",
        "report",
        "notifications",
        "dismiss_announcements",
        "export_markdown",
        "attach",
        "dictate",
        "cycle_filter",
        "copy",
        "copy_code",
        "leave_composer",
        "account",
        "pick_files",
        "detach",
        "edit_in_editor",
        "help",
    }
)


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
    #welcome { color: $text-muted; padding: 1 0; }
    #failure { display: none; background: $error 20%; color: $text; padding: 0 1; }
    #failure.shown { display: block; }
    #announcements { display: none; background: $accent 20%; color: $text; padding: 0 1; }
    #announcements.shown { display: block; }
    #temporary { display: none; background: $warning 20%; color: $text; padding: 0 1; }
    #temporary.shown { display: block; }
    #source { display: none; background: $primary 20%; color: $text; padding: 0 1; }
    #source.shown { display: block; }
    #composer { height: 6; border: round $panel; }
    #composer:focus { border: round $accent; }
    #status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    """
    BINDINGS = [
        Binding("ctrl+enter,ctrl+j,ctrl+s", "send", "Send", priority=True, id="send"),
        Binding("n", "new", "New", id="new"),
        Binding("slash", "search", "Search", id="search"),
        Binding("m", "pick_model", "Model", id="pick_model"),
        Binding("M", "pick_preset", "Preset", id="pick_preset"),
        Binding("e", "edit", "Edit", id="edit"),
        Binding("r", "regenerate", "Regenerate", id="regenerate"),
        Binding("left_square_bracket", "branch(-1)", "Prev branch", id="prev_branch"),
        Binding("right_square_bracket", "branch(1)", "Next branch", id="next_branch"),
        Binding("x", "toggle_originals", "Originals", id="toggle_originals"),
        Binding("c", "compact", "Compact", id="compact"),
        Binding("t", "instructions", "Instructions", id="instructions"),
        Binding("b", "feedback", "Feedback", id="feedback"),
        Binding("f2", "rename", "Rename", id="rename"),
        Binding("p", "toggle_pin", "Pin", id="toggle_pin"),
        Binding("a", "toggle_archive", "Archive", id="toggle_archive"),
        Binding("d", "delete_conversation", "Delete", id="delete_conversation"),
        Binding("f", "fork", "Fork", id="fork"),
        Binding("s", "share", "Share", id="share"),
        Binding("S", "my_shares", "Shares", id="my_shares"),
        Binding("l", "library", "Library", id="library"),
        Binding("L", "roles", "Roles", id="roles"),
        Binding("R", "report", "Report", id="report"),
        Binding("N", "notifications", "Inbox", id="notifications"),
        Binding("A", "dismiss_announcements", "Dismiss", id="dismiss_announcements"),
        Binding("U", "account", "Account", id="account"),
        Binding("w", "export_markdown", "Export", id="export_markdown"),
        Binding("i", "attach", "Attach", id="attach"),
        Binding("ctrl+o", "pick_files", "Pick files", id="pick_files"),
        Binding("I", "detach", "Detach", id="detach"),
        Binding("v", "dictate", "Dictate", id="dictate"),
        Binding("o", "cycle_filter", "List", id="cycle_filter"),
        Binding("y", "copy", "Copy", id="copy"),
        Binding("Y", "copy_code", "Copy code", id="copy_code"),
        Binding("ctrl+e", "edit_in_editor", "Editor", priority=True, id="editor"),
        Binding("question_mark,f1", "help", "Help", id="help"),
        Binding("escape", "leave_composer", "Transcript", show=False, id="leave_composer"),
        Binding("ctrl+q", "quit", "Quit", priority=True, id="quit"),
    ]

    def __init__(
        self,
        api: ChatAPI,
        *,
        relogin: DeviceLoginFn | None = None,
        shell_mode: bool = False,
        model: str | None = None,
        theme: str | None = None,
        environ: Mapping[str, str] | None = None,
        keymap: Mapping[str, str] | None = None,
        update_check: bool = False,
        show_answer_usage: bool = True,
        plain: bool = False,
        agent: SessionOptions | None = None,
    ) -> None:
        super().__init__()
        self.api = api
        # `matterwave-tui code [path]`: the agent screen opens over the chat,
        # and the chat's own loading waits until the person goes to it.
        self.agent_options = agent
        self._chat_loaded = False
        # `--plain`: every mark drawn as its word, no symbols.
        markers.configure(plain)
        # Tokens and cost under each answer this session streamed, by
        # message id; the config file's `answer_usage = "hide"` hides them.
        self.show_answer_usage = show_answer_usage
        self.answer_tokens: dict[str, tuple[int | None, int | None]] = {}
        self._turn_tokens: tuple[int | None, int | None] | None = None
        # The once-a-day look for a newer release; the CLI turns it on.
        self.update_check = update_check
        # The config file's [keys]: binding id -> key, over the defaults.
        self.keymap_overrides = dict(keymap or {})
        # Hands the composer's file to the editor; a test stands in.
        self._run_editor: Callable[[list[str]], int] = self._suspend_for_editor
        # The environment the image and shell settings are read from: the
        # process's own, with the config file's filling its gaps.
        environ = os.environ if environ is None else environ
        self._environ: Mapping[str, str] = environ
        self.agent_engine: str = "auto"
        self._pi_bridge: PiRpcBridge | None = None
        # The config file's theme, applied once the app is up.
        self.configured_theme = theme
        self.relogin_fn = relogin
        self.conversations: list[Conversation] = []
        self.conversation: Conversation | None = None
        self.thread = Thread()
        self.drafts: dict[str, str] = {}
        self.models_list: list[dict[str, Any]] = []
        self.default_model: str | None = None
        self.model: str | None = model
        # `-m` at launch: the model for every turn, whatever a conversation
        # was last on, until the picker chooses another.
        self.model_override = model
        # The org's live presets (GET /api/org/presets), the preset chosen
        # before a conversation exists (or on the open conversation), and a
        # deleted preset the open conversation is still on.
        self.presets_list: list[dict[str, Any]] = []
        self.selected_preset_id: str | None = None
        self.retired_preset: dict[str, Any] | None = None
        self.editing: str | None = None
        self.streaming = False
        # Typed while an answer streams: it sends itself when the turn
        # ends. Esc keeps it in the composer; it never sends unconfirmed.
        self.queued_prompt: str | None = None
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
        # Files uploaded but not sent yet, per conversation ("" before one
        # exists), the way drafts are kept: their ids go with that
        # conversation's next turn and nowhere else.
        self.attachment_drafts: dict[str, list[Attachment]] = {}
        # A send asked for while an upload was still being scanned: it goes
        # by itself once every scan is clean, and not at all if one fails.
        self.send_after_scan = False
        # The open conversation is temporary: created so in this session, so
        # no share or fork, and a notice standing in for the usual history.
        self.temporary_chat = False
        # Where a connector opened the open conversation (one started from
        # Slack): answered in that thread alone, so no send, edit or
        # regenerate here, and a notice standing in for the composer's
        # job. None for a conversation a person started.
        self.connector_source: ConversationSource | None = None
        # The caller's own ratings in the open conversation, by message id.
        self.feedback: dict[str, dict[str, Any]] = {}
        # Which slice of the conversations the sidebar shows, and the folder
        # or tag it is narrowed to: ("folder" | "tag", id), or None.
        self.list_filter = "default"
        self.library_filter: tuple[str, str] | None = None
        # The search the sidebar shows, and the cursor of its next page.
        self.search_query = ""
        self.search_cursor: str | None = None
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
        self.image_backend: str | None = images.detect_backend(environ)
        self._image_placements: dict[str, images.Placement] = {}
        self._image_ops: list[str] = []
        self._image_seq = 0
        # Attachment bytes already fetched for the transcript's image parts,
        # so a redraw hands a rebuilt view its picture without a re-fetch.
        self._image_data: dict[str, bytes] = {}
        # The prompt library: named system texts on this device, applied
        # with /role or the `L` screen. Empty until roles.json is read,
        # which is after an earlier release's files have moved over.
        migrate_legacy_state()
        self.roles = load_roles(roles_path())
        # The temperature a conversation's turns are sent with, set by
        # /temp; absent, the server's default.
        self.temperatures: dict[str, float] = {}
        # Shell assistant mode: each turn tells the model the OS and shell,
        # and each finished answer is a proposed command, run only after
        # the person confirms. `--shell` starts with it on; /shell toggles.
        # The runner is an attribute so a test can stand in.
        self.shell_mode = shell_mode
        self.shell = detect_shell(environ)
        self._run_shell: Callable[[str], Awaitable[str]] = self._run_in_shell
        # The answer the turn that just ended produced, for the shell gate.
        self._completed_answer: Message | None = None
        self._search_seq = 0

    @property
    def pending_attachments(self) -> list[Attachment]:
        """The open conversation's uploads, waiting for its next turn."""
        return self.attachment_drafts.get(self._draft_key(), [])

    @pending_attachments.setter
    def pending_attachments(self, attachments: list[Attachment]) -> None:
        key = self._draft_key()
        if attachments:
            self.attachment_drafts[key] = list(attachments)
        else:
            self.attachment_drafts.pop(key, None)

    def _draft_key(self) -> str:
        return self.conversation.id if self.conversation is not None else ""

    # --- keys, help, theme, editor --------------------------------------------

    def _apply_keymap(self) -> None:
        """The config file's key overrides, by binding id; an id no binding
        has is said rather than ignored."""
        known = {b.id for b in self.BINDINGS if isinstance(b, Binding) and b.id}
        unknown = sorted(set(self.keymap_overrides) - known)
        if unknown:
            self.notify(
                f"[keys] names {', '.join(unknown)}, which no key does; "
                "the help (?) lists the names.",
                severity="warning",
            )
        self.set_keymap({k: v for k, v in self.keymap_overrides.items() if k in known})

    @work(group="update", exclusive=True)
    async def look_for_update(self) -> None:
        async with httpx.AsyncClient() as http:
            latest = await check_for_update(__version__, state_dir() / UPDATE_STATE_FILE, http=http)
        if latest:
            self.notify(
                f"matterwave-tui {latest} is out (this is {__version__}): "
                "uv tool upgrade matterwave-tui."
            )

    def _remember_theme(self, theme: Any) -> None:
        save_theme(str(getattr(theme, "name", theme)))

    def help_rows(self) -> list[tuple[str, str, str]]:
        """(keys, what it does, its name for [keys]) for every binding, the
        config file's overrides in place."""
        rows: list[tuple[str, str, str]] = []
        for binding in [*self.BINDINGS, *Transcript.BINDINGS]:
            if not isinstance(binding, Binding):
                continue
            keys = self.keymap_overrides.get(binding.id or "", binding.key)
            shown = " / ".join(
                self.get_key_display(replace(binding, key=k)) for k in keys.split(",")
            )
            rows.append((shown, binding.description or binding.action, binding.id or ""))
        rows.append(("/pi <prompt>", "Run prompt with Pi 1.0 agent in transcript", ""))
        rows.append(("/pi-engine", "Inspect or switch agent engine (auto, pi, builtin)", ""))
        rows.append(("/pi-compact [instructions]", "Compact the Pi 1.0 RPC session context", ""))
        rows.append(("/pi-status", "Inspect Pi 1.0 binary, engine, and session status", ""))
        rows.append(("/pi-new", "Reset the Pi 1.0 RPC session", ""))
        return rows

    def action_help(self) -> None:
        """`?` / F1: every key, generated from the bindings."""
        self.push_screen(HelpScreen(self.help_rows()))

    def action_edit_in_editor(self) -> None:
        """Ctrl+E: the composer's text in `$VISUAL` / `$EDITOR`, the app
        suspended while it runs; what the editor saves comes back."""
        composer = self.query_one("#composer", TextArea)
        command = editor_command(os.environ)
        handle, name = tempfile.mkstemp(suffix=".md", prefix="matterwave-")
        path = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as draft:
                draft.write(composer.text)
            status = self._run_editor([*command, str(path)])
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            self.notify(f"The editor ({command[0]}) could not run: {exc}", severity="warning")
            return
        finally:
            path.unlink(missing_ok=True)
        if status != 0:
            self.notify(f"The editor exited {status}; the composer is as it was.")
            return
        composer.text = text.rstrip("\n")
        composer.focus()
        self.render_status()

    def _suspend_for_editor(self, argv: list[str]) -> int:
        with self.suspend():
            # The person's own editor, named by their own environment.
            return subprocess.call(argv)  # noqa: S603

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
                yield Static("", id="source", markup=False)
                yield TextArea(id="composer", placeholder="Message… (Ctrl+Enter sends)")
                yield Static("", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        # The config file's theme, else the one last chosen here; a theme
        # chosen from the palette is remembered for the next start.
        theme = self.configured_theme or load_saved_theme()
        if theme:
            if theme in self.available_themes:
                self.theme = theme
            else:
                self.notify(
                    f"No theme named {theme}; the palette's “Change theme” lists them.",
                    severity="warning",
                )
        self.theme_changed_signal.subscribe(self, self._remember_theme)
        self._apply_keymap()
        if self.update_check:
            self.look_for_update()
        self.set_interval(LIVE_REDRAW_S, self._flush_live)
        self.set_interval(1.0, self._tick_rate_limit)
        if self.image_backend is not None:
            self.set_interval(IMAGE_REFRESH_S, self._refresh_images)
        self.render_status()
        self.query_one("#composer", TextArea).focus()
        if self.agent_options is not None:
            self.open_agent(self.agent_options)
        else:
            self._load_chat()

    def _load_chat(self) -> None:
        if not self._chat_loaded:
            self._chat_loaded = True
            self.load_initial()

    # --- the coding agent ------------------------------------------------------------

    def open_agent(self, options: SessionOptions) -> None:
        """The agent screen over the chat. The API adapter is the wire: its
        `/v1` and `/mcp` methods are the agent's."""
        from typing import cast

        from .agent.screen import AgentScreen
        from .agent.wire import AgentWire

        def leave() -> None:
            self.pop_screen()
            self._load_chat()

        self.push_screen(AgentScreen(cast(AgentWire, self.api), options, leave=leave))

    def action_open_agent(self) -> None:
        self.open_agent(SessionOptions(root=Path.cwd()))

    async def on_unmount(self) -> None:
        if self._pi_bridge is not None:
            await self._pi_bridge.close()
            self._pi_bridge = None
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

    # --- presets ---------------------------------------------------------------

    def active_preset_id(self) -> str | None:
        if self.conversation is not None:
            if self.conversation.preset is not None:
                return self.conversation.preset.id
            return self.conversation.preset_id
        return self.selected_preset_id

    def active_preset(self) -> dict[str, Any] | None:
        pid = self.active_preset_id()
        if not pid:
            return None
        for preset in self.presets_list:
            if str(preset.get("id") or "") == pid:
                return preset
        if self.retired_preset is not None and str(self.retired_preset.get("id") or "") == pid:
            return self.retired_preset
        if (
            self.conversation is not None
            and self.conversation.preset is not None
            and self.conversation.preset.id == pid
        ):
            return {"id": pid, "name": self.conversation.preset.name}
        return {"id": pid, "name": pid}

    def active_preset_model(self) -> str | None:
        preset = self.active_preset()
        model = preset.get("model") if preset is not None else None
        return str(model) if isinstance(model, str) and model else None

    def effective_model(self) -> str | None:
        return self.active_preset_model() or self.model or self.default_model

    def active_welcome_message(self) -> str | None:
        preset = self.active_preset()
        welcome = preset.get("welcome_message") if preset is not None else None
        return welcome.strip() if isinstance(welcome, str) and welcome.strip() else None

    def has_preset_picker(self) -> bool:
        return bool(self.presets_list or self.active_preset_id() is not None)

    async def load_presets(self) -> None:
        try:
            rows = await self.api.presets()
        except Exception:
            # No presets route on this deployment, or an account outside an org.
            return
        self.presets_list = [row for row in rows if isinstance(row, dict)]
        self.refresh_bindings()

    async def _resolve_conversation_preset(self, conversation: Conversation) -> None:
        pid = conversation.preset.id if conversation.preset is not None else conversation.preset_id
        self.selected_preset_id = pid
        if not pid or any(str(p.get("id") or "") == pid for p in self.presets_list):
            self.retired_preset = None
            self.refresh_bindings()
            return
        try:
            fetched = await self.api.preset(pid)
        except Exception:
            name = conversation.preset.name if conversation.preset is not None else pid
            fetched = {"id": pid, "name": name, "deleted_at": "retired"}
        self.retired_preset = fetched
        self.refresh_bindings()

    # --- status line -----------------------------------------------------------

    def status_text(self) -> str:
        model = self.effective_model()
        bits = [f"model {model or '—'}"]
        preset = self.active_preset()
        if preset is not None and preset.get("name"):
            bits.append(f"preset {preset['name']}")
        window = self.context_window or model_window(self.models_list, model)
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
                warn = markers.lead("warning") if balance.warn else ""
                bits.append(
                    f"{warn}usage {balance.used:,}/{balance.included:,} ({round(balance.percent)}%)"
                )
        budget = self.budget
        if budget is not None and budget.get("overage_enabled"):
            overage = int(budget.get("overage_micros") or 0)
            cap = int(budget.get("overage_cap_micros") or 0)
            if cap:
                warn = markers.lead("warning") if overage >= cap else ""
                bits.append(f"{warn}overage ${overage / 1e6:,.2f} of ${cap / 1e6:,.2f}")
        if self.unread_notifications:
            bits.append(f"{markers.icon('unread')}{self.unread_notifications} unread")
        if self.pending_attachments:
            names = ", ".join(attachment_label(a) for a in self.pending_attachments)
            bits.append(f"attached: {names}")
        if self.send_after_scan:
            bits.append("sends when the scan is done")
        if self.streaming:
            bits.append("streaming… (Esc stops)")
        if self.queued_prompt:
            bits.append("queued: sends when this answer finishes")
        bits.append(self.connection)
        wait = self.rate_limit_remaining()
        if wait is not None:
            bits.append(f"rate limited · retry in {retry_in(wait)}")
        if self.last_error_request_id:
            bits.append(f"last error ref {self.last_error_request_id}")
        if self.editing:
            bits.append("editing (Esc cancels)")
        if self.shell_mode:
            bits.append("shell: answers are commands to confirm")
        if self.library_filter is not None:
            bits.append(f"showing only {self._library_filter_name()} (o shows all)")
        temperature = self.temperatures.get(self._draft_key())
        if temperature is not None:
            bits.append(f"temperature {temperature:g}")
        if self.agent_engine != "auto":
            bits.append(f"engine {self.agent_engine}")
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
        if isinstance(exc, ConnectorConversationError):
            # The list was stale: the server says the conversation is a
            # connector's and names the thread. Not a failure of the turn.
            self._lock_connector_conversation(exc.source)
        elif isinstance(exc, APIConnectionError):
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
        """Share and fork do not exist for a temporary conversation, nor
        edit and regenerate for a connector's: while one is open the keys
        are dead and the commands off the footer. The preset picker exists
        only when the org has presets or one is in force on the conversation,
        and the announcements banner offers its dismiss key only while it
        stands."""
        if action in CHAT_ACTIONS and self._agent_screen() is not None:
            # The agent screen is in front: the chat's keys wait behind it.
            return False
        if action == "dismiss_announcements":
            return bool(self.announcements)
        if action == "pick_preset":
            return self.has_preset_picker()
        if self.connector_source is not None and action in ("edit", "regenerate"):
            return False
        return not (self.temporary_chat and action in ("share", "fork"))

    def _agent_screen(self) -> AgentScreen | None:
        """The agent screen when it is open, under a modal of its own or
        not: the chat is behind it either way."""
        from .agent.screen import AgentScreen

        for screen in reversed(self.screen_stack):
            if isinstance(screen, AgentScreen):
                return screen
        return None

    # --- a connector's conversation ---------------------------------------------

    def set_connector_source(self, source: ConversationSource | None) -> None:
        """Marks the open conversation as a connector's (or not) and
        re-derives what depends on it: the notice over the composer and
        the edit and regenerate keys."""
        self.connector_source = source
        banner = self.query_one("#source", Static)
        if source is not None:
            banner.update(connector_notice(source))
            banner.add_class("shown")
        else:
            banner.remove_class("shown")
            banner.update("")
        self.refresh_bindings()

    def _lock_connector_conversation(self, source: ConversationSource) -> None:
        """The server refused a turn as a connector's: the conversation is
        marked so here too, in the sidebar and over the composer, and the
        draft stays for the fork to carry on with."""
        if self.conversation is not None:
            self._sync_conversation(replace(self.conversation, source=source))
        self.set_connector_source(source)
        self.show_failure(None)
        self.notify(connector_notice(source))

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
        loaded = await self.load_models()
        override = self.model_override
        if loaded and override and self.models_list and not self.model_allowed(override):
            self.notify(
                f"{override} is not a model you can use here; each conversation keeps its own.",
                severity="warning",
            )
            self.model = self.model_override = None
        await self.load_presets()
        await self.refresh_conversations()
        self.refresh_account_lines()
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
        self.models_list = (
            [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []
        )
        default = models.get("default_model")
        self.default_model = str(default) if default else None
        return True

    async def refresh_conversations(self, query: str = "") -> None:
        """The sidebar: a search's first page, or the list -- every page of
        it when a folder or tag narrows it, since that filter is the
        client's and must not miss what the first page left out."""
        more: str | None = None
        try:
            if query:
                page = await self.api.search(query)
                found, more = page.conversations, page.next_cursor
            elif self.library_filter is not None:
                everything = await self.api.all_conversations(filter=self.list_filter)
                found = [c for c in everything if self._in_library_filter(c)]
            else:
                found = await self.api.list_conversations(filter=self.list_filter)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        if not query:
            self.conversations = found
        self.search_query, self.search_cursor = query, more
        view = self.query_one("#conversations", ListView)
        await view.clear()
        items: list[ListItem] = [
            ConversationItem(c, library=self._library_suffix(c)) for c in found
        ]
        if more:
            items.append(MoreResults())
        await view.extend(items)

    def _in_library_filter(self, conversation: Conversation) -> bool:
        if self.library_filter is None:
            return True
        kind, row_id = self.library_filter
        if kind == "folder":
            return conversation.folder_id == row_id
        return row_id in self.library_assignments.get(conversation.id, [])

    @work(group="search", exclusive=True)
    async def load_more_results(self) -> None:
        """The search's next page, in place of its "More results…" row."""
        if not self.search_query or not self.search_cursor:
            return
        try:
            page = await self.api.search(self.search_query, self.search_cursor)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        view = self.query_one("#conversations", ListView)
        for row in list(view.query(MoreResults)):
            await row.remove()
        self.search_cursor = page.next_cursor
        items: list[ListItem] = [
            ConversationItem(c, library=self._library_suffix(c)) for c in page.conversations
        ]
        if page.next_cursor:
            items.append(MoreResults())
        await view.extend(items)

    def refresh_account_lines(self) -> None:
        """The status line's account bits as the server now holds them: the
        period's usage, the overage against its cap, the unread count."""
        self.load_usage()
        self.load_budget()
        self.load_notifications()

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
        elif isinstance(event.item, MoreResults):
            self.load_more_results()

    @work(group="load", exclusive=True)
    async def open_worker(self, conversation_id: str) -> None:
        await self.open_conversation(conversation_id)

    async def open_conversation(self, conversation_id: str) -> None:
        if self.streaming and (
            self.conversation is None or self.conversation.id != conversation_id
        ):
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
        # A send that was waiting on a scan belonged to the conversation
        # left; its text and files stay there, and nothing goes by itself.
        self.send_after_scan = False
        self.thread = Thread.from_tree(tree.messages, tree.current_leaf_message_id)
        self.model = self.model_override or tree.default_model or self.model
        self.editing = None
        self.feedback = {}
        # A conversation read from the list is never the temporary one that
        # was just created: the server keeps those out of every list and
        # every search, so only the creation itself can open one.
        self.set_temporary_chat(False)
        self.set_connector_source(tree.source)
        await self._resolve_conversation_preset(tree)
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
                    MessageView(
                        item,
                        usage=self._usage_line(item),
                        position=self.thread.position(item),
                        feedback=self.feedback.get(item.id),
                        image_backend=self.image_backend,
                    )
                )
        if self.live is not None:
            widgets.append(MessageView(self.live, streaming=True))
        if not widgets and (welcome := self.active_welcome_message()):
            widgets.append(Static(welcome, id="welcome", classes="welcome", markup=False))
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
                placement = (
                    images.iterm2_placement(
                        image_id, at.x + 1, at.y + 1, max(at.width, 8), max(at.height, 2), view.data
                    )
                    if self.image_backend == "iterm2"
                    else images.sixel_placement(image_id, at.x + 1, at.y + 1, view.data)
                )
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

    def _usage_line(self, message: Message) -> str | None:
        """Under an answer: what it took and cost, when this session saw
        its turn (the server keeps no per-answer usage on the message)."""
        tokens = self.answer_tokens.get(message.id)
        if not self.show_answer_usage or tokens is None or message.role != "assistant":
            return None
        return answer_usage(
            self.models_list, message.model or self.active_preset_model() or self.model, *tokens
        )

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

    # --- clipboard -----------------------------------------------------------------

    def _copy_target(self) -> Message | None:
        """The message copy works on: the one selected in the transcript,
        else the last answer."""
        widget = self.query_one("#transcript", Transcript).selected_widget()
        if isinstance(widget, MessageView):
            return widget.message
        return self.thread.last("assistant")

    def action_copy(self) -> None:
        """`y`: the selected message, or the last answer, onto the
        clipboard (OSC 52, where the terminal takes it)."""
        target = self._copy_target()
        text = str(target.content or "") if target is not None else ""
        if not text.strip():
            self.notify("Nothing to copy.")
            return
        self.copy_to_clipboard(text)
        self.notify("Copied.")

    def action_copy_code(self) -> None:
        """`Y`: just the fenced code of that message — the part a person
        paste-able wants."""
        target = self._copy_target()
        blocks = code_blocks(str(target.content or "")) if target is not None else []
        if not blocks:
            self.notify("No code blocks in this message.")
            return
        self.copy_to_clipboard("\n\n".join(blocks))
        self.notify("Code copied.")

    def action_new(self) -> None:
        """A new chat. Temporary is decided here or not at all: the flag is
        fixed at creation and no route flips it afterwards. Enter keeps the
        chat; only `t` makes the kind that deletes itself."""

        def chosen(kind: str | None) -> None:
            if kind is not None:
                self._new_conversation(kind == "temporary")

        self.push_screen(NewChat(), chosen)

    @work(group="action")
    async def _new_conversation(self, temporary: bool) -> None:
        self.selected_preset_id = None
        self.retired_preset = None
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
        preset = self.active_preset()
        fixed_by = str(preset.get("name") or "") if preset is not None else ""

        def chosen(model_id: str | None) -> None:
            if model_id:
                self.apply_model(model_id)

        self.push_screen(
            ModelPicker(
                self.models_list,
                self.effective_model() if fixed_by else self.model,
                fixed_by=fixed_by or None,
            ),
            chosen,
        )

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
        preset = self.active_preset()
        if preset is not None:
            fixed = str(preset.get("model") or self.effective_model() or "")
            name = str(preset.get("name") or "active")
            self.notify(
                f"Model is fixed to {fixed} by the {name} preset; choose No preset to change it.",
                severity="warning",
            )
            return
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
        self.model_override = None
        self.render_status()

    @work(group="action")
    async def action_pick_preset(self) -> None:
        await self.load_presets()
        if not self.has_preset_picker():
            return
        current = self.active_preset_id()
        retired = (
            self.retired_preset
            if self.retired_preset is not None
            and str(self.retired_preset.get("id") or "") == current
            else None
        )

        def chosen(choice: str | None) -> None:
            if choice is not None:
                self.apply_preset(choice or None)

        self.push_screen(
            PresetPicker(self.presets_list, current, retired=retired),
            chosen,
        )

    @work(group="action")
    async def apply_preset(self, preset_id: str | None) -> None:
        """Put the conversation on `preset_id`, or clear it (`None`) back to
        the conversation's own model and instructions."""
        if (
            preset_id is not None
            and not any(str(p.get("id") or "") == preset_id for p in self.presets_list)
            and not (
                self.retired_preset is not None
                and str(self.retired_preset.get("id") or "") == preset_id
                and self.active_preset_id() == preset_id
            )
        ):
            self.notify(f"{preset_id} is not an available preset.", severity="warning")
            return
        if self.conversation is not None:
            try:
                updated = await self.api.set_preset(self.conversation.id, preset_id)
            except Exception as exc:
                self.fail_from_exception(exc)
                return
            self._sync_conversation(updated)
            await self._resolve_conversation_preset(updated)
        else:
            self.selected_preset_id = preset_id
            if preset_id is None:
                self.retired_preset = None
            self.refresh_bindings()
        await self.render_thread(keep_selection=True)
        self.render_status()

    def action_send(self) -> None:
        agent = self._agent_screen()
        if agent is not None:
            # Ctrl+Enter is the app's priority key; in code mode it sends
            # to the agent, and not while one of its dialogs is open.
            front: object = self.screen
            if front is agent:
                agent.action_send()
            return
        composer = self.query_one("#composer", TextArea)
        text = composer.text.strip()
        if not text:
            return
        if text.startswith("/") and not text.startswith("//"):
            # A command, not a message; an unknown one leaves the text where
            # it is, to be fixed or escaped.
            if self._composer_command(text):
                composer.text = ""
                self.drafts.pop(self.conversation.id if self.conversation else "", None)
            return
        if self.connector_source is not None:
            # Answered in its own thread: nothing is posted, the draft stays,
            # and the notice says where to reply and how to continue here.
            self.notify(connector_notice(self.connector_source))
            return
        if self.streaming:
            # The turn is going: the prompt queues and sends itself when
            # the answer is whole.
            self.queued_prompt = text
            composer.text = ""
            if self.conversation is not None:
                self.drafts.pop(self.conversation.id, None)
            self.notify("Queued — it sends when this answer finishes.")
            self.render_status()
            return
        scanning = [a.filename for a in self.pending_attachments if scan_pending(a.scan_status)]
        if scanning and not self.editing:
            # A turn naming an upload the scanner has not cleared is refused,
            # so the message waits in the composer for the verdict.
            self.send_after_scan = True
            self.notify(f"Waiting for the scan of {', '.join(scanning)}; the message sends then.")
            self.render_status()
            return
        # `//` is a leading slash escaped: "//etc/hosts" sends "/etc/hosts".
        # The queue above keeps the escape, since the queued text comes back
        # through here.
        if text.startswith("//"):
            text = text[1:]
        if self.shell_mode:
            text = shell_request(text, self.shell)
        attachment_ids = [a.id for a in self.pending_attachments] or None
        if self.conversation is None:
            self.run_worker(
                self._create_then_send(text, attachment_ids), group="stream", exclusive=True
            )
            return
        cid = self.conversation.id
        temperature = self.temperatures.get(cid)
        turn_model = self.active_preset_model() or self.model
        if self.editing:
            stream = self.api.edit(cid, self.editing, text, turn_model, temperature=temperature)
        else:
            stream = self.api.send(
                cid, text, turn_model, attachment_ids=attachment_ids, temperature=temperature
            )
        composer.text = ""
        self.drafts.pop(cid, None)
        self.editing = None
        self.pending_attachments = []
        self.consume(stream)

    async def _create_then_send(self, text: str, attachment_ids: list[str] | None = None) -> None:
        preset_id = self.selected_preset_id
        try:
            conv = await self.api.create_conversation(model=self.model, preset_id=preset_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        # The files attached before the conversation existed go with this
        # turn; they are no longer waiting for one.
        self.attachment_drafts.pop("", None)
        # A temperature set before the conversation existed is its own now.
        temperature = self.temperatures.pop("", None)
        if temperature is not None:
            self.temperatures[conv.id] = temperature
        await self.refresh_conversations()
        await self.open_conversation(conv.id)
        self.query_one("#composer", TextArea).text = ""
        await self._consume(
            self.api.send(
                conv.id,
                text,
                self.active_preset_model() or self.model,
                attachment_ids=attachment_ids,
                temperature=temperature,
            )
        )

    def action_edit(self) -> None:
        if self.connector_source is not None:
            self.notify(connector_notice(self.connector_source))
            return
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
        if self.connector_source is not None:
            self.notify(connector_notice(self.connector_source))
            return
        message = self.selected_message()
        if message is None or message.role != "assistant":
            message = self.thread.last("assistant")
        if message is None:
            return
        self.consume(
            self.api.regenerate(
                self.conversation.id,
                message.id,
                self.active_preset_model() or self.model,
                temperature=self.temperatures.get(self.conversation.id),
            )
        )

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
        self.attachment_drafts.pop(conversation_id, None)
        if self.conversation is not None and self.conversation.id == conversation_id:
            if self.streaming:
                self._detach_turn()
            self.conversation = None
            self.selected_preset_id = None
            self.retired_preset = None
            self.refresh_bindings()
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
        """`s`: the share dialog for the open conversation, with the links
        already made for it."""
        if self.conversation is None:
            return
        target = self.conversation
        try:
            rows = await self.api.conversation_shares(target.id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return

        async def create(
            expires_in_s: int | None, include_attachments: bool
        ) -> tuple[str, list[dict[str, Any]]] | None:
            return await self.apply_share(target.id, expires_in_s, include_attachments)

        async def revoke(share_id: str) -> list[dict[str, Any]]:
            await self.apply_share_revoke(share_id)
            return await self.api.conversation_shares(target.id)

        self.push_screen(ShareDialog(target.title, rows, create=create, revoke=revoke))

    async def apply_share(
        self, conversation_id: str, expires_in_s: int | None, include_attachments: bool
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """A new link, copied to the clipboard; the dialog's list is read
        again so it shows it."""
        try:
            url = await self.api.share_conversation(
                conversation_id,
                expires_in_s=expires_in_s,
                include_attachments=include_attachments,
            )
        except ConflictError as exc:
            self.notify(exc.message or "This conversation cannot be shared.")
            return None
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.copy_to_clipboard(url)
        self.notify(f"Share link copied: {url}")
        try:
            rows = await self.api.conversation_shares(conversation_id)
        except Exception:
            rows = []
        return url, rows

    @work(group="action")
    async def action_export_markdown(self) -> None:
        if self.conversation is None:
            return
        conversation, thread = self.conversation, self.thread
        document = conversation_markdown(conversation, thread)

        def write() -> Path:
            # Exclusive creation: a file that appeared since the free name
            # was picked is not overwritten either.
            path = free_path(Path.cwd() / export_filename(conversation))
            with path.open("x", encoding="utf-8") as out:
                out.write(document)
            return path

        try:
            path = await asyncio.to_thread(write)
        except OSError as exc:
            self.notify(f"Could not write the export: {exc}", severity="error")
            return
        self.notify(f"Exported to {path}")

    def action_attach(self) -> None:
        def chosen(path_text: str | None) -> None:
            if path_text:
                self.apply_attach(path_text)

        self.push_screen(Prompt("Attach a file (its path):"), chosen)

    @work(group="action")
    async def apply_attach(self, path_text: str) -> None:
        """A path, or a glob (`~/shots/*.png`) for every file it matches."""
        paths = expand_paths(path_text)
        if paths is None:
            await self._attach_one(path_text)
            return
        if not paths:
            self.notify(f"No file matches {path_text}.", severity="warning")
            return
        for path in paths:
            await self._attach_one(path)

    def action_pick_files(self) -> None:
        """Ctrl+O: files picked from a tree of the working directory."""

        def chosen(paths: list[str] | None) -> None:
            if paths:
                self.run_worker(self._attach_all(paths), group="action")

        self.push_screen(FilePicker(Path.cwd()), chosen)

    async def _attach_all(self, paths: list[str]) -> None:
        for path in paths:
            await self._attach_one(path)

    async def _attach_one(self, path_text: str) -> None:
        try:
            data, filename = await asyncio.to_thread(_read_attachment, path_text)
        except OSError as exc:
            self.notify(f"Could not read {path_text}: {exc}", severity="error")
            return
        # The vision gate, before the upload: a model the list says cannot
        # see is told by name, with the ones that can. An unknown model is
        # allowed through; the server's 422 remains the backstop.
        model_id = self.effective_model()
        if is_image_filename(filename) and not model_accepts_images(self.models_list, model_id):
            capable = vision_capable_model_ids(self.models_list)
            names = ", ".join(capable) if capable else "none of your models"
            self.notify(
                f"{model_id or 'This model'} cannot read images. Vision-capable models: {names}.",
                severity="warning",
            )
            return
        refusal = await self._over_the_limits(filename, len(data))
        if refusal:
            self.notify(refusal, severity="warning")
            return
        conversation_id = self.conversation.id if self.conversation else None
        # The file belongs to the conversation it was attached in, even if
        # another is open by the time the upload lands.
        key = self._draft_key()
        try:
            attachment = await self.api.upload_attachment(
                data, filename, conversation_id=conversation_id
            )
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        if attachment.scan_status in SCAN_REJECTED:
            # A scanner that decides inside the upload, and said no.
            self.notify(scan_refusal(attachment.filename, attachment.scan_status), severity="error")
            return
        self.attachment_drafts[key] = [*self.attachment_drafts.get(key, []), attachment]
        if scan_pending(attachment.scan_status):
            self.notify(f"Attached {attachment.filename}; it is being scanned.")
            self.run_worker(self._watch_scan(attachment.id), group="scan")
        else:
            self.notify(f"Attached {attachment.filename}.")
        self.render_status()

    async def _watch_scan(self, attachment_id: str) -> None:
        """Poll one upload's scan until it has a verdict, or the limit
        runs out."""
        scan_status = ""
        for _ in range(SCAN_POLLS_MAX):
            await asyncio.sleep(SCAN_POLL_S)
            if self._attachment_key(attachment_id) is None:
                return
            try:
                scan_status = await self.api.attachment_scan_status(attachment_id)
            except Exception:
                # A failed poll is asked again, as the web composer does;
                # only the limit ends the wait.
                scan_status = ""
            if scan_status and not scan_pending(scan_status):
                break
        self._scan_settled(attachment_id, scan_status)

    def _scan_settled(self, attachment_id: str, scan_status: str) -> None:
        """A verdict, or none in time: a clean file stays attached; any
        other leaves the message with the file named, and a send that was
        waiting stays in the composer for the person to decide. When the
        last scan comes back clean, the waiting send goes."""
        key = self._attachment_key(attachment_id)
        if key is None:
            return
        held = self.attachment_drafts[key]
        attachment = next(a for a in held if a.id == attachment_id)
        if scan_status == SCAN_CLEAN:
            self.attachment_drafts[key] = [
                replace(a, scan_status=SCAN_CLEAN) if a.id == attachment_id else a for a in held
            ]
        else:
            kept = [a for a in held if a.id != attachment_id]
            if kept:
                self.attachment_drafts[key] = kept
            else:
                del self.attachment_drafts[key]
            if key == self._draft_key():
                self.send_after_scan = False
            self.notify(scan_refusal(attachment.filename, scan_status), severity="error")
        self.render_status()
        if self.send_after_scan and not any(
            scan_pending(a.scan_status) for a in self.pending_attachments
        ):
            self.send_after_scan = False
            self.action_send()

    async def _over_the_limits(self, filename: str, size: int) -> str | None:
        """Why the server would refuse this upload for its size, said before
        the bytes go: bigger than one upload may be, or than what is left of
        the quota. A deployment without the limits route checks nothing
        here, and the server's own refusal stays the backstop."""
        try:
            limits = await self.api.attachment_limits()
        except Exception:
            return None
        if limits.max_upload_bytes and size > limits.max_upload_bytes:
            return (
                f"{filename} is {human_size(size)}; one upload may be at most "
                f"{human_size(limits.max_upload_bytes)}."
            )
        if limits.quota_bytes and size > limits.quota_remaining_bytes:
            return (
                f"{filename} is {human_size(size)}, and only "
                f"{human_size(limits.quota_remaining_bytes)} of your "
                f"{human_size(limits.quota_bytes)} storage is left."
            )
        return None

    def _attachment_key(self, attachment_id: str) -> str | None:
        """Which conversation's pending files hold this upload, if any."""
        for key, held in self.attachment_drafts.items():
            if any(a.id == attachment_id for a in held):
                return key
        return None

    def action_detach(self) -> None:
        """`I`: take a file off the open conversation's next message."""
        pending = list(self.pending_attachments)
        if not pending:
            self.notify("Nothing is attached to this message.")
            return

        def chosen(attachment_id: str | None) -> None:
            if attachment_id:
                self.apply_detach(attachment_id)

        self.push_screen(PendingAttachments([(a.id, attachment_label(a)) for a in pending]), chosen)

    @work(group="action")
    async def apply_detach(self, attachment_id: str) -> None:
        """Off the message at once; then off the server too, where nothing
        else names the upload and it would count against the quota until
        the sweeper took it."""
        removed = next((a for a in self.pending_attachments if a.id == attachment_id), None)
        if removed is None:
            return
        self.pending_attachments = [a for a in self.pending_attachments if a.id != attachment_id]
        self.render_status()
        self.notify(f"Removed {removed.filename}.")
        # The file is off the message whatever the server says; one it
        # keeps is unreferenced, and its sweeper removes it.
        with contextlib.suppress(Exception):
            await self.api.delete_attachment(attachment_id)

    @work(group="action")
    async def action_cycle_filter(self) -> None:
        """The next slice of the list; with a folder or tag narrowing it,
        the first press shows the whole slice again."""
        if self.library_filter is not None:
            self.library_filter = None
        else:
            index = LIST_FILTERS.index(self.list_filter)
            self.list_filter = LIST_FILTERS[(index + 1) % len(LIST_FILTERS)]
        await self.refresh_conversations()
        self.render_status()
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
            severity = str(row.get("severity"))
            name = severity if severity in ("critical", "warning") else "notice"
            lines.append(f"{markers.badge(name)}: {row.get('message') or ''}")
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
        self.announcements = [row for row in self.announcements if not row.get("dismissible", True)]
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
        selected = transcript.selected_widget() if transcript.selected is not None else None
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
        offered = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
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
        self.attachment_drafts.clear()
        self.send_after_scan = False
        self.feedback = {}
        self.notification_rows = []
        self.unread_notifications = None
        if self.streaming:
            self._detach_turn()
        self.conversation = None
        self.presets_list = []
        self.selected_preset_id = None
        self.retired_preset = None
        self.thread = Thread()
        self.editing = None
        self.live = None
        await self.render_thread()
        await self.load_presets()
        await self.refresh_conversations()
        self.refresh_account_lines()
        self.load_announcements()
        if self.conversations:
            await self.open_conversation(self.conversations[0].id)
        self.render_status()

    # --- the person's own account ------------------------------------------------------

    @work(group="action")
    async def action_account(self) -> None:
        """`U`: keys, browsers, preferences, instructions and the plan."""
        state = await self._account_state()
        if state is None:
            return
        self.push_screen(
            Account(
                state,
                AccountActions(
                    revoke_key=self.apply_key_revoke,
                    rotate_key=self.apply_key_rotate,
                    revoke_session=self.apply_session_revoke,
                    set_preferences=self.apply_preferences,
                    set_instructions=self.apply_user_instructions,
                ),
            )
        )

    async def _account_state(self) -> AccountState | None:
        """Everything the account screen shows, read fresh. The keys, the
        preferences and the instructions are the screen; a summary line
        whose route this deployment or this key cannot read is left out."""
        try:
            keys, preferences, instructions = await asyncio.gather(
                self.api.api_keys(), self.api.preferences(), self.api.user_instructions()
            )
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        reads = await asyncio.gather(
            self.api.entitlements(),
            self.api.billing_status(),
            self.api.plans(),
            self.api.enforcement(),
            self.api.sessions(),
            return_exceptions=True,
        )
        entitlements, billing, plans, enforcement, sessions = (
            None if isinstance(r, BaseException) else r for r in reads
        )
        return AccountState(
            summary=account_summary(entitlements, billing, plans, enforcement),
            keys=keys,
            sessions=sessions if isinstance(sessions, list) else [],
            preferences=preferences,
            instructions=instructions,
            own_prefix=self.api.own_key_prefix(),
        )

    async def _after_account_write(self) -> AccountState | None:
        return await self._account_state()

    async def apply_key_revoke(self, key_id: str) -> AccountState | None:
        try:
            await self.api.revoke_key(key_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.notify("Key revoked.")
        return await self._after_account_write()

    async def apply_key_rotate(self, key_id: str) -> tuple[AccountState, str] | None:
        try:
            rotated = await self.api.rotate_key(key_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        state = await self._after_account_write()
        secret = str(rotated.get("key") or "")
        if state is None or not secret:
            return None
        return state, secret

    async def apply_session_revoke(self, session_id: str) -> AccountState | None:
        try:
            await self.api.revoke_session(session_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.notify("That browser is signed out.")
        return await self._after_account_write()

    async def apply_preferences(self, preferences: dict[str, Any]) -> AccountState | None:
        """The whole preferences shape, as every client of the account
        reads it; the server refuses what it would not keep (a malformed
        locale, receipts turned off), and that refusal is what shows."""
        try:
            await self.api.set_preferences(preferences)
        except APIError as exc:
            self.notify(exc.message or "The preferences were not saved.", severity="warning")
            return None
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.notify("Preferences saved.")
        return await self._after_account_write()

    async def apply_user_instructions(self, about: str, style: str) -> AccountState | None:
        """The person's own instructions; a plan without them is the
        server's refusal, said in passing."""
        try:
            await self.api.set_user_instructions(about, style)
        except APIError as exc:
            self.notify(exc.message or "The instructions were not saved.", severity="warning")
            return None
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.notify("Instructions saved.")
        return await self._after_account_write()

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
        name = f"matterwave-export-{created[:10] or 'account'}.json"

        def write() -> Path:
            path = free_path(Path.cwd() / name)
            with path.open("xb") as out:
                out.write(data)
            return path

        try:
            path = await asyncio.to_thread(write)
        except OSError as exc:
            self.notify(f"Could not write {name}: {exc}", severity="error")
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
            self.notify(
                f"No microphone support installed (pip install matterwave-tui[dictation]); "
                f"naming an audio file instead ({', '.join(sorted(MEDIA_TYPES))})."
            )

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
            str(tag.get("id")): str(tag.get("name") or "")
            for tag in self.library_tags
            if tag.get("id")
        }
        bits: list[str] = []
        folder = folder_names.get(conversation.folder_id or "")
        if folder:
            bits.append(f"{markers.lead('folder')}{folder}")
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
                rename_folder=self.apply_folder_rename,
                delete_folder=self.apply_folder_delete,
                delete_tag=self.apply_tag_delete,
            ),
            self._show_only,
        )

    def _show_only(self, choice: tuple[str, str] | None) -> None:
        """The library's Enter: the sidebar narrowed to one folder or tag;
        the one already shown, chosen again, shows the whole list."""
        if choice is None:
            return
        self.library_filter = None if choice == self.library_filter else choice
        self.run_worker(self._refilter(), group="load", exclusive=True)

    async def _refilter(self) -> None:
        await self.refresh_conversations()
        self.render_status()
        if self.library_filter is None:
            self.notify(f"Showing {self.list_filter} conversations.")
        else:
            self.notify(f"Showing only {self._library_filter_name()}.")

    def _library_filter_name(self) -> str:
        if self.library_filter is None:
            return ""
        kind, row_id = self.library_filter
        rows = self.library_folders if kind == "folder" else self.library_tags
        name = next((str(r.get("name") or "") for r in rows if r.get("id") == row_id), row_id)
        return f"{markers.lead('folder')}{name}" if kind == "folder" else f"#{name}"

    async def apply_folder_rename(self, folder_id: str, name: str) -> LibraryState | None:
        """A folder's new name; one another folder has is the server's 409,
        said in passing."""
        try:
            folder = await self.api.rename_folder(folder_id, name)
        except ConflictError as exc:
            self.notify(exc.message or "That folder name is taken.")
            return None
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.library_folders = [
            folder if f.get("id") == folder_id else f for f in self.library_folders
        ]
        self._refresh_library_captions()
        self.render_status()
        return self._library_state(self._open_folder_id())

    async def apply_folder_delete(self, folder_id: str) -> LibraryState | None:
        """The folder goes; the server returns its conversations to the
        main list, and so do the sidebar and the open conversation."""
        try:
            await self.api.delete_folder(folder_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.library_folders = [f for f in self.library_folders if f.get("id") != folder_id]
        for conversation in [c for c in self.conversations if c.folder_id == folder_id]:
            self._sync_conversation(replace(conversation, folder_id=None))
        if self.conversation is not None and self.conversation.folder_id == folder_id:
            self.conversation = replace(self.conversation, folder_id=None)
        await self._forget_filter(("folder", folder_id))
        self.notify("Folder deleted; its conversations are back in the main list.")
        return self._library_state(self._open_folder_id())

    async def apply_tag_delete(self, tag_id: str) -> LibraryState | None:
        """The tag goes, and off every conversation that carried it."""
        try:
            await self.api.delete_tag(tag_id)
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.library_tags = [t for t in self.library_tags if t.get("id") != tag_id]
        self.library_assignments = {
            cid: [t for t in tag_ids if t != tag_id]
            for cid, tag_ids in self.library_assignments.items()
        }
        await self._forget_filter(("tag", tag_id))
        self.notify("Tag deleted.")
        return self._library_state(self._open_folder_id())

    async def _forget_filter(self, deleted: tuple[str, str]) -> None:
        """A deleted folder or tag cannot narrow the sidebar any more."""
        if self.library_filter == deleted:
            self.library_filter = None
            await self.refresh_conversations()
        self._refresh_library_captions()
        self.render_status()

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
        when the name is taken, so a write is never refused for that, and
        the tag the library already lists is replaced rather than added."""
        try:
            tag = await self.api.create_tag(name)
        except Exception as exc:
            self.fail_from_exception(exc)
            return None
        self.library_tags = [
            *(t for t in self.library_tags if t.get("id") != tag.get("id")),
            tag,
        ]
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

    # --- the prompt library (named roles) -------------------------------------------

    def _composer_command(self, text: str) -> bool:
        """A leading slash is a command, not a message. True when the text
        was a command and is done with (handled, or refused with a reason);
        False for an unknown one, whose text stays in the composer."""
        body = text[1:]
        first = body.split(" ", 1)[0]
        if first not in (
            "role",
            "shell",
            "temp",
            "pi",
            "pi-engine",
            "agent-engine",
            "pi-compact",
            "pi-status",
            "pi-new",
        ):
            self.notify(
                f"Unknown command /{first}: /role, /shell, /temp, /pi, /pi-engine, /pi-compact and "
                "/pi-status are the commands. Start with // to send a message that begins with /.",
                severity="warning",
            )
            return False
        if first in ("pi-engine", "agent-engine"):
            self.set_agent_engine(body[len(first) :].strip())
            return True
        if first == "pi-status":
            self.run_pi_status()
            return True
        if first == "pi-compact":
            self.run_pi_compact(body[len("pi-compact") :].strip())
            return True
        if first == "pi-new":
            self.run_pi_new()
            return True
        if first == "pi":
            self.run_pi_prompt(body[len("pi") :].strip())
            return True
        if first == "temp":
            self.set_temperature(body[len("temp") :].strip())
            return True
        if first == "shell":
            if body.strip() != "shell":
                self.notify("/shell takes no arguments; it only toggles.", severity="warning")
            else:
                self.toggle_shell()
            return True
        rest = body[len("role") :].strip()
        if rest in ("", "--list"):
            self.action_roles()
            return True
        if rest.startswith("--add"):
            name = rest[len("--add") :].strip()
            if not name:
                self.notify("Name the role: /role --add name.", severity="warning")
                return True

            def written(role_text: str | None) -> None:
                if role_text is None:
                    return
                self.run_worker(self.save_role(name, role_text), group="roles", exclusive=True)
                self.notify(f"Role {name} saved.")

            self.push_screen(RoleEditor(name, self.roles.get(name, "")), written)
            return True
        self.apply_role(rest)
        return True

    # --- Pi 1.0 RPC interactive turns & engine selection -------------------------

    def set_agent_engine(self, value: str) -> None:
        """Inspect or switch the active coding-agent backend (`auto`, `pi`, `builtin`)."""
        choice = value.strip().lower()
        if not choice:
            resolved = (
                ("pi" if find_pi_binary(self._environ) else "builtin")
                if self.agent_engine == "auto"
                else self.agent_engine
            )
            self.notify(
                f"Agent engine: {self.agent_engine} (active: {resolved}). "
                f"Use /pi-engine [{'|'.join(AGENT_ENGINES)}] to switch."
            )
            return
        if choice not in AGENT_ENGINES:
            self.notify(
                f"Unknown agent engine {value.strip()!r}; choose {', '.join(AGENT_ENGINES)}.",
                severity="warning",
            )
            return
        self.agent_engine = choice
        if choice == "pi" and self._pi_bridge is None and find_pi_binary(self._environ) is None:
            self.notify(
                "Agent engine set to pi (pi binary not found on PATH; set MATTERWAVE_PI_BIN or install pi).",
                severity="warning",
            )
        else:
            self.notify(f"Agent engine set to {choice}.")
        self.render_status()

    def _append_pi_notice(self, text: str) -> None:
        note = ConversationMessage(
            id=f"_pi-notice-{time.monotonic_ns()}",
            conversation_id=self.conversation.id if self.conversation else "",
            role="system",
            parent_id=self.thread.leaf,
            parts=[{"type": "text", "text": text}],
            content=text,
            created_at="",
        )
        self.thread.add(note)
        self.thread.leaf = note.id
        self.run_worker(self.render_thread(), group="pi-notice")

    def _create_pi_bridge(self, pi_bin: str | None) -> PiRpcBridge:
        model = self.effective_model()
        cred = getattr(self.api, "_credential", None)
        api_key = str(getattr(cred, "token", "") or self._environ.get("MATTERWAVE_API_KEY") or "")
        base_url = str(
            getattr(self.api, "base_url", "")
            or self._environ.get("MATTERWAVE_BASE_URL")
            or DEFAULT_BASE_URL
        )
        env = build_pi_launch_env(
            api_key,
            base_url,
            preset_id=self.active_preset_id(),
            model=model,
            temporary=self.temporary_chat,
            base_env=self._environ,
        )
        argv = build_pi_argv(pi_bin or "pi", mode="rpc", model=model)
        return PiRpcBridge(argv, env=env)

    def run_pi_status(self) -> None:
        """Display `/pi-status`: binary availability, active engine, model, preset, and session."""
        pi_bin = find_pi_binary(self._environ)
        resolved = (
            ("pi" if pi_bin else "builtin") if self.agent_engine == "auto" else self.agent_engine
        )
        model = self.effective_model() or "default"
        preset = self.active_preset()
        preset_label = (
            str(preset.get("name") or preset.get("id") or "none")
            if preset is not None
            else (self.active_preset_id() or "none")
        )
        binary_label = pi_bin if pi_bin else "not found (set MATTERWAVE_PI_BIN or install pi)"
        session_label = "temporary" if self.temporary_chat else "persistent"
        summary = (
            f"Pi 1.0 status: engine={self.agent_engine} (active: {resolved}) | "
            f"binary={binary_label} | model={model} | preset={preset_label} | "
            f"session={session_label}"
        )
        self.notify(summary)
        self._append_pi_notice(summary)

    def run_pi_compact(self, instructions: str = "") -> None:
        """Execute `/pi-compact [instructions]` through `PiRpcBridge.compact(...)`."""
        if self.streaming:
            self.notify(
                "Wait for the current turn to finish, or press Esc to stop it.",
                severity="warning",
            )
            return
        pi_bin: str | None = None
        if self._pi_bridge is None:
            pi_bin = find_pi_binary(self._environ)
            if pi_bin is None:
                msg = "pi binary not found on PATH (set MATTERWAVE_PI_BIN or install pi)."
                self.notify(msg, severity="warning")
                self._append_pi_notice(msg)
                return
        self.run_worker(
            self._compact_pi(instructions.strip() or None, pi_bin=pi_bin),
            group="pi-compact",
            exclusive=True,
        )

    async def _compact_pi(
        self, custom_instructions: str | None = None, *, pi_bin: str | None = None
    ) -> None:
        bridge = self._pi_bridge
        owns_bridge = False
        if bridge is None:
            bridge = self._create_pi_bridge(pi_bin)
            owns_bridge = True
        try:
            if owns_bridge:
                await bridge.start()
            event = await bridge.compact(custom_instructions)
        except Exception as exc:
            msg = f"Pi compaction failed: {exc}"
            self.notify(msg, severity="error")
            self._append_pi_notice(msg)
            return
        finally:
            if owns_bridge:
                await bridge.close()

        if event is not None and event.is_error:
            err = event.error or "Pi RPC compaction failed"
            msg = f"Pi compaction failed: {err}"
            self.notify(msg, severity="error")
            self._append_pi_notice(msg)
            return

        summary = ""
        if event is not None:
            raw_data = event.raw.get("data")
            summary = str(
                event.raw.get("summary")
                or (raw_data.get("summary") if isinstance(raw_data, dict) else "")
                or event.text
                or ""
            ).strip()
        if summary:
            msg = f"Pi session compacted: {summary}"
        elif custom_instructions:
            msg = f"Pi session compacted ({custom_instructions})."
        else:
            msg = "Pi session compacted."
        self.notify(msg)
        note = ConversationMessage(
            id=f"_pi-compact-{time.monotonic_ns()}",
            conversation_id=self.conversation.id if self.conversation else "",
            role="system",
            parent_id=self.thread.leaf,
            parts=[{"type": "text", "text": msg}],
            content=msg,
            created_at="",
        )
        self.thread.add(note)
        self.thread.leaf = note.id
        await self.render_thread()

    def run_pi_new(self) -> None:
        """Execute `/pi-new` through `PiRpcBridge.new_session()`."""
        if self.streaming:
            self.notify(
                "Wait for the current turn to finish, or press Esc to stop it.",
                severity="warning",
            )
            return
        pi_bin: str | None = None
        if self._pi_bridge is None:
            pi_bin = find_pi_binary(self._environ)
            if pi_bin is None:
                msg = "pi binary not found on PATH (set MATTERWAVE_PI_BIN or install pi)."
                self.notify(msg, severity="warning")
                self._append_pi_notice(msg)
                return
        self.run_worker(
            self._new_pi_session(pi_bin=pi_bin),
            group="pi-new",
            exclusive=True,
        )

    async def _new_pi_session(self, *, pi_bin: str | None = None) -> None:
        bridge = self._pi_bridge
        owns_bridge = False
        if bridge is None:
            bridge = self._create_pi_bridge(pi_bin)
            owns_bridge = True
        try:
            if owns_bridge:
                await bridge.start()
            event = await bridge.new_session()
        except Exception as exc:
            msg = f"Pi session reset failed: {exc}"
            self.notify(msg, severity="error")
            self._append_pi_notice(msg)
            return
        finally:
            if owns_bridge:
                await bridge.close()

        if event is not None and event.is_error:
            err = event.error or "Pi RPC session reset failed"
            msg = f"Pi session reset failed: {err}"
            self.notify(msg, severity="error")
            self._append_pi_notice(msg)
            return

        msg = "Pi session reset."
        self.notify(msg)
        note = ConversationMessage(
            id=f"_pi-new-{time.monotonic_ns()}",
            conversation_id=self.conversation.id if self.conversation else "",
            role="system",
            parent_id=self.thread.leaf,
            parts=[{"type": "text", "text": msg}],
            content=msg,
            created_at="",
        )
        self.thread.add(note)
        self.thread.leaf = note.id
        await self.render_thread()

    def run_pi_prompt(self, prompt: str) -> None:
        """Execute `/pi <prompt>` through `PiRpcBridge`, streaming into the transcript."""
        if not prompt:
            self.notify("Usage: /pi <prompt> — run a prompt through Pi 1.0.", severity="warning")
            return
        if self.streaming:
            self.notify(
                "Wait for the current turn to finish, or press Esc to stop it.",
                severity="warning",
            )
            return
        pi_bin: str | None = None
        if self._pi_bridge is None:
            pi_bin = find_pi_binary(self._environ)
            if pi_bin is None:
                msg = "pi binary not found on PATH (set MATTERWAVE_PI_BIN or install pi)."
                self.notify(msg, severity="warning")
                self._append_pi_notice(msg)
                return
        self.run_worker(
            self._consume_pi(prompt, pi_bin=pi_bin),
            group="stream",
            exclusive=True,
        )

    async def _consume_pi(self, prompt: str, *, pi_bin: str | None = None) -> None:
        conv_id = self.conversation.id if self.conversation else ""
        user_msg = ConversationMessage(
            id=f"_pi-user-{time.monotonic_ns()}",
            conversation_id=conv_id,
            role="user",
            parent_id=self.thread.leaf,
            parts=[{"type": "text", "text": prompt}],
            content=prompt,
            created_at="",
        )
        self.thread.add(user_msg)
        self.thread.leaf = user_msg.id

        turn = object()
        self._turn = turn
        self.streaming = True
        self.show_failure(None)
        model = self.effective_model()
        self.live = ConversationMessage(
            id=LIVE_ID,
            conversation_id=conv_id,
            role="assistant",
            parent_id=user_msg.id,
            parts=[],
            content="",
            created_at="",
            model=model,
        )
        await self.render_thread()
        self.render_status()

        bridge = self._pi_bridge
        owns_bridge = False
        if bridge is None:
            cred = getattr(self.api, "_credential", None)
            api_key = str(
                getattr(cred, "token", "") or self._environ.get("MATTERWAVE_API_KEY") or ""
            )
            base_url = str(
                getattr(self.api, "base_url", "")
                or self._environ.get("MATTERWAVE_BASE_URL")
                or DEFAULT_BASE_URL
            )
            env = build_pi_launch_env(
                api_key,
                base_url,
                preset_id=self.active_preset_id(),
                model=model,
                temporary=self.temporary_chat,
                base_env=self._environ,
            )
            argv = build_pi_argv(pi_bin or "pi", mode="rpc", model=model)
            bridge = PiRpcBridge(argv, env=env)
            owns_bridge = True

        stopped = False
        try:
            if owns_bridge:
                await bridge.start()
            async for event in bridge.prompt(prompt):
                if self._turn is not turn:
                    break
                await self._apply_pi_event(event)
                if event.type in ("turn_end", "error"):
                    break
        except asyncio.CancelledError:
            stopped = True
            with contextlib.suppress(Exception):
                await bridge.abort()
            raise
        except Exception as exc:
            if self._turn is turn:
                self.notify(f"Pi RPC failed: {exc}", severity="error")
        finally:
            if owns_bridge:
                await bridge.close()
            if self._turn is turn:
                partial = self.live
                self._turn = None
                self.streaming = False
                self.live = None
                if partial is not None and (partial.parts or partial.content):
                    reply = replace(
                        partial,
                        id=f"_pi-assistant-{time.monotonic_ns()}",
                        truncated=stopped,
                    )
                    self.thread.add(reply)
                    self.thread.leaf = reply.id
                await self.render_thread()
                self.render_status()

    async def _apply_pi_event(self, event: PiRpcEvent) -> None:
        live = self.live
        if live is None:
            return
        if event.type == "text_delta":
            if event.text:
                if live.parts and live.parts[-1].get("type") == "text":
                    live.parts[-1]["text"] = str(live.parts[-1].get("text") or "") + event.text
                else:
                    live.parts.append({"type": "text", "text": event.text})
                self.live = replace(live, content=live.content + event.text)
                self._live_dirty = True
        elif event.type == "tool_start":
            args_json = json.dumps(event.tool_args) if event.tool_args is not None else ""
            call_part: dict[str, Any] = {
                "type": "tool_call",
                "name": event.tool_name or "tool",
                "arguments": args_json,
            }
            if event.tool_call_id is not None:
                call_part["id"] = event.tool_call_id
            live.parts.append(call_part)
            self._live_dirty = True
            await self._redraw_live()
        elif event.type == "tool_end":
            if isinstance(event.tool_result, (dict, list)):
                output = json.dumps(event.tool_result)
            elif event.tool_result is not None:
                output = str(event.tool_result)
            else:
                output = "done"
            result_part: dict[str, Any] = {
                "type": "tool_result",
                "name": event.tool_name or "tool",
                "output": output,
                "is_error": event.is_error,
            }
            if event.tool_call_id is not None:
                result_part["id"] = event.tool_call_id
            live.parts.append(result_part)
            self._live_dirty = True
            await self._redraw_live()
        elif event.type == "error":
            err_msg = event.error or "Pi RPC error"
            live.parts.append(
                {
                    "type": "tool_result",
                    "name": event.tool_name or "pi",
                    "output": err_msg,
                    "is_error": True,
                }
            )
            if not live.content:
                self.live = replace(live, content=err_msg)
            self.notify(f"Pi error: {err_msg}", severity="error")
            self._live_dirty = True

    def action_roles(self) -> None:
        """The prompt library screen; the role picked there applies to the
        conversation that is open."""

        def chosen(name: str | None) -> None:
            if name:
                self.apply_role(name)

        self.push_screen(
            Roles(dict(self.roles), add=self.save_role, delete=self.delete_role), chosen
        )

    async def save_role(self, name: str, text: str) -> dict[str, str]:
        """One role added or replaced, kept on this device."""
        self.roles = {**self.roles, name: text}
        if not save_roles(roles_path(), self.roles):
            self.notify("The role could not be written to disk.", severity="warning")
        return dict(self.roles)

    async def delete_role(self, name: str) -> dict[str, str]:
        self.roles = {k: v for k, v in self.roles.items() if k != name}
        if not save_roles(roles_path(), self.roles):
            self.notify("The change could not be written to disk.", severity="warning")
        return dict(self.roles)

    @work(group="action")
    async def apply_role(self, name: str) -> None:
        """A named prompt as this conversation's instructions: the same
        PUT the instructions editor uses, so the web app reads it too."""
        text = self.roles.get(name)
        if text is None:
            self.notify(f"No role named {name}.", severity="warning")
            return
        if self.conversation is None:
            self.notify("Open a conversation to apply a role to.")
            return
        try:
            await self.api.set_conversation_instructions(self.conversation.id, text)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        self.notify(f"Role {name} applied.")

    # --- shell assistant -------------------------------------------------------------

    def set_temperature(self, value: str) -> None:
        """`/temp 0.3` sets the open conversation's temperature (0 to 2, the
        platform's bounds) for its turns from now on; `/temp default` goes
        back to the server's; `/temp` alone says which is in force."""
        key = self._draft_key()
        if not value:
            current = self.temperatures.get(key)
            self.notify(
                f"Temperature {current:g} for this conversation."
                if current is not None
                else "The server's default temperature; /temp 0.3 sets one."
            )
            return
        if value == "default":
            self.temperatures.pop(key, None)
            self.notify("Back to the server's default temperature.")
            self.render_status()
            return
        try:
            temperature = float(value)
        except ValueError:
            temperature = -1.0
        if not 0.0 <= temperature <= 2.0:
            self.notify("A temperature is a number from 0 to 2: /temp 0.3.", severity="warning")
            return
        self.temperatures[key] = temperature
        self.notify(f"Temperature {temperature:g} for this conversation's turns.")
        self.render_status()

    def toggle_shell(self) -> None:
        self.shell_mode = not self.shell_mode
        if self.shell_mode:
            self.notify(
                f"Shell assistant on, for {self.shell.name} on {self.shell.os_name}. An answer "
                "becomes a proposed command; nothing runs until you confirm it."
            )
        else:
            self.notify("Shell assistant off.")
        self.render_status()

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
        frames reach the conversation opened next. A queued prompt goes
        back in the composer, where open_conversation files it as the old
        conversation's draft."""
        queued, self.queued_prompt = self.queued_prompt, None
        if queued:
            composer = self.query_one("#composer", TextArea)
            if not composer.text.strip():
                composer.text = queued
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
            model=self.active_preset_model() or self.model,
        )

    async def _consume(self, stream: AsyncIterator[StreamItem]) -> None:
        turn = object()
        self._turn = turn
        self.streaming = True
        self._completed_answer = None
        self._turn_tokens = None
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
        queued, self.queued_prompt = self.queued_prompt, None
        # A turn, finished or stopped, spent tokens and may have crossed a
        # budget line, which the server says with a notification: the
        # status line's usage, overage and unread badge are read again.
        self.refresh_account_lines()
        if stopped:
            await self._resync()
            # Esc cancels the stream and the queue with it; the queued
            # text is not lost — it is back in the composer.
            composer = self.query_one("#composer", TextArea)
            if queued and not composer.text.strip():
                composer.text = queued
            self.notify("Stopped.")
        elif not terminal and partial and partial.parts and self.failure is None:
            self.show_failure(Failure("truncated", request_id))
        await self.render_thread()
        self.render_status()
        # The answer is whole and shell mode is on: it is a proposed
        # command, shown and gated before anything runs.
        answer, self._completed_answer = self._completed_answer, None
        if self.shell_mode and not stopped and terminal and answer is not None:
            command = shell_command(str(answer.content or ""))
            if command:
                self._propose_shell(command)
        # The queued prompt sends when the turn's worker is done, never
        # from inside it: a stream of its own replaces this one.
        if queued and not stopped and self.failure is None:
            self.call_after_refresh(self._send_queued, queued)

    def _send_queued(self, text: str) -> None:
        composer = self.query_one("#composer", TextArea)
        composer.text = text
        self.action_send()

    def _propose_shell(self, command: str) -> None:
        """The gate: the command on show, run only on Enter. Nothing runs
        without that confirmation, whoever asked for the answer."""

        def approved(run: bool | None) -> None:
            if run:
                self.run_worker(self._execute_shell(command), group="shell", exclusive=True)

        self.push_screen(
            Confirm(
                f"Run this command in {self.shell.name}?\n\n{command}",
                hint="Enter runs it; Esc leaves it alone.",
            ),
            approved,
        )

    async def _run_in_shell(self, command: str) -> str:
        """The command in this terminal's shell, its output as the
        transcript shows it."""
        output, status = await run_shell_command(command, self.shell)
        if status is None:
            return output
        if not output:
            return f"(no output; exit {status})"
        return output if status == 0 else f"{output}\n(exit {status})"

    async def _execute_shell(self, command: str) -> None:
        try:
            output = await self._run_shell(command)
        except Exception as exc:
            self.notify(f"The command could not run: {exc}", severity="warning")
            return
        note = ConversationMessage(
            id=f"_shell-{time.monotonic_ns()}",
            conversation_id=self.conversation.id if self.conversation else "",
            role="system",
            parts=[],
            content=f"$ {command}\n\n{output}",
            created_at="",
        )
        self.thread.add(note)
        self.thread.leaf = note.id
        await self.render_thread()

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
            if item.message.role == "assistant":
                self._completed_answer = item.message
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
                str(parts[item.index].get("text", ""))
                + (item.content or "")
                + (item.reasoning or "")
            )
            self._live_dirty = True
        elif isinstance(item, fr.TitleFrame) and self.conversation is not None:
            self._retitle(self.conversation.id, item.title)
        elif isinstance(item, fr.ContextFrame):
            self._turn_tokens = (item.prompt_tokens, item.completion_tokens)
            self.context_used = (item.prompt_tokens or 0) + (item.completion_tokens or 0)
            if item.context_window:
                self.context_window = item.context_window
            self.render_status()
        elif isinstance(item, fr.NoticeFrame):
            if item.detail:
                self.notify(item.detail)
        elif isinstance(item, fr.DoneFrame):
            self.live = None
            if self._turn_tokens is not None:
                self.answer_tokens[item.message.id] = self._turn_tokens
            self.thread.add(item.message)
            self.thread.leaf = item.message.id
            self._completed_answer = item.message
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


def account_summary(
    entitlements: dict[str, Any] | None,
    billing: dict[str, Any] | None,
    plans: list[dict[str, Any]] | None,
    enforcement: dict[str, Any] | None,
) -> list[str]:
    """The account screen's read-only lines: the plan, how it is paid,
    what else is on offer, and any moderation action in force."""
    lines: list[str] = []
    if entitlements:
        plan = str(entitlements.get("plan_name") or entitlements.get("plan") or "?")
        line = f"Plan: {plan} ({entitlements.get('status') or 'active'})"
        if entitlements.get("per_seat"):
            line += f", {entitlements.get('seats')} seats"
        if entitlements.get("writes_blocked"):
            line += f" — read-only: {entitlements.get('read_only_reason') or 'over the plan'}"
        lines.append(line)
    if billing:
        method = "invoices" if billing.get("collection_method") == "send_invoice" else "card"
        line = f"Billing: {method}"
        if not billing.get("billing_enabled", True):
            line = "Billing: not enabled on this deployment"
        if billing.get("dunning"):
            line += " — a payment is late; the web app's billing page says what happens next"
        lines.append(line)
    if plans:
        offered = ", ".join(
            f"{p.get('name') or p.get('code')} ${int(p.get('price_cents_monthly') or 0) / 100:,.0f}"
            + ("/seat" if p.get("per_seat") else "")
            for p in plans
        )
        lines.append(f"Plans (a month): {offered}; change it in the web app")
    if enforcement and enforcement.get("state") not in (None, "none"):
        reason = enforcement.get("reason") or enforcement.get("message") or ""
        lines.append(f"Moderation: {enforcement.get('state')}" + (f" — {reason}" if reason else ""))
    return lines or ["The plan and billing are not readable with this key."]


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
