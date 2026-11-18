import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import core
import meds
from core import MedError, MedStore, check

CFG = {"channels": ["console"], "missed_after_minutes": 60, "refill_warning_days": 5,
       "patient": {"name": "Aji"}, "caregivers": [{"name": "Sam"}, {"name": "Rita"}]}


def at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


class Sink:
    def __init__(self):
        self.sent = []

    def __call__(self, ch, r, text):
        self.sent.append((r["name"], text))
        return True, "ok"


def store(stock=30, times=("08:00", "20:00"), **kw):
    s = MedStore()
    mid = s.add_med("Aji", "Metformin", list(times), "1 tablet", stock, today=date(2026, 11, 18), **kw)
    return s, mid


class ScheduleTests(unittest.TestCase):
    def test_validation(self):
        s = MedStore()
        for kw in ({"times": ["25:00"]}, {"times": []}, {"times": ["8am"]}, {"name": " "}, {"stock": -1},
                   {"per_dose": 0}, {"days": ["xyz"]}, {"end": "2026-01-01", "start": "2026-02-01"}):
            args = {"person": "A", "name": "M", "times": ["08:00"], **kw}
            with self.assertRaises(MedError, msg=str(kw)):
                s.add_med(**args)

    def test_times_sorted_and_deduped(self):
        s, mid = store(times=("20:00", "08:00", "8:00"))
        self.assertEqual(s.get(mid)["times"], "08:00,20:00")

    def test_weekday_and_date_range(self):
        s, mid = store(days=["mon", "thu"], start="2026-11-16", end="2026-11-30")
        self.assertEqual(len(s.scheduled(date(2026, 11, 19))), 2)     # Thursday
        self.assertEqual(s.scheduled(date(2026, 11, 18)), [])         # Wednesday
        self.assertEqual(s.scheduled(date(2026, 12, 3)), [])          # after end
        self.assertEqual(s.scheduled(date(2026, 11, 9)), [])          # before start


class AlertTests(unittest.TestCase):
    def test_nothing_before_due(self):
        s, _ = store()
        sink = Sink()
        self.assertEqual(check(s, CFG, at("2026-11-18 07:59"), sink), [])

    def test_reminder_to_patient_once(self):
        s, _ = store()
        sink = Sink()
        check(s, CFG, at("2026-11-18 08:00"), sink)
        check(s, CFG, at("2026-11-18 08:10"), sink)
        self.assertEqual([n for n, _ in sink.sent], ["Aji"])
        self.assertIn("08:00", sink.sent[0][1])

    def test_missed_dose_alerts_all_caregivers_exactly_once(self):
        s, _ = store()
        sink = Sink()
        check(s, CFG, at("2026-11-18 08:00"), sink)
        sink.sent.clear()
        check(s, CFG, at("2026-11-18 09:00"), sink)     # 60 min after due
        self.assertEqual(sorted(n for n, _ in sink.sent), ["Rita", "Sam"])
        self.assertIn("MISSED DOSE", sink.sent[0][1])
        sink.sent.clear()
        check(s, CFG, at("2026-11-18 09:30"), sink)
        self.assertEqual(sink.sent, [])

    def test_taken_dose_never_alerts(self):
        s, mid = store()
        sink = Sink()
        s.record(mid, at("2026-11-18 08:00"), "taken", at("2026-11-18 08:05"))
        check(s, CFG, at("2026-11-18 09:30"), sink)
        self.assertEqual(sink.sent, [])

    def test_late_confirmation_after_alert_still_counts(self):
        s, mid = store()
        sink = Sink()
        check(s, CFG, at("2026-11-18 09:30"), sink)
        s.record(mid, at("2026-11-18 08:00"), "taken", at("2026-11-18 09:40"))
        self.assertEqual(s.scheduled(date(2026, 11, 18))[0].status, "taken")

    def test_yesterdays_late_night_dose_is_still_checked(self):
        s, _ = store(times=("23:30",))
        sink = Sink()
        check(s, CFG, at("2026-11-19 00:45"), sink)      # 75 min after 23:30
        self.assertTrue(any("MISSED" in t for _, t in sink.sent))

    def test_dry_run_sends_and_records_nothing(self):
        s, _ = store()
        sink = Sink()
        log = check(s, CFG, at("2026-11-18 09:30"), sink, dry_run=True)
        self.assertTrue(log)
        self.assertEqual(sink.sent, [])
        check(s, CFG, at("2026-11-18 09:30"), sink)          # real run afterwards still alerts
        self.assertTrue(sink.sent)


