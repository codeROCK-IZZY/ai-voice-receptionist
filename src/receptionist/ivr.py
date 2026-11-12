"""Baseline: a directed-dialogue IVR with keyword grammars, the way most phone booking lines still work.

It asks fixed questions in a fixed order and spots the keywords its grammar lists anywhere in the utterance: a menu
keyword, a service keyword, a weekday name, "morning" or "afternoon", "yes" or "no". It books the first free slot
that matches, takes the caller ID as the callback number, records the name as spoken, and confirms with a single
yes/no. It answers the FAQ menu and transfers on "operator". It goes through the same desk, so it cannot
double-book either. Moving a booking and taking messages are not on its menu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time, timedelta

from .desk import Blocked, Desk, Session, say_day, say_time
from .dialog import Reply

MENU = "Thank you for calling. Please say: booking, cancel, opening hours, parking, prices, or operator."


@dataclass
class _State:
    step: str = "menu"
    service: str | None = None
    day: object = None
    window: tuple[time, time] | None = None
    slot_id: str | None = None
    name: str | None = None
    ref: str | None = None
    retries: int = 0
    data: dict[str, object] = field(default_factory=dict)


class IvrBrain:
    name = "ivr"

    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.st: dict[str, _State] = {}

    def greet(self, s: Session) -> Reply:
        self.st[s.call_id] = _State()
        s.transcript.append(("receptionist", MENU))
        return Reply(MENU, "greet")

    def respond(self, s: Session, text: str) -> Reply:
        s.heard(text)
        r = self._respond(s, self.st[s.call_id], text.lower())
        s.transcript.append(("receptionist", r.text))
        if r.end:
            s.ended = True
        return r

    def _retry(self, st: _State, text: str, act: str) -> Reply:
        st.retries += 1
        if st.retries >= 3:
            return Reply("Sorry, I could not understand. Goodbye.", "goodbye", end=True)
        return Reply("Sorry, I didn't understand. " + text, act if act != "greet" else "clarify")

    def _respond(self, s: Session, st: _State, t: str) -> Reply:
        d = self.desk
        if re.search(r"\boperator\b", t):
            d.transfer(s, "operator")
            return Reply("Transferring you to an operator.", "transfer", end=True, transfer="operator")
        if st.step == "menu":
            for key, words in (
                ("parking", "park|parking"),
                ("prices", "price|prices|cost|how much"),
                ("hours", "open|opening|hours|close"),
            ):
                if re.search(rf"\b({words})\b", t):
                    return Reply(d.faq(key) + " Say booking, cancel, or goodbye.", "faq_answer", {"faq": key})
            if re.search(r"\b(goodbye|bye)\b", t):
                return Reply("Goodbye.", "goodbye", end=True)
            if re.search(r"\b(book|booking|appointment)\b", t):
                st.step, st.retries = "service", 0
                return Reply("Say assessment, follow up, or massage.", "ask_service")
            if re.search(r"\bcancel\b", t):
                st.step, st.retries = "ref", 0
                return Reply("Say your four digit booking reference.", "ask_ref")
            return self._retry(st, MENU, "greet")
        if st.step == "service":
            for key, word in (
                ("assessment", "assessment|initial"),
                ("followup", "follow up|follow-up"),
                ("massage", "massage"),
            ):
                if re.search(rf"\b({word})\b", t):
                    st.service, st.step, st.retries = key, "day", 0
                    return Reply("Say the day of the week you would like.", "ask_day")
            return self._retry(st, "Say assessment, follow up, or massage.", "ask_service")
        if st.step == "day":
            days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
            m = re.search(r"\b(" + "|".join(days) + r")\b", t)
            if not m:
                return self._retry(st, "Say the day of the week you would like.", "ask_day")
            ahead = (days.index(m.group(1)) - s.today.weekday()) % 7 or 7
            st.day, st.step, st.retries = s.today + timedelta(days=ahead), "part", 0
            return Reply("Say morning or afternoon.", "ask_time")
        if st.step == "part":
            if re.search(r"\bmorning\b", t):
                st.window = (time(8), time(12))
            elif re.search(r"\bafternoon\b", t):
                st.window = (time(12), time(20))
            else:
                return self._retry(st, "Say morning or afternoon.", "ask_time")
            assert st.service is not None
            found = d.find_slots(s, st.service, st.day, earliest=st.window[0], latest=st.window[1], limit=1)  # type: ignore[arg-type]
            if not found:
                st.step = "menu"
                return Reply(
                    "There are no appointments at that time. Say booking to try again, or goodbye.", "no_availability"
                )
            sid, slot = found[0]
            st.slot_id, st.step = sid, "accept"
            return Reply(
                f"The first available time is {say_time(slot.start)} on {say_day(slot.start)}. Say yes to "
                "take it, or no.",
                "offer_slots",
                {
                    "slots": [
                        {
                            "id": sid,
                            "start": slot.start.isoformat(),
                            "practitioner": slot.practitioner,
                            "service": slot.service,
                        }
                    ]
                },
            )
        if st.step == "accept":
            if re.search(r"\b(yes|yeah|yep)\b", t):
                st.step = "name"
                return Reply("Say your name after the tone.", "ask_name")
            st.step = "menu"
            return Reply("Say booking to try again, or goodbye.", "clarify")
        if st.step == "name":
            t = re.sub(r"^((yeah|so|um|uh) )*(my name is|my name's|the name's|it's|it is|this is|i'm)\s+", "", t)
            st.name = " ".join(w.capitalize() for w in re.findall(r"[a-z']+", t)) or "Unknown"
            phone = s.caller_number or ""
            if not phone:
                st.step = "menu"
                return Reply(
                    "We cannot book without caller ID. Please call back from a visible number. Goodbye.",
                    "goodbye",
                    end=True,
                )
            st.step = "confirm"
            text = d.read_back_booking(s, st.slot_id or "", st.name, phone)
            return Reply(
                text + " Say yes or no.",
                "read_back",
                {
                    "kind": "book",
                    "name": st.name,
                    "phone": phone,
                    "slot": {
                        "start": s.offered[st.slot_id or ""].start.isoformat(),
                        "service": s.offered[st.slot_id or ""].service,
                    },
                },
            )
        if st.step == "confirm":
            if re.search(r"\b(yes|yeah|yep)\b", t):
                try:
                    b = d.book(s, st.slot_id or "", st.name or "", s.caller_number or "")
                except Blocked:
                    st.step = "menu"
                    return Reply("Sorry, that time is no longer available. Say booking to try again.", "clarify")
                st.step = "menu"
                return Reply(
                    f"Booked. Your reference is {' '.join(b.ref)}. Say booking, cancel, or goodbye.",
                    "booked",
                    {"ref": b.ref},
                )
            st.step = "menu"
            return Reply("Not booked. Say booking to start again, or goodbye.", "clarify")
        if st.step == "ref":
            digits = re.sub(r"\D", "", t)
            if len(digits) != 4:
                return self._retry(st, "Say your four digit booking reference.", "ask_ref")
            st.ref = digits
            try:
                text = d.read_back_cancel(s, digits)
            except Exception:
                st.step = "menu"
                return Reply("That reference cannot be cancelled by phone. Goodbye.", "goodbye", end=True)
            st.step = "cancel_confirm"
            return Reply(text + " Say yes or no.", "read_back", {"kind": "cancel", "ref": digits})
        if st.step == "cancel_confirm":
            if re.search(r"\b(yes|yeah|yep)\b", t):
                d.cancel(s, st.ref or "")
                st.step = "menu"
                return Reply("Cancelled. Goodbye.", "cancelled", end=True)
            st.step = "menu"
            return Reply("Not cancelled. Goodbye.", "goodbye", end=True)
        return Reply("Goodbye.", "goodbye", end=True)
