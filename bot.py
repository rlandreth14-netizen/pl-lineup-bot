import os
import asyncio
import logging
import threading
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", 8000))

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- LEAGUES TO WATCH ---
# Season 2025 is correct for games happening in Jan 2026
WATCHED_LEAGUES = {
    39: "Premier League",
    40: "Championship",
    61: "Ligue 1",
    78: "Bundesliga",
    135: "Serie A",
    140: "La Liga"
}

def fetch_data():
    headers = {'x-rapidapi-key': FOOTBALL_API_KEY, 'x-rapidapi-host': 'v3.football.api-sports.io'}
    results = []
    
    # 1. Try Live Games first (Global check)
    try:
        live_url = "https://v3.football.api-sports.io/fixtures?live=all"
        res = requests.get(live_url, headers=headers, timeout=10).json()
        if res.get("response"):
            for item in res["response"]:
                l_id = item['league']['id']
                if l_id in WATCHED_LEAGUES:
                    home = item['teams']['home']['name']
                    away = item['teams']['away']['name']
                    score = f"{item['goals']['home']}-{item['goals']['away']}"
                    results.append(f"🔴 LIVE: {home} {score} {away} ({WATCHED_LEAGUES[l_id]})")
    except Exception as e:
        logger.error(f"Live check failed: {e}")

    # 2. Check Today's Schedule (If no live games or just to be thorough)
    today = datetime.now().strftime('%Y-%m-%d')
    for l_id, l_name in WATCHED_LEAGUES.items():
        try:
            # We use season 2025 for the 25/26 campaign
            url = f"https://v3.football.api-sports.io/fixtures?league={l_id}&season=2025&date={today}"
            res = requests.get(url, headers=headers, timeout=10).json()
            if res.get("response"):
                for item in res["response"]:
                    status = item['fixture']['status']['short']
                    if status == "NS": # Not Started
                        home = item['teams']['home']['name']
                        away = item['teams']['away']['name']
                        time = item['fixture']['date'][11:16]
                        results.append(f"📅 {time} UTC: {home} vs {away} ({l_name})")
        except Exception as e:
            logger.error(f"Schedule check failed for {l_name}: {e}")

    return list(set(results)) # Remove duplicates

# --- BOT COMMANDS ---
async def next_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛰️ Scanning for live games and today's fixtures...")
    fixtures = await asyncio.to_thread(fetch_data)
    
    if not fixtures:
        await update.message.reply_text("Empty pitch! No games found in your tracked leagues for today.")
    else:
        # Sort and send
        msg = "<b>Match Day Report:</b>\n\n" + "\n".join(sorted(fixtures))
        await update.message.reply_text(msg, parse_mode='HTML')

# --- HEALTH CHECK & MAIN ---
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")

def main():
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', PORT), HealthCheck).serve_forever(), daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("next", next_games))
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Ready! Use /next")))
    logger.info("Bot started...")
    app.run_polling(drop_pending_updates=True) # Clears the conflict error

if __name__ == '__main__':
    main()
