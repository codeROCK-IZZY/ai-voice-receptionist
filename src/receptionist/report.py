"""Markdown and HTML versions of the evaluation."""

from __future__ import annotations

import html
from typing import Any

KIND_LABEL = {
    "book": "Book",
    "faq": "Questions",
    "cancel": "Cancel",
    "move": "Move",
    "message": "Message",
    "emergency": "Emergency",
    "human": "Ask for a person",
    "adversarial": "Misuse attempts",
}


def _p(x: float) -> str:
    return f"{x * 100:.1f}%"


def _cfg(r: dict[str, Any], brain: str, bank: str, wer: float) -> dict[str, Any]:
    return next(c for c in r["configs"] if c["brain"] == brain and c["bank"] == bank and c["wer"] == wer)


def markdown(r: dict[str, Any]) -> str:
    lines = ["# AI Voice Receptionist: evaluation", ""]
    lines += ["Simulated calls to a physiotherapy clinic, judged on the diary and message book afterwards.", ""]
    lines += ["## Task success", "", "| Brain | Phrasing | 0% WER | 10% WER | 20% WER |", "|---|---|---:|---:|---:|"]
    for brain, label in (("rules", "This receptionist (offline brain)"), ("ivr", "Keyword IVR (baseline)")):
        for bank in ("dev", "heldout"):
            vals = " | ".join(_p(_cfg(r, brain, bank, w)["success"]) for w in (0.0, 0.1, 0.2))
            lines.append(f"| {label} | {bank} | {vals} |")
    lines += [
        "",
        "## By call type (offline brain, held-out phrasing, 10% WER)",
        "",
        "| Call type | Succeeded |",
        "|---|---:|",
    ]
    c = _cfg(r, "rules", "heldout", 0.1)
    for kind, (ok, n) in c["by_kind"].items():
        lines.append(f"| {KIND_LABEL.get(kind, kind)} | {ok} / {n} |")
    lines += ["", "## Safety", ""]
    wrong = {b: sum(x["wrong_changes"] for x in r["configs"] if x["brain"] == b) for b in ("rules", "ivr")}
    calls = {b: sum(x["calls"] for x in r["configs"] if x["brain"] == b) for b in ("rules", "ivr")}
    lines.append(
        f"- Wrong changes (a booking, cancellation or move the caller did not want, or another caller's "
        f"details spoken): **{wrong['rules']}** in {calls['rules']:,} calls (IVR: {wrong['ivr']})."
    )
    lines.append(
        f"- Double bookings: **{sum(x['double_bookings'] for x in r['configs']) + r['race']['double_bookings']}**"
        f"; race test: {r['race']['rounds']} pairs of callers confirming the same slot at once, "
        f"{r['race']['bookings']} booked, {r['race']['told_slot_taken']} told it had gone."
    )
    lines += [
        "",
        "Unsafe actions attempted directly against the desk (40 trials each):",
        "",
        "| Attempt | Guards on | Guards off |",
        "|---|---:|---:|",
    ]
    for attack, n in r["ablation"]["guards on"].items():
        lines.append(f"| {attack} | {n} | {r['ablation']['guards off'][attack]} |")
    lines += ["", f"Offline brain turn time: p50 {r['latency']['p50_ms']} ms, p95 {r['latency']['p95_ms']} ms.", ""]
    lines += ["## Gate", ""]
    for k, v in r["checks"].items():
        lines.append(f"- {'PASS' if r['passed'][k] else 'FAIL'} {k}: {v if not isinstance(v, float) else _p(v)}")
    return "\n".join(lines) + "\n"


CSS = """
:root{--bg:#0d1117;--panel:#151b26;--line:#263042;--ink:#e8edf5;--ink2:#9aa7bb;--ok:#3fcf8e;--bad:#ff6b6b;--mid:#ffb454;
--brand:#7aa2ff;font-family:Inter,system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;background:var(--bg);
color:var(--ink);line-height:1.5}main{max-width:1180px;margin:0 auto;padding:36px 28px 60px}h1{font-size:28px;margin:0 0 4px;
letter-spacing:-.02em}h2{font-size:18px;margin:32px 0 12px}.sub{color:var(--ink2);margin:0 0 22px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.tile{background:var(--panel);border:1px solid var(--line);
border-radius:12px;padding:16px 18px}.tile b{display:block;font-size:28px;letter-spacing:-.02em}.tile span{color:var(--ink2);
font-size:13px}table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
border-radius:12px;overflow:hidden;font-size:13.5px}th,td{padding:8px 12px;border-bottom:1px solid var(--line);
text-align:right}th:first-child,td:first-child{text-align:left}th{color:var(--ink2);font-weight:500;font-size:12px}
.good{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}.mid{color:var(--mid);font-weight:600}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}.bar{height:8px;border-radius:4px;background:var(--line);
position:relative;min-width:120px}.bar i{position:absolute;inset:0 auto 0 0;border-radius:4px;background:var(--brand)}
.bar i.b{background:#5b6578}.call{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;
font-size:13px}.call p{margin:3px 0}.call .c{color:var(--ink2)}
"""


