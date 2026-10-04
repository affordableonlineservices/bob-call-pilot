"""
BoB Call Pilot — proof of concept for bobthelobstar.com's Missed-Call Pilot.
Owner is customer #1.

Flow:
  1. Caller dials the owner's cell, no answer -> carrier forwards to the Twilio number.
  2. POST /voice answers, logs the missed call, fires an instant SMS text-back, hangs up.
  3. POST /sms carries the conversation (Groq-powered): qualifies the lead, helps book.
  4. Owner texts "SOLD <buyer-number>" from his own cell -> buyer gets a review request.
  5. Owner texts "BOOKED <caller-number>" -> logged as a booked job.
  6. GET /weekly?token=... -> Monday-morning SMS summary to the owner.

Env vars (set on Render):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_NUMBER (+1xxx, the Twilio number),
  OWNER_CELL (+14806045005), BUSINESS_NAME, GROQ_API_KEY,
  GROQ_MODEL (default openai/gpt-oss-20b),
  SCHEDULING_LINK (optional, e.g. Calendly — if empty the bot collects availability),
  REVIEW_LINK (optional Google review link),
  WEEKLY_TOKEN (secret for the /weekly endpoint)
"""
import os
import sqlite3
import time

import requests
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
try:
    import libsql
    _has_libsql = True
except ImportError:
    libsql = None
    _has_libsql = False

app = Flask(__name__)

SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "")
OWNER_CELL = os.environ.get("OWNER_CELL", "+14806045005")
BUSINESS = os.environ.get("BUSINESS_NAME", "BoB the Lobstar")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
SCHEDULING_LINK = os.environ.get("SCHEDULING_LINK", "")
REVIEW_LINK = os.environ.get("REVIEW_LINK", "")
WEEKLY_TOKEN = os.environ.get("WEEKLY_TOKEN", "change-me")

DB = "/tmp/bob_pilot.db"  # NOTE: ephemeral on Render free tier; fine for the trial


