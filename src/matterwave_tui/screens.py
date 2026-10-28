"""Modal screens: the model picker, the in-app device sign-in, the one-line
prompt and yes/no the conversation actions share, the per-thread custom
instructions editor, and the reply-feedback dialog."""

from __future__ import annotations

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

from .auth import DeviceCode, LoginError
from .models import picker_rows

# The per-thread instructions budget: shown as a counter, never enforced
# here -- the server is authoritative and refuses more with a 422.
INSTRUCTIONS_MAX = 2000
FEEDBACK_COMMENT_MAX = 2000
REASONS_HINT = "Reasons, comma-separated from: " + ", ".join(FEEDBACK_REASONS)


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


class FeedbackDialog(ModalScreen[tuple[int, list[str], str | None]]):
    """Optional reasons and comment for a rating that was already set (or
    is being set now). Reasons come from the platform's fixed vocabulary;
    the comment's 2000-character budget is a counter, the server being
    authoritative. Ctrl+T hands back (rating, reasons, comment), Esc None."""

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

    def __init__(self, rating: int, reasons: list[str], comment: str) -> None:
        super().__init__()
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
        self.dismiss((self.rating, reasons, comment or None))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
