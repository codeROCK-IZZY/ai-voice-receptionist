from __future__ import annotations

import random
from datetime import date, datetime

import pytest

from receptionist.business import Business, demo
from receptionist.calendar import Calendar
from receptionist.desk import Desk, Session

TODAY = date(2026, 11, 11)  # a Wednesday


@pytest.fixture
def business() -> Business:
    return demo()


@pytest.fixture
def now(business: Business) -> datetime:
    return datetime(2026, 11, 11, 9, 0, tzinfo=business.timezone)


@pytest.fixture
def calendar(business: Business) -> Calendar:
    return Calendar(business, refs=random.Random(1))


@pytest.fixture
def desk(business: Business, calendar: Calendar) -> Desk:
    return Desk(business, calendar)


@pytest.fixture
def session(now: datetime) -> Session:
    return Session("call-1", "07700900142", now)
