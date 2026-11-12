"""The offline brain: a deterministic dialogue manager on top of the desk.

It needs no network and no API key, answers in a few milliseconds, and is the brain the evaluation measures. The
Claude brain (agent.py) drives the same desk, so the same guards apply to both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

from . import nlu
from .calendar import NotFound
from .desk import Blocked, Desk, Session, SlotGone, say_day, say_phone, say_time


@dataclass
class Reply:
    text: str
    act: str
    data: dict[str, Any] = field(default_factory=dict)
    end: bool = False
    transfer: str | None = None


@dataclass
class Flow:
    kind: str  # book | cancel | reschedule | message
    service: str | None = None
    day: date | None = None
    at: time | None = None
    window: tuple[time | None, time | None] | None = None
    slot_id: str | None = None
    name: str | None = None
    phone: str | None = None
    phone_confirmed: bool = False
    ref: str | None = None
    message: str | None = None
    for_whom: str | None = None
    stage: str = "start"
    verify_tries: int = 0


class RuleBrain:
    name = "rules"

    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.b = desk.business
        self.flows: dict[str, Flow | None] = {}
        self.offers: dict[str, list[str]] = {}
        self.expect: dict[str, str | None] = {}
        self.misses: dict[str, int] = {}
        self.acts: dict[str, list[str]] = {}
        self.mentioned: dict[str, str] = {}  # the last service the caller named, e.g. in a price question

    # ------------------------------------------------------------------ entry points
    def greet(self, s: Session) -> Reply:
        text = f"Thank you for calling {self.b.name}. How can I help you today?"
        return self._say(s, Reply(text, "greet"))

    def respond(self, s: Session, text: str) -> Reply:
        s.heard(text)
        reply = self._respond(s, text)
        # Never ask the same thing a fourth time in a row: hand the caller to a person instead.
        acts = self.acts.setdefault(s.call_id, [])
        acts.append(reply.act)
        if not reply.end and len(acts) >= 4 and len(set(acts[-4:])) == 1:
            self.desk.transfer(s, "conversation stuck")
            reply = Reply(
                "I'm sorry, I don't think I'm getting this right. Let me put you through to a colleague.",
                "transfer",
                {"reason": "conversation stuck"},
                end=True,
                transfer=self.b.phone_for_humans,
            )
        return self._say(s, reply)

    def _say(self, s: Session, r: Reply) -> Reply:
        r.text = self.desk.check_speech(s, r.text)
        s.transcript.append(("receptionist", r.text))
        if r.end:
            s.ended = True
        return r

    # ------------------------------------------------------------------ dialogue
    def _respond(self, s: Session, text: str) -> Reply:
        cid = s.call_id
        flow = self.flows.get(cid)
        offered = len(self.offers.get(cid, []))
        expecting = self.expect.get(cid)
        p = nlu.understand(text, self.b, s.today, expecting=expecting, offered=offered)
        if expecting == "service" and not p.service:
            # "the first one" / "the massage one" after hearing the list of services
            pick = nlu.parse_choice(text, len(self.b.services))
            if pick is not None:
                p.service = self.b.services[pick].key
        if p.service:
            self.mentioned[cid] = p.service
        if p.intent == "goodbye" and not p.yes and (flow is None or flow.stage != "confirm" or not p.service):
            return Reply("Thank you for calling. Goodbye!", "goodbye", end=True)

        if p.intent == "emergency":
            self.desk.transfer(s, "emergency")
            return Reply(
                "If this is a medical emergency, please hang up and call 999 now. "
                "I'm putting you through to a member of our team.",
                "transfer",
                {"reason": "emergency"},
                end=True,
                transfer=self.b.phone_for_humans,
            )
        if p.intent == "human":
            self.desk.transfer(s, "asked for a person")
            return Reply(
                "Of course. I'm putting you through to a member of the team now.",
                "transfer",
                {"reason": "asked for a person"},
                end=True,
                transfer=self.b.phone_for_humans,
            )

        # A question in the middle of a booking is answered, then the booking carries on.
        if p.intent == "faq" and p.faq:
            answer = self.desk.faq(p.faq)
            if flow and flow.stage != "done":
                follow = self._advance(s, flow, nlu.Parsed(intent="none"))
                return Reply(answer + " " + follow.text, follow.act, {**follow.data, "faq": p.faq})
            return Reply(answer + " Is there anything else I can help with?", "faq_answer", {"faq": p.faq})

        if flow is None or flow.stage == "done":
            if p.intent == "goodbye" or (flow and flow.stage == "done" and p.no and p.intent == "unknown"):
                return Reply("Thank you for calling. Goodbye!", "goodbye", end=True)
            if p.intent in ("book", "cancel", "reschedule", "message"):
                flow = Flow(kind=p.intent)
                if p.intent == "book" and not p.service and re.search(r"\b(one|it|that)\b", nlu.normalise(text)):
                    flow.service = self.mentioned.get(cid)  # "I'll book one" after asking about a service
                self.flows[cid] = flow
                self.misses[cid] = 0
            elif flow and flow.stage == "done" and p.yes:
                return Reply("Sure, what else can I help with?", "clarify")
            else:
                return self._miss(
                    s,
                    "Sorry, I didn't catch that. I can book, move or cancel an appointment, take a "
                    "message, or answer a question about the clinic. What would you like to do?",
                )
        assert flow is not None
        self.misses[cid] = 0
        return self._advance(s, flow, p)

    def _miss(self, s: Session, text: str) -> Reply:
        n = self.misses.get(s.call_id, 0) + 1
        self.misses[s.call_id] = n
        if n >= 3:
            self.desk.transfer(s, "could not understand")
            return Reply(
                "I'm sorry, I'm having trouble understanding. Let me put you through to a colleague.",
                "transfer",
                {"reason": "could not understand"},
                end=True,
                transfer=self.b.phone_for_humans,
            )
        return Reply(text, "clarify")

    # ------------------------------------------------------------------ flows
    def _advance(self, s: Session, f: Flow, p: nlu.Parsed) -> Reply:
        if f.kind == "book":
            return self._book(s, f, p)
        if f.kind in ("cancel", "reschedule"):
            return self._existing(s, f, p)
        return self._message(s, f, p)

    def _absorb(self, f: Flow, p: nlu.Parsed, s: Session) -> bool:
        """Takes any values the caller volunteered. Returns True if the time request changed."""
        changed = False
        if p.service and p.service != f.service:
            f.service, changed = p.service, True
        if p.day and p.day != f.day:
            f.day, changed = p.day, True
            if not p.at and not p.window:
                f.at = None
        if p.at and p.at != f.at:
            f.at, f.window, changed = p.at, None, True
        if p.window and p.window != f.window:
            f.window, f.at, changed = p.window, None, True
        if (p.name and self.expect.get(s.call_id) == "name") or (
            p.name and not f.name and nlu.NAME_CUE.search(nlu.normalise(s.caller_turns[-1]))
        ):
            f.name = p.name
        if p.phone:
            f.phone, f.phone_confirmed = p.phone, True
        return changed

    def _book(self, s: Session, f: Flow, p: nlu.Parsed) -> Reply:
        cid = s.call_id
        if f.stage == "confirm":
            if p.yes and not (p.day or p.at or p.window or p.service):
                try:
                    booking = self.desk.book(s, f.slot_id or "", f.name or "", f.phone or "")
                except SlotGone:
                    f.slot_id, f.stage = None, "offer"
                    return self._offer(s, f, prefix="Sorry, that time has just gone. ")
                except Blocked:
                    return self._readback(s, f)  # the yes did not answer the read-back directly: ask again
                f.stage = "done"
                self.expect[cid] = None
                return Reply(
                    f"You're booked in. Your reference is {' '.join(booking.ref)}. We'll send a text to "
                    "confirm. Is there anything else I can help with?",
                    "booked",
                    {"ref": booking.ref},
                )
            # A correction at the read-back: take the new values and search again, even if they repeat the request.
            changed = self._absorb(f, p, s) or bool(p.day or p.at or p.window or p.service)
            if changed:
                f.slot_id, f.stage = None, "offer"
                return self._offer(s, f, prefix="No problem. ")
            if p.no:
                f.stage = "fix"
                self.expect[cid] = None
                return Reply(
                    "Sorry about that. What would you like to change: the day, the time, the name or the number?",
                    "ask_fix",
                )
            if p.name or p.phone:
                return self._readback(s, f)
            r = self._readback(s, f)  # say the details again, so the next yes answers this read-back
            return Reply("Sorry, I need a yes or no. " + r.text, "read_back", r.data)
        if f.stage == "fix":
            if "name" in nlu.normalise(s.caller_turns[-1]):
                f.name, f.stage = None, "details"
            elif "number" in nlu.normalise(s.caller_turns[-1]) or "phone" in nlu.normalise(s.caller_turns[-1]):
                f.phone, f.phone_confirmed, f.stage = None, False, "details"
            elif "day" in nlu.normalise(s.caller_turns[-1]) and not p.day:
                f.day, f.slot_id, f.stage = None, None, "start"
            elif "time" in nlu.normalise(s.caller_turns[-1]) and not (p.at or p.window):
                f.at = f.window = f.slot_id = None
                f.stage = "start"
                self.expect[cid] = "time"
                return Reply("What time would suit you better?", "ask_time")
            else:
                f.stage = "start"
        if f.stage == "pick":
            ids = self.offers.get(cid, [])
            chosen = None
            if p.choice is not None and p.choice < len(ids):
                chosen = ids[p.choice]
            elif p.at:
                chosen = next(
                    (
                        i
                        for i in ids
                        if s.offered[i].start.time() == p.at and (not p.day or s.offered[i].start.date() == p.day)
                    ),
                    None,
                )
            if chosen:
                f.slot_id, f.stage = chosen, "details"
                p = nlu.Parsed(intent="none", name=p.name, phone=p.phone)  # the time named was the choice
            elif (p.no and not (p.day or p.at or p.window)) or (
                not p.yes and not (p.day or p.at or p.window or p.service)
            ):
                f.stage = "start"
                self.expect[cid] = "day"
                if p.no:
                    return Reply("No problem. Which other day or time would suit you?", "ask_day")
                return Reply(
                    "Sorry, which of those times would you like? Or tell me another day.",
                    "ask_pick",
                    self._offer_data(s),
                )
            else:
                f.stage = "start"
        changed = self._absorb(f, p, s)
        if changed and f.stage in ("details",):
            f.slot_id, f.stage = None, "start"
        if f.slot_id is None:
            if not f.service:
                self.expect[cid] = "service"
                return Reply(
                    "Certainly. Is that for an initial assessment, a follow-up session or a sports massage?",
                    "ask_service",
                )
            if not f.day:
                self.expect[cid] = "day"
                return Reply(
                    f"Which day would you like to come in for your {self.b.service(f.service).name}?", "ask_day"
                )
            return self._offer(s, f)
        f.stage = "details"
        if not f.name:
            self.expect[cid] = "name"
            return Reply("Lovely. Can I take your full name, please?", "ask_name")
        if not f.phone:
            if s.caller_number and not f.phone_confirmed and self.expect.get(cid) != "phone_confirm":
                self.expect[cid] = "phone_confirm"
                return Reply(
                    f"Thanks, {f.name.split()[0]}. Is the number you're calling from, ending "
                    f"{' '.join(s.caller_number[-3:])}, the best one to reach you on?",
                    "confirm_caller_id",
                )
            if self.expect.get(cid) == "phone_confirm":
                if p.yes and s.caller_number:
                    f.phone, f.phone_confirmed = s.caller_number, True
                    return self._readback(s, f)
                self.expect[cid] = "phone"
                return Reply("What's the best number to reach you on?", "ask_phone")
            self.expect[cid] = "phone"
            return Reply("And what's the best number to reach you on?", "ask_phone")
        return self._readback(s, f)

    def _readback(self, s: Session, f: Flow) -> Reply:
        f.stage = "confirm"
        self.expect[s.call_id] = "confirm"
        text = self.desk.read_back_booking(s, f.slot_id or "", f.name or "", f.phone or "")
        slot = s.offered[f.slot_id or ""]
        return Reply(
            text,
            "read_back",
            {"kind": "book", "slot": _slot_data(f.slot_id or "", slot), "name": f.name, "phone": f.phone},
        )

    def _offer_data(self, s: Session) -> dict[str, Any]:
        ids = self.offers.get(s.call_id, [])
        return {"slots": [_slot_data(i, s.offered[i]) for i in ids]}

    def _offer(self, s: Session, f: Flow, prefix: str = "") -> Reply:
        cid = s.call_id
        assert f.service
        assert f.day
        svc = self.b.service(f.service)
        earliest, latest = f.window if f.window else (None, None)
        if f.day < s.today:
            f.day = None
            self.expect[cid] = "day"
            return Reply(prefix + "That date has already passed. Which day would you like?", "ask_day")
        found = self.desk.find_slots(s, f.service, f.day, at=f.at, earliest=earliest, latest=latest)
        note = ""
        if not found:
            nxt = self.desk.next_days_with_space(s, f.service, f.day, earliest=earliest, latest=latest)
            if nxt is None:
                f.stage = "done"
                self.expect[cid] = None
                return Reply(
                    prefix + f"I'm sorry, I can't see any {svc.name} appointments in the next two weeks "
                    "at that time of day. Would you like me to take a message so the team can call you back?",
                    "no_availability",
                )
            note = f"I don't have anything on {say_day(f.day)}{_window_words(f.window)}. "
            f.day = nxt
            found = self.desk.find_slots(s, f.service, nxt, earliest=earliest, latest=latest)
        self.offers[cid] = [sid for sid, _ in found]
        f.stage = "pick"
        self.expect[cid] = "pick"
        data = self._offer_data(s)
        if f.at and len(found) == 1 and found[0][1].start.time() == f.at:
            who = self.b.practitioner(found[0][1].practitioner).name
            return Reply(
                prefix + note + f"Yes, I can do {say_time(found[0][1].start)} on {say_day(found[0][1].start)} "
                f"with {who}. Would you like that one?",
                "offer_slots",
                data,
            )
        if f.at:
            note += f"{say_time_t(f.at)} isn't free, but "
        times = _join([say_time(x.start) for _, x in found])
        return Reply(prefix + note + f"I have {times} on {say_day(f.day)}. Which would you like?", "offer_slots", data)

    def _existing(self, s: Session, f: Flow, p: nlu.Parsed) -> Reply:
        cid = s.call_id
        if f.ref is None:
            last = nlu.normalise(s.caller_turns[-1])
            ref = p.ref or nlu.parse_ref(s.caller_turns[-1], self.expect.get(cid) == "ref")
            if ref:
                f.ref = ref
            elif s.caller_number and f.stage == "start":
                upcoming = self.desk.calendar.find_by_phone(s.caller_number, s.now)
                if len(upcoming) == 1:
                    f.ref = upcoming[0].ref
                    s.verified.add(f.ref)
            if f.ref is None and self.expect.get(cid) == "lookup_phone":
                # No reference: find the booking from the number on it, then check the name before touching it.
                number = p.phone or nlu.parse_phone(s.caller_turns[-1])
                found = self.desk.calendar.find_by_phone(number, s.now) if number else []
                if len(found) != 1:
                    self.desk.transfer(s, "booking not found")
                    return Reply(
                        "I'm sorry, I can't find a single booking under that number. Let me put you through to "
                        "a colleague who can look into it.",
                        "transfer",
                        {"reason": "booking not found"},
                        end=True,
                        transfer=self.b.phone_for_humans,
                    )
                f.stage, f.message = "lookup_name", found[0].ref
                self.expect[cid] = "name"
                return Reply("Thanks, I've found it. And the name on the booking?", "ask_name")
            if f.ref is None and f.stage == "lookup_name":
                candidate = f.message or ""
                b = self.desk.calendar.find(candidate)
                if p.name and p.name.lower() == b.name.lower():
                    f.ref, f.message = candidate, None
                    s.verified.add(f.ref)
                    f.stage = "start"
                else:
                    f.stage = "done"
                    self.expect[cid] = None
                    return Reply(
                        "I'm sorry, that doesn't match our records, so I can't change this booking over the "
                        "phone. Is there anything else I can help with?",
                        "verify_failed",
                    )
            if f.ref is None:
                if re.search(
                    r"\b(don't have|do not have|not got|haven't got|no idea|can't find|cannot find|lost|"
                    r"don't know|not sure)\b",
                    last,
                ):
                    self.expect[cid] = "lookup_phone"
                    return Reply("No problem. What's the phone number on the booking?", "ask_phone")
                f.stage = "ask_ref"
                self.expect[cid] = "ref"
                return Reply(
                    "Can you give me your four-digit booking reference? It's in your confirmation text.", "ask_ref"
                )
        try:
            booking = self.desk.calendar.find(f.ref)
        except NotFound:
            f.ref = None
            self.expect[cid] = "ref"
            return Reply("I can't find a booking with that reference. Could you read it to me again?", "ask_ref")
        if booking.status != "booked":
            f.stage = "done"
            return Reply(
                "That booking has already been cancelled. Is there anything else I can help with?", "anything_else"
            )
        if f.ref not in s.verified and self.desk.guards:
            if s.caller_number and self.desk.verify(s, f.ref, s.caller_number):
                pass
            elif self.expect.get(cid) == "verify":
                digits = nlu.spoken_digits(s.caller_turns[-1])
                if not self.desk.verify(s, f.ref, digits) and f.verify_tries == 0:
                    f.verify_tries = 1  # one more try: speech recognition often mishears digits
                    return Reply(
                        "Sorry, that doesn't match. Could you say the last four digits again, one at a time?",
                        "ask_verify",
                    )
                if f.ref not in s.verified:
                    f.stage = "done"
                    self.expect[cid] = None
                    return Reply(
                        "I'm sorry, that doesn't match our records, so I can't change this booking over the "
                        "phone. I can take a message for the team instead. Is there anything else?",
                        "verify_failed",
                    )
            else:
                self.expect[cid] = "verify"
                return Reply(
                    "For security, can you tell me the last four digits of the phone number on the booking?",
                    "ask_verify",
                )
        if f.kind == "cancel":
            if f.stage == "confirm":
                if p.yes:
                    self.desk.cancel(s, f.ref)
                    f.stage = "done"
                    self.expect[cid] = None
                    return Reply(
                        "That's cancelled. Is there anything else I can help with?", "cancelled", {"ref": f.ref}
                    )
                if p.no:
                    f.stage = "done"
                    self.expect[cid] = None
                    return Reply("No problem, I'll leave it as it is. Is there anything else I can help with?", "kept")
                return Reply(
                    "Sorry, I need a yes or no. " + self.desk.read_back_cancel(s, f.ref),
                    "read_back",
                    {"kind": "cancel", "ref": f.ref},
                )
            f.stage = "confirm"
            self.expect[cid] = "confirm"
            return Reply(self.desk.read_back_cancel(s, f.ref), "read_back", {"kind": "cancel", "ref": f.ref})
        # reschedule: reuse the booking flow's slot finding, then a move read-back
        if f.service is None:
            f.service = booking.service
        if f.stage == "confirm":
            if p.yes and not (p.day or p.at or p.window):
                try:
                    moved = self.desk.move(s, f.ref, f.slot_id or "")
                except SlotGone:
                    f.slot_id, f.stage = None, "offer"
                    return self._offer(s, f, prefix="Sorry, that time has just gone. ")
                except Blocked:
                    text = self.desk.read_back_move(s, f.ref, f.slot_id or "")
                    return Reply(
                        text,
                        "read_back",
                        {"kind": "move", "ref": f.ref, "slot": _slot_data(f.slot_id or "", s.offered[f.slot_id or ""])},
                    )
                f.stage = "done"
                self.expect[cid] = None
                return Reply(
                    f"Done. You're now booked for {say_day(moved.start)} at {say_time(moved.start)}. Is "
                    "there anything else I can help with?",
                    "moved",
                    {"ref": f.ref},
                )
            if p.no and not (p.day or p.at or p.window):
                f.slot_id, f.stage = None, "start"
                f.day = f.at = f.window = None
                self.expect[cid] = "day"
                return Reply("No problem. Which day would suit you instead?", "ask_day")
        if f.stage == "pick":
            ids = self.offers.get(cid, [])
            chosen = None
            if p.choice is not None and p.choice < len(ids):
                chosen = ids[p.choice]
            elif p.at:
                chosen = next((i for i in ids if s.offered[i].start.time() == p.at), None)
            if chosen:
                f.slot_id = chosen
                f.stage = "confirm"
                self.expect[cid] = "confirm"
                text = self.desk.read_back_move(s, f.ref, chosen)
                return Reply(
                    text, "read_back", {"kind": "move", "ref": f.ref, "slot": _slot_data(chosen, s.offered[chosen])}
                )
            if p.no and not (p.day or p.at or p.window):
                f.stage = "start"
                self.expect[cid] = "day"
                return Reply("No problem. Which other day or time would suit you?", "ask_day")
        self._absorb(f, p, s)
        if not f.day:
            self.expect[cid] = "day"
            f.stage = "start"
            return Reply(
                f"I can see your appointment on {say_day(booking.start)} at {say_time(booking.start)}. "
                "Which day would you like to move it to?",
                "ask_day",
            )
        return self._offer(s, f)

    def _message(self, s: Session, f: Flow, p: nlu.Parsed) -> Reply:
        cid = s.call_id
        last = s.caller_turns[-1]
        if f.stage == "start":
            f.stage = "text"
            self.expect[cid] = "message"
            return Reply("Of course. What would you like me to pass on?", "ask_message")
        if f.stage == "text":
            f.message = last
            f.stage = "details"
            p = nlu.Parsed(intent="none")
        if not f.name:
            if self.expect.get(cid) == "name" and p.name:
                f.name = p.name
            else:
                self.expect[cid] = "name"
                return Reply("Thanks. And your name, please?", "ask_name")
        if not f.phone:
            if self.expect.get(cid) == "phone_confirm" and p.yes and s.caller_number:
                f.phone = s.caller_number
            elif p.phone:
                f.phone = p.phone
            elif s.caller_number and self.expect.get(cid) not in ("phone_confirm", "phone"):
                self.expect[cid] = "phone_confirm"
                return Reply(
                    f"Shall the team call you back on the number ending {' '.join(s.caller_number[-3:])}?",
                    "confirm_caller_id",
                )
            elif self.expect.get(cid) == "phone":
                return Reply(
                    "Sorry, I didn't get all of that. Could you say the whole number again, slowly?", "ask_phone"
                )
            else:
                self.expect[cid] = "phone"
                return Reply("What number should they call you back on?", "ask_phone")
        if f.phone != s.caller_number and not f.phone_confirmed:
            # A number taken by ear is read back in full before anyone is asked to call it.
            if self.expect.get(cid) == "message_number":
                if p.yes and not p.no:
                    f.phone_confirmed = True
                else:
                    f.phone = p.phone
                    if not f.phone:
                        self.expect[cid] = "phone"
                        return Reply("Sorry about that. What's the right number?", "ask_phone")
            if not f.phone_confirmed:
                self.expect[cid] = "message_number"
                return Reply(
                    f"Let me check I've got that right: {say_phone(f.phone or '', full=True)}?",
                    "read_back",
                    {"kind": "message", "phone": f.phone},
                )
        self.desk.take_message(s, f.for_whom or "the team", f.message or "", f.name or "", f.phone or "")
        f.stage = "done"
        self.expect[cid] = None
        return Reply(
            "Thank you, I've passed that on and someone will call you back. Is there anything else I can help with?",
            "message_taken",
        )


def _slot_data(sid: str, slot: Any) -> dict[str, Any]:
    return {"id": sid, "start": slot.start.isoformat(), "practitioner": slot.practitioner, "service": slot.service}


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _window_words(window: tuple[time | None, time | None] | None) -> str:
    if not window:
        return ""
    a, b = window
    if a and b:
        return f" between {say_time_t(a)} and {say_time_t(b)}"
    return f" after {say_time_t(a)}" if a else f" before {say_time_t(b)}" if b else ""


def say_time_t(t: time | None) -> str:
    from datetime import datetime as _dt

    return say_time(_dt(2000, 1, 1, t.hour, t.minute)) if t else ""
