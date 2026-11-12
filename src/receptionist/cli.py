"""Command line: talk to the receptionist, serve the phone webhooks, run the evaluation."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="receptionist", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    chat = sub.add_parser("chat", help="type to the receptionist as if you were calling")
    chat.add_argument("--brain", choices=["rules", "claude"], default="rules")
    chat.add_argument("--caller", default="07700900142", help="caller ID to present ('' for hidden)")
    serve = sub.add_parser("serve", help="run the Twilio webhooks and the admin page")
    serve.add_argument("--brain", choices=["rules", "claude"], default="rules")
    serve.add_argument("--db", default="receptionist.db")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    ev = sub.add_parser("evaluate", help="simulated calls, guard ablation and booking race")
    ev.add_argument("--calls", type=int, default=300, help="calls per configuration")
    ev.add_argument("--out", default="eval/reports")
    ev.add_argument("--gate", action="store_true", help="exit 1 if any check fails")
    args = ap.parse_args(argv)

    if args.cmd == "chat":
        return _chat(args.brain, args.caller or None)
    if args.cmd == "serve":
        import uvicorn

        from .telephony import build, create_app

        uvicorn.run(
            create_app(build(args.db, args.brain), public_url=os.environ.get("PUBLIC_URL")),
            host=args.host,
            port=args.port,
        )
        return 0
    from .evaluate import run, save

    result = run(calls=args.calls)
    save(result, Path(args.out))
    print((Path(args.out) / "report.md").read_text(encoding="utf-8"))
    return 1 if args.gate and not result["ok"] else 0


def _chat(brain_name: str, caller: str | None) -> int:
    from .business import demo
    from .calendar import Calendar
    from .desk import Desk, Session
    from .dialog import RuleBrain

    biz = demo()
    desk = Desk(biz, Calendar(biz))
    if brain_name == "claude":
        from .agent import ClaudeBrain

        brain: RuleBrain | ClaudeBrain = ClaudeBrain(desk)
    else:
        brain = RuleBrain(desk)
    s = Session("chat", caller, datetime.now(biz.timezone))
    print("Receptionist:", brain.greet(s).text)
    while not s.ended:
        try:
            line = input("You: ")
        except EOFError:
            break
        reply = brain.respond(s, line)
        print("Receptionist:", reply.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
