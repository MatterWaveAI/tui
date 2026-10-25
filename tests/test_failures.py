from __future__ import annotations

import pytest

from matterwave_tui.failures import Failure, classify


@pytest.mark.parametrize(
    ("detail", "status", "code", "kind"),
    [
        ("anything", None, "moderation", "moderation"),
        ("anything", None, "truncated", "truncated"),
        ("Your organisation's monthly token budget is used up", None, "budget", "budget"),
        ("quota", 429, None, "budget"),
        ("Your organisation's monthly token budget is used up", 402, None, "budget"),
        # keys.KEY_RATE_LIMIT_DETAIL: the window, which clears by itself.
        ("Rate limit of 60 requests per minute exceeded for this API key.", 429, None, "rate_limited"),
        # entitlement_gate.UpgradeRequired: the plan, which does not.
        ("The Free plan does not include the model 'grt-max'.", 402, None, "plan"),
        ("Lost the connection to the model gateway: read timed out", None, None, "timeout"),
        ("The upstream returned 500", None, None, "gateway"),
        ("", 502, None, "gateway"),
        ("Session expired", None, None, "session"),
        ("Not authenticated", 401, None, "session"),
        # A key without the scope is not a sign-out (keys.missing_scope_detail).
        ("This API key lacks the 'chat:write' scope", 403, None, "permission"),
        ("context length exceeded", None, None, "context"),
        ("weird", None, None, "unknown"),
    ],
)
def test_classification_mirrors_the_web(detail, status, code, kind):
    assert classify(detail, status, code) == kind


def test_the_request_id_is_always_shown():
    assert "Reference: req-1" in Failure("gateway", "req-1").render()
    assert "Reference: none" in Failure("network", None).render()
    # The server's own words never reach the banner.
    assert "budget is used up" not in Failure("budget", "r").render()


def test_a_rate_limit_says_how_long_to_wait():
    text = Failure("rate_limited", "req-429", retry_after=30).render()
    assert "Too many requests" in text and "Retry in 30s." in text
    assert "Retry in" not in Failure("rate_limited", "req-429").render()
    assert "Retry in 3min." in Failure("rate_limited", None, retry_after=150).render()


def test_a_plan_refusal_is_not_a_budget_refusal():
    plan, budget = Failure("plan", "r").render(), Failure("budget", "r").render()
    assert "not on your plan" in plan and "out of tokens" not in plan
    assert "out of tokens" in budget


def test_a_scope_refusal_does_not_say_the_session_expired():
    text = Failure(classify("This API key lacks the 'chat:write' scope", 403), "req-403").render()
    assert "session expired" not in text.lower()
    assert "chat:write" in text and "Reference: req-403" in text
