import os
import threading
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler

# --- CONFIG ---
logging.basicConfig(level=logging.INFO)
MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')
HIGH_OWNERSHIP_THRESHOLD = 20.0

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

# --- MONGO HELPER ---
def get_db():
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    return client, db

# --- SOFASCORE HELPERS ---
def fetch_sofascore_lineup(match_id):
    """Pulls the tactical lineup from SofaScore."""
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS)
        if res.status_code != 200: return None
        data = res.json()
        players = []
        for side in ['home', 'away']:
            team_name = data[side]['team']['name']
            for entry in data.get(side, {}).get('players', []):
                p = entry['player']
                players.append({
                    "name": p['name'],
                    "sofa_id": p['id'],
                    "tactical_pos": entry.get('position', 'Unknown'), # D, M, F
                    "team": team_name
                })
        return players
    except:
        return None

def get_today_sofascore_matches():
    """Finds today's EPL match IDs on SofaScore."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS).json()
        # EPL Unique Tournament ID is 17
        return [e for e in res.get('events', []) if e.get('uniqueTournament', {}).get('id') == 17]
    except:
        return []

# --- OOP DETECTION (READ FROM MONGO) ---
def get_stored_oop_insights(match_id):
    client, db = get_db()
    # 1. Get the tactical lineup we saved earlier
    tactical = db.tactical_data.find_one({"match_id": int(match_id)})
    if not tactical:
        client.close()
        return "No tactical lineup stored for this match yet."

    insights = []
    for p_sofa in tactical['players']:
        # 2. Bridge to FPL player via name (case-insensitive regex)
        fpl_p = db.players.find_one({"web_name": {"$regex": f"^{p_sofa['name']}$", "$options": "i"}})
        
        if fpl_p:
            # 3. Compare Positions
            fpl_pos = fpl_p['position'] # DEF, MID, FWD
            sofa_pos = p_sofa['tactical_pos'] # D, M, F
            
            is_oop = False
            if fpl_pos == 'DEF' and sofa_pos in ['M', 'F']: is_oop = True
            elif fpl_pos == 'MID' and sofa_pos == 'F': is_oop = True
            
            if is_oop:
                insights.append(f"🔥 *OOP ALERT:* {p_sofa['name']} ({p_sofa['team']})\n"
                                f"FPL: {fpl_pos} ➡️ Playing: {sofa_pos}")
    
    client.close()
    return "\n\n".join(insights) if insights else "✅ No tactical OOP shifts detected."

# --- UPDATED DATA FUNCTION ---
async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("🔄 Syncing FPL & SofaScore Tactical Data...")
    client, db = get_db()
    
    # 1. Standard FPL Pull
    base_url = "https://fantasy.premierleague.com/api/"
    bootstrap = requests.get(base_url + "bootstrap-static/").json()
    players = pd.DataFrame(bootstrap['elements'])
    pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    players['position'] = players['element_type'].map(pos_map)
    
    db.players.delete_many({})
    db.players.insert_many(players[['id', 'web_name', 'position', 'minutes', 'goals_scored', 'assists', 'selected_by_percent']].to_dict('records'))

    # 2. SofaScore Sync (The New "Save to Mongo" Part)
    today_events = get_today_sofascore_matches()
    matches_synced = 0
    
    for event in today_events:
        sofa_id = event['id']
        lineup = fetch_sofascore_lineup(sofa_id)
        
        if lineup:
            # We store it using a mapping to find it later
            # You can match by Team Names to link FPL Fixture ID to Sofa ID
            db.tactical_data.update_one(
                {"sofa_match_id": sofa_id},
                {"$set": {
                    "match_id": sofa_id, # Simplified for the test
                    "home_team": event['homeTeam']['name'],
                    "away_team": event['awayTeam']['name'],
                    "players": lineup,
                    "last_updated": datetime.now(timezone.utc)
                }},
                upsert=True
            )
            matches_synced += 1

    client.close()
    await update.message.reply_text(f"✅ FPL data updated.\n📡 {matches_synced} tactical lineups cached from SofaScore.")

# --- UPDATED CHECK COMMAND ---
async def check(update: Update, context: CallbackContext):
    client, db = get_db()
    # Fetch the most recent match we have tactical data for
    latest_tactical = db.tactical_data.find_one(sort=[("last_updated", -1)])
    client.close()

    if not latest_tactical:
        await update.message.reply_text("No tactical data found. Run /update first.")
        return

    m_id = latest_tactical['match_id']
    oop_report = get_stored_oop_insights(m_id)
    
    msg = (f"🏟 *Tactical Report: {latest_tactical['home_team']} vs {latest_tactical['away_team']}*\n\n"
           f"{oop_report}")
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- (Rest of your Flask/Main code remains the same) ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot running"

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("check", check))
    app.run_polling()
