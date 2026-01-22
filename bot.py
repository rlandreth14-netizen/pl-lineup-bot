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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

# --- MONGO HELPER ---
def get_db():
    client = MongoClient(MONGODB_URI)
    return client, client['premier_league']

# --- SOFASCORE ---
def fetch_sofascore_lineup(match_id, retries=2):
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"
    for _ in range(retries):
        try:
            res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
            if res.status_code != 200:
                time.sleep(2)
                continue
            data = res.json()
            players = []
            for side in ['home', 'away']:
                team = data.get(side)
                if not team:
                    continue
                for p in team.get('players', []):
                    players.append({
                        "name": p['player']['name'],
                        "sofa_id": p['player']['id'],
                        "tactical_pos": p.get('position', 'UNK'),
                        "team": team['team']['name']
                    })
            return players
        except Exception as e:
            logging.error(f"SofaScore lineup error: {e}")
            time.sleep(2)
    return None

def get_today_sofascore_matches():
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10).json()
        return [e for e in res.get('events', []) if e.get('uniqueTournament', {}).get('id') == 17]
    except:
        return []

# --- ANALYSIS ---
def detect_high_ownership_benched(match_id, db):
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    if not lineups:
        return None
    started = {l['player_id'] for l in lineups if l.get('minutes', 0) > 0}
    players = db.players.find({'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}})
    alerts = [f"🚨 {p['web_name']} — NOT STARTING" for p in players if p['id'] not in started]
    return "\n".join(alerts) if alerts else None

def detect_tactical_oop(db, match_id_filter=None):
    latest = db.tactical_data.find_one(
        {"match_id": match_id_filter} if match_id_filter else {},
        sort=[("last_updated", -1)]
    )
    if not latest:
        return None

    insights = []

    # Allowed attacking / meaningful shifts
    POSITION_SHIFTS = {
        'DEF': ['LWB', 'RWB', 'LB', 'RB'],
        'MID': ['CAM', 'RM', 'LM', 'RW', 'LW'],
        'FWD': ['ST']
    }

    for p in latest['players']:
        fpl_p = db.players.find_one({
            "web_name": {"$regex": f"^{p['name']}$", "$options": "i"}
        })
        if not fpl_p:
            continue

        fpl_pos = fpl_p['position']
        sofa_pos = p.get('tactical_pos', 'UNK')

        if sofa_pos in POSITION_SHIFTS.get(fpl_pos, []):
            insights.append(
                f"🔥 {p['name']} ({p['team']}): {fpl_pos} ➡️ {sofa_pos}"
            )

    return "\n".join(insights) if insights else None

# --- FIXTURES ---
def get_next_fixtures(db, limit=5):
    now = datetime.now(timezone.utc)
    fixtures = []
    for f in db.fixtures.find({'finished': False}):
        ko = f.get('kickoff_time')
        if not ko:
            continue
        ko_dt = datetime.fromisoformat(ko.replace('Z', '+00:00'))
        if ko_dt > now:
            fixtures.append((ko_dt, f))
    fixtures.sort(key=lambda x: x[0])
    return fixtures[:limit]

# --- MONITOR ---
def run_monitor():
    while True:
        time.sleep(60)
        client, db = get_db()
        now = datetime.now(timezone.utc)

        for f in db.fixtures.find({'finished': False, 'alert_sent': {'$ne': True}}):
            ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
            mins = (ko - now).total_seconds() / 60
            if 59 <= mins <= 61:
                msg = [f"📢 *Lineups Out: {f['team_h_name']} vs {f['team_a_name']}*"]

                events = get_today_sofascore_matches()
                event = next((e for e in events if
                              f['team_h_name'] in (e['homeTeam']['name'], e['awayTeam']['name'])), None)

                if event:
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
                        oop = detect_tactical_oop(db, event['id'])
                        if oop:
                            msg.append(f"\n*Tactical Shifts:*\n{oop}")

                benched = detect_high_ownership_benched(f['id'], db)
                if benched:
                    msg.append(f"\n*Benched Assets:*\n{benched}")

                for u in db.users.find():
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id": u['chat_id'], "text": "\n".join(msg), "parse_mode": "Markdown"}
                    )

                db.fixtures.update_one({'id': f['id']}, {'$set': {'alert_sent': True}})

        client.close()

# --- TELEGRAM ---
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    chat_id = update.effective_chat.id
    db.users.update_one({'chat_id': chat_id}, {'$set': {'joined': datetime.now()}}, upsert=True)

    msg = (
        "👋 *Welcome to the Premier League Lineup Bot*\n\n"
        "• Confirms lineups 60 mins before kickoff\n"
        "• Flags tactical position shifts\n"
        "• Alerts when key assets are benched\n\n"
        "You’re now registered for alerts."
    )

    keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    client.close()

async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    client, db = get_db()

    if query.data == "next_fixtures":
        fixtures = get_next_fixtures(db)
        if not fixtures:
            await query.edit_message_text("No upcoming fixtures.")
        else:
            msg = ["📆 *Upcoming Fixtures:*"]
            for ko, f in fixtures:
                msg.append(f"• {f['team_h_name']} vs {f['team_a_name']} ({ko:%d %b %H:%M UTC})")
            await query.edit_message_text("\n".join(msg), parse_mode="Markdown")

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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.run_polling()
