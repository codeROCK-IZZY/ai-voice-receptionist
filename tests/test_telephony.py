"""Twilio webhooks over HTTP, signature checks and the admin page."""

from __future__ import annotations

import random
from datetime import datetime

from fastapi.testclient import TestClient

from receptionist.business import Business
from receptionist.calendar import Calendar
from receptionist.dialog import RuleBrain
from receptionist.telephony import Phone, create_app, twilio_signature

TOKEN = "twilio-test-token"
PUBLIC = "https://receptionist.example"


def make(business: Business) -> tuple[TestClient, Phone]:
    clock = datetime(2026, 11, 11, 9, 0, tzinfo=business.timezone)
    phone = Phone(business, Calendar(business, refs=random.Random(3)), RuleBrain, clock=lambda: clock)
    app = create_app(phone, auth_token=TOKEN, admin_token="admin-secret", public_url=PUBLIC)
    return TestClient(app), phone


def post(client: TestClient, path: str, data: dict[str, str]) -> str:
    sig = twilio_signature(TOKEN, PUBLIC + path, data)
    r = client.post(path, data=data, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    return r.text


def test_signature_is_required(business: Business) -> None:
    client, _ = make(business)
    r = client.post(
        "/voice/incoming", data={"CallSid": "CA1", "From": "+447700900142"}, headers={"X-Twilio-Signature": "forged"}
    )
    assert r.status_code == 403


def test_a_booking_call_over_twiml(business: Business) -> None:
    client, phone = make(business)
    xml = post(client, "/voice/incoming", {"CallSid": "CA1", "From": "+447700900142"})
    assert '<Gather input="speech"' in xml and "Brightside Physiotherapy" in xml
    for said in ["I'd like to book a follow up on Wednesday the 18th", "the first one", "Sam Jones", "yes"]:
        xml = post(client, "/voice/turn", {"CallSid": "CA1", "SpeechResult": said, "Confidence": "0.92"})
    assert "Shall I book that?" in xml
    xml = post(client, "/voice/turn", {"CallSid": "CA1", "SpeechResult": "yes please", "Confidence": "0.9"})
    assert "Your reference is" in xml
    xml = post(client, "/voice/turn", {"CallSid": "CA1", "SpeechResult": "no thanks bye", "Confidence": "0.9"})
    assert "<Hangup/>" in xml
    assert [b.name for b in phone.desk.calendar.all_bookings()] == ["Sam Jones"]


def test_low_confidence_and_silence(business: Business) -> None:
    client, _ = make(business)
    post(client, "/voice/incoming", {"CallSid": "CA2", "From": "anonymous"})
    xml = post(client, "/voice/turn", {"CallSid": "CA2", "SpeechResult": "mumble", "Confidence": "0.1"})
    assert "isn't very clear" in xml
    for _ in range(2):
        xml = post(client, "/voice/turn", {"CallSid": "CA2", "SpeechResult": "", "Confidence": "0"})
    assert "<Gather" in xml
    xml = post(client, "/voice/turn", {"CallSid": "CA2", "SpeechResult": "", "Confidence": "0"})
    assert "<Hangup/>" in xml


def test_emergency_dials_a_person(business: Business) -> None:
    client, _ = make(business)
    post(client, "/voice/incoming", {"CallSid": "CA3", "From": "+447700900999"})
    xml = post(client, "/voice/turn", {"CallSid": "CA3", "SpeechResult": "I have chest pain", "Confidence": "0.9"})
    assert "<Dial>" in xml and "999" in xml


def test_admin_needs_a_token_and_masks_numbers(business: Business) -> None:
    client, _ = make(business)
    post(client, "/voice/incoming", {"CallSid": "CA4", "From": "+447700900142"})
    post(client, "/voice/turn", {"CallSid": "CA4", "SpeechResult": "can I speak to a real person", "Confidence": "0.9"})
    assert client.get("/admin").status_code == 401
    page = client.get("/admin", params={"token": "admin-secret"}).text
    assert "•••• 142" in page and "07700900142" not in page


def test_stored_transcripts_mask_numbers_however_they_were_read() -> None:
    from receptionist.telephony import _redact

    assert _redact("the number 0 7 7 9 9, 1 2 3, 4 5 6?") == "the number •••456?"
    assert _redact("call 07700900142 now") == "call •••142 now"
    assert _redact("your reference is 4 0 9 0") == "your reference is 4 0 9 0"
