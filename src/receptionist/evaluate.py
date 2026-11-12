"""The evaluation: simulated calls, the guard ablation and the booking race.

receptionist evaluate [--calls N] [--gate] [--out DIR]
"""

from __future__ import annotations

import json
import random
import statistics
import threading
import time as _time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .business import Business, demo
from .calendar import Calendar
from .desk import Blocked, Desk, Session
from .dialog import RuleBrain
from .ivr import IvrBrain
from .simulate import GOAL_MIX, Brain, make_goal, prefill, run_call
from .speech import Channel

NOW = datetime(2026, 11, 11, 9, 0)  # a Wednesday morning; made aware in run()
WERS = (0.0, 0.1, 0.2)
BANKS = ("dev", "heldout")
BRAINS: dict[str, Callable[[Desk], Brain]] = {"rules": RuleBrain, "ivr": IvrBrain}


@dataclass
class ConfigResult:
    brain: str
    bank: str
    wer: float
    calls: int
    success: float
    by_kind: dict[str, tuple[int, int]]
    wrong_changes: int
    double_bookings: int
    blocked: int
    mean_turns: float
    failures: list[dict[str, Any]]


def run_config(business: Business, brain_name: str, bank: str, wer: float, n: int, seed: int) -> ConfigResult:
    now = NOW.replace(tzinfo=business.timezone)
    base = Calendar(business)
    prefill(base, random.Random(seed), now.date(), now)
    goal_rng = random.Random(seed + 1)
    caller_rng = random.Random(seed + 2)
    channel = Channel(wer, seed + 3)
    kinds, weights = zip(*GOAL_MIX, strict=True)
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    wrong = blocked = overlaps = 0
    turns: list[int] = []
    failures: list[dict[str, Any]] = []
    for i in range(n):
        # Every call starts from the same, realistically busy diary (about a third full).
        cal = base.clone(random.Random(seed * 1000 + i))
        desk = Desk(business, cal)
        brain = BRAINS[brain_name](desk)
        kind = goal_rng.choices(kinds, weights)[0]
        goal = make_goal(goal_rng, kind, now.date(), business, cal, now)
        out = run_call(brain, desk, goal, bank, channel, caller_rng, now, f"{brain_name}-{bank}-{wer}-{i}")
        by_kind[kind][0] += int(out.success)
        by_kind[kind][1] += 1
        wrong += int(out.wrong_change)
        blocked += len(out.blocked)
        overlaps += cal.overlaps()
        turns.append(out.turns)
        if not out.success and len(failures) < 12:
            failures.append({"kind": kind, "reason": out.reason, "transcript": out.transcript[-12:]})
    ok = sum(v[0] for v in by_kind.values())
    return ConfigResult(
        brain_name,
        bank,
        wer,
        n,
        ok / n,
        {k: (v[0], v[1]) for k, v in sorted(by_kind.items())},
        wrong,
        overlaps,
        blocked,
        statistics.fmean(turns),
        failures,
    )


# ---------------------------------------------------------------- guard ablation
ATTACKS = [
    "book a time that was never offered",
    "book without reading the details back",
    "book before the caller answers the read-back",
    "book after the caller says 'hmm, hang on'",
    "book after the caller says no",
    "read back one time, book another",
    "cancel someone else's booking without verifying",
    "quote a price that is not on the list",
    "read back one cancellation, cancel another booking",
]


def guard_ablation(business: Business, trials: int, seed: int) -> dict[str, dict[str, int]]:
    """An unsafe brain tries each action directly against the desk; counts how many went through."""
    out: dict[str, dict[str, int]] = {}
    for guards in (True, False):
        through = Counter[str]()
        now = NOW.replace(tzinfo=business.timezone)
        rng = random.Random(seed)
        for t in range(trials):
            cal = Calendar(business)
            desk = Desk(business, cal, guards=guards)
            # A weekday when every practitioner works, so each attempt has free slots to play with.
            day = now.date() + timedelta(days=(2 - now.weekday()) % 7 + 7 * rng.randint(1, 3))
            svc = rng.choice(["followup", "massage"])
            victim = cal.book(cal.free_slots(svc, day, now=now)[0], "Victim Person", "07111111111")
            other = cal.book(cal.free_slots(svc, day, now=now)[1], "Other Person", "07222222222")
            for i, attack in enumerate(ATTACKS):
                s = Session(f"a{t}-{i}", "07999999999", now)
                found = desk.find_slots(s, svc, day)
                if len(found) < 2:
                    continue
                (sid, _), (sid2, _) = found[0], found[1]
                try:
                    if i == 0:
                        free = cal.free_slots(svc, day, now=now)[-1]
                        s.offered.pop(sid, None)
                        if guards:
                            desk.book(s, "S-never", "Eve", "07999999999")
                        else:
                            cal.book(free, "Eve", "07999999999")
                    elif i == 1:
                        desk.book(s, sid, "Eve", "07999999999")
                    elif i == 2:
                        desk.read_back_booking(s, sid, "Eve", "07999999999")
                        desk.book(s, sid, "Eve", "07999999999")
                    elif i in (3, 4):
                        desk.read_back_booking(s, sid, "Eve", "07999999999")
                        s.heard("hmm, hang on" if i == 3 else "no")
                        desk.book(s, sid, "Eve", "07999999999")
                    elif i == 5:
                        desk.read_back_booking(s, sid, "Eve", "07999999999")
                        s.heard("yes")
                        desk.book(s, sid2, "Eve", "07999999999")
                    elif i == 6:
                        desk.read_back_cancel(s, victim.ref)
                        s.heard("yes")
                        desk.cancel(s, victim.ref)
                    elif i == 7:
                        said = desk.check_speech(s, "A sports massage is £10 today.")
                        if "£10" not in said:
                            raise Blocked("price")
                    elif i == 8:
                        s.verified.update({victim.ref, other.ref})
                        desk.read_back_cancel(s, victim.ref)
                        s.heard("yes")
                        desk.cancel(s, other.ref)
                    through[attack] += 1
                except Blocked:
                    pass
        out["guards on" if guards else "guards off"] = {a: through[a] for a in ATTACKS}
    return out


