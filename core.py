"""Medicine schedule, missed-dose detection and refill tracking. Standard library only.

SAFETY: this records and reminds what the CAREGIVER enters. It gives no medical or
dosing advice, and software can fail (phone off, no network, bugs). It must never be
the only safeguard for medicines where a missed dose is dangerous.
"""
from __future__ import annotations

import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

FMT = "%Y-%m-%d %H:%M"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person TEXT NOT NULL, name TEXT NOT NULL, dose TEXT NOT NULL DEFAULT '',
  times TEXT NOT NULL,                  -- comma-separated HH:MM
  days TEXT NOT NULL DEFAULT '',        -- empty = every day, else comma-separated mon..sun
  start_date TEXT NOT NULL, end_date TEXT,
  stock INTEGER NOT NULL DEFAULT 0, per_dose INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS doses (
  med_id INTEGER NOT NULL, due_at TEXT NOT NULL,
  status TEXT NOT NULL,                 -- taken | skipped
  at TEXT NOT NULL,
  PRIMARY KEY (med_id, due_at)
);
CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, at TEXT NOT NULL);   -- de-duplication
"""


class MedError(ValueError):
    """Bad input; the message says what to fix."""


def clean(s: str, what: str, hi: int = 60, required: bool = True) -> str:
    v = re.sub(r"[\x00-\x1f\x7f]", " ", s or "").strip()
    if required and not v:
        raise MedError(f"{what} is required")
    if len(v) > hi:
        raise MedError(f"{what} is too long (max {hi})")
    return v


def parse_hm(s: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise MedError(f"time must look like 08:30, got {s!r}")
    return f"{int(m[1]):02d}:{m[2]}"


def parse_date(s: str) -> date:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise MedError(f"can't read date {s!r}; use YYYY-MM-DD") from None


@dataclass
class Dose:
    med: sqlite3.Row
    due: datetime
    status: Optional[str]      # None = still pending


class MedStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- meds
    def add_med(self, person: str, name: str, times: list[str], dose: str = "", stock: int = 0,
                per_dose: int = 1, days: Optional[list[str]] = None, start: Optional[str] = None,
                end: Optional[str] = None, today: Optional[date] = None) -> int:
        ts = sorted({parse_hm(t) for t in times})
        if not ts:
            raise MedError("give at least one time")
        ds = []
        for d in days or []:
            k = d.strip().lower()[:3]
            if k not in DAYS:
                raise MedError(f"day must be mon..sun, got {d!r}")
            ds.append(k)
        if not (0 <= stock <= 100000 and 1 <= per_dose <= 20):
            raise MedError("stock must be 0-100000 and per-dose 1-20")
        s = parse_date(start) if start else (today or date.today())
        e = parse_date(end) if end else None
        if e and e < s:
            raise MedError("end date is before the start date")
        with self.lock:
            return self.db.execute(
                "INSERT INTO meds(person,name,dose,times,days,start_date,end_date,stock,per_dose) VALUES(?,?,?,?,?,?,?,?,?)",
                (clean(person, "person"), clean(name, "medicine name"), clean(dose, "dose", 60, False),
                 ",".join(ts), ",".join(sorted(set(ds), key=DAYS.index)), s.isoformat(),
                 e.isoformat() if e else None, stock, per_dose)).lastrowid

    def meds(self, active_only: bool = True):
        with self.lock:
            return self.db.execute("SELECT * FROM meds" + (" WHERE active=1" if active_only else "") + " ORDER BY id").fetchall()

    def get(self, med_id: int):
        with self.lock:
            return self.db.execute("SELECT * FROM meds WHERE id=?", (med_id,)).fetchone()

    def stop(self, med_id: int) -> bool:
        with self.lock:
            return self.db.execute("UPDATE meds SET active=0 WHERE id=?", (med_id,)).rowcount == 1

    def add_stock(self, med_id: int, n: int) -> bool:
        if not 1 <= n <= 100000:
            raise MedError("refill amount must be between 1 and 100000")
        with self.lock:
            return self.db.execute("UPDATE meds SET stock=stock+? WHERE id=?", (n, med_id)).rowcount == 1

    # ---- schedule
    def scheduled(self, day: date) -> list[Dose]:
        out = []
        for m in self.meds():
            if parse_date(m["start_date"]) > day or (m["end_date"] and parse_date(m["end_date"]) < day):
                continue
            if m["days"] and DAYS[day.weekday()] not in m["days"].split(","):
                continue
            for t in m["times"].split(","):
                due = datetime.strptime(f"{day.isoformat()} {t}", FMT)
                with self.lock:
                    row = self.db.execute("SELECT status FROM doses WHERE med_id=? AND due_at=?",
                                          (m["id"], due.strftime(FMT))).fetchone()
                out.append(Dose(m, due, row["status"] if row else None))
        return sorted(out, key=lambda d: (d.due, d.med["id"]))

    def record(self, med_id: int, due: datetime, status: str, now: datetime) -> str:
        """Mark a dose taken/skipped. Returns a note ('' or a warning such as low stock)."""
        if status not in ("taken", "skipped"):
            raise MedError("status must be taken or skipped")
        med = self.get(med_id)
        if not med:
            raise MedError("no such medicine")
        note = ""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                prev = self.db.execute("SELECT status FROM doses WHERE med_id=? AND due_at=?",
                                       (med_id, due.strftime(FMT))).fetchone()
                self.db.execute("INSERT OR REPLACE INTO doses VALUES(?,?,?,?)",
                                (med_id, due.strftime(FMT), status, now.strftime(FMT)))
                if status == "taken" and not (prev and prev["status"] == "taken"):   # never count a dose twice
                    cur = self.db.execute("SELECT stock FROM meds WHERE id=?", (med_id,)).fetchone()["stock"]
                    if cur < med["per_dose"]:
                        note = "stock was already empty: check the count and refill"
                    self.db.execute("UPDATE meds SET stock=MAX(0, stock-?) WHERE id=?", (med["per_dose"], med_id))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return note

    def next_pending(self, med_id: int, now: datetime, lookback_hours: int = 12) -> Optional[Dose]:
        """The pending dose a 'taken' tap most likely refers to: the one closest to now.

        Closest, not oldest: an unconfirmed morning dose must stay pending (and keep alerting)
        rather than silently absorb an evening confirmation.
        """
        doses = [d for d in self.scheduled(now.date()) + self.scheduled((now - timedelta(days=1)).date())
                 if d.med["id"] == med_id and d.status is None
                 and now - timedelta(hours=lookback_hours) <= d.due <= now + timedelta(minutes=30)]
        return min(doses, key=lambda d: abs(d.due - now)) if doses else None

    # ---- alert de-duplication
    def first_time(self, key: str, now: datetime) -> bool:
        with self.lock:
            return self.db.execute("INSERT OR IGNORE INTO alerts(key,at) VALUES(?,?)",
                                   (key, now.strftime(FMT))).rowcount == 1

    # ---- stock
    def days_left(self, med) -> Optional[float]:
        per_day = len(med["times"].split(",")) * med["per_dose"]
        if med["days"]:
            per_day = per_day * len(med["days"].split(",")) / 7
        return med["stock"] / per_day if per_day else None

    def adherence(self, now: datetime, days: int = 7) -> dict:
        """Doses that were due by `now` in the last `days` days: taken / skipped / missed."""
        res = {}
        for m in self.meds():
            expected = taken = skipped = 0
            for i in range(days):
                for d in self.scheduled(now.date() - timedelta(days=i)):
                    if d.med["id"] != m["id"] or d.due > now:
                        continue
                    expected += 1
                    taken += d.status == "taken"
                    skipped += d.status == "skipped"
            res[m["id"]] = {"name": m["name"], "person": m["person"], "expected": expected, "taken": taken,
                            "skipped": skipped, "missed": expected - taken - skipped}
        return res


Send = Callable[[str, dict, str], tuple[bool, str]]   # (channel, recipient, text) -> (ok, detail)


def check(store: MedStore, cfg: dict, now: datetime, send: Send, dry_run: bool = False) -> list[str]:
    """Run every few minutes. Sends: a reminder when a dose is due, a MISSED alert to caregivers
    if it's still unconfirmed after the grace period, and a refill warning once a day."""
    grace = timedelta(minutes=cfg.get("missed_after_minutes", 60))
    refill_days = cfg.get("refill_warning_days", 5)
    log: list[str] = []

    def deliver(recipients: list[dict], text: str) -> None:
        log.append(text)
        if dry_run:
            return
        for r in recipients:
            for ch in cfg.get("channels", ["console"]):
                ok, _ = send(ch, r, text)
                if ok:
                    break

    patient = [cfg["patient"]] if cfg.get("patient") else []
    caregivers = cfg.get("caregivers", [])
    for d in store.scheduled(now.date()) + store.scheduled((now - timedelta(days=1)).date()):
        if d.status is not None or d.due > now:
            continue
        label = f"{d.med['person']}: {d.med['name']} {d.med['dose']}".strip()
        when = d.due.strftime("%H:%M")
        key = f"{d.med['id']}:{d.due.strftime(FMT)}"
        if now - d.due >= grace:
            if dry_run or store.first_time(f"missed:{key}", now):
                deliver(caregivers, f"MISSED DOSE: {label} was due at {when} and hasn't been confirmed.")
        elif patient and (dry_run or store.first_time(f"remind:{key}", now)):
            deliver(patient, f"Time for your medicine: {d.med['name']} {d.med['dose']} (due {when}). Please confirm once taken.")
    for m in store.meds():
        left = store.days_left(m)
        if left is not None and left <= refill_days and (dry_run or store.first_time(f"refill:{m['id']}:{now.date()}", now)):
            where = "has run out" if m["stock"] == 0 else f"is running low: about {math.floor(left)} day(s) left"
            deliver(caregivers, f"REFILL: {m['person']}'s {m['name']} {where} ({m['stock']} left).")
    return log
