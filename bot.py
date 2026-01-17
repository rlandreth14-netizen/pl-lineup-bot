import os
import requests
import logging
import json
import threading
from datetime import datetime
from pymongo import MongoClient
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# --- RECURSIVE PLAYER EXTRACTOR ---
def find_players_in_json(obj):
    players = []
    if isinstance(obj, dict):
        # Match pattern for FotMob player objects
        if ('name' in obj and 'position' in obj) or ('name' in obj and 'positionShort' in obj):
            name_data = obj.get('name')
            full_name = name_data.get('fullName') if isinstance(name_data, dict) else name_data
            pos = obj.get('positionShort') or obj.get('position')
            # isFirstEleven is true for starters, false for subs
            if full_name and pos and obj.get('isFirstEleven', False):
                players.append({'name': full_name, 'pos': pos})
        
        for value in obj.values():
            players.extend(find_players_in_json(value))
    elif isinstance(obj, list):
        for item in obj:
            players.extend(find_players_in_json(item))
    return players

# --- SCRAPERS ---
def get_league_matches(league_id):
    url = f"https://www.fotmob.com/api/leagues?id={league_id}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}
    try:
        data = requests.get(url, headers=headers, timeout=10).json()
        today = datetime.now().strftime('%Y-%m-%d')
        matches, seen = [], set()
        
        def search_m(o):
            if isinstance(o, dict):
                if 'home' in o and 'away' in o and 'id' in o:
                    m_id = o['id']
                    time_val = str(o.get('status', {}).get('utcTime', '')) or str(o.get('time', ''))
                    if today in time_val and m_id not in seen:
                        h = o['home']['name'] if isinstance(o['home'], dict) else o['home']
                        a = o['away']['name'] if isinstance(o['away'], dict) else o['away']
                        matches.append({'id': m_id, 'home': h, 'away': a})
                        seen.add(m_id)
                for v in o.values(): search_m(v)
            elif isinstance(o, list):
                for i in o: search_m(i)
        
        search_m(data)
        return matches
    except: return []

def scrape_lineup(match_id):
    url = f"https://www.fotmob.com/api/matchDetails?matchId={match_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    try:
        logger.info(f"🔍 Deep API Scan: {match_id}")
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        
        # Scrape starters using the recursive finder
        all_found = find_players_in_json(data)
        
        # Remove duplicates
        unique_list = {p['name']: p for p in all_found}.values()
        
        if len(unique_list) < 15: # Expecting 22, but 15+ is a safe 'found' threshold
            logger.warning(f"⚠️ Only {len(unique_list)} players found. Lineup likely not official.")
            return None
            
        logger.info(f"✅ Found {len(unique_list)} starting players.")
        return list(unique_list)
    except Exception as e:
        logger.error(f"❌ Scraper Error: {e}")
        return None

# --- TELEGRAM ---
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
            await q.message.reply_text("⏳ Lineups not confirmed (usually 60m before kick-off).")
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
        res = "🚨 **EDGES:**\n\n" + ("\n".join(alerts) if alerts else "✅ No changes detected.")
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
