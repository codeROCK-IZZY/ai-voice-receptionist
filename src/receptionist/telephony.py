"""Phone calls over Twilio-compatible webhooks, and the admin pages.

Twilio (or any provider speaking the same TwiML) answers the call, runs speech recognition and text-to-speech, and
posts each caller turn here. The app replies with TwiML: say this, then listen; or transfer; or hang up. Requests are
authenticated with the X-Twilio-Signature header (HMAC-SHA1 of the URL and the form parameters).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .business import Business
from .calendar import Calendar
from .desk import Desk, Session, say_day, say_time
from .dialog import Reply, RuleBrain

VOICE = "Polly.Amy-Neural"
LOW_CONFIDENCE = 0.35


def twilio_signature(token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def gather(text: str, action: str = "/voice/turn") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto" language="en-GB" '
        f'speechModel="phone_call" actionOnEmptyResult="true"><Say voice="{VOICE}">{escape(text)}</Say></Gather>'
        "</Response>"
    )


def hangup(text: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{VOICE}">{escape(text)}</Say><Hangup/></Response>'
    )


def dial(text: str, number: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{VOICE}">{escape(text)}</Say>'
        f"<Dial>{escape(number)}</Dial></Response>"
    )


def mask(number: str | None) -> str:
    return "hidden" if not number else "•••• " + number[-3:]


@dataclass
class Call:
    session: Session
    silences: int = 0
    replies: list[Reply] = field(default_factory=list)


class Phone:
    def __init__(
        self,
        business: Business,
        calendar: Calendar,
        brain_factory: Callable[[Desk], Any],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.business = business
        self.desk = Desk(business, calendar)
        self.brain = brain_factory(self.desk)
        self.calls: dict[str, Call] = {}
        self.clock = clock or (lambda: datetime.now(business.timezone))
        calendar.db.executescript(
            "CREATE TABLE IF NOT EXISTS calls (call_id TEXT PRIMARY KEY, caller TEXT, started TEXT, outcome TEXT,"
            " transcript TEXT)"
        )
        self.db: sqlite3.Connection = calendar.db

    def start(self, call_id: str, caller: str | None) -> str:
        s = Session(call_id, _normalise_number(caller), self.clock())
        self.calls[call_id] = Call(s)
        return gather(self.brain.greet(s).text)

    def turn(self, call_id: str, speech: str, confidence: float) -> str:
        call = self.calls.get(call_id)
        if call is None:
            return hangup("Sorry, something went wrong with this call. Please call us again.")
        if not speech.strip():
            call.silences += 1
            if call.silences >= 3:
                self._save(call, "silent")
                return hangup("I haven't heard anything, so I'll end the call. Please call back any time. Goodbye.")
            return gather("Sorry, I didn't hear anything. How can I help?")
        if confidence and confidence < LOW_CONFIDENCE:
            return gather("Sorry, the line isn't very clear. Could you say that again?")
        call.silences = 0
        reply = self.brain.respond(call.session, speech)
        call.replies.append(reply)
        if reply.transfer:
            self._save(call, "transferred")
            return dial(reply.text, reply.transfer)
        if reply.end:
            self._save(call, "completed")
            return hangup(reply.text)
        return gather(reply.text)

    def _save(self, call: Call, outcome: str) -> None:
        s = call.session
        acts = [a["type"] for a in s.actions]
        self.db.execute(
            "INSERT OR REPLACE INTO calls (call_id, caller, started, outcome, transcript) VALUES (?, ?, ?, ?, ?)",
            (
                s.call_id,
                mask(s.caller_number),
                s.now.isoformat(),
                ", ".join(acts) or outcome,
                json.dumps([[r, _redact(t)] for r, t in s.transcript]),
            ),
        )


def _normalise_number(n: str | None) -> str | None:
    if not n or n.lower() in ("anonymous", "unknown", "private", "restricted"):
        return None
    digits = "".join(c for c in n if c.isdigit())
    return "0" + digits[2:] if digits.startswith("44") else digits


def _redact(text: str) -> str:
    """Phone numbers in stored transcripts keep only their last three digits."""
    import re

    # Digits may be grouped with spaces or commas when read aloud ("0 7 7 9 9, 1 2 3, 4 5 6").
    return re.sub(r"\d(?:[ ,]*\d){6,}", lambda m: "•••" + re.sub(r"\D", "", m.group(0))[-3:], text)


def create_app(
    phone: Phone, *, auth_token: str | None = None, admin_token: str | None = None, public_url: str | None = None
) -> FastAPI:
    app = FastAPI(title="AI Voice Receptionist")
    auth_token = auth_token if auth_token is not None else os.environ.get("TWILIO_AUTH_TOKEN")
    admin_token = admin_token if admin_token is not None else os.environ.get("RECEPTIONIST_ADMIN_TOKEN")

    async def form(request: Request) -> dict[str, str]:
        data = {k: str(v) for k, v in (await request.form()).items()}
        if auth_token:
            url = (public_url.rstrip("/") + request.url.path) if public_url else str(request.url)
            expected = twilio_signature(auth_token, url, data)
            if not hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")):
                raise HTTPException(403, "bad signature")
        return data

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "brain": getattr(phone.brain, "name", "?")}

    @app.post("/voice/incoming")
    async def incoming(request: Request) -> Response:
        data = await form(request)
        return Response(phone.start(data.get("CallSid", ""), data.get("From")), media_type="application/xml")

    @app.post("/voice/turn")
    async def turn(request: Request) -> Response:
        data = await form(request)
        xml = phone.turn(data.get("CallSid", ""), data.get("SpeechResult", ""), float(data.get("Confidence") or 0))
        return Response(xml, media_type="application/xml")

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request) -> str:
        token = request.query_params.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not admin_token or not hmac.compare_digest(token, admin_token):
            raise HTTPException(401, "admin token required")
        return admin_page(phone)

    return app


ADMIN_CSS = """
:root{--bg:#f6f7fb;--panel:#fff;--ink:#141a2b;--muted:#5b6478;--line:#e2e6ef;--brand:#2456d6;--ok:#13795b;--warn:#a15c00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,sans-serif}
header{background:var(--ink);color:#fff;padding:18px 32px;display:flex;align-items:center;gap:14px}
header b{font-size:18px}header span{color:#aab4cc}main{max-width:1240px;margin:0 auto;padding:28px 32px;display:grid;
grid-template-columns:1.25fr 1fr;gap:24px}section{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:18px 20px}h2{font-size:16px;margin:0 0 12px}table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);
font-weight:500;font-size:12px}.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:#e9efff;
color:var(--brand);font-size:12px}.chip.t{background:#fff3e0;color:var(--warn)}.chip.b{background:#e6f6ef;color:var(--ok)}
.turn{margin:4px 0;padding:6px 10px;border-radius:10px;max-width:92%}.c{background:#eef1f7}.r{background:#e9efff;
margin-left:auto}.call{border-top:1px solid var(--line);padding:10px 0}.muted{color:var(--muted);font-size:13px}
.stack{display:grid;gap:24px;align-content:start}
"""


def admin_page(phone: Phone) -> str:
    rows = phone.db.execute(
        "SELECT call_id, caller, started, outcome, transcript FROM calls ORDER BY started DESC LIMIT 20"
    ).fetchall()
    calls_html = ""
    for cid, caller, started, outcome, transcript in rows:
        chips = "".join(
            f'<span class="chip {_chip(o.strip())}">{html.escape(o.strip())}</span> ' for o in outcome.split(",")
        )
        turns = "".join(
            f'<div class="turn {"c" if r == "caller" else "r"}">{html.escape(t)}</div>'
            for r, t in json.loads(transcript)
        )
        calls_html += (
            f'<div class="call"><div><b>{html.escape(caller)}</b> <span class="muted">'
            f"{html.escape(started[11:16])} · {html.escape(cid)}</span> {chips}</div>{turns}</div>"
        )
    upcoming = [b for b in phone.desk.calendar.all_bookings() if b.status == "booked"]
    biz = phone.business
    book_rows = "".join(
        f"<tr><td>{html.escape(say_day(b.start).split(' the ')[0][:3])} {b.start.day}</td><td>{say_time(b.start)}</td>"
        f"<td>{html.escape(biz.service(b.service).name)}</td><td>{html.escape(biz.practitioner(b.practitioner).name)}</td>"
        f"<td>{html.escape(b.name)}</td><td>{b.ref}</td></tr>"
        for b in upcoming[:14]
    )
    msg_rows = "".join(
        f"<tr><td>{html.escape(m.name)}</td><td>{html.escape(m.text)}</td><td>{mask(m.phone)}</td></tr>"
        for m in phone.desk.messages[-8:]
    )
    return (
        f"<!doctype html><html lang='en'><meta charset='utf-8'><title>Receptionist</title><style>{ADMIN_CSS}</style>"
        f"<header><b>{html.escape(biz.name)}</b><span>AI receptionist · {html.escape(getattr(phone.brain, 'name', ''))}"
        f" brain</span></header><main><section><h2>Recent calls</h2>{calls_html or '<p class=muted>No calls yet.</p>'}"
        f"</section><div class='stack'><section><h2>Upcoming bookings</h2><table><tr><th>Day</th><th>Time</th>"
        f"<th>Service</th><th>With</th><th>Name</th><th>Ref</th></tr>{book_rows}</table></section><section>"
        f"<h2>Messages for the team</h2><table><tr><th>From</th><th>Message</th><th>Phone</th></tr>{msg_rows}"
        "</table></section></div></main></html>"
    )


def _chip(outcome: str) -> str:
    if outcome in ("booked", "moved", "cancelled"):
        return "b"
    return "t" if "transfer" in outcome else ""


def build(db_path: str = "receptionist.db", brain: str = "rules", business: Business | None = None) -> Phone:
    from .business import demo

    biz = business or demo()
    cal = Calendar(biz, db_path)
    if brain == "claude":
        from .agent import ClaudeBrain

        return Phone(biz, cal, ClaudeBrain)
    return Phone(biz, cal, RuleBrain)
