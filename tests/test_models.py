from __future__ import annotations

import pytest
from textual.widgets import OptionList

from matterwave_tui.app import MatterWaveApp
from matterwave_tui.models import (
    GRT_HEADING,
    PARTNER_HEADING,
    PREVIEW_HEADING,
    format_context_window,
    picker_rows,
)
from matterwave_tui.screens import ModelPicker

from .fake_api import MODELS, FakeAPI


def test_rows_are_grouped_like_the_web_picker():
    rows = picker_rows(MODELS)
    assert [r.label for r in rows if r.is_heading] == [GRT_HEADING, PARTNER_HEADING, PREVIEW_HEADING]
    assert [r.model_id for r in rows] == [
        None, "grt-nano", "grt-max", None, "other-7b", None, "lab-x"
    ]


def test_a_model_off_the_plan_shows_its_upgrade_and_is_not_selectable():
    rows = {r.model_id: r for r in picker_rows(MODELS) if not r.is_heading}
    assert rows["grt-max"].selectable is False
    assert "on Pro" in rows["grt-max"].label
    assert rows["grt-nano"].selectable is True
    assert "(8k context)" in rows["grt-nano"].label


def test_an_api_without_plan_gating_includes_everything():
    (row,) = [r for r in picker_rows([{"id": "m"}]) if not r.is_heading]
    assert row.selectable


@pytest.mark.parametrize(
    ("n", "s"), [(32768, "33k"), (2_000_000, "2M"), (512, "512"), (None, None)]
)
def test_context_window_format(n, s):
    assert format_context_window(n) == s


@pytest.mark.asyncio
async def test_the_picker_disables_the_gated_row_and_returns_a_chosen_one():
    app = MatterWaveApp(FakeAPI())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")  # leave the composer so `m` is a binding
        await pilot.press("m")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPicker)
        options = screen.query_one(OptionList)
        by_id = {
            options.get_option_at_index(i).id: options.get_option_at_index(i)
            for i in range(options.option_count)
        }
        gated = next(k for k, r in enumerate(screen.rows) if r.model_id == "grt-max")
        assert by_id[f"model-{gated}"].disabled
        assert all(
            by_id[f"heading-{k}"].disabled for k, r in enumerate(screen.rows) if r.is_heading
        )
        # Enter on the gated row does nothing; the picker stays open.
        options.highlighted = gated
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker)
        preview = next(k for k, r in enumerate(screen.rows) if r.model_id == "lab-x")
        options.highlighted = preview
        await pilot.press("enter")
        await pilot.pause()
        assert app.model == "lab-x"
