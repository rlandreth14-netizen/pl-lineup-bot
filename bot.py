import os
import asyncio
import logging
import threading
import requests
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", 8000))

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- KOYEB HEALTH CHECK WORKAROUND ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Healthy")
    def log_message(self, format, *args): return

def run_health_server():
    httpd = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    httpd.serve_forever()

# --- FOOTBALL LOGIC ---
LEAGUES = {
    "Premier League": 39,
    "Championship": 40,
    "Ligue 1": 61,
    "Bundesliga": 78,
    "Serie A": 135,
    "La Liga": 140
}

def fetch_fixtures():
    """Fetches games for today across top leagues."""
    today = datetime.now().strftime('%Y-%m-%d')
    all_fixtures = []
    
    headers = {
        'x-rapidapi-key': FOOTBALL_API_KEY,
        'x-rapidapi-host': 'v3.football.api-sports.io'
    }

    for name, league_id in LEAGUES.items():
        # NOTE: Season 2025 covers the 2025/2026 period
        url = f"https://v3.football.api-sports.io/fixtures?league={league_id}&season=2025&date={today}"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            data = response.json()
            if data.get("response"):
                for item in data["response"]:
                    home = item['teams']['home']['name']
                    away = item['teams']['away']['name']
                    time = item['fixture']['date'][11:16]
                    all_fixtures.append(f"⚽ {name}: {home} vs {away} ({time} UTC)")
        except Exception as e:
            logger.error(f"Error fetching {name}: {e}")
            
    return all_fixtures

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Lineup Checker is active!\nUse /next to see today's fixtures.")

async def next_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Checking today's top fixtures...")
    
    # Run API call in a thread to avoid blocking the bot
    fixtures = await asyncio.to_thread(fetch_fixtures)
    
    if not fixtures:
        await update.message.reply_text("📅 No top-tier fixtures found for today (Jan 16).")
    else:
        message = "<b>Today's Fixtures:</b>\n\n" + "\n".join(fixtures)
        await update.message.reply_text(message, parse_mode='HTML')

# --- MAIN RUNNER ---
def main():
    # Start Koyeb heart-beat
    threading.Thread(target=run_health_server, daemon=True).start()

    # Build Bot
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("next", next_games))
    app.add_handler(CommandHandler("leagues", next_games)) # Alias for convenience

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True) # Clears the 'Conflict' error on start

if __name__ == '__main__':
    main()
