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

# Internal only – NOT shown to users
HIGH_OWNERSHIP_THRESHOLD = 16.0  

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

# --- MONGO HELPER ---
def get_db():
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    return client, db

# --- CORE FUNCTIONS ---
def fetch_sofascore_lineup(match_id):
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS)
        if res.status_code != 200:
            return None
        data = res.json()
        players = []
        for side in ['home', 'away']:
            team_data = data.get(side)
            if not team_data:
                continue
            team_name = team_data['team']['name']
            for entry in team_data.get('players', []):
                p = entry.get('player')
                if not p:
                    continue
                players.append({
                    "name": p.get('name', 'Unknown'),
                    "sofa_id": p.get('id'),
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
        return [
            e for e in res.get('events', [])
            if e.get('uniqueTournament', {}).get('id') == 17
        ]
    except Exception as e:
        logging.error(f"SofaScore Events Fetch Error: {e}")
        return []

def detect_high_ownership_benched(match_id, db):
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    if not lineups:
        return None

    started_ids = {l['player_id'] for l in lineups if l.get('minutes', 0) > 0}
    players = list(db.players.find({'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}}))

    alerts = [
        f"🚨 {p['web_name']} — NOT STARTING"
        for p in players
        if p['id'] not in started_ids
    ]

    return "\n".join(alerts) if alerts else None

def detect_tactical_oop(db, match_id_filter=None):
    query = {}
    if match_id_filter:
        query = {"match_id": match_id_filter}

    latest = db.tactical_data.find_one(query, sort=[("last_updated", -1)])
    if not latest:
        return None

    insights = []
    fpl_map = {'GK': 'GK', 'DEF': 'DEF', 'MID': 'MID', 'FWD': 'FWD'}

    for p_sofa in latest['players']:
        fpl_p = db.players.find_one({
            "web_name": {"$regex": f"^{p_sofa['name']}$", "$options": "i"}
        })
        if not fpl_p:
            continue

        sofa_pos = p_sofa.get('tactical_pos', 'Unknown')
        fpl_pos = fpl_map.get(fpl_p.get('position'))
        if not fpl_pos:
            continue

        is_oop = (
            (fpl_pos == 'DEF' and sofa_pos not in ['DEF', 'GK']) or
            (fpl_pos == 'MID' and sofa_pos == 'FWD') or
            (fpl_pos == 'FWD' and sofa_pos in ['MID', 'DEF'])
        )

        if is_oop:
            insights.append(
                f"🔥 {p_sofa['name']} ({p_sofa['team']}): {fpl_pos} ➡️ {sofa_pos}"
            )

    return "\n".join(insights) if insights else None

def get_next_fixtures(db, limit=5):
    now = datetime.now(timezone.utc)
    upcoming = []
    for f in db.fixtures.find({'started': False, 'finished': False}):
        ko_time = f.get('kickoff_time')
        if not ko_time:
            continue
        ko = datetime.fromisoformat(ko_time.replace('Z', '+00:00'))
        if ko > now:
            upcoming.append((ko, f))
    upcoming.sort(key=lambda x: x[0])
    return upcoming[:limit]

# --- BACKGROUND MONITOR ---
def run_monitor():
    while True:
        try:
            time.sleep(60)
            client, db = get_db()
            now = datetime.now(timezone.utc)

            upcoming = db.fixtures.find({
                'kickoff_time': {'$exists': True},
                'finished': False,
                'alert_sent': {'$ne': True}
            })

            for f in upcoming:
                ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
                diff_mins = (ko - now).total_seconds() / 60

                # Confirmed lineups only – 60 minute window
                if 59 <= diff_mins <= 61:
                    logging.info(f"Auto-checking match: {f['team_h_name']} vs {f['team_a_name']}")

                    sofa_events = get_today_sofascore_matches()
                    target_event = next(
                        (e for e in sofa_events
                         if e.get('homeTeam', {}).get('name') == f['team_h_name']
                         or e.get('awayTeam', {}).get('name') == f['team_a_name']),
                        None
                    )

                    msg_parts = [f"📢 *Lineups Out: {f['team_h_name']} vs {f['team_a_name']}*"]

                    if target_event:
                        sofa_lineup = fetch_sofascore_lineup(target_event['id'])
                        if sofa_lineup:
                            db.tactical_data.update_one(
                                {"match_id": target_event['id']},
                                {"$set": {
                                    "home_team": target_event['homeTeam']['name'],
                                    "away_team": target_event['awayTeam']['name'],
                                    "players": sofa_lineup,
                                    "last_updated": datetime.now(timezone.utc)
                                }},
                                upsert=True
                            )

                            oop = detect_tactical_oop(db, target_event['id'])
                            if oop:
                                msg_parts.append(f"\n*Tactical Shifts:*\n{oop}")

                    benched = detect_high_ownership_benched(f['id'], db)
                    if benched:
                        msg_parts.append(f"\n*Key Absences:*\n{benched}")

                    final_msg = "\n".join(msg_parts)

                    for u in db.users.find():
                        try:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                json={
                                    "chat_id": u['chat_id'],
                                    "text": final_msg,
                                    "parse_mode": "Markdown"
                                }
                            )
                        except Exception as e:
                            logging.error(f"Failed to send alert: {e}")

                    db.fixtures.update_one(
                        {'id': f['id']},
                        {'$set': {'alert_sent': True}}
                    )

            client.close()
        except Exception as e:
            logging.error(f"Monitor Loop Error: {e}")

# --- TELEGRAM COMMANDS ---
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    user_id = update.effective_chat.id
    db.users.update_one(
        {'chat_id': user_id},
        {'$set': {'chat_id': user_id, 'joined': datetime.now()}},
        upsert=True
    )

    welcome_msg = (
        "👋 Welcome to the Premier League Lineup Bot!\n\n"
        "I monitor confirmed lineups and alert you when:\n"
        "• Key players are benched\n"
        "• Players are used out of position\n\n"
        "Alerts are sent automatically ~60 minutes before kickoff."
    )

    keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
    await update.message.reply_text(
        welcome_msg,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    client.close()

async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("🔄 Syncing FPL & SofaScore Data...")
    client, db = get_db()
    try:
        base_url = "https://fantasy.premierleague.com/api/"
        bootstrap = requests.get(base_url + "bootstrap-static/").json()

        players = pd.DataFrame(bootstrap['elements'])
        pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
        players['position'] = players['element_type'].map(pos_map)
        players_dict = players[
            ['id', 'web_name', 'position', 'minutes',
             'goals_scored', 'assists', 'total_points', 'selected_by_percent']
        ].to_dict('records')

        db.players.delete_many({})
        db.players.insert_many(players_dict)

        fixtures = requests.get(base_url + "fixtures/").json()
        teams_df = pd.DataFrame(bootstrap['teams'])
        team_map = dict(zip(teams_df['id'], teams_df['name']))

        fixtures_dict = []
        for f in fixtures:
            fixtures_dict.append({
                'id': f['id'],
                'event': f['event'],
                'team_h': f['team_h'],
                'team_a': f['team_a'],
                'team_h_name': team_map.get(f['team_h']),
                'team_a_name': team_map.get(f['team_a']),
                'kickoff_time': f['kickoff_time'],
                'started': f['started'],
                'finished': f['finished']
            })

        db.fixtures.delete_many({})
        db.fixtures.insert_many(fixtures_dict)

        await update.message.reply_text("✅ Sync Complete.")
    except Exception as e:
        logging.error(f"/update error: {e}")
        await update.message.reply_text("⚠️ Failed to sync data.")
    finally:
        client.close()

async def check(update: Update, context: CallbackContext):
    client, db = get_db()
    latest = db.tactical_data.find_one(sort=[("last_updated", -1)])
    if not latest:
        await update.message.reply_text("No tactical data available.")
        client.close()
        return

    msg = f"📊 *Analysis: {latest['home_team']} vs {latest['away_team']}*\n"
    oop = detect_tactical_oop(db, latest['match_id'])
    msg += oop if oop else "No notable tactical shifts."

    await update.message.reply_text(msg, parse_mode="Markdown")
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
            lines = ["📆 *Upcoming Fixtures:*"]
            for ko, f in fixtures:
                lines.append(
                    f"• {f['team_h_name']} vs {f['team_a_name']} "
                    f"({ko.strftime('%d %b %H:%M UTC')})"
                )
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

    client.close()

# --- FLASK ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot Live"

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# --- MAIN ---
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=run_monitor, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(handle_callbacks))

    app.run_polling()
