import os
import asyncio
import logging
import threading
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", 8000))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- LEAGUES ---
LEAGUES = {
    "Premier League": 39,
    "La Liga": 140,
    "Bundesliga": 78,
    "Ligue 1": 61,
    "Serie A": 135
}

# --- API LOGIC ---
def get_league_fixtures(league_id):
    headers = {'x-rapidapi-key': API_KEY, 'x-rapidapi-host': 'v3.football.api-sports.io'}
    today = datetime.now().strftime('%Y-%m-%d')
    # Season 2025 is the correct start year for Jan 2026 games
    url = f"https://v3.football.api-sports.io/fixtures?league={league_id}&season=2025&date={today}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10).json()
        fixtures = response.get("response", [])
        if not fixtures:
            return "No games found for today."
        
        lines = []
        for f in fixtures:
            home = f['teams']['home']['name']
            away = f['teams']['away']['name']
            time = f['fixture']['date'][11:16]
            status = f['fixture']['status']['short']
            lines.append(f"⏰ {time} | {home} vs {away} ({status})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=str(lid))] for name, lid in LEAGUES.items()]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a league to check today's lineups:", reply_markup=reply_markup)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    league_id = query.data
    
    await query.edit_message_text(text="Searching API... please wait.")
    results = await asyncio.to_thread(get_league_fixtures, league_id)
    await query.edit_message_text(text=results)

# --- KOYEB HEALTH CHECK ---
class Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")

def run_health():
    HTTPServer(('0.0.0.0', PORT), Health).serve_forever()

# --- MAIN ---
if __name__ == '__main__':
    threading.Thread(target=run_health, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    logger.info("Bot is alive...")
    app.run_polling(drop_pending_updates=True)
