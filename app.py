"""Email Extractor - web version.

Stateless by design: credentials are used for one IMAP session and then dropped.
Nothing is written to disk, a database, or the logs.
"""
import imaplib
import email
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from email import policy
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # real client IP behind Render/Railway
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024    # requests are tiny; reject anything big

# Allow-list of IMAP hosts. Never accept a server name from the user (SSRF risk).
PROVIDERS = {
    "gmail":   "imap.gmail.com",
    "outlook": "outlook.office365.com",
    "yahoo":   "imap.mail.yahoo.com",
    "icloud":  "imap.mail.me.com",
    "zoho":    "imap.zoho.in",
}
DOMAIN_TO_PROVIDER = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "outlook.com": "outlook", "hotmail.com": "outlook", "live.com": "outlook",
    "yahoo.com": "yahoo", "yahoo.in": "yahoo",
    "icloud.com": "icloud", "me.com": "icloud",
    "zoho.com": "zoho", "zoho.in": "zoho",
}

MAX_DAYS = 0         # widest date range per request, 0 = no limit
MAX_EMAILS = 0       # newest N emails per request, 0 = no limit
CHUNK = 200          # IMAP ids per fetch call
RATE_LIMIT = 6       # list requests ...
MSG_RATE_LIMIT = 60  # ... and single-email opens ...
RATE_WINDOW = 600    # ... per 10 minutes per IP
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UID_RE = re.compile(r"^\d{1,12}$")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_hits = defaultdict(deque)


def rate_limited(bucket: str, ip: str, limit: int) -> bool:
    now = time.time()
    q = _hits[(bucket, ip)]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def imap_date(d: datetime) -> str:
    # Manual formatting: strftime("%b") depends on server locale, IMAP needs English.
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def decode(raw) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def resolve_host(addr: str, provider: str):
    if provider == "auto":
        provider = DOMAIN_TO_PROVIDER.get(addr.split("@")[1].lower(), "")
    return PROVIDERS.get(provider)


