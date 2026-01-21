import os
import time
import threading
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler

logging.basicConfig(level=logging.INFO)

# --- CONFIG ---
MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')
HIGH_OWNERSHIP_THRESHOLD = 20.0  # %

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

# --- SOFASCORE ---
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
                    "tactical_pos": entry.get('position', 'Unknown'),
                    "team": team_name
                })
        return players
    except Exception as e:
        logging.error(f"SofaScore Fetch Error: {e}")
        return None

def get_today_sofascore_matches():
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS).json()
        return [e for e in res.get('events', []) if e.get('uniqueTournament', {}).get('id') == 17]
    except:
        return []

# --- PLAYER ANALYSIS ---
def detect_high_ownership_benched(match_id, db):
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    if not lineups: return None
    started_ids = {l['player_id'] for l in lineups if l['minutes'] > 0}
    players = list(db.players.find({'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}}))
    alerts = [f"🚨 {p['web_name']} ({p['selected_by_percent']}%) — NOT STARTING" 
              for p in players if p['id'] not in started_ids]
    return "\n".join(alerts) if alerts else None

def detect_tactical_oop(db, match_id_filter=None):
    query = {}
    if match_id_filter:
        query = {"match_id": match_id_filter}
        
    latest = db.tactical_data.find_one(query, sort=[("last_updated", -1)])
    if not latest: return None
    
    insights = []
    for p_sofa in latest['players']:
        fpl_p = db.players.find_one({"web_name": {"$regex": f"^{p_sofa['name']}$", "$options": "i"}})
        if fpl_p:
            fpl_pos = fpl_p['position']
            sofa_pos = p_sofa['tactical_pos']
            # Include full tactical position shifts (e.g., LB → LWB)
            if fpl_pos != sofa_pos:
                insights.append(f"🔥 {p_sofa['name']} ({p_sofa['team']}): {fpl_pos} ➡️ {sofa_pos}")
    return "\n".join(insights) if insights else None

# --- FIXTURE HELPERS ---
def get_next_fixtures(db, limit=5):
    now = datetime.now(timezone.utc)
    upcoming = []
    for f in db.fixtures.find({'started': False, 'finished': False}):
        if not f.get('kickoff_time'): continue
        ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
        if ko > now:
            upcoming.append((ko, f))
    upcoming.sort(key=lambda x: x[0])
    return upcoming[:limit]

# --- TELEGRAM UI ---
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    user_id = update.effective_chat.id
    db.users.update_one({'chat_id': user_id}, {'$set': {'chat_id': user_id, 'joined': datetime.now()}}, upsert=True)
    
    today = datetime.now(timezone.utc).date()
    todays = []

    for f in db.fixtures.find():
        if f.get('kickoff_time'):
            ko = datetime.fromisoformat(f['kickoff_time'].replace('Z','+00:00'))
            if ko.date() == today:
                todays.append((ko, f))

    if not todays:
        keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
        await update.message.reply_text("No games today. You are registered for lineup alerts!", reply_markup=InlineKeyboardMarkup(keyboard))
        client.close()
        return

    msg = ["⚽ *Matches today:*"]
    buttons = []
    for ko, f in todays:
        msg.append(f"• {f['team_h_name']} vs {f['team_a_name']} — {ko.strftime('%H:%M UTC')}")
        buttons.append([InlineKeyboardButton(f"{f['team_h_name']} vs {f['team_a_name']}", callback_data=f"fixture_{f['id']}")])
    
    await update.message.reply_text("\n".join(msg), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    client.close()

async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    client, db = get_db()
    
    if query.data == "next_fixtures":
        fixtures = get_next_fixtures(db)
        if not fixtures:
            await query.edit_message_text("No upcoming fixtures found.")
        else:
            buttons = []
            lines = ["📆 *Upcoming Fixtures:*"]
            for ko, f in fixtures:
                date_str = ko.strftime('%d %b %H:%M UTC')
                lines.append(f"• {f['team_h_name']} vs {f['team_a_name']} — {date_str}")
                buttons.append([InlineKeyboardButton(f"{f['team_h_name']} vs {f['team_a_name']}", callback_data=f"fixture_{f['id']}")])
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    
    elif query.data.startswith("fixture_"):
        match_id = int(query.data.split("_")[1])
        fixture = db.fixtures.find_one({'id': match_id})
        if not fixture:
            await query.edit_message_text("Fixture not found.")
        else:
            msg_parts = [f"⚽ *{fixture['team_h_name']} vs {fixture['team_a_name']}*"]
            # Tactical shifts
            oop = detect_tactical_oop(db, match_id)
            if oop: msg_parts.append(f"\n*Tactical Shifts:*\n{oop}")
            else: msg_parts.append("\n✅ No tactical OOP shifts detected.")
            # Benched high-ownership
            benched = detect_high_ownership_benched(match_id, db)
            if benched: msg_parts.append(f"\n*Benched Assets:*\n{benched}")
            await query.edit_message_text("\n".join(msg_parts), parse_mode="Markdown")
    
    client.close()

# --- UPDATE DATA ---
async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("🔄 Syncing FPL & SofaScore Data...")
    client, db = get_db()
    base_url = "https://fantasy.premierleague.com/api/"

    # FPL Players
    bootstrap = requests.get(base_url + "bootstrap-static/").json()
    players = pd.DataFrame(bootstrap['elements'])
    pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    players['position'] = players['element_type'].map(pos_map)
    players_dict = players[['id', 'web_name', 'position', 'minutes', 'goals_scored', 'assists', 'total_points', 'selected_by_percent']].to_dict('records')
    db.players.delete_many({})
    db.players.insert_many(players_dict)

    # FPL Fixtures
    fixtures = requests.get(base_url + "fixtures/").json()
    teams_df = pd.DataFrame(bootstrap['teams'])
    team_map = dict(zip(teams_df['id'], teams_df['name']))
    fixtures_dict = []
    for f in fixtures:
        fixtures_dict.append({
            'id': f['id'], 'event': f['event'], 'team_h': f['team_h'], 'team_a': f['team_a'],
            'team_h_name': team_map.get(f['team_h'], str(f['team_h'])),
            'team_a_name': team_map.get(f['team_a'], str(f['team_a'])),
            'kickoff_time': f['kickoff_time'], 'started': f['started'], 'finished': f['finished']
        })
    db.fixtures.delete_many({})
    db.fixtures.insert_many(fixtures_dict)

    # Lineups (Minutes)
    lineup_entries = []
    for f in fixtures:
        for s in f.get('stats', []):
            if s.get('identifier') == 'minutes':
                for side in ('h','a'):
                    for p in s.get(side, []):
                        lineup_entries.append({"match_id": f['id'], "player_id": p['element'], "minutes": p['value']})
    db.lineups.delete_many({})
    if lineup_entries: db.lineups.insert_many(lineup_entries)

    # SofaScore Sync
    today_events = get_today_sofascore_matches()
    for event in today_events:
        sofa_lineup = fetch_sofascore_lineup(event['id'])
        if sofa_lineup:
            db.tactical_data.update_one(
                {"match_id": event['id']},
                {"$set": {"home_team": event['homeTeam']['name'], "away_team": event['awayTeam']['name'], "players": sofa_lineup, "last_updated": datetime.now(timezone.utc)}},
                upsert=True
            )

    client.close()
    await update.message.reply_text(f"✅ Sync Complete.")

# --- CHECK COMMAND ---
async def check(update: Update, context: CallbackContext):
    client, db = get_db()
    latest_tactical = db.tactical_data.find_one(sort=[("last_updated", -1)])
    
    if not latest_tactical:
        client.close()
        await update.message.reply_text("No tactical data found. Run /update.")
        return

    msg = f"📊 *Analysis: {latest_tactical['home_team']} vs {latest_tactical['away_team']}*\n"
    oop = detect_tactical_oop(db, latest_tactical['match_id'])
    msg += oop if oop else "✅ No tactical OOP shifts."
    
    client.close()
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- FLASK & MAIN ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot Live"

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT',10000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.run_polling()
