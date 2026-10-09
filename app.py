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
  7. POST /track <- bobthelobstar.com visit tracker (JS beacon on every page).
  8. GET /stats?token=... -> visit dashboard for the owner.
  9. GET /r/<slug> -> logs a trackable visit (QR codes), redirects to the site.

Env vars (set on Render):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_NUMBER (+1xxx, the Twilio number),
  OWNER_CELL (+14806045005), BUSINESS_NAME, GROQ_API_KEY,
  GROQ_MODEL (default openai/gpt-oss-20b),
  SCHEDULING_LINK (optional, e.g. Calendly — if empty the bot collects availability),
  REVIEW_LINK (optional Google review link),
  WEEKLY_TOKEN (secret for the /weekly and /stats endpoints)

Note: the SQLite DB lives on Render's ephemeral filesystem (free tier), so
visit history resets on restarts/redeploys. Weekly visit totals are preserved
via the Monday SMS summary. For permanent history, wire up Turso/Postgres.
"""
import hashlib
import json as _json
import os
import sqlite3
import time
from urllib.parse import urlparse

import requests
from flask import Flask, redirect, request
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client

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

SITE_ORIGINS = {"https://bobthelobstar.com", "https://www.bobthelobstar.com"}
BOT_HINTS = ("bot", "crawl", "spider", "slurp", "mediapartners", "ahrefs", "semrush")


def db():
    c = sqlite3.connect(DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS events("
        "id INTEGER PRIMARY KEY, ts REAL, kind TEXT, peer TEXT, note TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS messages("
        "id INTEGER PRIMARY KEY, ts REAL, peer TEXT, role TEXT, body TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS visits("
        "id INTEGER PRIMARY KEY, ts REAL, page TEXT, ref_domain TEXT, "
        "ua TEXT, ip_hash TEXT)"
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


def _cors_headers():
    origin = request.headers.get("Origin", "")
    if origin in SITE_ORIGINS:
        return {"Access-Control-Allow-Origin": origin}
    return {}

@app.route("/track", methods=["OPTIONS"])
def track_preflight():
    h = _cors_headers()
    h.update({
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    })
    return "", 204, h

@app.post("/track")
def track():
    try:
        data = _json.loads(request.data or b"{}")
    except Exception:
        data = {}
    ua = request.headers.get("User-Agent", "")
    if any(h in ua.lower() for h in BOT_HINTS):
        return "", 204, _cors_headers()
    page = str(data.get("page", "") or "/")[:120]
    if not page.startswith("/"):
        page = "/" + page
    ref = str(data.get("ref", "") or "")[:300]
    ref_domain = ""
    try:
        ref_domain = urlparse(ref).netloc[:120]
    except Exception:
        pass
    if ref_domain in ("bobthelobstar.com", "www.bobthelobstar.com"):
        ref_domain = ""
    ip = (request.headers.get("X-Forwarded-For", "") or
          request.remote_addr or "").split(",")[0].strip()
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else ""
    c = db()
    c.execute(
        "INSERT INTO visits(ts,page,ref_domain,ua,ip_hash) VALUES(?,?,?,?,?)",
        (time.time(), page, ref_domain, ua[:200], ip_hash))
    c.commit()
    c.close()
    return "", 204, _cors_headers()


@app.get("/r/<slug>")
def qr_redirect(slug):
    """Trackable short link (e.g. QR on the business card): logs the visit,
    then sends the visitor to the site."""
    ok = bool(slug) and len(slug) <= 32 and slug.replace("-", "").replace("_", "").isalnum()
    if not ok:
        return "nope", 404
    ua = request.headers.get("User-Agent", "")
    if not any(h in ua.lower() for h in BOT_HINTS):
        ip = (request.headers.get("X-Forwarded-For", "") or
              request.remote_addr or "").split(",")[0].strip()
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else ""
        c = db()
        c.execute(
            "INSERT INTO visits(ts,page,ref_domain,ua,ip_hash) VALUES(?,?,?,?,?)",
            (time.time(), f"/r/{slug}", "qr", ua[:200], ip_hash))
        c.commit()
        c.close()
    return redirect("https://bobthelobstar.com/", code=302)

def _phx(ts):
    return time.strftime("%m/%d %I:%M %p", time.gmtime(ts - 7 * 3600))

@app.get("/stats")
def stats():
    if request.args.get("token") != WEEKLY_TOKEN:
        return "nope", 403
    now = time.time()
    c = db()
    total = c.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    today = c.execute(
        "SELECT COUNT(*) FROM visits WHERE ts>?", (now - 86400,)).fetchone()[0]
    week = c.execute(
        "SELECT COUNT(*) FROM visits WHERE ts>?", (now - 7 * 86400,)).fetchone()[0]
    pages = c.execute(
        "SELECT page, COUNT(*) n FROM visits GROUP BY page "
        "ORDER BY n DESC LIMIT 20").fetchall()
    refs = c.execute(
        "SELECT ref_domain, COUNT(*) n FROM visits WHERE ref_domain<>'' "
        "GROUP BY ref_domain ORDER BY n DESC LIMIT 20").fetchall()
    recent = c.execute(
        "SELECT ts, page, ref_domain FROM visits "
        "ORDER BY id DESC LIMIT 50").fetchall()
    c.close()

    def rows(pairs):
        return "".join(
            f"<tr><td>{p}</td><td>{n}</td></tr>" for p, n in pairs) or \
            "<tr><td colspan=2>No data yet</td></tr>"

    recent_rows = "".join(
        f"<tr><td>{_phx(ts)}</td><td>{page}</td><td>{rd or '-'}</td></tr>"
        for ts, page, rd in recent) or \
        "<tr><td colspan=3>No visits logged yet</td></tr>"

    html = f"""<!doctype html><html><head><meta name="viewport"
