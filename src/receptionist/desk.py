"""The front desk: the only way any brain (rules or Claude) can read or change the diary.

Every action that changes something is guarded here, in code, not in a prompt:

* a slot can only be booked if it was offered on this call (no invented times);
* a booking, cancellation or move only happens after the receptionist has read the exact details back and the
  caller's very next words were a yes;
* an existing booking is only shown or changed once the caller is verified (calling from the number on the
  booking, or giving its last four digits);
* any price in what the receptionist says must be a price from the price list.

Guards can be switched off for the ablation in the evaluation, to measure what they prevent.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from . import nlu
from .business import Business
from .calendar import Booking, Calendar, NotFound, Slot, SlotTaken


class Blocked(Exception):
    """A guard refused an action. The message is safe to show the brain, which must recover."""


class SlotGone(Blocked):
    """The slot was taken by another caller between the offer and the booking."""


@dataclass
class ReadBack:
    kind: str  # book | cancel | move
    details: dict[str, str]
    caller_turn: int  # number of caller turns heard when the read-back was spoken


@dataclass
class Message:
    call_id: str
    for_whom: str
    text: str
    name: str
    phone: str


@dataclass
class Session:
    call_id: str
    caller_number: str | None
    now: datetime
    caller_turns: list[str] = field(default_factory=list)
    transcript: list[tuple[str, str]] = field(default_factory=list)
    offered: dict[str, Slot] = field(default_factory=dict)
    readback: ReadBack | None = None
    verified: set[str] = field(default_factory=set)
    blocked: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    transferred: str | None = None
    ended: bool = False

    @property
    def today(self) -> date:
        return self.now.date()

    def heard(self, text: str) -> None:
        self.caller_turns.append(text)
        self.transcript.append(("caller", text))


class Desk:
    def __init__(self, business: Business, calendar: Calendar, *, guards: bool = True) -> None:
        self.business = business
        self.calendar = calendar
        self.guards = guards
        self.messages: list[Message] = []
        self.prices = {s.price_text for s in business.services}

    # ------------------------------------------------------------------ reading
    def find_slots(
        self,
        s: Session,
        service: str,
        day: date,
        *,
        at: time | None = None,
        earliest: time | None = None,
        latest: time | None = None,
        practitioner: str | None = None,
        limit: int = 3,
    ) -> list[tuple[str, Slot]]:
        """Free slots, registered as offered on this call. With `at`, the exact time first, then the nearest."""
        slots = self.calendar.free_slots(
            service, day, earliest=earliest, latest=latest, practitioner=practitioner, now=s.now
        )
        if at is not None:
            target = datetime.combine(day, at, self.business.timezone)
            exact = [x for x in slots if x.start == target]
            if exact:
                slots = exact[:1]
            else:
                slots.sort(key=lambda x: abs((x.start - target).total_seconds()))
                slots = sorted(_distinct_times(slots)[:2], key=lambda x: x.start)
        else:
            slots = _spread(_distinct_times(slots), limit)
        out = []
        for slot in slots[:limit]:
            sid = "S" + secrets.token_hex(2)
            s.offered[sid] = slot
            out.append((sid, slot))
        return out

    def next_days_with_space(
        self, s: Session, service: str, start: date, *, earliest: time | None, latest: time | None, days: int = 14
    ) -> date | None:
        for i in range(days):
            d = start + timedelta(days=i)
            if self.calendar.free_slots(service, d, earliest=earliest, latest=latest, now=s.now):
                return d
        return None

    def faq(self, key: str) -> str:
        return self.business.faq(key).answer

    def lookup(self, s: Session, ref: str) -> Booking:
        b = self.calendar.find(ref)
        if self.guards and ref not in s.verified:
            if s.caller_number and _digits(s.caller_number)[-10:] == _digits(b.phone)[-10:]:
                s.verified.add(ref)
            else:
                raise Blocked("caller not verified for this booking: ask for the last four digits of its phone number")
        return b

    def verify(self, s: Session, ref: str, last4: str) -> bool:
        try:
            b = self.calendar.find(ref)
        except NotFound:
            return False
        if _digits(b.phone)[-4:] == _digits(last4)[-4:]:
            s.verified.add(ref)
            return True
        return False

    # ------------------------------------------------------------------ read-backs
    def read_back_booking(self, s: Session, slot_id: str, name: str, phone: str) -> str:
        slot = self._offered(s, slot_id)
        svc = self.business.service(slot.service)
        who = self.business.practitioner(slot.practitioner).name
        text = (
            f"So that's {_a(svc.name)} with {who} on {say_day(slot.start)} at {say_time(slot.start)}, "
            f"for {name}, on {say_phone(phone, full=_digits(phone) != _digits(s.caller_number or ''))}. "
            "Shall I book that?"
        )
        s.readback = ReadBack("book", {"slot": slot_id, "name": name, "phone": _digits(phone)}, len(s.caller_turns))
        return text

    def read_back_cancel(self, s: Session, ref: str) -> str:
        b = self.lookup(s, ref)
        svc = self.business.service(b.service)
        late = b.start - s.now < timedelta(hours=self.business.cancellation_hours)
        text = f"That's your {svc.name} on {say_day(b.start)} at {say_time(b.start)}. "
        if late:
            text += (
                f"As it's less than {self.business.cancellation_hours} hours away, the late cancellation charge "
                f"of {svc.price_text} applies. "
            )
        text += "Shall I cancel it?"
        s.readback = ReadBack("cancel", {"ref": ref}, len(s.caller_turns))
        return text

    def read_back_move(self, s: Session, ref: str, slot_id: str) -> str:
        b = self.lookup(s, ref)
        slot = self._offered(s, slot_id)
        who = self.business.practitioner(slot.practitioner).name
        text = (
            f"So I'll move your appointment from {say_day(b.start)} at {say_time(b.start)} to "
            f"{say_day(slot.start)} at {say_time(slot.start)} with {who}. Is that right?"
        )
        s.readback = ReadBack("move", {"ref": ref, "slot": slot_id}, len(s.caller_turns))
        return text

    # ------------------------------------------------------------------ changes
    def book(self, s: Session, slot_id: str, name: str, phone: str) -> Booking:
        slot = self._offered(s, slot_id)
        self._confirmed(s, "book", {"slot": slot_id, "name": name, "phone": _digits(phone)})
        try:
            b = self.calendar.book(slot, name, _digits(phone), call_id=s.call_id)
        except SlotTaken:
            s.offered.pop(slot_id, None)
            raise SlotGone("that time has just been taken: offer other times") from None
        s.actions.append({"type": "booked", "ref": b.ref, "slot": slot.key, "name": name, "phone": _digits(phone)})
        return b

    def cancel(self, s: Session, ref: str) -> Booking:
        self.lookup(s, ref)
        self._confirmed(s, "cancel", {"ref": ref})
        b = self.calendar.cancel(ref)
        s.actions.append({"type": "cancelled", "ref": ref})
        return b

    def move(self, s: Session, ref: str, slot_id: str) -> Booking:
        self.lookup(s, ref)
        slot = self._offered(s, slot_id)
        self._confirmed(s, "move", {"ref": ref, "slot": slot_id})
        try:
            b = self.calendar.move(ref, slot)
        except SlotTaken:
            s.offered.pop(slot_id, None)
            raise SlotGone("that time has just been taken: offer other times") from None
        s.actions.append({"type": "moved", "ref": ref, "slot": slot.key})
        return b

    def take_message(self, s: Session, for_whom: str, text: str, name: str, phone: str) -> Message:
        m = Message(s.call_id, for_whom or "the team", text, name, _digits(phone))
        self.messages.append(m)
        s.actions.append({"type": "message", "for": m.for_whom, "name": name})
        return m

    def transfer(self, s: Session, reason: str) -> None:
        s.transferred = reason
        s.actions.append({"type": "transfer", "reason": reason})

    # ------------------------------------------------------------------ speech
    def check_speech(self, s: Session, text: str) -> str:
        """Removes any sentence quoting a price that is not on the price list."""
        if not self.guards:
            return text
        out = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            bad = [p for p in re.findall(r"£\d+(?:\.\d{2})?", sentence) if p not in self.prices]
            if bad:
                s.blocked.append(f"price not on the list: {', '.join(bad)}")
                out.append("I'll need a colleague to confirm that price for you.")
            else:
                out.append(sentence)
        return " ".join(out)

    # ------------------------------------------------------------------ guards
    def _offered(self, s: Session, slot_id: str) -> Slot:
        slot = s.offered.get(slot_id)
        if slot is None:
            if self.guards:
                s.blocked.append(f"slot {slot_id} was never offered")
                raise Blocked("that slot was not offered on this call: look up free slots first")
            raise Blocked("unknown slot")
        return slot

    def _confirmed(self, s: Session, kind: str, details: dict[str, str]) -> None:
        if not self.guards:
            return
        rb = s.readback
        if rb is None or rb.kind != kind or rb.details != details:
            s.blocked.append(f"{kind} without reading these exact details back")
            raise Blocked(f"read the {kind} details back to the caller and wait for a yes before doing it")
        answers = s.caller_turns[rb.caller_turn :]
        if len(answers) != 1:
            s.blocked.append(f"{kind} without the caller's answer to the read-back")
            raise Blocked("the caller has not answered the read-back yet")
        p = nlu.understand(answers[0], self.business, s.today)
        if not p.yes or p.no:
            s.blocked.append(f"{kind} after the caller did not say yes")
            raise Blocked("the caller did not clearly say yes: ask again or change the details")
        s.readback = None


def _digits(text: str) -> str:
    d = re.sub(r"\D", "", text or "")
    return "0" + d[2:] if d.startswith("44") and len(d) == 12 else d


def _distinct_times(slots: list[Slot]) -> list[Slot]:
    seen: set[datetime] = set()
    out = []
    for x in slots:
        if x.start not in seen:
            seen.add(x.start)
            out.append(x)
    return out


def _spread(slots: list[Slot], n: int) -> list[Slot]:
    """First, middle and last of the free times, so the caller hears a real choice across the window."""
    if len(slots) <= n or n == 1:
        return slots[:n]
    idx = sorted({round(i * (len(slots) - 1) / (n - 1)) for i in range(n)})
    return [slots[i] for i in idx]


def say_time(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    if h == 12 and m == 0:
        return "midday"
    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"


def say_day(dt: datetime | date) -> str:
    d = dt.date() if isinstance(dt, datetime) else dt
    n = d.day
    suffix = "th" if 11 <= n <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{d.strftime('%A')} the {n}{suffix} of {d.strftime('%B')}"


def say_phone(phone: str, full: bool = False) -> str:
    """Caller-ID numbers are confirmed by their last digits; numbers taken by ear are read back in full,
    in the usual 5-3-3 grouping, because speech recognition can mishear any digit."""
    d = _digits(phone)
    if full and len(d) >= 10:
        return "the number " + ", ".join(" ".join(part) for part in (d[:5], d[5:8], d[8:]))
    return "the number ending " + " ".join(d[-3:]) if len(d) >= 3 else "that number"


def _a(noun: str) -> str:
    return ("an " if noun[:1].lower() in "aeiou" else "a ") + noun