def _cls(x: float) -> str:
    return "good" if x >= 0.9 else ("mid" if x >= 0.7 else "bad")


def html_report(r: dict[str, Any]) -> str:
    e = html.escape
    best = _cfg(r, "rules", "heldout", 0.1)
    ivr = _cfg(r, "ivr", "heldout", 0.1)
    wrong = sum(x["wrong_changes"] for x in r["configs"] if x["brain"] == "rules")
    calls = sum(x["calls"] for x in r["configs"] if x["brain"] == "rules")
    tiles = [
        (_p(best["success"]), "calls handled end to end (held-out phrasing, 10% speech errors)"),
        (_p(ivr["success"]), "keyword IVR on the same calls"),
        (str(wrong), f"wrong changes to the diary in {calls:,} calls"),
        (str(sum(r["ablation"]["guards on"].values())), "unsafe actions that got past the guards"),
    ]
    out = [
        f"<!doctype html><html lang='en'><meta charset='utf-8'><title>Voice receptionist evaluation</title>"
        f"<style>{CSS}</style><main><h1>AI Voice Receptionist: evaluation</h1><p class='sub'>Simulated calls to a "
        "physiotherapy clinic through a speech-recognition error channel, judged on the diary afterwards.</p>"
        "<div class='tiles'>"
    ]
    out += [f"<div class='tile'><b>{e(v)}</b><span>{e(t)}</span></div>" for v, t in tiles]
    out.append(
        "</div><h2>Task success</h2><table><tr><th>Brain</th><th>Phrasing</th><th>0% WER</th><th>10% WER</th>"
        "<th>20% WER</th></tr>"
    )
    for brain, label in (("rules", "This receptionist"), ("ivr", "Keyword IVR")):
        for bank in ("dev", "heldout"):
            cells = "".join(
                f"<td class='{_cls(_cfg(r, brain, bank, w)['success'])}'>{_p(_cfg(r, brain, bank, w)['success'])}</td>"
                for w in (0.0, 0.1, 0.2)
            )
            out.append(f"<tr><td>{label}</td><td>{bank}</td>{cells}</tr>")
    out.append(
        "</table><div class='cols'><div><h2>By call type (held-out, 10% WER)</h2><table><tr><th>Call type</th>"
        "<th>Receptionist</th><th>IVR</th><th></th></tr>"
    )
    for kind, (ok, n) in best["by_kind"].items():
        iok, inn = ivr["by_kind"].get(kind, (0, 1))
        out.append(
            f"<tr><td>{e(KIND_LABEL.get(kind, kind))}</td><td class='{_cls(ok / n)}'>{ok}/{n}</td>"
            f"<td class='{_cls(iok / inn)}'>{iok}/{inn}</td><td><div class='bar'><i style='width:{ok / n * 100:.0f}%'>"
            f"</i></div></td></tr>"
        )
    out.append(
        "</table></div><div><h2>Guards: unsafe actions that went through</h2><table><tr><th>Attempt</th>"
        "<th>Guards on</th><th>Guards off</th></tr>"
    )
    for attack, n in r["ablation"]["guards on"].items():
        off = r["ablation"]["guards off"][attack]
        out.append(
            f"<tr><td>{e(attack)}</td><td class='{'good' if n == 0 else 'bad'}'>{n}</td>"
            f"<td class='{'bad' if off else 'good'}'>{off}</td></tr>"
        )
    race = r["race"]
    out.append(
        f"</table><p class='sub' style='margin-top:10px'>Race test: {race['rounds']} pairs of callers confirm "
        f"the same slot at the same moment: {race['bookings']} booked, {race['told_slot_taken']} told it had gone,"
        f" {race['double_bookings']} double bookings.</p></div></div>"
    )
    sample = next((c for c in r["configs"] if c["brain"] == "rules" and c["failures"]), None)
    if sample:
        f = sample["failures"][0]
        lines = "".join(
            f"<p class='{'c' if who == 'caller' else ''}'><b>{'Caller' if who == 'caller' else 'Receptionist'}"
            f":</b> {e(t)}</p>"
            for who, t in f["transcript"]
        )
        out.append(
            f"<h2>A failed call, as it happened ({e(sample['bank'])}, {sample['wer']:.0%} WER: "
            f"{e(f['reason'])})</h2><div class='call'>{lines}</div>"
        )
    out.append("<h2>Gate</h2><table><tr><th>Check</th><th>Value</th><th>Result</th></tr>")
    for k, v in r["checks"].items():
        ok = r["passed"][k]
        out.append(
            f"<tr><td>{e(k)}</td><td>{e(_p(v) if isinstance(v, float) else str(v))}</td>"
            f"<td class='{'good' if ok else 'bad'}'>{'pass' if ok else 'fail'}</td></tr>"
        )
    out.append("</table></main></html>")
    return "".join(out) + "\n"
