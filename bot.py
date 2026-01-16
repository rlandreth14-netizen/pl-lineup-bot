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
API_KEY = os.getenv("api_key") 

# TIME MACHINE SETTINGS
TEST_DATE = "2024-01-20"
TEST_SEASON = "2023" # The 2023/24 season

# --- KOYEB HEALTH CHECK ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Time Machine Active")
    def log_message(self, format, *args): pass

def run_health_server():
    port = int(os.getenv("PORT", 8000))
    HTTPServer(('0.0.0.0', port), HealthCheckHandler).serve_forever()

# --- API LOGIC ---
logging.basicConfig(level=logging.INFO)
LEAGUE_MAP = {"pl": 39, "ligue1": 61, "laliga": 140, "seriea": 135}

# Memory to track position changes
player_positions = {}

def get_api_data(endpoint):
    headers = {'x-apisports-key': API_KEY}
    url = f"https://v3.football.api-sports.io/{endpoint}"
    logging.info(f"🚀 Time Machine API Call: {endpoint}")
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        return data.get('response', [])
    except Exception as e:
        logging.error(f"❌ API Error: {e}")
        return []

def analyze_position(player_id, current_pos, player_name):
    # For the TEST, let's simulate that we've seen this player before.
    # We will trigger a fake 'Alert' for any Midfielder (M) to show you the logic.
    if current_pos == 'M':
        return {
            'player': player_name, 
            'usual': 'D', 
            'current': 'M', 
            'insights': ["✅ BACK: Player shots on target", "✅ BACK: Over 0.5 Assists"]
        }
    return None

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"⏳ Test {k.upper()}", callback_data=f"list_{v}")] for k, v in LEAGUE_MAP.items()]
    await update.message.reply_text(
        "🕒 **TIME MACHINE BOT**\nTesting logic using data from **Jan 20, 2024**.\n\nSelect a league to see historical lineups:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def list_fixtures_func(update, league_id, name):
    # Calling specific historical date
    fixtures = get_api_data(f"fixtures?league={league_id}&season={TEST_SEASON}&date={TEST_DATE}")

    if not fixtures:
        await update.callback_query.edit_message_text(f"❌ No matches found for {name} on {TEST_DATE}.")
        return

    await update.callback_query.message.reply_text(f"📅 **{name} matches from {TEST_DATE}:**")

    for f in fixtures:
        home, away = f['teams']['home']['name'], f['teams']['away']['name']
        f_id = f['fixture']['id']
        btns = [[InlineKeyboardButton("📋 Run Position Analysis", callback_data=f"lineup_{f_id}")]]
        await update.callback_query.message.reply_text(
            f"🏟️ {home} vs {away}", 
            reply_markup=InlineKeyboardMarkup(btns)
        )

async def show_lineups(update: Update, fixture_id: str):
    query = update.callback_query
    await query.answer()
    
    lineups = get_api_data(f"fixtures/lineups?fixture={fixture_id}")
    if not lineups:
        await query.message.reply_text("⚠️ No lineup data found for this historical match.")
        return

    res = "📊 **ANALYSIS RESULTS**\n\n"
    opps = []
    
    for team in lineups:
        res += f"**{team['team']['name']} Starting XI:**\n"
        for p in team['startXI']:
            p_name = p['player']['name']
            p_pos = p['player']['pos'] # G, D, M, F
            res += f"• {p_name} ({p_pos})\n"
            
            # Run the analyzer logic
            insight = analyze_position(p['player']['id'], p_pos, p_name)
            if insight: opps.append(insight)

    if opps:
        res += "\n🚨 **OPPORTUNITY DETECTED!**\n"
        for o in opps:
            res += f"📍 *{o['player']}* (Usual: {o['usual']} → Today: {o['current']})\n"
            res += "\n".join([f"  {i}" for i in o['insights']]) + "\n"
    
    await query.message.reply_text(res, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data.startswith("list_"):
        l_id = int(query.data.split("_")[1])
        l_name = [k for k, v in LEAGUE_MAP.items() if v == l_id][0].upper()
        await list_fixtures_func(update, l_id, l_name)
    elif query.data.startswith("lineup_"):
        await show_lineups(update, query.data.split("_")[1])

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == '__main__': main()
