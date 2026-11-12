from __future__ import annotations

from datetime import date, time

import pytest

from receptionist import nlu
from receptionist.business import Business

TODAY = date(2026, 11, 11)  # Wednesday


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("tomorrow", date(2026, 11, 12)),
        ("on Thursday", date(2026, 11, 12)),
        ("next Monday", date(2026, 11, 16)),
        ("Thursday the nineteenth", date(2026, 11, 19)),  # the number wins over the weekday
        ("the 20th of November", date(2026, 11, 20)),
        ("November the twenty second", date(2026, 11, 22)),
        ("on the twenty first", date(2026, 11, 21)),
        ("the 3rd", date(2026, 12, 3)),  # already past this month: next month
        ("may I come in on the 25th", date(2026, 11, 25)),  # "may" is not the month
        ("the second one", None),  # a choice, not a date
        ("whenever suits", None),
    ],
)
def test_dates(text: str, want: date | None) -> None:
    assert nlu.parse_date(text, TODAY) == want


@pytest.mark.parametrize(
    ("text", "at", "window"),
    [
        ("3pm", time(15), None),
        ("half past two", time(14, 30), None),
        ("quarter to four", time(15, 45), None),
        ("10 30", time(10, 30), None),
        ("ten thirty", time(10, 30), None),
        ("nine in the morning", time(9), None),
        ("at three", time(15), None),  # a clinic is not open at 3 am
        ("midday", time(12), None),
        ("in the afternoon", None, (time(12), time(17))),
        ("first thing", None, (time(8), time(10))),
        ("after 4", None, (time(16), None)),
    ],
)
def test_times(text: str, at: time | None, window: tuple[time | None, time | None] | None) -> None:
    assert nlu.parse_time(text) == (at, window)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("oh seven seven double nine one two three four five six", "07799123456"),
        ("07700 900 142", "07700900142"),
        ("+44 7700 900142", "07700900142"),
        ("seven seven", None),
    ],
)
def test_phone_numbers(text: str, want: str | None) -> None:
    assert nlu.parse_phone(text) == want


def test_names_and_choices() -> None:
    assert nlu.parse_name("my name is sarah o'connor", expecting=False) == "Sarah O'Connor"
    assert nlu.parse_name("uh it's Tom Hughes", expecting=True) == "Tom Hughes"
    assert nlu.parse_name("I'd like a massage", expecting=False) is None
    assert nlu.parse_choice("the second one please", 3) == 1
    assert nlu.parse_choice("first thing", 3) is None
    assert nlu.parse_choice("yes", 1) == 0


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("I'd like to book a sports massage", "book"),
        ("is there any space on Thursday", "book"),
        ("I need to cancel my appointment", "cancel"),
        ("can I move my appointment to another day", "reschedule"),
        ("can I leave a message for Amara", "message"),
        ("can I speak to a real person", "human"),
        ("I've got chest pain", "emergency"),
        ("is there parking", "faq"),
        ("Saturday at 3 pm", "unknown"),  # a time request, not a question about opening hours
    ],
)
def test_intents(business: Business, text: str, intent: str) -> None:
    assert nlu.understand(text, business, TODAY).intent == intent


def test_yes_and_no(business: Business) -> None:
    assert nlu.understand("yes that's right", business, TODAY).yes
    assert nlu.understand("no, not quite", business, TODAY).no
    p = nlu.understand("hmm, hang on", business, TODAY)
    assert not p.yes
