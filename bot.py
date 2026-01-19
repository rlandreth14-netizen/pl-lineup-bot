import os
import threading
import requests
import logging
from datetime import datetime, timedelta
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- 1. SETUP & CONFIG ---
logging.basicConfig(level=logging.INFO)

# MongoDB Setup
MONGO_URI = os.environ.get("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client['football_bot']
player_collection = db['player_history']
cache_collection = db['player_stats_cache']

# Flask for Render Health Checks
app = Flask(__name__)
@app.route('/')
def health(): return "Bot Active", 200

# --- 2. CACHING & STATS LOGIC ---

async def get_player_form(player_id):
    """Fetches stats for last 5 matches with a 24-hour MongoDB cache."""
    # Check if we have a valid cache entry
    cached_data = cache_collection.find_one({"player_id": player_id})
    if cached_data:
        expiry = cached_data['timestamp'] + timedelta(hours=24)
        if datetime.utcnow() < expiry:
            return cached_data['stats_text']

    # If no cache or expired, fetch from API
    url = f"https://www.fotmob.com/api/playerData?id={player_id}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        recent_matches = data.get('recentMatches', [])[:5]
        
        stats_lines = []
        for m in recent_matches:
            # Safely extract stats
            s = m.get('stats', {})
            sot = s.get('Shots on target', 0)
            fouls = s.get('Fouls committed', 0)
            stats_lines.append(f"◽ `SoT: {sot} | Fls: {fouls}`")
        
        stats_text = "\n".join(stats_lines) if stats_lines else "No recent stat data available."
        
        # Update Cache
        cache_collection.update_one(
            {"player_id": player_id},
            {"$set": {"stats_text": stats_text, "timestamp": datetime.utcnow()}},
            upsert=True
        )
        return stats_text
    except Exception as e:
        logging.error(f"Error fetching form for {player_id}: {e}")
        return "⚠️ Stats unavailable."

# --- 3. ANALYZE LINEUPS LOGIC ---

async def analyze_lineups(query):
    # Fotmob API - Today's matches
    url = "https://www.fotmob.com/api/allmatches?timezone=Europe/London"
    matches_data = requests.get(url).json()
    
    # Target specific leagues (e.g., Premier League id: 47)
    target_leagues = [47, 42, 87, 54, 55] # PL, La Liga, Ligue 1, Serie A, Bunesliga
    leagues = [l for l in matches_data.get('leagues', []) if l['id'] in target_leagues]
    
    alerts = []
    MARKETS = {
        "ATTACKING": "🎯 *Target: Over 0.5/1.5 Shots on Target*",
        "DEFENSIVE": "⚠️ *Target: Over 1.5 Fouls Committed*",
        "CONTROL": "🔄 *Target: Over 50.5/70.5 Passes*"
    }

    for league in leagues:
        for match in league.get('matches', []):
            if not match.get('status', {}).get('started'): # Only check matches not yet started
                m_id = match['id']
                try:
                    m_url = f"https://www.fotmob.com/api/matchDetails?matchId={m_id}"
                    details = requests.get(m_url).json()
                    lineups = details.get('content', {}).get('lineup', {}).get('lineup', [])
                    
                    for team in lineups:
                        t_name = team.get('teamName')
                        for player_list in team.get('players', []):
                            for p in player_list:
                                name = p['name']['fullName']
                                p_id = p['id']
                                current_pos = p.get('positionShort', '??')
                                
                                # Check Database for "Usual" position
                                hist = player_collection.find_one({"name": name})
                                if hist and 'positions' in hist:
                                    usual_pos = max(hist['positions'], key=hist['positions'].get)
                                    alert_msg = ""
                                    market_tip = ""

                                    # LOGIC: Forward Shift
                                    if (usual_pos in ['CB', 'RB', 'LB'] and current_pos in ['DM', 'CM', 'RM', 'LM', 'RW', 'LW', 'ST']) or \
                                       (usual_pos in ['DM', 'CM'] and current_pos in ['AM', 'ST', 'RW', 'LW']):
                                        alert_msg = f"🚀 *FORWARD SHIFT* ({t_name})\n*{name}* at *{current_pos}* (Usual: {usual_pos})"
                                        market_tip = MARKETS['ATTACKING']

                                    # LOGIC: Defensive Shift
                                    elif (usual_pos in ['ST', 'RW', 'LW', 'AM'] and current_pos in ['CM', 'DM', 'RB', 'LB']) or \
                                         (usual_pos in ['CM', 'RM', 'LM'] and current_pos in ['RB', 'LB', 'CB']):
                                        alert_msg = f"🛡️ *DEFENSIVE SHIFT* ({t_name})\n*{name}* at *{current_pos}* (Usual: {usual_pos})"
                                        market_tip = MARKETS['DEFENSIVE']

                                    if alert_msg:
                                        form = await get_player_form(p_id)
                                        alerts.append(f"{alert_msg}\n{market_tip}\n*Last 5 Form:*\n{form}")
                except: continue

    if not alerts:
        await query.edit_message_text("✅ No major positional changes found in current lineups.")
    else:
        report = "📊 *SCOUT REPORT*\n\n" + "\n---\n".join(alerts)
        await query.edit_message_text(report[:4090], parse_mode="Markdown")

# --- 4. BOT HANDLERS & SERVER ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("🔍 Analyze Today's Lineups", callback_query_data='analyze')]]
    await update.message.reply_text("Football IQ Bot Online. Monitoring lineups...", reply_markup=InlineKeyboardMarkup(kb))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'analyze':
        await query.edit_message_text("⏳ Scanning live data...")
        await analyze_lineups(query)

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    threading.Thread(target=run_flask, daemon=True).start()
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
