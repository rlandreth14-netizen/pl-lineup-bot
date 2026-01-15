import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ================= CONFIG =================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "YOUR_API_FOOTBALL_KEY")
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
HEALTH_CHECK_PORT = int(os.getenv("PORT", "8000"))

# IMPORTANT:
# API-Football uses the *start year* of the season
# 2025 = 2025/26 season (still valid in Jan–May 2026)
CURRENT_SEASON = 2025


LEAGUES = {
    'pl': {'id': 39, 'name': 'Premier League', 'emoji': '🏴'},
    'ucl': {'id': 2, 'name': 'Champions League', 'emoji': '⭐'},
    'laliga': {'id': 140, 'name': 'La Liga', 'emoji': '🇪🇸'},
    'seriea': {'id': 135, 'name': 'Serie A', 'emoji': '🇮🇹'},
    'bundesliga': {'id': 78, 'name': 'Bundesliga', 'emoji': '🇩🇪'},
    'ligue1': {'id': 61, 'name': 'Ligue 1', 'emoji': '🇫🇷'}
}

POSITION_MAP = {'G': 'Goalkeeper', 'D': 'Defender', 'M': 'Midfielder', 'F': 'Forward'}


# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ================= STORAGE =================

player_history: Dict[int, "PlayerStats"] = {}
user_preferences: Dict[int, Set[str]] = {}


# ================= HEALTH CHECK =================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Bot is running')

    def log_message(self, *_):
        pass


def run_health_server():
    HTTPServer(('0.0.0.0', HEALTH_CHECK_PORT), HealthCheckHandler).serve_forever()


# ================= PLAYER MODEL =================

class PlayerStats:
    def __init__(self, pid: int, name: str):
        self.id = pid
        self.name = name
        self.positions: List[str] = []
        self.fouls_per_90 = 0.0
        self.cards_per_90 = 0.0
        self.shots_per_90 = 0.0

    def add_position(self, pos: str):
        self.positions.append(pos)
        if len(self.positions) > 10:
            self.positions.pop(0)

    def usual_position(self) -> str:
        return max(set(self.positions), key=self.positions.count) if self.positions else "Unknown"

    def is_out_of_position(self, current: str) -> bool:
        return len(self.positions) >= 3 and current != self.usual_position()


# ================= API =================

async def api_get(endpoint: str, params: Dict = None):
    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{API_FOOTBALL_BASE}/{endpoint}",
            headers=headers,
            params=params,
            timeout=10
        ) as r:
            if r.status == 200:
                return await r.json()
            logger.error(f"API error {r.status}")
            return None


# ================= FIXED FIXTURE FETCH =================

async def get_upcoming_matches(league_codes: List[str], hours_ahead: int = 168):
    today = datetime.utcnow()
    future = today + timedelta(hours=hours_ahead)
    matches = []

    for code in league_codes:
        league = LEAGUES[code]

        # CRITICAL FIX:
        # Do NOT combine season + date range
        params = {
            'league': league['id'],
            'from': today.strftime('%Y-%m-%d'),
            'to': future.strftime('%Y-%m-%d')
        }

        data = await api_get('fixtures', params)
        if data and data.get('response'):
            for m in data['response']:
                m['league_code'] = code
                matches.append(m)

        await asyncio.sleep(0.25)

    return sorted(matches, key=lambda x: x['fixture']['date'])


# ================= TODAY FIX =================

async def get_today_matches(league_codes: List[str]):
    today = datetime.utcnow().strftime('%Y-%m-%d')
    matches = []

    for code in league_codes:
        params = {
            'league': LEAGUES[code]['id'],
            'date': today
        }

        data = await api_get('fixtures', params)
        if data and data.get('response'):
            for m in data['response']:
                m['league_code'] = code
                matches.append(m)

    return matches


# ================= COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_preferences.setdefault(update.effective_user.id, {'pl', 'ucl'})
    await update.message.reply_text("⚽ Bot ready. Use /next or /today")


async def next_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leagues = user_preferences.setdefault(update.effective_user.id, {'pl', 'ucl'})
    await update.message.reply_text("🔍 Fetching upcoming matches...")

    matches = await get_upcoming_matches(list(leagues))
    if not matches:
        await update.message.reply_text("No upcoming matches found.")
        return

    msg = "📅 Upcoming Matches:\n\n"
    for m in matches[:20]:
        kickoff = datetime.fromisoformat(m['fixture']['date'].replace('Z', '+00:00'))
        msg += (
            f"{LEAGUES[m['league_code']]['emoji']} "
            f"{m['teams']['home']['name']} vs {m['teams']['away']['name']}\n"
            f"🕐 {kickoff:%a %d %b %H:%M} | 🆔 `{m['fixture']['id']}`\n\n"
        )

    await update.message.reply_text(msg)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leagues = user_preferences.setdefault(update.effective_user.id, {'pl', 'ucl'})
    matches = await get_today_matches(list(leagues))

    if not matches:
        await update.message.reply_text("No matches today.")
        return

    msg = "📅 Today's Matches:\n\n"
    for m in matches:
        kickoff = datetime.fromisoformat(m['fixture']['date'].replace('Z', '+00:00'))
        msg += (
            f"{LEAGUES[m['league_code']]['emoji']} "
            f"{m['teams']['home']['name']} vs {m['teams']['away']['name']}\n"
            f"🕐 {kickoff:%H:%M} | 🆔 `{m['fixture']['id']}`\n\n"
        )

    await update.message.reply_text(msg)


# ================= MAIN =================

def main():
    Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("next", next_matches))
    app.add_handler(CommandHandler("today", today))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
