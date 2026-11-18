# medicine-reminder

[![tests](https://github.com/SamruddhBorker/medicine-reminder/actions/workflows/tests.yml/badge.svg)](https://github.com/SamruddhBorker/medicine-reminder/actions/workflows/tests.yml)

A medicine schedule for an elderly relative that **alerts a caregiver when a dose
is missed** and warns before the tablets run out.

**The problem:** families living apart worry whether Aji or Ajoba took their
medicines today, and discover on a Sunday night that the pharmacy stock ran out.

- Python 3.9+, **standard library only**, SQLite storage
- Reminders and alerts by WhatsApp or email; `console` mode prints them for testing

> ### ⚠️ Read this first
> This tool **records and reminds what the caregiver enters**. It gives **no medical or
> dosing advice**, and it can fail (phone off, no internet, a bug, a forgotten `check` job).
> **Never rely on it as the only safeguard** for medicines where a missed or doubled dose is
> dangerous. Use it alongside pill organisers, phone calls and the doctor's guidance.

## Quick start

```bash
python meds.py add --person Aji --name Metformin --dose "1 tablet" --times 08:00 20:00 --stock 30
python meds.py add --person Aji --name "Vitamin D" --times 09:00 --days mon --stock 12   # weekly
python meds.py today                  # today's doses and status
python meds.py taken 1                # confirm the dose that is due (or --time 08:00)
python meds.py skip 1
python meds.py check                  # run every 5-10 minutes (Task Scheduler / cron)
python meds.py check --dry-run        # preview alerts without sending
python meds.py refill 1 30            # 30 more tablets bought
python meds.py list
python meds.py report --days 7        # taken / skipped / missed per medicine
```

Put the patient and caregiver contacts in `config.json`.

## What `check` does

| When | Who is told |
|---|---|
| A dose becomes due | the patient: "Time for your medicine..." (once) |
| 60 minutes later and still unconfirmed | **all caregivers: "MISSED DOSE"** (once) |
| Stock covers 5 days or fewer | caregivers: "REFILL" (once a day) |

Alerts are never repeated, and a late confirmation still counts. If the 08:00 dose was
missed, a "taken" tap at 19:50 confirms the 20:00 dose and leaves the 08:00 one as missed:
the dose *closest to now* is the one confirmed.

## Details that matter

- Stock goes down only when a dose is marked **taken** (once, even if tapped twice) and never below zero
- Weekly schedules, start and end dates, several units per dose
- Doses that fall just before midnight are still checked after midnight
- Times and names are validated; text is stripped of control characters

## Honest limits

- A person has to confirm doses (CLI today). There is no pill-box hardware or phone app
- WhatsApp needs an approved message template to start conversations (see the other repos' notes); email works without it
- It doesn't know drug interactions, doses or what's safe. That is the doctor's and pharmacist's job

## Layout

| File | Role |
|---|---|
| `meds.py` | command-line interface |
| `core.py` | schedule, missed-dose checks, stock |
| `notify.py` | console, WhatsApp, email |

## Tests

```bash
python -m unittest -v
```

## License

MIT
