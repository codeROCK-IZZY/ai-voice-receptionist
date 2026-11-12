"""Understanding what a caller said, from speech-recognition text.

Speech recognition writes what it hears: "half past two", "oh seven seven double nine", "the fourteenth", "Tuesday
afternoon". This module turns that into an intent and typed values. It is used directly by the offline brain and,
for yes/no detection, by the guard that stops any brain from booking without the caller's confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta

from .business import Business

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
UNITS = {
    "zero": 0,
    "oh": 0,
    "o": 0,
    "nought": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "thirtieth": 30,
}

YES = re.compile(
    r"\b(yes|yeah|yep|yup|yes please|correct|that's right|that is right|right|perfect|sounds good|great|sure|"
    r"please do|go ahead|ok|okay|book it|lovely|absolutely|definitely|fine|that's fine|that works|that's it)\b"
)
NO = re.compile(r"\b(no|nope|nah|not right|wrong|that's not|that is not|incorrect|actually|wait|hang on|not quite)\b")
WINDOWS = {
    "first thing": (time(8), time(10)),
    "early morning": (time(8), time(10)),
    "morning": (time(8), time(12)),
    "lunchtime": (time(12), time(14)),
    "lunch time": (time(12), time(14)),
    "afternoon": (time(12), time(17)),
    "evening": (time(17), time(20)),
    "after work": (time(17), time(20)),
}


@dataclass
class Parsed:
    intent: str
    service: str | None = None
    day: date | None = None
    at: time | None = None
    window: tuple[time | None, time | None] | None = None
    name: str | None = None
    phone: str | None = None
    ref: str | None = None
    choice: int | None = None  # "the second one" -> 1 (zero-based)
    faq: str | None = None
    yes: bool = False
    no: bool = False
    extras: dict[str, str] = field(default_factory=dict)


def normalise(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[,.!?;:]+(?=\s|$)", " ", t)
    t = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", t)  # 14th -> 14
    t = re.sub(r"\s+", " ", t).strip()
    return t


def words_to_number(tokens: list[str]) -> int | None:
    """ "twenty three" -> 23, "fourteen" -> 14, "14" -> 14."""
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0].isdigit():
        return int(tokens[0])
    total = 0
    for tok in tokens:
        if tok in TENS:
            total += TENS[tok]
        elif tok in UNITS:
            total += UNITS[tok]
        else:
            return None
    return total


ORD = "|".join(ORDINALS)
DAYNUM = rf"(\d{{1,2}}|(?:twenty |thirty )?(?:{ORD}))"


def _day_number(t: str, month: str | None) -> int | None:
    patterns = [rf"\bthe {DAYNUM}\b"]
    if month:
        patterns = [rf"\b{DAYNUM} (?:of )?{month}\b", rf"\b{month} (?:the )?{DAYNUM}\b", *patterns]
    for pat in patterns:
        m = re.search(pat, t)
        if not m:
            continue
        raw = m.group(1)
        if raw.isdigit():
            n = int(raw)
        else:
            parts = raw.split()
            n = ORDINALS[parts[-1]] + (TENS[parts[0]] if len(parts) == 2 else 0)
        if 1 <= n <= 31:
            return n
    return None


def _explicit_date(t: str, today: date) -> date | None:
    t = re.sub(r"\bmay (i|we|be|have|you)\b", " ", t)  # "may I book" is not the month of May
    month = next((i for i, mo in enumerate(MONTHS, 1) if re.search(rf"\b{mo}\b", t)), None)
    # "the second one" picks an offered option; it is not the 2nd of the month.
    t = re.sub(rf"\bthe ({ORD}) (one|option|slot|time)\b", " ", t)
    if month or re.search(rf"\bthe {DAYNUM}\b", t):
        n = _day_number(t, MONTHS[month - 1] if month else None)
        if n:
            year, mon = today.year, month or today.month
            if not month and n < today.day:
                mon += 1
            if mon > 12:
                mon, year = 1, year + 1
            try:
                d = date(year, mon, n)
            except ValueError:
                return None
            if d < today:
                d = date(year + 1, mon, n)
            return d
    return None


def parse_date(text: str, today: date) -> date | None:
    t = normalise(text)
    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b", t):
        return today + timedelta(days=1)
    if re.search(r"\b(today|this afternoon|this morning|this evening|tonight)\b", t):
        return today
    explicit = _explicit_date(t, today)
    if explicit:
        return explicit  # "Thursday the 19th": the number decides, not the weekday
    m = re.search(r"\b(next|this|on|coming)?\s*(" + "|".join(WEEKDAYS) + r")\b", t)
    if m:
        target = WEEKDAYS.index(m.group(2))
        ahead = (target - today.weekday()) % 7
        if ahead == 0 and m.group(1) == "next":
            ahead = 7
        if m.group(1) == "next" and ahead < 7 and re.search(r"\bnext week\b", t):
            ahead += 7
        return today + timedelta(days=ahead)
    if re.search(r"\bnext week\b", t):
        return today + timedelta(days=7 - today.weekday())
    return None


def _clock(hour: int, minute: int, meridiem: str | None) -> time | None:
    if meridiem in ("pm", "p m", "in the afternoon", "in the evening") and hour < 12:
        hour += 12
    elif meridiem in ("am", "a m", "in the morning") and hour == 12:
        hour = 0
    elif meridiem is None and 1 <= hour <= 7:
        hour += 12  # a clinic is not open at 3 am: "three" means 3 pm
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return time(hour, minute)


NUM = r"(\d{1,2}|" + "|".join([*list(UNITS)[4:], "twenty", "thirty", "forty", "fifty"]) + r")"
MER = r"(?:\s*(am|pm|a m|p m|in the morning|in the afternoon|in the evening|o'clock))?"


def _n(s: str) -> int:
    return int(s) if s.isdigit() else words_to_number(s.split()) or 0


def parse_time(text: str) -> tuple[time | None, tuple[time | None, time | None] | None]:
    t = normalise(text)
    if re.search(r"\b(noon|midday)\b", t):
        return time(12), None
    m = re.search(rf"\b(half past|quarter past|quarter to) {NUM}{MER}", t)
    if m:
        h = _n(m.group(2))
        mer = m.group(3) if m.group(3) != "o'clock" else None
        if m.group(1) == "quarter to":
            base = _clock(h, 0, mer)
            if base:
                return (time(base.hour - 1, 45) if base.hour > 0 else None), None
        mins = 30 if m.group(1) == "half past" else 15
        return _clock(h, mins, mer), None
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|a m|p m)?\b", t) or re.search(
        r"\b(\d{1,2}) (00|15|30|45)\s*(am|pm|a m|p m)?\b", t
    )
    if m:
        return _clock(int(m.group(1)), int(m.group(2)), m.group(3)), None
    m = re.search(rf"\b{NUM} (fifteen|thirty|forty five|forty-five)\b{MER}", t)
    if m and not re.search(r"\b(the|on) " + re.escape(m.group(1)), t):
        mins = {"fifteen": 15, "thirty": 30}.get(m.group(2), 45)
        mer = m.group(3) if m.group(3) != "o'clock" else None
        return _clock(_n(m.group(1)), mins, mer), None
    m = re.search(rf"\b(?:at|for|around|about|from) {NUM}{MER}(?!\w)", t) or re.search(
        rf"\b{NUM}\s*(am|pm|a m|p m|o'clock|in the morning|in the afternoon|in the evening)\b", t
    )
    if m:
        groups = [g for g in m.groups() if g]
        h = _n(groups[0])
        mer = groups[1] if len(groups) > 1 and groups[1] != "o'clock" else None
        if 1 <= h <= 12:
            return _clock(h, 0, mer), None
    m = re.search(rf"\b(after|before|not before|not after|no earlier than|no later than) {NUM}{MER}", t)
    if m:
        edge = _clock(_n(m.group(2)), 0, m.group(3) if m.group(3) != "o'clock" else None)
        if edge:
            later = m.group(1) in ("after", "not before", "no earlier than")
            return None, ((edge, None) if later else (None, edge))
    for phrase, window in WINDOWS.items():
        if re.search(rf"\b{phrase}\b", t):
            return None, window
    return None, None


SPOKEN_DIGIT = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "nought": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def spoken_digits(text: str) -> str:
    """ "oh seven seven double nine 1 2" -> "0779912"; keeps only the digit run."""
    out: list[str] = []
    repeat = 1
    for tok in normalise(text).replace("-", " ").split():
        if tok in ("double", "triple"):
            repeat = 2 if tok == "double" else 3
            continue
        if tok.startswith("+44"):
            out.append("0")
            tok = tok[3:]
        if tok.isdigit():
            out.append(tok[0] * repeat + tok[1:])
        elif tok in SPOKEN_DIGIT:
            out.append(SPOKEN_DIGIT[tok] * repeat)
        repeat = 1
    return "".join(out)


def parse_phone(text: str) -> str | None:
    digits = spoken_digits(text)
    if digits.startswith("44") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits if re.fullmatch(r"0\d{9,10}", digits) else None


def parse_ref(text: str, expecting: bool) -> str | None:
    t = normalise(text)
    if not expecting and not re.search(r"\b(reference|ref|booking number|confirmation)\b", t):
        return None
    digits = spoken_digits(t)
    return digits[-4:] if len(digits) >= 4 else None


NAME_CUE = re.compile(r"\b(?:my name is|my name's|name's|name is|it's|it is|this is|i'm|i am|call me)\s+(.+)$")
FILLER = {
    "uh",
    "um",
    "erm",
    "er",
    "yeah",
    "yes",
    "so",
    "well",
    "oh",
    "hi",
    "hello",
    "sure",
    "ok",
    "okay",
    "it's",
    "its",
    "my",
    "name",
    "is",
    "the",
    "please",
    "thanks",
    "thank",
    "you",
    "a",
    "booking",
    "for",
    "under",
}


def parse_name(text: str, expecting: bool) -> str | None:
    t = normalise(text)
    m = NAME_CUE.search(t)
    if m:
        cand = m.group(1)
    elif expecting:
        cand = t
    else:
        return None
    toks = [w for w in re.findall(r"[a-z][a-z'\-]*", cand) if w not in FILLER]
    toks = [w for w in toks if w not in WEEKDAYS and w not in MONTHS][:3]
    if not toks or len(toks) > 3:
        return None
    return " ".join(w.capitalize() if "'" not in w else w.title() for w in toks)


def parse_choice(text: str, offered: int) -> int | None:
    t = re.sub(r"\bfirst thing\b", " ", normalise(text))
    # Ordinals that are dates ("the twenty first", "second of December", "November the third") are not choices.
    t = re.sub(rf"\b(twenty|thirty) ({ORD})\b|\b({ORD}) of\b", " ", t)
    t = re.sub(rf"\b({'|'.join(MONTHS)}) (the )?({ORD})\b|\bon the ({ORD})\b", " ", t)
    for word, idx in (
        ("first", 0),
        ("second", 1),
        ("third", 2),
        ("last", offered - 1),
        ("earliest", 0),
        ("latest", offered - 1),
        ("one", None),
    ):
        if word == "one":
            continue
        if re.search(rf"\b(the )?{word}( one| option| slot)?\b", t):
            return idx if idx is not None and 0 <= idx < offered else None
    if offered == 1 and YES.search(t) and not NO.search(t):
        return 0
    return None


def find_service(text: str, business: Business) -> str | None:
    t = normalise(text)
    best: tuple[int, str] | None = None
    for s in business.services:
        for phrase in (s.name, *s.synonyms):
            if re.search(rf"\b{re.escape(phrase)}\b", t) and (best is None or len(phrase) > best[0]):
                best = (len(phrase), s.key)
    return best[1] if best else None


def find_faq(text: str, business: Business) -> str | None:
    t = normalise(text)
    scores = []
    for f in business.faqs:
        hits = sum(1 for k in f.keywords if re.search(rf"\b{re.escape(k)}\b", t))
        if hits:
            scores.append((hits, max(len(k) for k in f.keywords if re.search(rf"\b{re.escape(k)}\b", t)), f.key))
    return max(scores)[2] if scores else None


INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "human",
        re.compile(
            r"\b(real person|a human|speak to (someone|a person|somebody|reception)|talk to (someone|a person"
            r"|somebody)|receptionist|operator|put me through)\b"
        ),
    ),
    (
        "reschedule",
        re.compile(
            r"\b(move|reschedule|re-schedule|change (my|the) (appointment|booking|time|day)|"
            r"different (time|day)|another (time|day)|rearrange|push (it|my appointment) back)\b"
        ),
    ),
    (
        "cancel",
        re.compile(
            r"\b(cancel|cancelling|canceling|can't make (it|my appointment)|cannot make (it|my appointment)|"
            r"won't be able to make)\b"
        ),
    ),
    (
        "message",
        re.compile(
            r"\b(leave a message|take a message|pass (on )?a message|message for|call me back|ring me back"
            r"|get back to me)\b"
        ),
    ),
    (
        "book",
        re.compile(
            r"\b(book|booking|appointment|make an|come in|see someone|see a physio|available|availability|"
            r"slot|slots|space|get in|anything (on|for|at|first|free|this|next|tomorrow)|any (space|slots|"
            r"times|availability)|free (on|at|this|next|tomorrow))\b"
        ),
    ),
    (
        "goodbye",
        re.compile(
            r"\b(bye|goodbye|that's all|that is all|nothing else|no thanks|no thank you|that's everything|"
            r"that's it thanks|all good)\b"
        ),
    ),
]


def understand(text: str, business: Business, today: date, expecting: str | None = None, offered: int = 0) -> Parsed:
    """Intent and values from one caller utterance. `expecting` names the value the receptionist just asked for."""
    t = normalise(text)
    p = Parsed(intent="unknown")
    if any(w in t for w in business.emergency_words):
        p.intent = "emergency"
        return p
    p.service = find_service(t, business)
    p.day = parse_date(t, today)
    p.at, p.window = parse_time(t)
    p.phone = parse_phone(t) if (expecting == "phone" or re.search(r"\b(number|mobile|phone)\b", t)) else None
    p.ref = parse_ref(t, expecting == "ref")
    p.name = parse_name(text, expecting == "name")
    p.choice = parse_choice(t, offered) if offered else None
    p.yes = bool(YES.search(t)) and not re.search(r"\b(no|not|don't|nope)\b", t)
    p.no = bool(NO.search(t)) and not p.yes
    faq = find_faq(t, business)
    for intent, pattern in INTENT_PATTERNS:
        if pattern.search(t):
            p.intent = intent
            break
    question = re.search(r"\b(do you|is there|what|how|where|when|can i|are you|does|is it)\b", t)
    when = p.day or p.at or p.window
    # "Saturday at 3 pm" is a time request, not a question about opening hours.
    if faq and (
        (p.intent in ("unknown", "goodbye") and (question or not when))
        or (question and p.intent == "book" and not when)
    ):
        p.intent, p.faq = "faq", faq
    elif faq:
        p.faq = faq
    if p.intent == "unknown" and p.service:
        p.intent = "book"
    if p.intent == "goodbye" and (p.service or p.day):
        p.intent = "book"
    return p
