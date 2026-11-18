"""Delivery channels for alerts: console, WhatsApp Cloud API, email. Standard library only."""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

API_VERSION = "v20.0"


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def normalize_phone(raw, cc: str = "91"):
    if not raw:
        return None
    d = re.sub(r"\D", "", raw)
    if len(d) == 10:
        d = cc + d
    elif len(d) == 11 and d.startswith("0"):
        d = cc + d[1:]
    return d if 11 <= len(d) <= 15 else None


def send(channel: str, r: dict, text: str, cfg: dict) -> tuple[bool, str]:
    name = r.get("name", "?")
    if channel == "console":
        print(f"[console -> {name}] {text}")
        return True, "printed"
    if channel == "whatsapp":
        token, pid = os.environ.get("WHATSAPP_TOKEN"), os.environ.get("WHATSAPP_PHONE_ID")
        to = normalize_phone(r.get("phone"), cfg.get("default_country_code", "91"))
        if not to:
            return False, "no valid phone"
        if not token or not pid:
            return False, "WHATSAPP_TOKEN / WHATSAPP_PHONE_ID not set"
        req = urllib.request.Request(
            f"https://graph.facebook.com/{API_VERSION}/{pid}/messages",
            data=json.dumps({"messaging_product": "whatsapp", "to": to, "type": "text",
                             "text": {"body": text[:4000]}}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15):
                return True, "sent via WhatsApp"
        except urllib.error.HTTPError as e:
            return False, f"WhatsApp HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError) as e:
            return False, f"WhatsApp error: {e}"
    if channel == "email":
        email = r.get("email") or ""
        host, user, pwd = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return False, "no valid email"
        if not (host and user and pwd):
            return False, "SMTP_HOST / SMTP_USER / SMTP_PASSWORD not set"
        msg = EmailMessage()
        msg["Subject"] = "Medicine alert"
        msg["From"] = os.environ.get("SMTP_FROM", user)
        msg["To"] = email
        msg.set_content(text)
        port = int(os.environ.get("SMTP_PORT", "587"))
        try:
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20) as s:
                    s.login(user, pwd)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=20) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(user, pwd)
                    s.send_message(msg)
            return True, "sent via email"
        except (smtplib.SMTPException, OSError) as e:
            return False, f"email error: {type(e).__name__}"
    return False, f"unknown channel {channel!r}"
