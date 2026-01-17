import os
import requests
import logging
import json
import threading
from bs4 import BeautifulSoup
from datetime import datetime
from pymongo import MongoClient
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
PORT = int(os.getenv("PORT", 8000))

# Database Connection
client = MongoClient(MONGO_URI)
db = client['football_bot']
player_collection = db['player_history']

LEAGUE_MAP = {"pl": 47, "championship": 48, "laliga": 87, "seriea": 55, "bundesliga": 54, "ligue1": 53}

POSITION_GROUPS = {
    'GK': 'G', 'CB': 'D', 'LCB': 'D', 'RCB': 'D', 'LB': 'D', 'RB': 'D',
    'LWB': 'W', 'RWB': 'W', 'LM': 'W', 'RM': 'W', 'LW': 'W', 'RW': 'W',
    'CDM': 'M', 'LDM': 'M', 'RDM': 'M', 'CM': 'M', 'LCM': 'M', 'RCM': 'M',
    'CAM': 'M', 'AM': 'M', 'ST': 'A', 'CF': 'A'
}

# --- HEALTH CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    server.serve_forever()

# --- DATABASE LOGIC ---
def update_player_knowledge(lineup_data):
    for p in lineup_data:
        player_collection.update_one(
            {"name": p['name']},
            {"$inc": {f"positions.{p['pos']}": 1}},
            upsert=True
        )

def get_usual_position(player_name):
    player = player_collection.find_one({"name": player_name})
    if player and 'positions' in player:
        return max(player['positions'], key=player['positions'].get)
    return None

# --- NEW ROBUST SCRAPER LOGIC ---
def get_league_matches(league_id):
    url = f"https://www.fotmob.com/api/leagues?id={league_id}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    try:
        logger.info(f"🚀 Scraping all data for league {league_id}...")
        response = requests.get(url, headers=headers, timeout=15)
        data = response.json()
        
        # We search everywhere for matches (matches, overview, fixtures, etc.)
        def find_matches_in_dict(obj):
            found = []
            if isinstance(obj, dict):
                if 'home' in obj and 'away' in obj and 'id' in obj:
                    found.append(obj)
                for k, v in obj.items():
                    found.extend(find_matches_in_dict(v))
            elif isinstance(obj, list):
                for item in obj:
                    found.extend(find_matches_in_dict(item))
            return found

        all_matches = find_matches_in_dict(data)
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        found_today = []
        seen_ids = set()

        for m in all_matches:
            m_id = m.get('id')
            # Look for today's date in any time field
            utc_time = str(m.get('status', {}).get('utcTime', '')) or str(m.get('time', ''))
            
            if today_str in utc_time and m_id not in seen_ids:
                found_today.append({
                    'id': m_id,
                    'home': m.get('home', {}).get('name') if isinstance(m['home'], dict) else m['home'],
                    'away': m.get('away', {}).get('name') if isinstance(m['away'], dict) else m['away']
                })
                seen_ids.add(m_id)
        
        logger.info(f"✅ Success: Found {len(found_today)} matches for {today_str}")
        return found_today
    except Exception as e:
        logger.error(f"❌ Scraper error: {e}")
        return []

def scrape_lineup(match_id):
    url = f"https://www.fotmob.com/matches/{match_id}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers)
        soup = BeautifulSoup(res.content, 'html.parser')
        script = soup.find('script', id='__NEXT_DATA__')
        data = json.loads(script.string)
        content = data['props']['pageProps']['content']
        
        if 'lineup' not in content or not content['lineup']: return None
            
        players = []
        for side in ['home', 'away']:
            l = content['lineup'].get(side, {})
            if 'starting' in l:
                for p in l['starting']:
                    players.append({'name': p['name']['fullName'], 'pos': p.get('positionStringShort', '??')})
        return players
    except: return None

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"⚽ {k.upper()}", callback_data=f"list_{v}")] for k, v in LEAGUE_MAP.items()]
    await update.message.reply_text("🔍 **Football Analyzer Live:**\nChoose a league to find today's edges:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("list_"):
        l_id = query.data.split("_")[1]
        matches = get_league_matches(l_id)
        
        if not matches:
            try: await query.edit_message_text(f"📭 No matches found for today ({datetime.now().strftime('%Y-%m-%d')}).")
            except BadRequest: pass
            return

        try: await query.edit_message_text("⬇️ **Select a match to analyze:**", parse_mode='Markdown')
        except BadRequest: pass

        for m in matches:
            btn = [[InlineKeyboardButton("📋 Analyze Lineup", callback_data=f"an_{m['id']}")]]
            await query.message.reply_text(f"🏟 **{m['home']} vs {m['away']}**", reply_markup=InlineKeyboardMarkup(btn), parse_mode='Markdown')

    elif query.data.startswith("an_"):
        m_id = query.data.split("_")[1]
        lineup = scrape_lineup(m_id)
        if not lineup:
            await query.message.reply_text("⏳ Lineups not out yet (60m before KO).")
            return
        
        update_player_knowledge(lineup)
        alerts = []
        for p in lineup:
            usual = get_usual_position(p['name'])
            if usual and usual != p['pos']:
                u_zone, c_zone = POSITION_GROUPS.get(usual, 'M'), POSITION_GROUPS.get(p['pos'], 'M')
                if u_zone == 'D' and c_zone in ['M', 'A']:
                    alerts.append(f"🎯 **{p['name']}** ({usual}➔{p['pos']}): **SOT / Shots**")
                elif u_zone in ['A', 'M'] and c_zone == 'D':
                    alerts.append(f"⚠️ **{p['name']}** ({usual}➔{p['pos']}): **Fouls / Card**")
        
        res = "🚨 **EDGES FOUND:**\n\n" + ("\n".join(alerts) if alerts else "✅ No position changes detected.")
        await query.message.reply_text(res, parse_mode='Markdown')

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("✅ Bot is fully operational and listening...")
    app.run_polling()

if __name__ == '__main__':
    main()