content="width=device-width,initial-scale=1"><title>BoB site stats</title>
<style>body{{font-family:sans-serif;background:#0f1420;color:#e8ecf4;
margin:0;padding:16px}}h1{{font-size:20px}}h2{{font-size:15px;color:#8fb4ff;
margin-top:22px}}.cards{{display:flex;gap:10px}}.card{{background:#1a2338;
border-radius:10px;padding:12px 16px;flex:1}}.card b{{font-size:24px;
display:block}}table{{width:100%;border-collapse:collapse;font-size:13px;
margin-top:6px}}td,th{{padding:6px 8px;border-bottom:1px solid #26314d;
text-align:left}}th{{color:#8fb4ff}}.note{{color:#8a93a8;font-size:12px;
margin-top:18px}}</style></head><body>
<h1>bobthelobstar.com visits</h1>
<div class="cards"><div class="card"><b>{total}</b>total</div>
<div class="card"><b>{today}</b>last 24h</div>
<div class="card"><b>{week}</b>last 7 days</div></div>
<h2>Top pages</h2><table><tr><th>Page</th><th>Visits</th></tr>{rows(pages)}</table>
<h2>Top referrers</h2><table><tr><th>Source</th><th>Visits</th></tr>{rows(refs)}</table>
<h2>Recent visits (Phoenix time)</h2>
<table><tr><th>When</th><th>Page</th><th>From</th></tr>{recent_rows}</table>
<p class="note">Counts reset if the Render free tier restarts. Weekly totals are
also texted to you every Monday morning.</p>
</body></html>"""
    return html, 200, {"Content-Type": "text/html"}

@app.post("/voice")
def voice():
    caller = request.values.get("From", "")
    log("missed_call", caller, "forwarded no-answer")
    textback = (f"Sorry we missed your call! This is {BUSINESS} "
                "are you looking for a quote, or is something urgent?")
    try:
        send_sms(caller, textback)
        log("sms_out", caller, textback[:80])
    except Exception as e:
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
    save_msg(frm, body)
    if frm == OWNER_CELL:
        upper = body.upper()
        if upper.startswith("SOLD"):
            parts = body.split()
            if len(parts) >= 2:
                buyer = parts[1]
                review_txt = (
                    f"Hi! Thanks for your purchase with {BUSINESS} "
                    "If everything went well, a quick review would mean a lot."
                    + (f" {REVIEW_LINK}" if REVIEW_LINK else
                       " Just reply here and let us know!")
                )
                try:
                    send_sms(buyer, review_txt)
                    log("review_sent", buyer, "")
                    save_msg(frm, f"Review request sent to {buyer}.")
                except Exception as e:
                    log("sms_failed", buyer, str(e)[:120])
                return "", 200
        if upper.startswith("BOOKED"):
            parts = body.split()
            peer = parts[1] if len(parts) >= 2 else ""
            log("booking", peer, "marked by owner")
            return "", 200
    try:
        reply = groq_reply(frm, body)
    except Exception as e:
        log("groq_failed", frm, str(e)[:120])
        reply = (f"Thanks for reaching out to {BUSINESS}! "
                 "The owner will follow up with you shortly.")
    save_msg(frm, reply)
    try:
        send_sms(frm, reply)
        log("sms_out", frm, reply[:80])
    except Exception as e:
        log("sms_failed", frm, str(e)[:120])
    return "", 200

@app.get("/weekly")
def weekly():
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
    visits_week = c.execute(
        "SELECT COUNT(*) FROM visits WHERE ts>?", (week_ago,)).fetchone()[0]
    top = c.execute(
        "SELECT page, COUNT(*) n FROM visits WHERE ts>? "
        "GROUP BY page ORDER BY n DESC LIMIT 1", (week_ago,)).fetchone()
    c.close()
    summary = (f"{BUSINESS} weekly: {missed} missed calls caught, "
               f"{convos} text conversations, {booked} bookings marked, "
               f"{reviews} review requests sent. "
               f"Site: {visits_week} visits"
               + (f" (top: {top[0]} x{top[1]})" if top else "") + ".")
    try:
        send_sms(OWNER_CELL, summary)
        log("weekly_sent", OWNER_CELL, summary)
    except Exception as e:
        log("sms_failed", OWNER_CELL, str(e)[:120])
    return "ok", 200

@app.get("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