def db():
    """Durable storage: Turso (libSQL) when TURSO_URL is set, else local SQLite."""
    turso_url = os.environ.get("TURSO_URL", "")
    if turso_url and _has_libsql:
        c = libsql.connect(turso_url, auth_token=os.environ.get("TURSO_AUTH_TOKEN", ""))
    else:
        c = sqlite3.connect(DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS events("
        "id INTEGER PRIMARY KEY, ts REAL, kind TEXT, peer TEXT, note TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS messages("
        "id INTEGER PRIMARY KEY, ts REAL, peer TEXT, role TEXT, body TEXT)"
    )
    return c


def log(kind, peer, note=""):
    c = db()
    c.execute("INSERT INTO events(ts,kind,peer,note) VALUES(?,?,?,?)",
              (time.time(), kind, peer, note[:200]))
    c.commit()
    c.close()


def save_msg(peer, role, body):
    c = db()
    c.execute("INSERT INTO messages(ts,peer,role,body) VALUES(?,?,?,?)",
              (time.time(), peer, role, body[:500]))
    c.commit()
    c.close()


def history(peer, n=10):
    c = db()
    rows = c.execute(
        "SELECT role,body FROM messages WHERE peer=? ORDER BY id DESC LIMIT ?",
        (peer, n)).fetchall()
    c.close()
    return [{"role": r, "content": b} for r, b in reversed(rows)]


def send_sms(to, body):
    client = Client(SID, TOKEN)
    msg = client.messages.create(to=to, from_=TWILIO_NUMBER, body=body[:1500])
    return msg.sid


def schedule_line():
    if SCHEDULING_LINK:
        return f"When they're ready to book, send this link: {SCHEDULING_LINK}"
    return ("There is no online booking link configured. Ask what day/time works "
            "for them and say the owner will confirm shortly.")


SYSTEM = (
    f"You are BoB, the friendly AI texting assistant for {BUSINESS} ({TWILIO_NUMBER}). "
    "A customer just called and couldn't reach anyone, so you're continuing the conversation by text. "
    "Rules: keep EVERY reply under 300 characters. Plain SMS tone, at most one emoji. "
    "First figure out what they need (service, quote, or something urgent) and how urgent it is. "
    f"Then help them book. {schedule_line()} "
    "Never invent prices, availability, or promises. If unsure, say the owner will confirm. "
    "You're an AI assistant; don't claim to be human."
)


def groq_reply(peer, user_msg):
    msgs = [{"role": "system", "content": SYSTEM}] + history(peer) + [
        {"role": "user", "content": user_msg}]
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={"model": GROQ_MODEL, "messages": msgs,
              "max_tokens": 160, "temperature": 0.7},
        timeout=25,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


@app.post("/voice")
def voice():
    """Inbound forwarded missed call: log it, fire the instant text-back, hang up."""
    caller = request.values.get("From", "")
    log("missed_call", caller, "forwarded no-answer")
    textback = (f"Sorry we missed your call! This is {BUSINESS} \u2014 "
                "are you looking for a quote, or is something urgent?")
    try:
        send_sms(caller, textback)
        log("sms_out", caller, textback[:80])
    except Exception as e:  # trial accounts can only text verified numbers
        log("sms_failed", caller, str(e)[:120])
    resp = VoiceResponse()
    resp.say("Sorry we missed your call. We're sending you a text right now.",
             voice="alice")
    resp.hangup()
    return str(resp), 200, {"Content-Type": "text/xml"}


@app.post("/sms")
def sms():
    frm = request.values.get("From", "")
    body = request.values.get("Body", "").strip()
    log("sms_in", frm, body[:120])
    save_msg(frm, "user", body)

    # --- owner commands from his own cell ---
    if frm == OWNER_CELL:
        upper = body.upper()
        if upper.startswith("SOLD"):
            parts = body.split()
            if len(parts) >= 2:
                buyer = parts[1]
                review_txt = (
                    f"Hi! Thanks for your purchase with {BUSINESS} \U0001f649 "
                    "If everything went well, a quick review would mean a lot."
                    + (f" {REVIEW_LINK}" if REVIEW_LINK else
                       " Just reply here and let us know!")
                )
                try:
                    send_sms(buyer, review_txt)
                    log("review_sent", buyer, "")
                    save_msg(frm, "assistant", f"Review request sent to {buyer}.")
                except Exception as e:
                    log("sms_failed", buyer, str(e)[:120])
                return "", 200
        if upper.startswith("BOOKED"):
            parts = body.split()
            peer = parts[1] if len(parts) >= 2 else ""
            log("booking", peer, "marked by owner")
            return "", 200

    # --- normal conversation ---
    try:
        reply = groq_reply(frm, body)
    except Exception as e:
        log("groq_failed", frm, str(e)[:120])
        reply = (f"Thanks for reaching out to {BUSINESS}! "
                 "The owner will follow up with you shortly.")
    save_msg(frm, "assistant", reply)
    try:
        send_sms(frm, reply)
        log("sms_out", frm, reply[:80])
    except Exception as e:
        log("sms_failed", frm, str(e)[:120])
    return "", 200


@app.get("/weekly")
def weekly():
    """Protected endpoint; hit it from a Monday-morning cron."""
    if request.args.get("token") != WEEKLY_TOKEN:
        return "nope", 403
    week_ago = time.time() - 7 * 86400
    c = db()
    def count(kind):
        return c.execute(
            "SELECT COUNT(*) FROM events WHERE kind=? AND ts>?",
            (kind, week_ago)).fetchone()[0]
    missed = count("missed_call")
    convos = c.execute(
        "SELECT COUNT(DISTINCT peer) FROM messages WHERE ts>?", (week_ago,)).fetchone()[0]
    booked = count("booking")
    reviews = count("review_sent")
    c.close()
    summary = (f"{BUSINESS} weekly: {missed} missed calls caught, "
               f"{convos} text conversations, {booked} bookings marked, "
               f"{reviews} review requests sent.")
    try:
        send_sms(OWNER_CELL, summary)
        log("weekly_sent", OWNER_CELL, summary)
    except Exception as e:
        log("sms_failed", OWNER_CELL, str(e)[:120])
    return "ok", 200


@app.get("/health")
def health():
    return "ok", 200


@app.get("/test_send")
def test_send():
    """Protected GET endpoint: sends a real test SMS to the owner and reports
    exactly what Twilio says. Used to diagnose outbound SMS failures."""
    if request.args.get("token") != WEEKLY_TOKEN:
        return "nope", 403
    try:
        sid = send_sms(OWNER_CELL, f"{BUSINESS} pilot: outbound SMS test OK")
        log("sms_out", OWNER_CELL, "test_send ok")
        return f"sent sid={sid}", 200
    except Exception as e:
        log("sms_failed", OWNER_CELL, f"test_send: {e}"[:200])
        return f"FAILED: {e}", 200
      
@app.get("/debug")
def debug():
    if request.args.get("token") != WEEKLY_TOKEN:
        return "nope", 403
    c = db()
    events = c.execute(
        "SELECT datetime(ts,'unixepoch'),kind,peer,note FROM events "
        "ORDER BY id DESC LIMIT 20").fetchall()
    msgs = c.execute(
        "SELECT datetime(ts,'unixepoch'),peer,role,substr(body,1,160) "
        "FROM messages ORDER BY id DESC LIMIT 20").fetchall()
    c.close()
    out = ["== events =="]
    out += [f"{t} {k} {p} :: {n}" for t, k, p, n in events]
    out += ["== messages =="]
    out += [f"{t} {p} [{r}] {b}" for t, p, r, b in msgs]
    return "\n".join(out), 200, {"Content-Type": "text/plain"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