class StockTests(unittest.TestCase):
    def test_taken_decrements_once_and_never_negative(self):
        s, mid = store(stock=1)
        due = at("2026-11-18 08:00")
        self.assertEqual(s.record(mid, due, "taken", due), "")
        self.assertEqual(s.get(mid)["stock"], 0)
        s.record(mid, due, "taken", due)                       # tapping twice doesn't double count
        self.assertEqual(s.get(mid)["stock"], 0)
        note = s.record(mid, at("2026-11-18 20:00"), "taken", at("2026-11-18 20:00"))
        self.assertIn("empty", note)
        self.assertEqual(s.get(mid)["stock"], 0)

    def test_skip_does_not_use_stock(self):
        s, mid = store(stock=10)
        s.record(mid, at("2026-11-18 08:00"), "skipped", at("2026-11-18 08:00"))
        self.assertEqual(s.get(mid)["stock"], 10)

    def test_per_dose(self):
        s, mid = store(stock=10, per_dose=2)
        s.record(mid, at("2026-11-18 08:00"), "taken", at("2026-11-18 08:00"))
        self.assertEqual(s.get(mid)["stock"], 8)

    def test_days_left(self):
        s, mid = store(stock=10)                  # 2 doses/day
        self.assertEqual(s.days_left(s.get(mid)), 5.0)

    def test_refill_alert_once_per_day(self):
        s, _ = store(stock=10)                    # exactly 5 days left
        sink = Sink()
        check(s, CFG, at("2026-11-18 07:00"), sink)
        check(s, CFG, at("2026-11-18 12:00"), sink)
        refills = [t for _, t in sink.sent if "REFILL" in t]
        self.assertEqual(len(refills), 2)         # once per caregiver, not twice
        sink.sent.clear()
        check(s, CFG, at("2026-11-19 07:00"), sink)
        self.assertTrue(any("REFILL" in t for _, t in sink.sent))   # next day: again

    def test_no_refill_alert_with_plenty(self):
        s, _ = store(stock=100)
        sink = Sink()
        check(s, CFG, at("2026-11-18 07:00"), sink)
        self.assertEqual(sink.sent, [])

    def test_refill_adds(self):
        s, mid = store(stock=1)
        s.add_stock(mid, 30)
        self.assertEqual(s.get(mid)["stock"], 31)
        with self.assertRaises(MedError):
            s.add_stock(mid, 0)


class ConfirmTests(unittest.TestCase):
    def test_next_pending_picks_due_dose(self):
        s, mid = store()
        self.assertEqual(s.next_pending(mid, at("2026-11-18 08:10")).due, at("2026-11-18 08:00"))
        self.assertEqual(s.next_pending(mid, at("2026-11-18 19:50")).due, at("2026-11-18 20:00"))  # about to be due

    def test_adherence(self):
        s, mid = store()
        for day in ("2026-11-16", "2026-11-17"):
            s.record(mid, at(f"{day} 08:00"), "taken", at(f"{day} 08:05"))
        s.record(mid, at("2026-11-17 20:00"), "skipped", at("2026-11-17 20:05"))
        rep = s.adherence(at("2026-11-18 09:00"), days=3)[mid]
        # window: 16th, 17th, 18th. Doses due by 09:00 on the 18th: but start_date is the 18th
        self.assertEqual(rep["expected"], 1)      # only 18th 08:00 is on/after the start date
        s2 = MedStore()
        m2 = s2.add_med("A", "X", ["08:00", "20:00"], start="2026-11-10", stock=50)
        for day in ("2026-11-16", "2026-11-17"):
            s2.record(m2, at(f"{day} 08:00"), "taken", at(f"{day} 08:05"))
        s2.record(m2, at("2026-11-17 20:00"), "skipped", at("2026-11-17 20:05"))
        r = s2.adherence(at("2026-11-18 09:00"), days=3)[m2]
        # due by 18th 09:00: 16th x2, 17th x2, 18th 08:00 = 5 doses
        self.assertEqual((r["expected"], r["taken"], r["skipped"], r["missed"]), (5, 2, 1, 2))


class CliTests(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as t:
            cfgp = Path(t) / "c.json"
            cfgp.write_text(json.dumps(CFG), encoding="utf-8")
            base = ["--db", str(Path(t) / "m.db"), "--config", str(cfgp)]
            now = ["--now", "2026-11-18 08:10"]
            self.assertEqual(meds.main(base + now + ["add", "--person", "Aji", "--name", "Metformin",
                                                       "--times", "08:00", "20:00", "--stock", "9"]), 0)
            self.assertEqual(meds.main(base + now + ["add", "--person", "Aji", "--name", "X", "--times", "99:00"]), 1)
            self.assertEqual(meds.main(base + now + ["today"]), 0)
            self.assertEqual(meds.main(base + now + ["check", "--dry-run"]), 0)
            self.assertEqual(meds.main(base + now + ["taken", "1"]), 0)
            self.assertEqual(meds.main(base + now + ["taken", "1", "--time", "13:00"]), 1)   # not a scheduled time
            self.assertEqual(meds.main(base + now + ["taken", "9"]), 1)
            self.assertEqual(meds.main(base + now + ["list"]), 0)
            self.assertEqual(meds.main(base + now + ["report"]), 0)
            self.assertEqual(meds.main(base + now + ["refill", "1", "20"]), 0)
            self.assertEqual(meds.main(base + ["--now", "bad", "today"]), 1)
            self.assertEqual(meds.main(base + now + ["stop", "1"]), 0)


if __name__ == "__main__":
    unittest.main()
