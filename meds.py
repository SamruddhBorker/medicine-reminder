#!/usr/bin/env python3
"""medicine-reminder: schedule, confirm and track medicines; alert caregivers on missed doses.

    python meds.py add --person Aji --name Metformin --dose "1 tablet" --times 08:00 20:00 --stock 30
    python meds.py today                       # today's doses and their status
    python meds.py taken 1                     # confirm the dose that is due (or: --time 08:00)
    python meds.py skip 1
    python meds.py check                       # run every 5-10 min: reminders, MISSED alerts, refill warnings
    python meds.py check --dry-run
    python meds.py refill 1 30                 # added 30 tablets
    python meds.py list | report [--days 7] | stop 1

Not medical advice. It records and reminds what the caregiver enters; never rely on it
as the only safeguard for critical medicines. See README.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import notify
from core import DAYS, MedError, MedStore, check, parse_hm

HERE = Path(__file__).parent


def parse_now(s: str | None) -> datetime:
    if not s:
        return datetime.now().replace(second=0, microsecond=0)
    try:
        return datetime.strptime(s.replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        raise MedError("--now must look like '2026-11-18 08:30'") from None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # piped output on Windows defaults to cp1252
        if not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="meds.db")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--now", help="pretend it is 'YYYY-MM-DD HH:MM' (testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--person", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--dose", default="")
    a.add_argument("--times", nargs="+", required=True)
    a.add_argument("--stock", type=int, default=0)
    a.add_argument("--per-dose", type=int, default=1, help="how many units are used per dose")
    a.add_argument("--days", nargs="+", help="only these weekdays, e.g. mon thu")
    a.add_argument("--start")
    a.add_argument("--end")
    for c in ("taken", "skip"):
        t = sub.add_parser(c)
        t.add_argument("id", type=int)
        t.add_argument("--time", help="which scheduled time, HH:MM (default: the dose that is due)")
    r = sub.add_parser("refill")
    r.add_argument("id", type=int)
    r.add_argument("amount", type=int)
    ck = sub.add_parser("check")
    ck.add_argument("--dry-run", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("--days", type=int, default=7)
    sub.add_parser("stop").add_argument("id", type=int)
    for c in ("list", "today"):
        sub.add_parser(c)
    args = ap.parse_args(argv)

    notify.load_env(HERE / ".env")
    store = MedStore(args.db)
    try:
        now = parse_now(args.now)
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if args.cmd == "add":
            mid = store.add_med(args.person, args.name, args.times, args.dose, args.stock, args.per_dose,
                                args.days, args.start, args.end, now.date())
            print(f"added #{mid}")
        elif args.cmd in ("taken", "skip"):
            med = store.get(args.id)
            if not med:
                raise MedError("no such medicine")
            if args.time:
                due = datetime.strptime(f"{now.date()} {parse_hm(args.time)}", "%Y-%m-%d %H:%M")
                if parse_hm(args.time) not in med["times"].split(","):
                    raise MedError(f"{med['name']} isn't scheduled at {args.time} (times: {med['times']})")
            else:
                dose = store.next_pending(args.id, now)
                if not dose:
                    raise MedError("no pending dose to confirm right now (use --time HH:MM to pick one)")
                due = dose.due
            note = store.record(args.id, due, "taken" if args.cmd == "taken" else "skipped", now)
            print(f"recorded: {med['name']} {due:%H:%M} {args.cmd}" + (f"  WARNING: {note}" if note else ""))
        elif args.cmd == "check":
            log = check(store, cfg, now, lambda ch, rcp, text: notify.send(ch, rcp, text, cfg), args.dry_run)
            print(f"{len(log)} alert(s)" + (" (dry run, nothing sent or recorded)" if args.dry_run else ""))
            if args.dry_run:
                print("\n".join(log))
        elif args.cmd == "refill":
            print("stock updated" if store.add_stock(args.id, args.amount) else "no such medicine")
        elif args.cmd == "stop":
            print("stopped" if store.stop(args.id) else "no such medicine")
        elif args.cmd == "list":
            for m in store.meds():
                left = store.days_left(m)
                print(f"#{m['id']} {m['person']}: {m['name']} {m['dose']} at {m['times'].replace(',', ', ')}"
                      f"{' on ' + m['days'] if m['days'] else ''} | stock {m['stock']}"
                      f"{f' (~{left:.0f} days)' if left is not None else ''}")
        elif args.cmd == "today":
            for d in store.scheduled(now.date()):
                state = d.status or ("MISSED?" if d.due < now else "pending")
                print(f"{d.due:%H:%M}  {d.med['person']}: {d.med['name']} {d.med['dose']}  [{state}]")
        elif args.cmd == "report":
            for r_ in store.adherence(now, args.days).values():
                pct = f"{100 * r_['taken'] / r_['expected']:.0f}%" if r_["expected"] else "n/a"
                print(f"{r_['person']} {r_['name']}: taken {r_['taken']}/{r_['expected']} ({pct}), "
                      f"skipped {r_['skipped']}, missed {r_['missed']}  (last {args.days} days)")
    except (MedError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
