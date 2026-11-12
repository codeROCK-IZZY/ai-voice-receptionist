"""The calendar and the guards every brain goes through."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from receptionist.calendar import Calendar, SlotTaken
from receptionist.desk import Blocked, Desk, Session, SlotGone

DAY = datetime(2026, 11, 18).date()  # a Wednesday: everyone works


def _offer(desk: Desk, s: Session, service: str = "followup") -> str:
    found = desk.find_slots(s, service, DAY)
    assert found
    return found[0][0]


def test_calendar_never_overlaps(calendar: Calendar, now: datetime) -> None:
    slot = calendar.free_slots("assessment", DAY, now=now)[0]
    calendar.book(slot, "A", "07000000001")
    with pytest.raises(SlotTaken):
        calendar.book(slot, "B", "07000000002")
    assert calendar.overlaps() == 0
    # a 60-minute booking blocks the overlapping 15-minute starts for the same practitioner
    later = [x for x in calendar.free_slots("assessment", DAY, now=now) if x.practitioner == slot.practitioner]
    assert all(
        x.start >= slot.start + timedelta(minutes=60) or x.start + timedelta(minutes=60) <= slot.start for x in later
    )


def test_move_keeps_the_reference(calendar: Calendar, now: datetime) -> None:
    first, *_, last = calendar.free_slots("followup", DAY, now=now)
    b = calendar.book(first, "A", "07000000001")
    moved = calendar.move(b.ref, last)
    assert moved.ref == b.ref and moved.start == last.start
    assert calendar.free_slots("followup", DAY, now=now)[0].start <= first.start


def test_concurrent_confirmations_book_once(desk: Desk, calendar: Calendar, now: datetime) -> None:
    slot = calendar.free_slots("followup", DAY, now=now)[0]
    sessions = []
    for k in range(2):
        s = Session(f"c{k}", f"0700000000{k}", now)
        s.offered["S1"] = slot
        desk.read_back_booking(s, "S1", f"Caller {k}", f"0700000000{k}")
        s.heard("yes")
        sessions.append(s)
    results: list[str] = []
    barrier = threading.Barrier(2)

    def go(s: Session) -> None:
        barrier.wait()
        try:
            desk.book(s, "S1", s.readback.details["name"] if s.readback else "", s.caller_number or "")
            results.append("won")
        except SlotGone:
            results.append("lost")

    threads = [threading.Thread(target=go, args=(s,)) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["lost", "won"]
    assert calendar.overlaps() == 0


def test_booking_needs_an_offered_slot(desk: Desk, session: Session) -> None:
    with pytest.raises(Blocked):
        desk.book(session, "S-made-up", "Eve", "07700900142")


def test_booking_needs_a_read_back_and_a_yes(desk: Desk, session: Session) -> None:
    sid = _offer(desk, session)
    with pytest.raises(Blocked, match="read the book details back"):
        desk.book(session, sid, "Sam Jones", "07700900142")
    desk.read_back_booking(session, sid, "Sam Jones", "07700900142")
    with pytest.raises(Blocked, match="not answered"):
        desk.book(session, sid, "Sam Jones", "07700900142")
    session.heard("hmm, hang on")
    with pytest.raises(Blocked, match="did not clearly say yes"):
        desk.book(session, sid, "Sam Jones", "07700900142")
    desk.read_back_booking(session, sid, "Sam Jones", "07700900142")
    session.heard("yes please")
    b = desk.book(session, sid, "Sam Jones", "07700900142")
    assert b.name == "Sam Jones" and len(session.blocked) == 3


def test_book_must_match_what_was_read_back(desk: Desk, session: Session) -> None:
    found = desk.find_slots(session, "followup", DAY)
    desk.read_back_booking(session, found[0][0], "Sam Jones", "07700900142")
    session.heard("yes")
    with pytest.raises(Blocked):
        desk.book(session, found[1][0], "Sam Jones", "07700900142")
    with pytest.raises(Blocked):
        desk.book(session, found[0][0], "Someone Else", "07700900142")


def test_changes_need_a_verified_caller(desk: Desk, calendar: Calendar, now: datetime) -> None:
    victim = calendar.book(calendar.free_slots("followup", DAY, now=now)[0], "Victim", "07111111111")
    stranger = Session("x", "07999999999", now)
    with pytest.raises(Blocked, match="not verified"):
        desk.read_back_cancel(stranger, victim.ref)
    assert not desk.verify(stranger, victim.ref, "0000")
    assert desk.verify(stranger, victim.ref, "1111")
    desk.read_back_cancel(stranger, victim.ref)
    stranger.heard("yes")
    assert desk.cancel(stranger, victim.ref).status == "cancelled"
    owner = Session("y", "07111111111", now)  # calling from the number on the booking verifies by itself
    second = calendar.book(calendar.free_slots("followup", DAY, now=now)[0], "Victim", "07111111111")
    assert desk.lookup(owner, second.ref).ref == second.ref


def test_prices_must_come_from_the_list(desk: Desk, session: Session) -> None:
    assert desk.check_speech(session, "A sports massage is £55.") == "A sports massage is £55."
    said = desk.check_speech(session, "A sports massage is £10 today. See you soon.")
    assert "£10" not in said and "See you soon." in said
    assert session.blocked


def test_guards_off_lets_everything_through(business, calendar: Calendar, session: Session) -> None:  # type: ignore[no-untyped-def]
    desk = Desk(business, calendar, guards=False)
    sid = desk.find_slots(session, "followup", DAY)[0][0]
    assert desk.book(session, sid, "Eve", "07999999999").ref
    assert "£10" in desk.check_speech(session, "It costs £10.")


def test_late_cancellation_warns_about_the_charge(desk: Desk, calendar: Calendar, now: datetime) -> None:
    soon = calendar.free_slots("followup", now.date() + timedelta(days=1), now=now)[0]
    b = calendar.book(soon, "Sam", "07700900142")
    s = Session("z", "07700900142", now)
    assert "late cancellation charge of £45" in desk.read_back_cancel(s, b.ref)
