import logging
import os
import asyncio
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

# ===================== CONFIG =====================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # set in env
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")  # set in env

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

# ===================== API =====================

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

# ===================== HELPERS =====================

def format_fixture(fx):
    date = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", ""))
    home = fx["teams"]["home"]["name"]
    away = fx["teams"]["away"]["name"]
    league = fx["league"]["name"]

    return f"⚽ {home} vs {away}\n🏆 {league}\n🕒 {date:%d %b %H:%M}\n"

async def fetch_fixtures(league_code: str, page: int):
    league = LEAGUES[league_code]

    data = api_get("fixtures", {
        "league": league["id"],
        "next": 20
    })

    if not data or not data.get("response"):
        return []

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    return data["response"][start:end]

# ===================== COMMANDS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ Lineup Checker Bot\n\n"
        "Commands:\n"
        "/pl – Premier League\n"
        "/ucl – Champions League\n"
        "/laliga\n"
        "/seriea\n"
        "/bundesliga\n"
        "/ligue1\n\n"
        "/next – next fixtures"
    )

async def league_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.replace("/", "")
    context.user_data["league"] = cmd
    context.user_data["page"] = 0

    await send_page(update, context)

async def next_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "league" not in context.user_data:
        await update.message.reply_text("Select a league first.")
        return

    context.user_data["page"] += 1
    await send_page(update, context)

async def send_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    league = context.user_data["league"]
    page = context.user_data["page"]

    fixtures = await fetch_fixtures(league, page)

    if not fixtures:
        await update.message.reply_text(
            "No upcoming matches found.\n\n"
            "This can happen if:\n"
            "• No fixtures scheduled\n"
            "• Competition between rounds\n"
            "• API temporarily unavailable"
        )
        return

    text = "\n".join(format_fixture(f) for f in fixtures)
    await update.message.reply_text(text)

# ===================== MAIN =====================

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("next", next_page))

    for cmd in LEAGUES.keys():
        app.add_handler(CommandHandler(cmd, league_command))

    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
