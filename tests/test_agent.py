"""The Claude brain, through the real Anthropic SDK with canned API responses (no network, no key)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import anthropic
import httpx2
import pytest

from receptionist.agent import FALLBACK_BETA, MODEL, ClaudeBrain
from receptionist.calendar import Calendar
from receptionist.desk import Desk, Session

DAY = "2026-11-18"


def message(*content: dict[str, Any], stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": list(content),
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def tool(name: str, args: dict[str, Any], tid: str = "tu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


class Script:
    """Answers each API request with the next canned response and records what was sent."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses: Iterator[dict[str, Any]] = iter(responses)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx2.Headers] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        return httpx2.Response(200, json=next(self.responses))


def brain_with(desk: Desk, script: Script) -> ClaudeBrain:
    client = anthropic.Anthropic(
        api_key="test-key",
        base_url="http://api.test",
        http_client=httpx2.Client(transport=httpx2.MockTransport(script)),
        max_retries=0,
    )
    return ClaudeBrain(desk, client)


def test_request_shape(desk: Desk, session: Session) -> None:
    script = Script([message(text("Of course. Which day would you like?"))])
    brain = brain_with(desk, script)
    brain.greet(session)
    reply = brain.respond(session, "I'd like to book a massage")
    assert reply.text == "Of course. Which day would you like?"
    req, headers = script.requests[0], script.headers[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default" and FALLBACK_BETA in headers["anthropic-beta"]
    assert req["output_config"] == {"effort": "low"}
    assert "thinking" not in req  # Claude Opus 5.5 thinks adaptively; it cannot be switched off
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in req["tools"])
    assert req["messages"][-1] == {"role": "user", "content": "I'd like to book a massage"}
    assert "£55" in req["system"][0]["text"]  # prices come from the business config


def test_booking_through_tools_and_the_exact_read_back(desk: Desk, calendar: Calendar, session: Session) -> None:
    script = Script(
        [
            message(
                tool(
                    "find_slots",
                    {"service": "massage", "date": DAY, "time": None, "earliest": "12:00", "latest": "17:00"},
                ),
                stop="tool_use",
            ),
            message(text("I have a few times. Which would you like?")),
        ]
    )
    brain = brain_with(desk, script)
    brain.greet(session)
    r = brain.respond(session, "a massage next Wednesday afternoon")
    assert r.act == "offer_slots"
    slot_id = r.data["slots"][0]["id"]
    # the tool result sent back to Claude lists only real free slots
    result = script.requests[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and slot_id in result["content"]

    script.responses = iter(
        [
            message(
                text("Let me read that back."),
                tool("read_back_booking", {"slot_id": slot_id, "name": "Sam Jones", "phone": "07700900142"}),
                stop="tool_use",
            ),
            message(text("Shall I book it?")),
        ]
    )
    r = brain.respond(session, "the first one, Sam Jones, this number")
    assert r.act == "read_back" and r.text.startswith("So that's a sports massage")  # the desk's sentence, verbatim

    script.responses = iter(
        [
            message(
                tool("book_appointment", {"slot_id": slot_id, "name": "Sam Jones", "phone": "07700900142"}),
                stop="tool_use",
            ),
            message(text("You're booked in.")),
        ]
    )
    r = brain.respond(session, "yes please")
    assert r.act == "booked"
    assert [b.name for b in calendar.all_bookings()] == ["Sam Jones"]


def test_guard_refusal_reaches_claude_as_a_tool_error(desk: Desk, calendar: Calendar, session: Session) -> None:
    script = Script(
        [
            message(
                tool(
                    "find_slots", {"service": "followup", "date": DAY, "time": None, "earliest": None, "latest": None}
                ),
                stop="tool_use",
            ),
            message(text("Here are some times.")),
        ]
    )
    brain = brain_with(desk, script)
    brain.greet(session)
    slot_id = brain.respond(session, "a follow up next Wednesday").data["slots"][0]["id"]
    # Claude tries to book straight away, without a read-back: the desk refuses and Claude must recover.
    script.responses = iter(
        [
            message(
                tool("book_appointment", {"slot_id": slot_id, "name": "Sam", "phone": "07700900142"}), stop="tool_use"
            ),
            message(text("Before I book, can I check your full name?")),
        ]
    )
    r = brain.respond(session, "book the first one")
    sent = script.requests[-1]["messages"][-1]["content"][0]
    assert sent["is_error"] is True and "read the book details back" in sent["content"]
    assert r.act != "booked" and calendar.all_bookings() == []
    assert session.blocked


def test_emergency_never_reaches_the_model(desk: Desk, session: Session) -> None:
    script = Script([])
    brain = brain_with(desk, script)
    brain.greet(session)
    r = brain.respond(session, "my husband has collapsed")
    assert r.transfer and r.end and script.requests == []


def test_refusal_transfers_to_a_person(desk: Desk, session: Session) -> None:
    script = Script([message(text(""), stop="refusal")])
    brain = brain_with(desk, script)
    brain.greet(session)
    r = brain.respond(session, "hello")
    assert r.act == "transfer" and r.end


def test_invented_price_is_removed(desk: Desk, session: Session) -> None:
    script = Script([message(text("Sure. A sports massage is only £10 this week."))])
    brain = brain_with(desk, script)
    brain.greet(session)
    r = brain.respond(session, "how much is a massage")
    assert "£10" not in r.text


@pytest.mark.parametrize("bad", ["S-made-up"])
def test_unoffered_slot_is_refused(desk: Desk, calendar: Calendar, now: datetime, bad: str) -> None:
    s = Session("c", "07700900142", now)
    script = Script(
        [
            message(
                tool("read_back_booking", {"slot_id": bad, "name": "Eve", "phone": "07700900142"}), stop="tool_use"
            ),
            message(text("Let me check the diary first.")),
        ]
    )
    brain = brain_with(desk, script)
    brain.greet(s)
    brain.respond(s, "book me in for Wednesday at 9")
    assert script.requests[-1]["messages"][-1]["content"][0]["is_error"] is True