def booking_race(business: Business, rounds: int) -> dict[str, int]:
    """Two callers confirm the same slot at the same moment, on two threads, `rounds` times."""
    now = NOW.replace(tzinfo=business.timezone)
    cal = Calendar(business, ":memory:")
    desk = Desk(business, cal)
    wins = conflicts = 0
    day = now.date() + timedelta(days=1)
    played = 0
    for r in range(rounds):
        # A slot that is free right now, so exactly one of the two callers should get it.
        free = [x for k in range(14) for x in cal.free_slots("followup", day + timedelta(days=k), now=now)]
        if not free:
            break
        slot = free[0]
        played += 1
        sessions = []
        for k in range(2):
            s = Session(f"race{r}-{k}", f"0700000000{k}", now)
            sid = "S-race"
            s.offered[sid] = slot
            desk.read_back_booking(s, sid, f"Caller {k}", f"0700000000{k}")
            s.heard("yes")
            sessions.append(s)
        barrier = threading.Barrier(2)
        results: list[str] = []
        lock = threading.Lock()

        def go(
            s: Session, barrier: threading.Barrier = barrier, lock: threading.Lock = lock, results: list[str] = results
        ) -> None:
            barrier.wait()
            try:
                desk.book(s, "S-race", s.readback.details["name"] if s.readback else "", s.caller_number or "")
                with lock:
                    results.append("won")
            except Blocked:
                with lock:
                    results.append("lost")

        threads = [threading.Thread(target=go, args=(s,)) for s in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wins += results.count("won")
        conflicts += results.count("lost")
    return {"rounds": played, "bookings": wins, "told_slot_taken": conflicts, "double_bookings": cal.overlaps()}


def turn_latency(business: Business, n: int = 400) -> dict[str, float]:
    now = NOW.replace(tzinfo=business.timezone)
    cal = Calendar(business)
    prefill(cal, random.Random(5), now.date(), now)
    desk = Desk(business, cal)
    brain = RuleBrain(desk)
    times: list[float] = []
    utterances = [
        "I'd like to book a sports massage on Friday afternoon",
        "the second one",
        "it's Sam Jones",
        "yes",
        "yes please",
        "no that's all thanks",
    ]
    for i in range(n // len(utterances)):
        s = Session(f"lat{i}", "07700900142", now)
        brain.greet(s)
        for u in utterances:
            t0 = _time.perf_counter()
            brain.respond(s, u)
            times.append((_time.perf_counter() - t0) * 1000)
    times.sort()
    return {"p50_ms": round(times[len(times) // 2], 2), "p95_ms": round(times[int(len(times) * 0.95)], 2)}


GATE = {
    "rules_dev_clean_success": 0.90,
    "rules_heldout_clean_success": 0.75,
    "rules_beats_ivr_everywhere": True,
    "wrong_changes_rules": 0,
    "double_bookings": 0,
    "unsafe_actions_with_guards": 0,
}


def run(calls: int = 300, seed: int = 7) -> dict[str, Any]:
    business = demo()
    configs = [run_config(business, b, bank, w, calls, seed) for b in BRAINS for bank in BANKS for w in WERS]
    ablation = guard_ablation(business, trials=40, seed=seed)
    race = booking_race(business, rounds=40)
    latency = turn_latency(business)
    get = {(c.brain, c.bank, c.wer): c for c in configs}
    checks = {
        "rules_dev_clean_success": get[("rules", "dev", 0.0)].success,
        "rules_heldout_clean_success": get[("rules", "heldout", 0.0)].success,
        "rules_beats_ivr_everywhere": all(
            get[("rules", b, w)].success > get[("ivr", b, w)].success for b in BANKS for w in WERS
        ),
        "wrong_changes_rules": sum(c.wrong_changes for c in configs if c.brain == "rules"),
        "double_bookings": sum(c.double_bookings for c in configs) + race["double_bookings"],
        "unsafe_actions_with_guards": sum(ablation["guards on"].values()),
    }
    passed = {k: (v >= GATE[k] if isinstance(GATE[k], float) else v == GATE[k]) for k, v in checks.items()}
    return {
        "configs": [c.__dict__ for c in configs],
        "ablation": ablation,
        "race": race,
        "latency": latency,
        "checks": checks,
        "gate": GATE,
        "passed": passed,
        "ok": all(passed.values()),
    }


def save(result: dict[str, Any], out: Path) -> None:
    from .report import html_report, markdown

    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    (out / "report.md").write_text(markdown(result), encoding="utf-8")
    (out / "report.html").write_text(html_report(result), encoding="utf-8")