def fetch_senders(addr, password, host, start, end, mailbox="INBOX"):
    """Return (rows, total_matches). Fetches headers only, never message bodies."""
    imap = imaplib.IMAP4_SSL(host, timeout=30)
    try:
        imap.login(addr, password)
        imap.select(mailbox, readonly=True)  # read-only: cannot change anything
        criteria = f"(SINCE {imap_date(start)} BEFORE {imap_date(end + timedelta(days=1))})"
        typ, data = imap.uid("SEARCH", None, criteria)  # UIDs stay stable between sessions
        if typ != "OK":
            raise RuntimeError("search failed")
        ids = (data[0] or b"").split()
        total = len(ids)
        if MAX_EMAILS:
            ids = ids[-MAX_EMAILS:]  # keep only the newest N

        rows = []
        for i in range(0, len(ids), CHUNK):
            batch = b",".join(ids[i:i + CHUNK]).decode()
            typ, parts = imap.uid("FETCH", batch, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK":
                continue
            for part in parts:
                if not isinstance(part, tuple):
                    continue
                m = re.search(rb"UID (\d+)", part[0])
                uid = m.group(1).decode() if m else ""
                msg = email.message_from_bytes(part[1])
                name, address = parseaddr(decode(msg.get("From", "")))
                try:
                    dt = parsedate_to_datetime(msg.get("Date"))
                    date = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    date = ""
                rows.append({
                    "uid": uid,
                    "name": name or address,
                    "email": address.lower(),
                    "subject": decode(msg.get("Subject", "")),
                    "date": date,
                })
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows, total
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def fetch_message(addr, password, host, uid, mailbox="INBOX"):
    """Return one full email as a dict, or None if it no longer exists."""
    imap = imaplib.IMAP4_SSL(host, timeout=30)
    try:
        imap.login(addr, password)
        imap.select(mailbox, readonly=True)
        typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: does not mark as read
        if typ != "OK":
            raise RuntimeError("fetch failed")
        raw = next((p[1] for p in data if isinstance(p, tuple)), None)
        if raw is None:
            return None

        msg = email.message_from_bytes(raw, policy=policy.default)

        def content(part):
            try:
                return part.get_content() if part else ""
            except Exception:
                return ""

        html = content(msg.get_body(preferencelist=("html",)))
        text = content(msg.get_body(preferencelist=("plain",)))

        attachments = []
        for a in msg.iter_attachments():
            attachments.append({
                "name": a.get_filename() or "(unnamed)",
                "type": a.get_content_type(),
                "size": len(a.get_payload(decode=True) or b""),
            })

        return {
            "from": str(msg.get("From", "")),
            "to": str(msg.get("To", "")),
            "cc": str(msg.get("Cc", "")),
            "subject": str(msg.get("Subject", "")),
            "date": str(msg.get("Date", "")),
            "html": html,
            "text": text,
            "attachments": attachments,
        }
    finally:
        try:
            imap.logout()
        except Exception:
            pass


@app.get("/")
def index():
    return render_template("index.html", max_days=MAX_DAYS, max_emails=MAX_EMAILS)


@app.get("/health")
def health():
    return "ok"


@app.post("/api/fetch")
def api_fetch():
    if rate_limited("list", request.remote_addr or "unknown", RATE_LIMIT):
        return jsonify(error="Too many requests. Please wait a few minutes."), 429

    body = request.get_json(silent=True) or {}
    addr = str(body.get("email", "")).strip()
    password = str(body.get("password", "")).replace(" ", "")  # app passwords show with spaces
    provider = str(body.get("provider", "auto"))

    if not EMAIL_RE.match(addr) or not password:
        return jsonify(error="Enter a valid email and app password."), 400

    host = resolve_host(addr, provider)
    if not host:
        return jsonify(error="Could not detect your provider. Pick one from the list."), 400

    try:
        start = datetime.strptime(body.get("start", ""), "%Y-%m-%d")
        end = datetime.strptime(body.get("end", ""), "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Dates must be in YYYY-MM-DD format."), 400
    if end < start:
        return jsonify(error="End date must be on or after the start date."), 400
    if MAX_DAYS and (end - start).days > MAX_DAYS:
        return jsonify(error=f"Please choose a range of {MAX_DAYS} days or less."), 400

    try:
        rows, total = fetch_senders(addr, password, host, start, end)
    except imaplib.IMAP4.error:
        return jsonify(error="Login failed. Check the email and use an app password, not your normal one."), 401
    except Exception:
        return jsonify(error="Could not reach the mail server. Try again in a moment."), 502

    return jsonify(rows=rows, total=total, shown=len(rows))


@app.post("/api/message")
def api_message():
    if rate_limited("msg", request.remote_addr or "unknown", MSG_RATE_LIMIT):
        return jsonify(error="Too many requests. Please wait a few minutes."), 429

    body = request.get_json(silent=True) or {}
    addr = str(body.get("email", "")).strip()
    password = str(body.get("password", "")).replace(" ", "")
    provider = str(body.get("provider", "auto"))
    uid = str(body.get("uid", ""))

    if not EMAIL_RE.match(addr) or not password:
        return jsonify(error="Enter a valid email and app password."), 400
    if not UID_RE.match(uid):
        return jsonify(error="Invalid message id."), 400

    host = resolve_host(addr, provider)
    if not host:
        return jsonify(error="Could not detect your provider. Pick one from the list."), 400

    try:
        msg = fetch_message(addr, password, host, uid)
    except imaplib.IMAP4.error:
        return jsonify(error="Login failed. Check the email and use an app password."), 401
    except Exception:
        return jsonify(error="Could not reach the mail server. Try again in a moment."), 502

    if msg is None:
        return jsonify(error="This email could not be found."), 404
    return jsonify(msg)


if __name__ == "__main__":
    app.run(debug=True)
