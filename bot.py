import os
import requests
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
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Bot is Healthy")

def run_health_server():
    port = int(os.getenv("PORT", 8000))
    HTTPServer(('0.0.0.0', port), HealthCheckHandler).serve_forever()

# --- API LOGIC ---
logging.basicConfig(level=logging.INFO)
LEAGUE_MAP = {"pl": 39, "championship": 40, "laliga": 140, "seriea": 135, "bundesliga": 78, "ligue1": 61, "ucl": 2}

def get_api_data(endpoint):
    headers = {'x-rapidapi-key': API_KEY, 'x-rapidapi-host': 'v3.football.api-sports.io'}
    try:
        response = requests.get(f"https://v3.football.api-sports.io/{endpoint}", headers=headers)
        return response.json().get('response', [])
    except Exception as e:
        logging.error(f"API Error: {e}"); return []

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name.upper(), callback_data=f"list_{id}")] for name, id in LEAGUE_MAP.items()]
    await update.message.reply_text("Select a league to find betting edges:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.lower().replace("/", "")
    if cmd in LEAGUE_MAP:
        await list_fixtures(update, LEAGUE_MAP[cmd], cmd.upper())

async def list_fixtures(update, league_id, name):
    today = datetime.now().strftime('%Y-%m-%d')
    fixtures = get_api_data(f"fixtures?league={league_id}&season={SEASON}&date={today}")
    
    if not fixtures:
        msg = f"No matches found for {name} today."
        if update.callback_query: await update.callback_query.edit_message_text(msg)
        else: await update.message.reply_text(msg)
        return

    for f in fixtures:
        f_id = f['fixture']['id']
        home = f['teams']['home']['name']
        away = f['teams']['away']['name']
        status = f['fixture']['status']['short']
        btn = [[InlineKeyboardButton("📋 View Lineups & Positions", callback_data=f"lineup_{f_id}")]]
        text = f"⚽ <b>{home} vs {away}</b>\nStatus: {status}"
        
        if update.callback_query: await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btn), parse_mode='HTML')
        else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btn), parse_mode='HTML')

async def show_lineups(update: Update, fixture_id: str):
    query = update.callback_query
    lineups = get_api_data(f"fixtures/lineups?fixture={fixture_id}")
    
    if not lineups:
        await query.edit_message_text("Lineups not yet announced (usually 60 mins before KO).")
        return

    msg = "📊 <b>TACTICAL LINEUPS</b>\n\n"
    for team in lineups:
        msg += f"<b>{team['team']['name']} ({team['formation']})</b>\n"
        for player in team['startXI']:
            p = player['player']
            # Spotting out-of-position players:
            msg += f"• {p['name']} ({p['pos']}) - No: {p['number']}\n"
        msg += "\n"
    
    # Add a button to check live betting stats (fouls/shots)
    btn = [[InlineKeyboardButton("🎯 Check Player Props (Live)", callback_data=f"stats_{fixture_id}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(btn), parse_mode='HTML')

async def show_stats(update: Update, fixture_id: str):
    query = update.callback_query
    stats = get_api_data(f"fixtures/statistics?fixture={fixture_id}")
    
    if not stats:
        await query.edit_message_text("Live stats available once match begins.")
        return

    msg = "🎯 <b>LIVE PLAYER PROPS DATA</b>\n\n"
    # Note: API provides team-level stats primarily; player-level requires 'players' endpoint
    player_data = get_api_data(f"fixtures/players?fixture={fixture_id}")
    
    for team in player_data:
        msg += f"<b>{team['team']['name']}</b>\n"
        for p in team['players'][:5]: # Show top 5 players for brevity
            s = p['statistics'][0]
            msg += f"{p['player']['name']}: 🎯 {s['shots']['on']} SOT | ⚠️ {s['fouls']['committed']} FLS\n"
        msg += "\n"

    await query.edit_message_text(msg, parse_mode='HTML')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data.startswith("list_"): await list_fixtures(update, query.data.split("_")[1], "League")
    elif query.data.startswith("lineup_"): await show_lineups(update, query.data.split("_")[1])
    elif query.data.startswith("stats_"): await show_stats(update, query.data.split("_")[1])

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    for cmd in LEAGUE_MAP.keys(): app.add_handler(CommandHandler(cmd, handle_text_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == '__main__': main()
