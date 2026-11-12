from __future__ import annotations

import random
from datetime import datetime

from receptionist import evaluate
from receptionist.business import Business
from receptionist.calendar import Calendar
from receptionist.speech import Channel


def test_small_run_is_safe_and_beats_the_ivr(business: Business) -> None:
    rules = evaluate.run_config(business, "rules", "dev", 0.0, 40, seed=11)
    ivr = evaluate.run_config(business, "ivr", "dev", 0.0, 40, seed=11)
    assert rules.success > ivr.success
    assert rules.wrong_changes == 0 and rules.double_bookings == 0 and rules.blocked == 0


def test_runs_are_reproducible(business: Business) -> None:
    a = evaluate.run_config(business, "rules", "heldout", 0.2, 30, seed=5)
    b = evaluate.run_config(business, "rules", "heldout", 0.2, 30, seed=5)
    assert a.by_kind == b.by_kind


def test_guards_stop_every_unsafe_action(business: Business) -> None:
    result = evaluate.guard_ablation(business, trials=3, seed=1)
    assert sum(result["guards on"].values()) == 0
    assert all(n == 3 for n in result["guards off"].values())


def test_booking_race(business: Business) -> None:
    race = evaluate.booking_race(business, rounds=5)
    assert race == {"rounds": 5, "bookings": 5, "told_slot_taken": 5, "double_bookings": 0}


def _wer(ref: list[str], hyp: list[str]) -> float:
    """Word error rate: word-level edit distance over the reference length."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


def test_noise_channel_hits_the_requested_word_error_rate() -> None:
    words = ("book a massage on tuesday at four please " * 100).split()
    for target in (0.1, 0.2):
        measured = _wer(words, Channel(target, seed=1).hear(" ".join(words)).split())
        assert abs(measured - target) < 0.04, (target, measured)
    assert Channel(0.0).hear("exactly this") == "exactly this"


def test_clone_is_independent(business: Business) -> None:
    now = datetime(2026, 11, 11, 9, tzinfo=business.timezone)
    base = Calendar(business)
    copy = base.clone(random.Random(1))
    copy.book(copy.free_slots("followup", now.date().replace(day=18), now=now)[0], "A", "07000000000")
    assert base.all_bookings() == [] and len(copy.all_bookings()) == 1
