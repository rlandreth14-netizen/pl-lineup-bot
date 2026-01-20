from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler
from pymongo import MongoClient
import os
import pandas as pd
from flask import Flask
import threading
import requests
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)

MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')

HIGH_OWNERSHIP_THRESHOLD = 20.0  # %

# ---------- Helper: Detect abnormal behaviour ----------
def detect_abnormal(match_id):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    if not lineups:
        client.close()
        return "No lineup data available yet."

    lineup_df = pd.DataFrame(lineups)
    players = list(db.players.find({'id': {'$in': lineup_df['player_id'].tolist()}}))
    players_df = pd.DataFrame(players)

    merged = lineup_df.merge(
        players_df[['id', 'web_name', 'position', 'minutes', 'goals_scored', 'assists']],
        left_on='player_id', right_on='id', how='left'
    )

    insights = []

    for _, row in merged.iterrows():
        mins = row['minutes_y'] or 0
        if mins < 300:  # avoid tiny samples
            continue

        avg_attack = (row['goals_scored'] + row['assists']) / (mins / 90)
        match_attack = (row['goals_scored'] + row['assists'])

        if match_attack >= avg_attack * 2 and match_attack > 0:
            insights.append(
                f"🔥 {row['web_name']} ({row['position']}) — abnormal attacking output"
            )

    client.close()
    return "\n".join(insights) if insights else "No abnormal player behaviour detected."


# ---------- Helper: High ownership NOT starting ----------
def detect_high_ownership_benched(match_id):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    started_ids = {l['player_id'] for l in lineups if l['minutes'] > 0}

    players = list(db.players.find({
        'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}
    }))

    alerts = []
    for p in players:
        if p['id'] not in started_ids:
            alerts.append(
                f"🚨 {p['web_name']} ({p['selected_by_percent']}%) — NOT STARTING"
            )

    client.close()
    return "\n".join(alerts)


# ---------- Helper: Next fixtures ----------
def get_next_fixtures(db, limit=5):
    now = datetime.now(timezone.utc)
    upcoming = []

    for f in db.fixtures.find({'started': False, 'finished': False}):
        if not f.get('kickoff_time'):
            continue
        kickoff = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
        if kickoff > now:
            upcoming.append((kickoff, f))

    upcoming.sort(key=lambda x: x[0])
    return upcoming[:limit]


# ---------- Command: update ----------
async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("Pulling latest FPL data...")

    base_url = "https://fantasy.premierleague.com/api/"
    bootstrap = requests.get(base_url + "bootstrap-static/").json()
    fixtures = requests.get(base_url + "fixtures/").json()

    players = pd.DataFrame(bootstrap['elements'])
    pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    players['position'] = players['element_type'].map(pos_map)

    players_dict = players[[
        'id', 'web_name', 'position', 'minutes',
        'goals_scored', 'assists', 'total_points',
        'selected_by_percent'
    ]].to_dict('records')

    fixtures_df = pd.DataFrame(fixtures)
    fixtures_dict = fixtures_df[[
        'id', 'event', 'team_h', 'team_a',
        'kickoff_time', 'started', 'finished'
    ]].to_dict('records')

    lineup_entries = []
    for f in fixtures:
        for s in f.get('stats', []):
            if s.get('identifier') == 'minutes':
                for side in ('h', 'a'):
                    for p in s.get(side, []):
                        lineup_entries.append({
                            "match_id": f['id'],
                            "player_id": p['element'],
                            "minutes": p['value']
                        })

    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    db.players.delete_many({})
    db.players.insert_many(players_dict)

    db.fixtures.delete_many({})
    db.fixtures.insert_many(fixtures_dict)

    db.lineups.delete_many({})
    if lineup_entries:
        db.lineups.insert_many(lineup_entries)

    client.close()

    await update.message.reply_text("✅ Update complete.")


# ---------- Command: start ----------
async def start(update: Update, context: CallbackContext):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    today = datetime.now(timezone.utc).date()

    todays = []
    for f in db.fixtures.find():
        if f.get('kickoff_time'):
            ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
            if ko.date() == today:
                todays.append((ko, f))

    client.close()

    if not todays:
        keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
        await update.message.reply_text("No games today.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    msg = ["⚽ Matches today:"]
    for ko, f in todays:
        msg.append(f"• Match ID {f['id']} — {ko.strftime('%H:%M UTC')}")

    await update.message.reply_text("\n".join(msg))


# ---------- Callback ----------
async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    if query.data == "next_fixtures":
        client = MongoClient(MONGODB_URI)
        db = client['premier_league']
        fixtures = get_next_fixtures(db)
        client.close()

        lines = ["📆 Upcoming fixtures:"]
        for ko, f in fixtures:
            lines.append(f"• Match ID {f['id']} — {ko.strftime('%d %b %H:%M UTC')}")

        await query.edit_message_text("\n".join(lines))


# ---------- Command: check ----------
async def check(update: Update, context: CallbackContext):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    latest = db.lineups.find_one(sort=[("match_id", -1)])
    if not latest:
        client.close()
        await update.message.reply_text("Run /update first.")
        return

    match_id = latest['match_id']
    client.close()

    abnormal = detect_abnormal(match_id)
    benched = detect_high_ownership_benched(match_id)

    message = f"📊 Match ID {match_id}\n\n{abnormal}"
    if benched:
        message += f"\n\n{benched}"

    await update.message.reply_text(message)


# ---------- Flask ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot running"

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))


# ---------- Main ----------
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.run_polling()
