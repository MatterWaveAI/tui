"""Modal screens: the model picker and the in-app device sign-in."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .auth import DeviceCode, LoginError
from .models import picker_rows


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
