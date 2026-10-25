"""The failure modes a chat stream ends in, and the copy each shows.

The same states and the same words as the web app's `streamErrors.ts`, so a
person sees one product whichever client they use, and a support answer
written for one applies to the other. The detail string is matched against,
never rendered: server and model text do not reach the failure banner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Kind = Literal[
    "gateway",
    "timeout",
    "truncated",
    "budget",
    "rate_limited",
    "plan",
    "context",
    "session",
    "permission",
    "network",
    "moderation",
    "unknown",
]

TRUNCATED_CODE = "truncated"


@dataclass(frozen=True)
class Copy:
    title: str
    guidance: str
    retryable: bool


COPY: dict[Kind, Copy] = {
    "gateway": Copy(
        "The model gateway did not answer",
        "The upstream that serves the models is unreachable or returned an error, so no answer "
        "was started. Your message is still here — try again, or pick a different model.",
        True,
    ),
    "timeout": Copy(
        "The model took too long to answer",
        "The request reached the gateway but no answer arrived in time — the model may still be "
        "loading or busy with other requests. Wait a moment and retry, or pick a different model.",
        True,
    ),
    "truncated": Copy(
        "The answer was cut off",
        "The stream ended before the model finished — a dropped connection or the gateway "
        "closing the stream early. Whatever arrived is kept above but is not the whole answer; "
        "retry to ask again from where the conversation now stands.",
        True,
    ),
    "budget": Copy(
        "This workspace is out of tokens",
        "This workspace has used its monthly token budget: the plan's included tokens and any "
        "overage its cap allows. Nothing is broken — see Usage for when it resets; an admin can "
        "raise the overage cap or upgrade the plan.",
        False,
    ),
    # Two refusals the web app never sees as a stream failure, because a
    # browser session has no per-key window and its picker hides a model the
    # plan lacks. A key has a window, and a key can ask for any model, so
    # the terminal has to say which of the two it ran into: one clears by
    # itself in seconds, the other not until someone changes the plan.
    "rate_limited": Copy(
        "Too many requests",
        "This API key has used its requests-per-minute allowance; nothing was sent. The status "
        "line counts down the wait the server asked for — retry after it.",
        True,
    ),
    "plan": Copy(
        "This model is not on your plan",
        "The plan this workspace is on does not include the model you picked, so no answer was "
        "started. Pick another model (m), or an admin can upgrade the plan.",
        False,
    ),
    "context": Copy(
        "This conversation is too long for the model",
        "The conversation plus your message exceeds the model's context window. Start a new "
        "chat, compact this one, or pick a model with a larger context window.",
        False,
    ),
    "session": Copy(
        "Your session expired mid-response",
        "You were signed out while the model was answering. Anything it had already sent is "
        "kept above. Sign in again, then retry.",
        True,
    ),
    # The web app never sees this one: a browser session holds every right
    # its roles give. A key can hold fewer, and a 403 naming a scope is not a
    # sign-out, so it must not tell the person their session expired.
    "permission": Copy(
        "This key cannot do that",
        "The API key in use lacks the scope this needs (chat:read to read conversations, "
        "chat:write to send). Sign in again by device code, or use a key that has it.",
        False,
    ),
    "network": Copy(
        "MatterWave could not be reached",
        "The request did not get through — check your connection. Your message is still here, "
        "and nothing was sent, so retrying sends it for the first time.",
        True,
    ),
    "moderation": Copy(
        "MatterWave would not send this message",
        "This message was blocked by MatterWave's content rules before it reached a model, so "
        "retrying it unchanged will be blocked again. Rephrase it, or quote the reference below "
        "to an administrator if you think this is wrong.",
        False,
    ),
    "unknown": Copy(
        "The response failed",
        "Something went wrong while the model was answering. Your message is still here — "
        "retry, and if it keeps happening tell an administrator what you were doing.",
        True,
    ),
}


def classify(detail: str, status: int | None = None, code: str | None = None) -> Kind:
    """Place a failure in one of the designed states; `unknown` when unsure.

    The machine code is checked first because it is the one signal a user
    cannot spoof by getting the model to echo "budget exceeded" back.
    """
    if code == "moderation":
        return "moderation"
    if code == TRUNCATED_CODE:
        return "truncated"
    if code in ("budget", "insufficient_quota"):
        return "budget"
    text = detail.lower()
    if status == 403 and "scope" in text:
        return "permission"
    if status in (401, 403) or "expired" in text or "unauthor" in text:
        return "session"
    if "budget" in text or "quota" in text:
        return "budget"
    # A 429 is the key's per-minute window once the budget words are ruled
    # out; a 402 is the plan gate (entitlement_gate.UpgradeRequired) once
    # the budget's 402 is.
    if status == 429:
        return "rate_limited"
    if status == 402:
        return "plan"
    if "context" in text and ("length" in text or "window" in text or "token" in text):
        return "context"
    # Before `gateway`: an upstream read timeout is reported as "Lost the
    # connection to the model gateway: ... timed out".
    if status == 504 or "timed out" in text or "timeout" in text or "deadline" in text:
        return "timeout"
    if "before the answer finished" in text or "ended before" in text:
        return "truncated"
    if status in (502, 503) or "gateway" in text or "upstream" in text:
        return "gateway"
    if "network" in text or "fetch" in text or "connection" in text:
        return "network"
    return "unknown"


@dataclass(frozen=True)
class Failure:
    kind: Kind
    request_id: str | None
    # The server's `Retry-After`, in seconds, when it sent one.
    retry_after: float | None = None

    @property
    def copy(self) -> Copy:
        return COPY[self.kind]

    def render(self) -> str:
        """Title, guidance, and the reference -- always, even when there is none."""
        reference = (
            f"Reference: {self.request_id}"
            if self.request_id
            else "Reference: none (the request never reached MatterWave)"
        )
        lines = [self.copy.title, self.copy.guidance]
        if self.retry_after is not None:
            lines.append(f"Retry in {retry_in(self.retry_after)}.")
        lines.append(reference)
        return "\n".join(lines)


def retry_in(seconds: float) -> str:
    """`Retry-After` for a person: rounded up, so the wait is never understated."""
    whole = max(1, math.ceil(seconds))
    return f"{whole}s" if whole < 90 else f"{(whole + 59) // 60}min"
