import os
import requests
import logging
import json
import threading
from datetime import datetime
from pymongo import MongoClient
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
PORT = int(os.getenv("PORT", 8000))

client = MongoClient(MONGO_URI)
db = client['football_bot']
player_collection = db['player_history']

LEAGUE_MAP = {"pl": 47, "championship": 48, "laliga": 87, "seriea": 55, "bundesliga": 54, "ligue1": 53}
POSITION_GROUPS = {'GK': 'G', 'CB': 'D', 'LCB': 'D', 'RCB': 'D', 'LB': 'D', 'RB': 'D', 'LWB': 'W', 'RWB': 'W', 'LM': 'W', 'RM': 'W', 'LW': 'W', 'RW': 'W', 'CDM': 'M', 'LDM': 'M', 'RDM': 'M', 'CM': 'M', 'LCM': 'M', 'RCM': 'M', 'CAM': 'M', 'AM': 'M', 'ST': 'A', 'CF': 'A'}

# --- HEALTH SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")

def run_health_server():
    HTTPServer(('0.0.0.0', PORT), HealthCheckHandler).serve_forever()

# --- DATABASE ---
def update_player_knowledge(lineup_data):
    for p in lineup_data:
        player_collection.update_one({"name": p['name']}, {"$inc": {f"positions.{p['pos']}": 1}}, upsert=True)

def get_usual_position(player_name):
    player = player_collection.find_one({"name": player_name})
    if player and 'positions' in player:
        return max(player['positions'], key=player['positions'].get)
    return None

# --- SCRAPER (MATCHES) ---
def get_league_matches(league_id):
    url = f"https://www.fotmob.com/api/leagues?id={league_id}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        data = requests.get(url, headers=headers, timeout=10).json()
        def find_m(obj):
            res = []
            if isinstance(obj, dict):
                if 'home' in obj and 'away' in obj and 'id' in obj: res.append(obj)
                for v in obj.values(): res.extend(find_m(v))
            elif isinstance(obj, list):
                for i in obj: res.extend(find_m(i))
            return res
        today = datetime.now().strftime('%Y-%m-%d')
        matches = []
        seen = set()
        for m in find_m(data):
            m_id = m.get('id')
            time_str = str(m.get('status', {}).get('utcTime', '')) or str(m.get('time', ''))
            if today in time_str and m_id not in seen:
                h = m['home']['name'] if isinstance(m['home'], dict) else m['home']
                a = m['away']['name'] if isinstance(m['away'], dict) else m['away']
                matches.append({'id': m_id, 'home': h, 'away': a})
                seen.add(m_id)
        return matches
    except: return []

# --- SCRAPER (LINEUPS - REWRITTEN) ---
def scrape_lineup(match_id):
    url = f"https://www.fotmob.com/api/matchDetails?matchId={match_id}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        logger.info(f"🔍 API Request for match {match_id}")
        data = requests.get(url, headers=headers, timeout=10).json()
        
        # Dig into the 'lineup' section
        content = data.get('content', {})
        lineup_root = content.get('lineup', {})
        
        # FotMob can use 'lineup' or 'starters' or a list
        players = []
        
        # Strategy: Check both home and away starters
        for side in ['home', 'away']:
            side_data = lineup_root.get(side, {})
            # Look in 'starting' or 'starters' or 'players'
            starters = side_data.get('starting') or side_data.get('starters')
            
            if starters:
                # starters is often a list of rows/positions
                for item in starters:
                    if isinstance(item, list): # Multi-level list structure
                        for p in item:
                            name = p.get('name', {}).get('fullName')
                            pos = p.get('positionShort', p.get('position'))
                            if name and pos: players.append({'name': name, 'pos': pos})
                    else: # Flat list structure
                        name = item.get('name', {}).get('fullName')
                        pos = item.get('positionShort', item.get('position'))
                        if name and pos: players.append({'name': name, 'pos': pos})

        if not players:
            logger.warning(f"⚠️ No players found in API response for {match_id}")
            return None
            
        logger.info(f"✅ Extracted {len(players)} players for {match_id}")
        return players
    except Exception as e:
        logger.error(f"❌ Scraper failure: {e}")
        return None

# --- TELEGRAM HANDLERS ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"⚽ {k.upper()}", callback_data=f"list_{v}")] for k, v in LEAGUE_MAP.items()]
    await u.message.reply_text("🔍 **Football Edge Finder:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def button(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data.startswith("list_"):
        l_id = q.data.split("_")[1]
        matches = get_league_matches(l_id)
        if not matches:
            await q.edit_message_text("📭 No matches for today.")
            return
        await q.edit_message_text("⬇️ **Select match:**", parse_mode='Markdown')
        for m in matches:
            btn = [[InlineKeyboardButton("📋 Analyze Lineup", callback_data=f"an_{m['id']}")]]
            await q.message.reply_text(f"🏟 **{m['home']} vs {m['away']}**", reply_markup=InlineKeyboardMarkup(btn), parse_mode='Markdown')
    elif q.data.startswith("an_"):
        m_id = q.data.split("_")[1]
        lineup = scrape_lineup(m_id)
        if not lineup:
            await q.message.reply_text("⏳ Lineups not confirmed (usually 60m before KO).")
            return
        update_player_knowledge(lineup)
        alerts = []
        for p in lineup:
            usual = get_usual_position(p['name'])
            if usual and usual != p['pos']:
                u_z, c_z = POSITION_GROUPS.get(usual, 'M'), POSITION_GROUPS.get(p['pos'], 'M')
                if u_z == 'D' and c_z in ['M', 'A']:
                    alerts.append(f"🎯 **{p['name']}** ({usual}➔{p['pos']}): **SOT / Shots**")
                elif u_z in ['A', 'M'] and c_z == 'D':
                    alerts.append(f"⚠️ **{p['name']}** ({usual}➔{p['pos']}): **Fouls / Card**")
        res = "🚨 **EDGES:**\n\n" + ("\n".join(alerts) if alerts else "✅ No changes.")
        await q.message.reply_text(res, parse_mode='Markdown')

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    logger.info("✅ Bot is active...")
    app.run_polling()

if __name__ == '__main__':
    main()
