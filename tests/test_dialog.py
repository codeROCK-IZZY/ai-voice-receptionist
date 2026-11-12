"""Whole conversations with the offline brain."""

from __future__ import annotations

from datetime import datetime, timedelta

from receptionist.calendar import Calendar
from receptionist.desk import Desk, Session
from receptionist.dialog import RuleBrain


def talk(brain: RuleBrain, s: Session, *lines: str) -> list[str]:
    acts = [brain.greet(s).act]
    for line in lines:
        r = brain.respond(s, line)
        acts.append(r.act)
        if r.end:
            break
    return acts


def test_booking_from_start_to_finish(desk: Desk, calendar: Calendar, session: Session) -> None:
    brain = RuleBrain(desk)
    acts = talk(
        brain,
        session,
        "Hi, I'd like to book a sports massage",
        "next Tuesday afternoon",
        "the second one",
        "Sam Jones",
        "yes",
        "yes please",
        "no that's all thanks",
    )
    assert acts == [
        "greet",
        "ask_day",
        "offer_slots",
        "ask_name",
        "confirm_caller_id",
        "read_back",
        "booked",
        "goodbye",
    ]
    [b] = [x for x in calendar.all_bookings() if x.name == "Sam Jones"]
    assert b.service == "massage" and b.phone == "07700900142" and b.start.weekday() == 1 and b.start.hour >= 12


def test_correction_at_the_read_back(desk: Desk, calendar: Calendar, session: Session) -> None:
    brain = RuleBrain(desk)
    talk(
        brain,
        session,
        "can I book a follow up on Thursday morning",
        "the first one",
        "my name is Leah Patel",
        "yes",
        "no actually make it Friday",
        "the first one",
        "yes",
        "bye",
    )
    [b] = [x for x in calendar.all_bookings() if x.name == "Leah Patel"]
    assert b.start.weekday() == 4


def test_typed_number_is_read_back_in_full(desk: Desk, session: Session) -> None:
    brain = RuleBrain(desk)
    talk(brain, session, "I'd like to book a follow up tomorrow", "the first one", "Ann Lee", "no")
    r = brain.respond(session, "oh seven seven double nine one two three four five six")
    assert r.act == "read_back" and "0 7 7 9 9, 1 2 3, 4 5 6" in r.text


def test_cancel_with_verification(desk: Desk, calendar: Calendar, now: datetime) -> None:
    b = calendar.book(
        calendar.free_slots("followup", now.date() + timedelta(days=5), now=now)[0], "Mei Chen", "07123456789"
    )
    s = Session("c", None, now)  # hidden number: must verify
    brain = RuleBrain(desk)
    acts = talk(brain, s, "I need to cancel my appointment", f"it's {' '.join(b.ref)}", "6 7 8 9", "yes", "bye")
    assert acts[1:4] == ["ask_ref", "ask_verify", "read_back"]
    assert calendar.find(b.ref).status == "cancelled"


def test_cancel_refused_when_verification_fails(desk: Desk, calendar: Calendar, now: datetime) -> None:
    b = calendar.book(
        calendar.free_slots("followup", now.date() + timedelta(days=5), now=now)[0], "Mei Chen", "07123456789"
    )
    s = Session("c", None, now)
    brain = RuleBrain(desk)
    talk(brain, s, "I need to cancel my appointment", f"it's {' '.join(b.ref)}", "1 1 1 1", "2 2 2 2", "bye")
    assert calendar.find(b.ref).status == "booked"


def test_no_reference_found_by_number_and_name(desk: Desk, calendar: Calendar, now: datetime) -> None:
    b = calendar.book(
        calendar.free_slots("followup", now.date() + timedelta(days=5), now=now)[0], "Mei Chen", "07123456789"
    )
    s = Session("c", None, now)
    brain = RuleBrain(desk)
    talk(
        brain,
        s,
        "I need to cancel my appointment",
        "I don't have it, sorry",
        "oh seven one two three four five six seven eight nine",
        "it's Mei Chen",
        "yes",
        "bye",
    )
    assert calendar.find(b.ref).status == "cancelled"


def test_emergency_is_transferred_at_once(desk: Desk, session: Session) -> None:
    r = RuleBrain(desk)
    r.greet(session)
    reply = r.respond(session, "my husband has collapsed")
    assert reply.transfer and reply.end and "999" in reply.text


def test_question_mid_booking_then_carries_on(desk: Desk, session: Session) -> None:
    brain = RuleBrain(desk)
    brain.greet(session)
    brain.respond(session, "I'd like to book an assessment")
    r = brain.respond(session, "is there parking")
    assert "free parking" in r.text and r.act == "ask_day"


def test_gives_up_to_a_person_instead_of_looping(desk: Desk, session: Session) -> None:
    brain = RuleBrain(desk)
    acts = talk(brain, session, "blah", "hmm", "what")
    assert acts[-1] == "transfer"
