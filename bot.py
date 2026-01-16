import os
import requests
import logging
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("api_key") 

# SAVED PREVIOUS CONFIG: 
# SEASON = "2024" (If no-season trick fails, we will put this back in the URL)

# --- KOYEB HEALTH CHECK ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")
    def log_message(self, format, *args): pass

def run_health_server():
    port = int(os.getenv("PORT", 8000))
    HTTPServer(('0.0.0.0', port), HealthCheckHandler).serve_forever()

# --- API LOGIC ---
logging.basicConfig(level=logging.INFO)
LEAGUE_MAP = {
    "pl": 39, "championship": 40, "laliga": 140, 
    "seriea": 135, "bundesliga": 78, "ligue1": 61, "ucl": 2
}

player_positions = {}

def get_api_data(endpoint):
    headers = {'x-apisports-key': API_KEY}
    url = f"https://v3.football.api-sports.io/{endpoint}"
    logging.info(f"API Call: {endpoint}")
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('errors'):
                logging.error(f"API Provider Error: {data['errors']}")
            return data.get('response', [])
        return []
    except Exception as e:
        logging.error(f"API Exception: {e}")
        return []

def analyze_position_change(player_id, current_pos, player_name):
    if player_id not in player_positions:
        player_positions[player_id] = {'name': player_name, 'positions': []}
    player_positions[player_id]['positions'].append(current_pos)
    if len(player_positions[player_id]['positions']) > 5:
        player_positions[player_id]['positions'].pop(0)
    pos_list = player_positions[player_id]['positions']
    if len(pos_list) < 2: return None
    usual_pos = max(set(pos_list[:-1]), key=pos_list[:-1].count)
    if usual_pos != current_pos:
        insights = []
        if usual_pos == 'D' and current_pos in ['M', 'F']:
            insights.append("✅ BACK: Player shots on target")
        elif usual_pos == 'M' and current_pos == 'D':
            insights.append("✅ BACK: Player to commit 2+ fouls")
        if insights:
            return {'player': player_name, 'usual': usual_pos, 'current': current_pos, 'insights': insights}
    return None

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"⚽ {k.upper()}", callback_data=f"list_{v}")] for k, v in LEAGUE_MAP.items()]
    await update.message.reply_text("⚽ **Position Analyzer**\nReady to find betting edges.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.lower().replace("/", "")
    if cmd in LEAGUE_MAP:
        await list_fixtures_func(update, LEAGUE_MAP[cmd], cmd.upper(), from_message=True)

async def list_fixtures_func(update, league_id, name, from_message=False):
    # TRICK: We are removing '&season=' to see if the API defaults to the active season
    dates_to_check = [
        datetime.now().strftime('%Y-%m-%d'),
        (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    ]
    
    fixtures = []
    found_date = ""

    for d in dates_to_check:
        # THE TRICK LINE: No season parameter used here
        results = get_api_data(f"fixtures?league={league_id}&date={d}")
        if results:
            fixtures, found_date = results, d
            break

    if not fixtures:
        msg = f"ℹ️ No {name} matches found for Today or Tomorrow. The Free Plan may be restricting this league's live data."
        if from_message: await update.message.reply_text(msg)
        else: await update.callback_query.edit_message_text(msg)
        return

    msg_header = f"📅 **{name} Matches ({found_date}):**\n"
    target = update.message if from_message else update.callback_query.message
    await target.reply_text(msg_header, parse_mode='Markdown')

    for f in fixtures:
        home, away = f['teams']['home']['name'], f['teams']['away']['name']
        f_time, f_id = f['fixture']['date'][11:16], f['fixture']['id']
        text = f"⚽ **{home} vs {away}**\n⏰ {f_time} UTC"
        btns = [[InlineKeyboardButton("📋 View Lineups", callback_data=f"lineup_{f_id}")]]
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode='Markdown')

async def show_lineups(update: Update, fixture_id: str):
    query = update.callback_query
    await query.answer()
    lineups = get_api_data(f"fixtures/lineups?fixture={fixture_id}")
    if not lineups:
        await query.message.reply_text("⚠️ Lineups usually appear 60m before KO.")
        return
    res = "📊 **ANALYSIS**\n\n"
    opps = []
    for team in lineups:
        res += f"**{team['team']['name']}**\n"
        for p in team['startXI']:
            res += f"• {p['player']['name']} ({p['player']['pos']})\n"
            insight = analyze_position_change(p['player']['id'], p['player']['pos'], p['player']['name'])
            if insight: opps.append(insight)
    if opps:
        res += "\n🚨 **OPPORTUNITIES**\n"
        for o in opps:
            res += f"📍 {o['player']} ({o['usual']}→{o['current']})\n" + "\n".join([f"- {i}" for i in o['insights']])
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
    for cmd in LEAGUE_MAP.keys(): app.add_handler(CommandHandler(cmd, handle_text_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == '__main__': main()
