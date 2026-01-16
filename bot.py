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
# FIX: Changed to lowercase 'api_key' to match your Koyeb settings exactly
API_KEY = os.getenv("api_key") 
SEASON = "2025"

# --- KOYEB HEALTH CHECK ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.getenv("PORT", 8000))
    HTTPServer(('0.0.0.0', port), HealthCheckHandler).serve_forever()

# --- API LOGIC ---
logging.basicConfig(level=logging.INFO)
LEAGUE_MAP = {
    "pl": 39,
    "championship": 40,
    "laliga": 140,
    "seriea": 135,
    "bundesliga": 78,
    "ligue1": 61,
    "ucl": 2
}

player_positions = {}

def get_api_data(endpoint):
    """Call API-Football with correct headers"""
    headers = {
        'x-apisports-key': API_KEY
    }
    url = f"https://v3.football.api-sports.io/{endpoint}"
    logging.info(f"API Call: {endpoint}")
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            # Log any errors returned inside the JSON from the provider
            if data.get('errors'):
                logging.error(f"API Provider Error: {data['errors']}")
            return data.get('response', [])
        else:
            logging.error(f"HTTP Error: {response.status_code}")
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
    
    positions = player_positions[player_id]['positions']
    if len(positions) < 2: return None
    
    usual_pos = max(set(positions[:-1]), key=positions[:-1].count)
    
    if usual_pos != current_pos:
        insights = []
        if usual_pos == 'D' and current_pos in ['M', 'F']:
            insights.append("✅ BACK: Player shots on target (attacking role)")
            insights.append("❌ AVOID: Player fouls (less defensive work)")
        elif usual_pos == 'M' and current_pos == 'D':
            insights.append("✅ BACK: Player to commit 2+ fouls (defensive role)")
            insights.append("✅ BACK: Player to be booked")
        elif usual_pos == 'M' and current_pos == 'F':
            insights.append("✅ BACK: Player shots on target / Anytime Goalscorer")
        
        if insights:
            return {'player': player_name, 'usual': usual_pos, 'current': current_pos, 'insights': insights}
    return None

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⚽ Premier League", callback_data="list_39")],
        [InlineKeyboardButton("🇪🇸 La Liga", callback_data="list_140")],
        [InlineKeyboardButton("🇮🇹 Serie A", callback_data="list_135")],
        [InlineKeyboardButton("🇩🇪 Bundesliga", callback_data="list_78")],
        [InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="list_61")],
        [InlineKeyboardButton("🏆 Championship", callback_data="list_40")],
    ]
    await update.message.reply_text(
        "⚽ **Football Position Analyzer**\nFind betting edges via lineup changes.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.lower().replace("/", "")
    if cmd in LEAGUE_MAP:
        await list_fixtures_func(update, LEAGUE_MAP[cmd], cmd.upper(), from_message=True)

async def list_fixtures_func(update, league_id, name, from_message=False):
    """List fixtures: Try today first, then fall back to next 10 if empty"""
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 1. Try today's date
    fixtures = get_api_data(f"fixtures?league={league_id}&season={SEASON}&date={today}")
    msg_header = f"📅 **{name} Matches Today:**\n\n"
    
    # 2. If no games today, get the next 10 games
    if not fixtures:
        logging.info(f"No games today for {name}, fetching upcoming...")
        fixtures = get_api_data(f"fixtures?league={league_id}&next=10")
        msg_header = f"📅 **Upcoming {name} Matches:**\n\n"

    if not fixtures:
        msg = f"ℹ️ No matches found for {name}."
        if from_message: await update.message.reply_text(msg)
        else: await update.callback_query.edit_message_text(msg)
        return

    if not from_message:
        await update.callback_query.message.reply_text(msg_header, parse_mode='Markdown')
    else:
        await update.message.reply_text(msg_header, parse_mode='Markdown')

    for f in fixtures:
        f_id = f['fixture']['id']
        home = f['teams']['home']['name']
        away = f['teams']['away']['name']
        # Format date for upcoming games
        f_date = f['fixture']['date'][5:10] # MM-DD
        f_time = f['fixture']['date'][11:16] # HH:MM
        
        text = f"⚽ **{home} vs {away}**\n🗓 Date: {f_date} | ⏰ Time: {f_time} UTC"
        btns = [[InlineKeyboardButton("📋 View Lineups & Positions", callback_data=f"lineup_{f_id}")]]
        
        if from_message:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode='Markdown')
        else:
            await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode='Markdown')

async def show_lineups(update: Update, fixture_id: str):
    query = update.callback_query
    await query.answer()
    
    lineups = get_api_data(f"fixtures/lineups?fixture={fixture_id}")
    
    if not lineups:
        await query.message.reply_text("⚠️ Lineups not yet announced (usually 60m before KO).")
        return

    lineup_text = "📊 **LINEUPS & ANALYSIS**\n\n"
    opportunities = []
    
    for team in lineups:
        lineup_text += f"**{team['team']['name']}** ({team['formation']})\n"
        for player in team['startXI']:
            p = player['player']
            lineup_text += f"• {p['name']} ({p['pos']})\n"
            insight = analyze_position_change(p['id'], p['pos'], p['name'])
            if insight: opportunities.append(insight)
        lineup_text += "\n"
    
    if opportunities:
        lineup_text += "🚨 **BETTING OPPORTUNITIES**\n"
        for opp in opportunities:
            lineup_text += f"📍 {opp['player']}: {opp['usual']} → {opp['current']}\n"
            for ins in opp['insights']: lineup_text += f"- {ins}\n"

    btn = [[InlineKeyboardButton("🎯 Live Stats", callback_data=f"stats_{fixture_id}")]]
    await query.message.reply_text(lineup_text, reply_markup=InlineKeyboardMarkup(btn), parse_mode='Markdown')

async def show_stats(update: Update, fixture_id: str):
    query = update.callback_query
    player_data = get_api_data(f"fixtures/players?fixture={fixture_id}")
    
    if not player_data:
        await query.message.reply_text("ℹ️ Live stats available once match begins.")
        return

    msg = "🎯 **LIVE PLAYER STATS**\n\n"
    for team in player_data:
        msg += f"**{team['team']['name']}**\n"
        for p in team['players'][:5]:
            s = p['statistics'][0]
            msg += f"• {p['player']['name']}: 🎯 {s.get('shots', {}).get('on', 0)} SOT | 🚫 {s.get('fouls', {}).get('committed', 0)} Fouls\n"
        msg += "\n"
    await query.message.reply_text(msg, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data.startswith("list_"):
        league_id = int(query.data.split("_")[1])
        name = [k for k, v in LEAGUE_MAP.items() if v == league_id][0]
        await list_fixtures_func(update, league_id, name.upper())
    elif query.data.startswith("lineup_"):
        await show_lineups(update, query.data.split("_")[1])
    elif query.data.startswith("stats_"):
        await show_stats(update, query.data.split("_")[1])

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    for cmd in LEAGUE_MAP.keys():
        app.add_handler(CommandHandler(cmd, handle_text_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == '__main__':
    main()
