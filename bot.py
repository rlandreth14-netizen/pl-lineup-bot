import os
import logging
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= CONFIG =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")

API_BASE = "https://v3.football.api-sports.io"

HEADERS = {
    "x-apisports-key": API_FOOTBALL_KEY
}

LEAGUES = {
    "pl": {"id": 39, "name": "Premier League"},
    "ucl": {"id": 2, "name": "Champions League"},
    "laliga": {"id": 140, "name": "La Liga"},
    "seriea": {"id": 135, "name": "Serie A"},
    "bundesliga": {"id": 78, "name": "Bundesliga"},
    "ligue1": {"id": 61, "name": "Ligue 1"},
}

PAGE_SIZE = 5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= HELPERS =================

def current_season():
    now = datetime.utcnow()
    return now.year if now.month >= 7 else now.year - 1

def api_get(endpoint: str, params: dict):
    try:
        r = requests.get(
            f"{API_BASE}/{endpoint}",
            headers=HEADERS,
            params=params,
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API error: {e}")
        return None

def format_fixture(f):
    date = datetime.fromisoformat(f["fixture"]["date"].replace("Z", ""))
    home = f["teams"]["home"]["name"]
    away = f["teams"]["away"]["name"]
    league = f["league"]["name"]

    return (
        f"⚽ {home} vs {away}\n"
        f"🏆 {league}\n"
        f"🕒 {date:%d %b %H:%M}\n"
    )

# ================= DATA =================

def fetch_upcoming(league_code, page):
    league = LEAGUES[league_code]

    data = api_get("fixtures", {
        "league": league["id"],
        "season": current_season(),
        "next": 20
    })

    if not data or not data.get("response"):
        return []

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    return data["response"][start:end]

def fetch_live():
    data = api_get("fixtures", {"live": "all"})
    return data["response"] if data and data.get("response") else []

# ================= COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ Lineup Checker Bot\n\n"
        "Leagues:\n"
        "/pl /ucl /laliga\n"
        "/seriea /bundesliga /ligue1\n\n"
        "Other:\n"
        "/live – live matches\n"
        "/next – next fixtures"
    )

async def league_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    league = update.message.text.replace("/", "")
    context.user_data["league"] = league
    context.user_data["page"] = 0
    await send_page(update, context)

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "league" not in context.user_data:
        await update.message.reply_text("Pick a league first.")
        return

    context.user_data["page"] += 1
    await send_page(update, context)

async def send_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    league = context.user_data["league"]
    page = context.user_data["page"]

    fixtures = fetch_upcoming(league, page)

    if not fixtures:
        await update.message.reply_text(
            "No upcoming matches found.\n\n"
            "This usually means the competition is between rounds."
        )
        return

    text = "\n".join(format_fixture(f) for f in fixtures)
    await update.message.reply_text(text)

async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    games = fetch_live()

    if not games:
        await update.message.reply_text("No live games right now.")
        return

    msg = ""
    for g in games[:10]:
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        gh = g["goals"]["home"]
        ga = g["goals"]["away"]
        minute = g["fixture"]["status"]["elapsed"]
        league = g["league"]["name"]

        msg += (
            f"🔴 LIVE {minute}'\n"
            f"{home} {gh}–{ga} {away}\n"
            f"🏆 {league}\n\n"
        )

    await update.message.reply_text(msg)

# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("live", live_cmd))

    for cmd in LEAGUES:
        app.add_handler(CommandHandler(cmd, league_cmd))

    logger.info("Bot is running")
    app.run_polling()

if __name__ == "__main__":
    main()
