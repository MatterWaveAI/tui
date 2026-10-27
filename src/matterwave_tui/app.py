"""The Textual app: conversation list, transcript, composer, status line."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from matterwave_sdk import frames as fr
from matterwave_sdk.errors import APIConnectionError, APIError, ConflictError
from matterwave_sdk.models import Attachment, Conversation, ConversationMessage, UsageBalance
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static, TextArea

from .api import ChatAPI, Cut, Opened, StreamItem
from .export import conversation_markdown, export_filename
from .failures import Failure, classify, retry_in
from .models import model_window
from .screens import Confirm, DeviceLogin, DeviceLoginFn, ModelPicker, Prompt
from .thread import Divider, Message, Thread
from .widgets import DividerView, MessageView

# How often a stream redraws the answer in progress. A redraw per delta is
# hundreds a second on a fast model and buys nothing a person can see.
LIVE_REDRAW_S = 1 / 15
OFFLINE_PROBE_S = 5.0
SEARCH_DEBOUNCE_S = 0.3
# The id of the answer being streamed, before the server has named it.
LIVE_ID = "_live"
# The sidebar list's slices, in the order the `List` key cycles them. The
# archived ones are only reachable here, which is what makes unarchiving
# possible at all.
LIST_FILTERS = ("default", "pinned", "archived", "all")


def estimate_tokens(text: str) -> int:
    """The server's own rough count (characters / 4), so the meter and the
    server's compaction decision agree to within the estimate."""
    return (len(text) + 3) // 4


def _read_attachment(path_text: str) -> tuple[bytes, str]:
    """The file's bytes and its name, off the event loop (it may be large)."""
    path = Path(path_text).expanduser()
    return path.read_bytes(), path.name


class ConversationItem(ListItem):
    def __init__(self, conversation: Conversation) -> None:
        self.conversation = conversation
        super().__init__(Label(self.caption(), markup=False))

    def caption(self) -> str:
        c = self.conversation
        marks = ("📌 " if c.pinned_at else "") + ("🗄 " if c.archived_at else "")
        return marks + (c.title or "New chat")

    def retitle(self, title: str) -> None:
        self.conversation = replace(self.conversation, title=title)
        self.query_one(Label).update(self.caption())

    def replace_conversation(self, conversation: Conversation) -> None:
        """The conversation changed on the server (pin, archive): redraw the row."""
        self.conversation = conversation
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


class MatterWaveApp(App[None]):
    TITLE = "MatterWave"
    CSS = """
    #sidebar { width: 32; border-right: solid $panel; }
    #search { margin: 0 0 1 0; }
    #main { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; }
    #failure { display: none; background: $error 20%; color: $text; padding: 0 1; }
    #failure.shown { display: block; }
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
        Binding("f2", "rename", "Rename"),
        Binding("p", "toggle_pin", "Pin"),
        Binding("a", "toggle_archive", "Archive"),
        Binding("d", "delete_conversation", "Delete"),
        Binding("f", "fork", "Fork"),
        Binding("s", "share", "Share"),
        Binding("w", "export_markdown", "Export"),
        Binding("i", "attach", "Attach"),
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
        # Which slice of the conversations the sidebar shows.
        self.list_filter = "default"
        # When the server's `Retry-After` runs out, on the monotonic clock.
        self.rate_limited_until: float | None = None
        self._search_seq = 0

    # --- layout ----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Input(placeholder="Search (/)", id="search")
                yield ListView(id="conversations")
            with Vertical(id="main"):
                yield Transcript(id="transcript")
                yield Static("", id="failure", markup=False)
                yield TextArea(id="composer", placeholder="Message… (Ctrl+Enter sends)")
                yield Static("", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(LIVE_REDRAW_S, self._flush_live)
        self.set_interval(1.0, self._tick_rate_limit)
        self.render_status()
        self.query_one("#composer", TextArea).focus()
        self.load_initial()

    async def on_unmount(self) -> None:
        await self.api.aclose()

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
        await view.extend([ConversationItem(c) for c in found])

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
        self.show_failure(None)
        composer.text = self.drafts.get(conversation_id, "")
        self.context_used, self.context_window = None, None
        await self.render_thread()
        self.load_context(conversation_id)
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
                widgets.append(MessageView(item, position=self.thread.position(item)))
        if self.live is not None:
            widgets.append(MessageView(self.live, streaming=True))
        await transcript.mount_all(widgets)
        transcript.select(selected)
        transcript.scroll_end(animate=False)

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

    @work(group="action")
    async def action_new(self) -> None:
        try:
            conv = await self.api.create_conversation(model=self.model)
        except Exception as exc:
            self.fail_from_exception(exc)
            return
        await self.refresh_conversations()
        await self.open_conversation(conv.id)
        self.query_one("#composer", TextArea).focus()

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
        await view.extend([ConversationItem(c) for c in self.conversations])
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
