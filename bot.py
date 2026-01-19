import os
import threading
import requests
import logging
from datetime import datetime, timedelta, timezone  # Added timezone for deprecation fix
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
import time  # For rate limiting delays

# --- 1. SETUP & CONFIG ---
logging.basicConfig(level=logging.INFO)

# No headers needed for TheSportsDB
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

# League mappings for TheSportsDB (updated IDs)
target_leagues = [4328, 4329, 4335, 4331, 4332]  # PL, Championship, La Liga, Bundesliga, Serie A
league_names = {
    4328: "Premier League",
    4329: "Championship",
    4335: "La Liga",
    4331: "Bundesliga",
    4332: "Serie A"
}

# Position mapping (TheSportsDB uses full names)
def map_position(pos):
    mapping = {
        'Centre Back': 'CB',
        'Right Back': 'RB',
        'Left Back': 'LB',
        'Defensive Midfield': 'DM',
        'Central Midfield': 'CM',
        'Right Midfield': 'RM',
        'Left Midfield': 'LM',
        'Attacking Midfield': 'AM',
        'Right Wing': 'RW',
        'Left Wing': 'LW',
        'Striker': 'ST',
        'Forward': 'ST',
        'Midfielder': 'CM',  # Fallbacks
        'Defender': 'CB',
        # Add more as needed based on real data
    }
    return mapping.get(pos, '??')

# --- 2. CACHING & STATS LOGIC ---

async def get_player_form(player_id):
    """Fetches stats for last 5 matches with a 24-hour MongoDB cache. Note: Limited in TheSportsDB - using placeholder."""
    cached_data = cache_collection.find_one({"player_id": player_id})
    if cached_data:
        expiry = cached_data['timestamp'] + timedelta(hours=24)
        if datetime.now(timezone.utc) < expiry:
            return cached_data['stats_text']

    # TheSportsDB doesn't provide per-match player stats like SoT/fouls in free tier. Use player lookup for basics.
    url = f"https://www.thesportsdb.com/api/v1/json/3/lookupplayer.php?id={player_id}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return "⚠️ Stats currently restricted."
            
        data = response.json().get('players', [{}])[0]
        # Example: Use aggregate stats if available (e.g., goals, but no SoT/fouls)
        stats_text = f"Season Goals: {data.get('strGoals', 'N/A')} | Cards: {data.get('strYellowCards', 'N/A')}/{data.get('strRedCards', 'N/A')}"
        
        cache_collection.update_one(
            {"player_id": player_id},
            {"$set": {"stats_text": stats_text, "timestamp": datetime.now(timezone.utc)}},
            upsert=True
        )
        return stats_text
    except Exception as e:
        logging.error(f"Error fetching form for {player_id}: {e}")
        return "⚠️ Detailed stats unavailable in this API."

# --- 3. ANALYZE LINEUPS LOGIC ---

async def analyze_lineups(query, league_id=None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d={today}"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            await query.edit_message_text(f"⚠️ API Error: {response.status_code}. TheSportsDB may be limiting requests.")
            return
        matches_data = response.json().get('events', [])
    except Exception as e:
        logging.error(f"Main API Error: {e}")
        await query.edit_message_text("❌ Connection failed. Please try again in a few moments.")
        return
    
    # Filter matches for target leagues and upcoming
    upcoming_matches = [m for m in matches_data if m['idLeague'] in map(str, target_leagues) and m['strStatus'] != 'Match Finished']
    if league_id:
        upcoming_matches = [m for m in upcoming_matches if m['idLeague'] == league_id]
        if not upcoming_matches:
            await query.edit_message_text("❌ No upcoming matches for the selected league today.")
            return
    
    alerts = []
    MARKETS = {
        "ATTACKING": "🎯 *Target: Over 0.5/1.5 Shots on Target*",
        "DEFENSIVE": "⚠️ *Target: Over 1.5 Fouls Committed*",
        "CONTROL": "🔄 *Target: Over 50.5/70.5 Passes*"
    }

    for match in upcoming_matches:
        m_id = match['idEvent']
        try:
            m_url = f"https://www.thesportsdb.com/api/v1/json/3/lookuplineup.php?id={m_id}"
            m_res = requests.get(m_url, timeout=10)
            if m_res.status_code != 200: continue
            
            details = m_res.json()
            # Lineup structure: details['lineup'] is list of players for both teams
            # Adjust based on real response - may need to separate home/away
            lineups = details.get('lineup', [])
            
            for p in lineups:
                name = p.get('strPlayer')
                p_id = p.get('idPlayer')
                current_pos = map_position(p.get('strPosition', '??'))
                t_name = p.get('strTeam', match['strHomeTeam'] or match['strAwayTeam'])  # Approximate
                
                hist = player_collection.find_one({"name": name})
                if hist and 'positions' in hist:
                    usual_pos = max(hist['positions'], key=hist['positions'].get)
                    alert_msg = ""
                    market_tip = ""

                    if (usual_pos in ['CB', 'RB', 'LB'] and current_pos in ['DM', 'CM', 'RM', 'LM', 'RW', 'LW', 'ST']) or \
                       (usual_pos in ['DM', 'CM'] and current_pos in ['AM', 'ST', 'RW', 'LW']):
                        alert_msg = f"🚀 *FORWARD SHIFT* ({t_name})\n*{name}* at *{current_pos}* (Usual: {usual_pos})"
                        market_tip = MARKETS['ATTACKING']

                    elif (usual_pos in ['ST', 'RW', 'LW', 'AM'] and current_pos in ['CM', 'DM', 'RB', 'LB']) or \
                         (usual_pos in ['CM', 'RM', 'LM'] and current_pos in ['RB', 'LB', 'CB']):
                        alert_msg = f"🛡️ *DEFENSIVE SHIFT* ({t_name})\n*{name}* at *{current_pos}* (Usual: {usual_pos})"
                        market_tip = MARKETS['DEFENSIVE']

                    if alert_msg:
                        form = await get_player_form(p_id)
                        alerts.append(f"{alert_msg}\n{market_tip}\n*Last 5 Form:*\n{form}")
            time.sleep(2)  # Delay for rate limits
        except Exception as e:
            logging.error(f"Error processing match {m_id}: {e}")
            continue

    if not alerts:
        await query.edit_message_text("✅ No major positional changes found in current lineups.")
    else:
        report = "📊 *SCOUT REPORT*\n\n" + "\n---\n".join(alerts)
        await query.edit_message_text(report[:4090], parse_mode="Markdown")

# --- 4. BOT HANDLERS & SERVER ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    for lid in target_leagues:
        name = league_names.get(lid, f"League {lid}")
        kb.append([InlineKeyboardButton(name, callback_data=f'league:{lid}')])
    await update.message.reply_text("Football IQ Bot Online. Choose a league to monitor:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith('league:'):
        league_id = data.split(':')[1]
        name = league_names.get(int(league_id), f"League {league_id}")
        kb = [[InlineKeyboardButton("🔍 Analyze Today's Lineups", callback_data=f'analyze:{league_id}')]]
        await query.edit_message_text(f"Selected {name}.", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data.startswith('analyze:'):
        league_id = data.split(':')[1]
        await query.edit_message_text("⏳ Scanning live data...")
        await analyze_lineups(query, league_id=league_id)

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
