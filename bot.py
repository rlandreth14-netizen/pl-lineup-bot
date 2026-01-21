import os
import time
import threading
import logging
import requests
import pandas as pd
import unicodedata
from datetime import datetime, timezone
from flask import Flask
from pymongo import MongoClient
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext

logging.basicConfig(level=logging.INFO)

# --- CONFIG ---
MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

# --- POSITION MAPS ---
SOFA_POSITION_GROUPS = {
    "DEF": ["LB", "RB", "CB", "LCB", "RCB", "LWB", "RWB"],
    "MID": ["CDM", "CM", "LCM", "RCM", "CAM", "LM", "RM", "LW", "RW"],
    "FWD": ["ST", "CF", "SS"]
}

ADVANCED_POSITIONS = ["CAM", "LW", "RW", "SS", "ST", "LWB", "RWB"]

# --- DB ---
def get_db():
    client = MongoClient(MONGODB_URI)
    return client, client['premier_league']

# --- UTILITIES ---
def normalize_name(name):
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('utf-8')
    return name.lower().strip()

def extract_surname(name):
    return normalize_name(name).split()[-1]

# --- SOFASCORE ---
def fetch_sofascore_lineup(match_id):
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"
    res = requests.get(url, headers=SOFASCORE_HEADERS)
    if res.status_code != 200:
        return None

    data = res.json()
    players = []

    for side in ['home', 'away']:
        team = data[side]['team']['name']
        for entry in data[side].get('players', []):
            players.append({
                "name": entry['player']['name'],
                "team": team,
                "tactical_pos": entry.get('position', 'UNK')
            })

    return players

def get_today_sofascore_matches():
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    res = requests.get(url, headers=SOFASCORE_HEADERS).json()
    return [e for e in res.get('events', []) if e.get('uniqueTournament', {}).get('id') == 17]

# --- PLAYER MATCHING ---
def match_sofa_to_fpl(sofa_player, db):
    surname = extract_surname(sofa_player['name'])
    candidates = []

    for p in db.players.find():
        if surname in normalize_name(p['web_name']):
            candidates.append(p)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x.get('minutes', 0), reverse=True)
    return candidates[0]

def map_sofa_role(pos):
    for role, positions in SOFA_POSITION_GROUPS.items():
        if pos in positions:
            return role
    return "UNK"

# --- STEP 3: OPPORTUNITY SCORING ---
def calculate_opportunity_scores(db, match_id):
    doc = db.tactical_data.find_one({"match_id": match_id})
    if not doc:
        return None

    results = []

    for p in doc['players']:
        fpl = match_sofa_to_fpl(p, db)
        if not fpl:
            continue

        shot_score = 0
        foul_score = 0

        fpl_role = fpl['position']
        sofa_pos = p['tactical_pos']
        sofa_role = map_sofa_role(sofa_pos)

        # --- Shot Logic ---
        if fpl_role == "DEF" and sofa_role in ["MID", "FWD"]:
            shot_score += 3
        if sofa_pos in ["CAM", "LW", "RW", "ST"]:
            shot_score += 2
        if fpl.get('minutes', 0) > 600:
            shot_score += 1
        if (fpl.get('goals_scored', 0) + fpl.get('assists', 0)) / max(fpl.get('minutes', 1) / 90, 1) > 0.4:
            shot_score += 2

        # --- Foul Logic ---
        if fpl_role == "DEF" and sofa_pos in ["LB", "RB", "LWB", "RWB"]:
            foul_score += 2
        if fpl_role == "MID" and sofa_pos in ["CDM", "CM"]:
            foul_score += 1
        if fpl.get('minutes', 0) > 600:
            foul_score += 1

        if shot_score >= 4 or foul_score >= 3:
            results.append(
                f"⭐ {p['name']} ({p['team']}) — Shots:{shot_score} | Fouls:{foul_score}"
            )

    return "\n".join(results) if results else None

# --- MONITOR ---
def run_monitor():
    while True:
        try:
            time.sleep(60)
            client, db = get_db()
            now = datetime.now(timezone.utc)

            fixtures = db.fixtures.find({'finished': False, 'alert_sent': {'$ne': True}})
            for f in fixtures:
                ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
                mins = (ko - now).total_seconds() / 60

                if 55 <= mins <= 65:
                    events = get_today_sofascore_matches()
                    match = next((e for e in events if f['team_h_name'] in e['homeTeam']['name']), None)
                    if not match:
                        continue

                    lineup = fetch_sofascore_lineup(match['id'])
                    if not lineup:
                        continue

                    db.tactical_data.update_one(
                        {"match_id": match['id']},
                        {"$set": {
                            "players": lineup,
                            "home_team": match['homeTeam']['name'],
                            "away_team": match['awayTeam']['name'],
                            "last_updated": datetime.now(timezone.utc)
                        }},
                        upsert=True
                    )

                    msg = [f"📢 *Confirmed Lineups & Betting Edges*"]
                    opportunities = calculate_opportunity_scores(db, match['id'])
                    if opportunities:
                        msg.append(f"\n*Player Opportunities:*\n{opportunities}")

                    for u in db.users.find():
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                            json={
                                "chat_id": u['chat_id'],
                                "text": "\n".join(msg),
                                "parse_mode": "Markdown"
                            }
                        )

                    db.fixtures.update_one({'id': f['id']}, {'$set': {'alert_sent': True}})

            client.close()

        except Exception as e:
            logging.error(f"Monitor Error: {e}")

# --- BOT ---
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    db.users.update_one(
        {'chat_id': update.effective_chat.id},
        {'$set': {'chat_id': update.effective_chat.id, 'joined': datetime.now()}},
        upsert=True
    )
    client.close()
    await update.message.reply_text("✅ You’ll now receive confirmed lineup betting edges.")

# --- FLASK ---
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Bot Live"

def run_flask():
    app_flask.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# --- MAIN ---
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=run_monitor, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()
