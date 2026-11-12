"""Appointment book in SQLite: free slots, bookings, cancellations and moves.

Double booking is prevented inside the database transaction (BEGIN IMMEDIATE takes the write lock before the
overlap check), so two calls racing for the same slot cannot both win, whichever brain is driving the call.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from .business import Business

GRID_MINUTES = 15


@dataclass(frozen=True)
class Slot:
    practitioner: str
    service: str
    start: datetime  # aware, business time zone

    @property
    def key(self) -> str:
        return f"{self.practitioner}|{self.service}|{self.start.isoformat()}"


@dataclass(frozen=True)
class Booking:
    ref: str
    practitioner: str
    service: str
    start: datetime
    name: str
    phone: str
    status: str


class SlotTaken(Exception):
    """The slot was booked by someone else between offering and booking it."""


class NotFound(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    ref TEXT PRIMARY KEY,
    practitioner TEXT NOT NULL,
    service TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',
    created_utc TEXT NOT NULL,
    call_id TEXT
);
CREATE INDEX IF NOT EXISTS bookings_practitioner ON bookings (practitioner, start_utc);
"""


class Calendar:
    def __init__(self, business: Business, path: str = ":memory:", *, refs: random.Random | None = None) -> None:
        # Booking references come from the OS random source; the evaluation passes a seeded generator so
        # its runs are reproducible.
        self._refs: random.Random = refs or random.SystemRandom()
        self.business = business
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        # One connection is shared by the web server's worker threads: every use of it is serialised, so two
        # transactions can never interleave on it (BEGIN IMMEDIATE still serialises separate processes).
        self.lock = threading.RLock()
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def clone(self, refs: random.Random | None = None) -> Calendar:
        """An independent in-memory copy (used to start every simulated call from the same diary)."""
        copy = Calendar(self.business, refs=refs)
        self.db.backup(copy.db)
        return copy

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    # ------------------------------------------------------------------ queries
    def _busy(self, practitioner: str, start: datetime, end: datetime) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM bookings WHERE practitioner = ? AND status = 'booked' AND start_utc < ? AND end_utc > ?",
                (practitioner, _utc(end), _utc(start)),
            ).fetchone()
        return row is not None

    def free_slots(
        self,
        service: str,
        day: date,
        *,
        earliest: time | None = None,
        latest: time | None = None,
        practitioner: str | None = None,
        now: datetime,
    ) -> list[Slot]:
        """Every bookable start time on one day, earliest first, at most one per practitioner per time."""
        svc = self.business.service(service)
        tz = self.business.timezone
        out: list[Slot] = []
        for p in self.business.practitioners:
            if service not in p.services or (practitioner and p.key != practitioner):
                continue
            hours = p.hours.get(day.weekday())
            if not hours:
                continue
            t = datetime.combine(day, hours[0], tz)
            close = datetime.combine(day, hours[1], tz)
            while t + timedelta(minutes=svc.minutes) <= close:
                ok_window = (earliest is None or t.time() >= earliest) and (latest is None or t.time() < latest)
                if ok_window and t > now + timedelta(hours=1) and not self._busy(p.key, t, t + _dur(svc.minutes)):
                    out.append(Slot(p.key, service, t))
                t += timedelta(minutes=GRID_MINUTES)
        out.sort(key=lambda s: (s.start, s.practitioner))
        return out

    def is_free(self, slot: Slot) -> bool:
        svc = self.business.service(slot.service)
        p = self.business.practitioner(slot.practitioner)
        hours = p.hours.get(slot.start.weekday())
        if slot.service not in p.services or not hours:
            return False
        start = slot.start.astimezone(self.business.timezone)
        end = start + _dur(svc.minutes)
        if start.time() < hours[0] or end.time() > hours[1] or end.date() != start.date():
            return False
        return not self._busy(slot.practitioner, start, end)

    # ------------------------------------------------------------------ changes
    def book(self, slot: Slot, name: str, phone: str, call_id: str | None = None) -> Booking:
        svc = self.business.service(slot.service)
        end = slot.start + _dur(svc.minutes)
        with self._tx() as db:
            if not self.is_free(slot):
                raise SlotTaken(slot.key)
            ref = self._new_ref()
            db.execute(
                "INSERT INTO bookings (ref, practitioner, service, start_utc, end_utc, name, phone, created_utc,"
                " call_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ref,
                    slot.practitioner,
                    slot.service,
                    _utc(slot.start),
                    _utc(end),
                    name,
                    phone,
                    _utc(datetime.now().astimezone()),
                    call_id,
                ),
            )
        return Booking(ref, slot.practitioner, slot.service, slot.start, name, phone, "booked")

    def _new_ref(self) -> str:
        while True:
            ref = "".join(self._refs.choice("0123456789") for _ in range(4))
            if not self.db.execute("SELECT 1 FROM bookings WHERE ref = ?", (ref,)).fetchone():
                return ref

    def find(self, ref: str) -> Booking:
        with self.lock:
            row = self.db.execute(
                "SELECT ref, practitioner, service, start_utc, name, phone, status FROM bookings WHERE ref = ?", (ref,)
            ).fetchone()
        if not row:
            raise NotFound(ref)
        return self._row(row)

    def find_by_phone(self, phone: str, now: datetime) -> list[Booking]:
        with self.lock:
            rows = self.db.execute(
                "SELECT ref, practitioner, service, start_utc, name, phone, status FROM bookings"
                " WHERE phone = ? AND status = 'booked' AND start_utc > ? ORDER BY start_utc",
                (phone, _utc(now)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def cancel(self, ref: str) -> Booking:
        with self._tx() as db:
            b = self.find(ref)
            if b.status != "booked":
                raise NotFound(ref)
            db.execute("UPDATE bookings SET status = 'cancelled' WHERE ref = ?", (ref,))
        return Booking(b.ref, b.practitioner, b.service, b.start, b.name, b.phone, "cancelled")

    def move(self, ref: str, slot: Slot) -> Booking:
        """Move a booking in one transaction: the reference stays the same, and a failed move changes nothing."""
        svc = self.business.service(slot.service)
        with self._tx() as db:
            b = self.find(ref)
            if b.status != "booked":
                raise NotFound(ref)
            # Free the old time first so a move to an overlapping time with the same practitioner is allowed.
            db.execute("UPDATE bookings SET status = 'moving' WHERE ref = ?", (ref,))
            if not self.is_free(slot):
                raise SlotTaken(slot.key)
            db.execute(
                "UPDATE bookings SET practitioner = ?, service = ?, start_utc = ?, end_utc = ?, status = 'booked'"
                " WHERE ref = ?",
                (slot.practitioner, slot.service, _utc(slot.start), _utc(slot.start + _dur(svc.minutes)), ref),
            )
        return self.find(ref)

    def all_bookings(self) -> list[Booking]:
        with self.lock:
            rows = self.db.execute(
                "SELECT ref, practitioner, service, start_utc, name, phone, status FROM bookings ORDER BY start_utc"
            ).fetchall()
        return [self._row(r) for r in rows]

    def overlaps(self) -> int:
        """Pairs of live bookings for the same practitioner that overlap (must always be 0)."""
        row = self.db.execute(
            "SELECT COUNT(*) FROM bookings a JOIN bookings b ON a.practitioner = b.practitioner AND a.ref < b.ref"
            " WHERE a.status = 'booked' AND b.status = 'booked' AND a.start_utc < b.end_utc AND b.start_utc < a.end_utc"
        ).fetchone()
        return int(row[0])

    def _row(self, r: tuple[str, ...]) -> Booking:
        start = datetime.fromisoformat(r[3]).astimezone(self.business.timezone)
        return Booking(r[0], r[1], r[2], start, r[4], r[5], r[6])


def _dur(minutes: int) -> timedelta:
    return timedelta(minutes=minutes)


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
