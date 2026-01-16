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
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("API_FOOTBALL_KEY")
SEASON = "2025" 

# --- KOYEB HEALTH CHECK ---
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
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Map the commands from your screenshot to League IDs
LEAGUE_MAP = {
    "pl": 39,
    "championship": 40,
    "laliga": 140,
    "seriea": 135,
    "bundesliga": 78,
    "ligue1": 61,
    "ucl": 2
}

def get_fixtures(league_id):
    headers = {'x-rapidapi-key': API_KEY, 'x-rapidapi-host': 'v3.football.api-sports.io'}
    today = datetime.now().strftime('%Y-%m-%d')
    url = f"https://v3.football.api-sports.io/fixtures?league={league_id}&season={SEASON}&date={today}"
    
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return data.get('response', [])
    except Exception as e:
        logging.error(f"API Error: {e}")
        return []

# Unified function to handle both button clicks and text commands
async def show_league_fixtures(update: Update, league_id: str, league_name: str):
    fixtures = get_fixtures(league_id)
    
    if not fixtures:
        text = f"No matches found for {league_name} today (Season {SEASON})."
    else:
        text = f"<b>Today's {league_name} Matches:</b>\n\n"
        for f in fixtures:
            home = f['teams']['home']['name']
            away = f['teams']['away']['name']
            status = f['fixture']['status']['long']
            time = f['fixture']['date'][11:16] # Gets HH:MM
            text += f"⚽ {home} vs {away}\n⏰ {time} | {status}\n\n"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='HTML')
    else:
        await update.message.reply_text(text, parse_mode='HTML')

# Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name.upper(), callback_data=str(id))] for name, id in LEAGUE_MAP.items()]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a league or type a command (e.g., /pl):", reply_markup=reply_markup)

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This detects which command was used (e.g., 'pl' from '/pl')
    command = update.message.text.lower().replace("/", "")
    if command in LEAGUE_MAP:
        await show_league_fixtures(update, LEAGUE_MAP[command], command.upper())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Find the league name from the ID to show in the message
    league_name = next((name for name, id in LEAGUE_MAP.items() if str(id) == query.data), "League")
    await show_league_fixtures(update, query.data, league_name.upper())

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    
    # Add handlers for every command in your Telegram menu
    for cmd in LEAGUE_MAP.keys():
        application.add_handler(CommandHandler(cmd, handle_text_command))
    
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("Bot is starting with all commands...")
    application.run_polling()

if __name__ == '__main__':
    main()
