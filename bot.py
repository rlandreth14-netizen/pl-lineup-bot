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
                    "tactical_pos": entry.get('position', 'Unknown'),
                    "team": team_name
                })
        return players
    except Exception as e:
        logging.error(f"SofaScore Error: {e}")
        return None

def get_today_sofascore_matches():
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS).json()
        return [e for e in res.get('events', []) if e.get('uniqueTournament', {}).get('id') == 17]
    except:
        return []

# --- CORE LOGIC ---
async def start(update: Update, context: CallbackContext):
    """Entry point command."""
    client, db = get_db()
    today = datetime.now(timezone.utc).date()
    todays = []

    # Check if database has data
    if db.fixtures.count_documents({}) == 0:
        await update.message.reply_text("👋 Database is empty. Please run /update first to pull the latest data!")
        client.close()
        return

    for f in db.fixtures.find():
        if f.get('kickoff_time'):
            ko = datetime.fromisoformat(f['kickoff_time'].replace('Z','+00:00'))
            if ko.date() == today:
                todays.append((ko, f))
    client.close()

    if not todays:
        keyboard = [[InlineKeyboardButton("📆 Show Next fixtures", callback_data="next_fixtures")]]
        await update.message.reply_text("No games scheduled for today.", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        msg = ["⚽ *Matches today:*"]
        for ko, f in todays:
            msg.append(f"• {f['team_h_name']} vs {f['team_a_name']} — {ko.strftime('%H:%M UTC')}")
        await update.message.reply_text("\n".join(msg), parse_mode="Markdown")

async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("🔄 Syncing FPL & SofaScore Tactical Data...")
    client, db = get_db()
    
    # 1. FPL Pull
    base_url = "https://fantasy.premierleague.com/api/"
    bootstrap = requests.get(base_url + "bootstrap-static/").json()
    players = pd.DataFrame(bootstrap['elements'])
    pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    players['position'] = players['element_type'].map(pos_map)
    
    db.players.delete_many({})
    db.players.insert_many(players[['id', 'web_name', 'position', 'minutes', 'goals_scored', 'assists', 'selected_by_percent']].to_dict('records'))

    # 2. Fixtures Pull
    fixtures = requests.get(base_url + "fixtures/").json()
    teams_df = pd.DataFrame(bootstrap['teams'])
    team_map = dict(zip(teams_df['id'], teams_df['name']))
    
    fixtures_dict = []
    for f in fixtures:
        fixtures_dict.append({
            'id': f['id'],
            'team_h_name': team_map.get(f['team_h']),
            'team_a_name': team_map.get(f['team_a']),
            'kickoff_time': f['kickoff_time'],
            'started': f['started'],
            'finished': f['finished']
        })
    db.fixtures.delete_many({})
    db.fixtures.insert_many(fixtures_dict)

    # 3. SofaScore Lineup Sync
    today_events = get_today_sofascore_matches()
    for event in today_events:
        lineup = fetch_sofascore_lineup(event['id'])
        if lineup:
            db.tactical_data.update_one(
                {"match_id": event['id']},
                {"$set": {
                    "home_team": event['homeTeam']['name'],
                    "away_team": event['awayTeam']['name'],
                    "players": lineup,
                    "last_updated": datetime.now(timezone.utc)
                }},
                upsert=True
            )

    client.close()
    await update.message.reply_text("✅ Sync Complete. Try /start or /check now.")

async def check(update: Update, context: CallbackContext):
    client, db = get_db()
    latest = db.tactical_data.find_one(sort=[("last_updated", -1)])
    if not latest:
        await update.message.reply_text("No tactical data saved. Use /update.")
        client.close()
        return

    report = [f"📊 *Tactical Check: {latest['home_team']} vs {latest['away_team']}*"]
    for p in latest['players']:
        # Simple Logic: Check if player name exists in FPL DB
        fpl_p = db.players.find_one({"web_name": {"$regex": p['name'], "$options": "i"}})
        if fpl_p and fpl_p['position'] == 'DEF' and p['tactical_pos'] in ['M', 'F']:
            report.append(f"🔥 *OOP:* {p['name']} is playing as {p['tactical_pos']}!")
            
    client.close()
    await update.message.reply_text("\n".join(report) if len(report) > 1 else "No OOP shifts found.", parse_mode="Markdown")

async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    if query.data == "next_fixtures":
        # Simplified for brevity
        await query.edit_message_text("Check back soon or run /update for the latest schedule.")

# --- FLASK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot running"

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# --- MAIN ---
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # THE MISSING REGISTRATIONS:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    
    logging.info("Starting polling...")
    app.run_polling()
