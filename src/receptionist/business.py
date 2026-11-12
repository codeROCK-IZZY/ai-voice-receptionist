"""The business the receptionist answers for: services, prices, staff hours and FAQs.

Everything the receptionist says about the business comes from here. Nothing about prices, hours or policies is
generated, so it cannot be invented on a call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    minutes: int
    price_pence: int
    synonyms: tuple[str, ...] = ()

    @property
    def price_text(self) -> str:
        pounds, pence = divmod(self.price_pence, 100)
        return f"£{pounds}" if pence == 0 else f"£{pounds}.{pence:02d}"


@dataclass(frozen=True)
class Practitioner:
    key: str
    name: str
    services: tuple[str, ...]
    # weekday (0 = Monday) -> (start, end); missing days are days off
    hours: dict[int, tuple[time, time]]


@dataclass(frozen=True)
class Faq:
    key: str
    answer: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Business:
    name: str
    timezone: ZoneInfo
    phone_for_humans: str
    services: tuple[Service, ...]
    practitioners: tuple[Practitioner, ...]
    faqs: tuple[Faq, ...]
    cancellation_hours: int
    emergency_words: tuple[str, ...] = field(default=())

    def service(self, key: str) -> Service:
        for s in self.services:
            if s.key == key:
                return s
        raise KeyError(key)

    def practitioner(self, key: str) -> Practitioner:
        for p in self.practitioners:
            if p.key == key:
                return p
        raise KeyError(key)

    def faq(self, key: str) -> Faq:
        for f in self.faqs:
            if f.key == key:
                return f
        raise KeyError(key)


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def load(path: str | Path) -> Business:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Business(
        name=raw["name"],
        timezone=ZoneInfo(raw["timezone"]),
        phone_for_humans=raw["phone_for_humans"],
        services=tuple(
            Service(s["key"], s["name"], s["minutes"], s["price_pence"], tuple(s.get("synonyms", [])))
            for s in raw["services"]
        ),
        practitioners=tuple(
            Practitioner(
                p["key"],
                p["name"],
                tuple(p["services"]),
                {int(d): (_t(a), _t(b)) for d, (a, b) in p["hours"].items()},
            )
            for p in raw["practitioners"]
        ),
        faqs=tuple(Faq(f["key"], f["answer"], tuple(f["keywords"])) for f in raw["faqs"]),
        cancellation_hours=raw["cancellation_hours"],
        emergency_words=tuple(raw.get("emergency_words", [])),
    )


DEMO = Path(__file__).with_name("demo_business.json")


def demo() -> Business:
    return load(DEMO)
