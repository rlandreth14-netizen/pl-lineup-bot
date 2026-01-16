import os
import requests
import asyncio
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIGURATION ---
# These must be set as Environment Variables in Koyeb
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("API_FOOTBALL_KEY")
# Today is Jan 16, 2026. Season index for these games is 2025.
SEASON = "2025" 

# --- KOYEB HEALTH CHECK SERVER ---
# This prevents Koyeb from thinking the bot has failed
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")

def run_health_server():
    port = int(os.getenv("PORT", 8000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# --- BOT LOGIC ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

LEAGUES = {
    "Premier League": 39,
    "Championship": 40,
    "La Liga": 140,
    "Serie A": 135,
    "Ligue 1": 61,
    "Bundesliga": 78
}

def get_fixtures(league_id):
    headers = {
        'x-rapidapi-key': API_KEY, 
        'x-rapidapi-host': 'v3.football.api-sports.io'
    }
    today = datetime.now().strftime('%Y-%m-%d')
    url = f"https://v3.football.api-sports.io/fixtures?league={league_id}&season={SEASON}&date={today}"
    
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return data.get('response', [])
    except Exception as e:
        logging.error(f"API Error: {e}")
        return []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=str(id))] for name, id in LEAGUES.items()]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a league to check today's lineups:", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    league_id = query.data
    fixtures = get_fixtures(league_id)

    if not fixtures:
        await query.edit_message_text(f"No matches found for today (Season {SEASON}).")
        return

    message = "<b>Today's Matches:</b>\n\n"
    for f in fixtures:
        home = f['teams']['home']['name']
        away = f['teams']['away']['name']
        status = f['fixture']['status']['long']
        message += f"⚽ {home} vs {away}\nStatus: {status}\n\n"
    
    await query.edit_message_text(message, parse_mode='HTML')

def main():
    # 1. Start Health Check server in a background thread
    threading.Thread(target=run_health_server, daemon=True).start()
    
    # 2. Start the Telegram Bot
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
