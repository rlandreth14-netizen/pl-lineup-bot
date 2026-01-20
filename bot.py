import os
import threading
import logging
from datetime import datetime, timezone
import requests
import pandas as pd
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler

# Import your pre-match module
from pre_match_lineup import process_prematch_lineup

logging.basicConfig(level=logging.INFO)

# --- ENV VARIABLES ---
MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')
HIGH_OWNERSHIP_THRESHOLD = 20.0  # %

# --- MONGO HELPER ---
def get_db():
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    return client, db

# --- TEAM NAME HELPER ---
def get_team_name(db, team_id):
    team = db.teams.find_one({'_id': team_id})
    return team['name'] if team else str(team_id)

# --- HISTORICAL STORAGE HELPER ---
def save_gameweek_stats(gameweek, players_df):
    os.makedirs("historical_stats", exist_ok=True)
    file_path = f"historical_stats/gameweek_{gameweek}.csv"
    players_df.to_csv(file_path, index=False)
    logging.info(f"Saved historical stats to {file_path}")

# --- DETECT ABNORMAL PLAYER PERFORMANCE ---
def detect_abnormal(match_id):
    client, db = get_db()
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
        mins = row.get('minutes_y', 0) or 0
        if mins < 300:
            continue

        avg_attack = (row['goals_scored'] + row['assists']) / (mins / 90)
        match_attack = row['goals_scored'] + row['assists']

        if match_attack >= avg_attack * 2 and match_attack > 0:
            insights.append(
                f"🔥 {row['web_name']} ({row['position']}) — abnormal attacking output"
            )

    client.close()
    return "\n".join(insights) if insights else "No abnormal player behaviour detected."

# --- DETECT HIGH-OWNERSHIP BENCHED PLAYERS ---
def detect_high_ownership_benched(match_id):
    client, db = get_db()
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    started_ids = {l['player_id'] for l in lineups if l['minutes'] > 0}

    players = list(
        db.players.find({'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}})
    )

    alerts = [
        f"🚨 {p['web_name']} ({p['selected_by_percent']}%) — NOT STARTING"
        for p in players if p['id'] not in started_ids
    ]

    client.close()
    return "\n".join(alerts)

# --- GET NEXT FIXTURES ---
def get_next_fixtures(db, limit=5):
    now = datetime.now(timezone.utc)
    upcoming = []

    for f in db.fixtures.find({'started': False, 'finished': False}):
        if not f.get('kickoff_time'):
            continue

        ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
        if ko > now:
            upcoming.append((ko, f))

    upcoming.sort(key=lambda x: x[0])
    return upcoming[:limit]

# --- UPDATE DATA ---
async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("Pulling latest FPL data...")
    base_url = "https://fantasy.premierleague.com/api/"

    bootstrap = requests.get(base_url + "bootstrap-static/").json()

    # --- PLAYERS ---
    players = pd.DataFrame(bootstrap['elements'])
    pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    players['position'] = players['element_type'].map(pos_map)

    players_dict = players[[
        'id', 'web_name', 'position', 'minutes',
        'goals_scored', 'assists', 'total_points',
        'selected_by_percent'
    ]].to_dict('records')

    # --- TEAMS ---
    teams_df = pd.DataFrame(bootstrap['teams'])
    teams_dict = teams_df[['id', 'name', 'short_name']] \
        .rename(columns={'id': '_id', 'short_name': 'short'}) \
        .to_dict('records')

    # --- FIXTURES ---
    fixtures = requests.get(base_url + "fixtures/").json()
    fixtures_dict = [{
        'fixture_id': f['id'],
        'event': f['event'],
        'team_h': f['team_h'],
        'team_a': f['team_a'],
        'kickoff_time': f['kickoff_time'],
        'started': f['started'],
        'finished': f['finished']
    } for f in fixtures]

    # --- LINEUPS ---
    lineup_entries = []
    for f in fixtures:
        for s in f.get('stats', []):
            if s.get('identifier') == 'minutes':
                for side in ('h', 'a'):
                    for p in s.get(side, []):
                        lineup_entries.append({
                            'match_id': f['id'],
                            'player_id': p['element'],
                            'minutes': p['value']
                        })

    client, db = get_db()
    db.players.delete_many({})
    db.players.insert_many(players_dict)
    db.teams.delete_many({})
    db.teams.insert_many(teams_dict)
    db.fixtures.delete_many({})
    db.fixtures.insert_many(fixtures_dict)
    db.lineups.delete_many({})
    if lineup_entries:
        db.lineups.insert_many(lineup_entries)

    current_gw = bootstrap['events'][0]['id']
    save_gameweek_stats(current_gw, players)
    client.close()

    await update.message.reply_text("✅ Update complete.")

# --- START COMMAND ---
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    today = datetime.now(timezone.utc).date()
    todays = []

    for f in db.fixtures.find():
        if f.get('kickoff_time'):
            ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
            if ko.date() == today:
                todays.append((ko, f))

    if not todays:
        keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
        await update.message.reply_text("No games today.", reply_markup=InlineKeyboardMarkup(keyboard))
        client.close()
        return

    msg = ["⚽ Matches today:"]
    for ko, f in todays:
        home = get_team_name(db, f['team_h'])
        away = get_team_name(db, f['team_a'])
        msg.append(f"• {home} vs {away} — {ko.strftime('%H:%M UTC')}")
    client.close()
    await update.message.reply_text("\n".join(msg))

# --- PRE-MATCH LINEUP COMMAND ---
async def prematch_lineup(update: Update, context: CallbackContext):
    """
    Example usage: /prematch_lineup <match_id> <team_name> <image_url>
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /prematch_lineup <match_id> <team_name> <image_url>"
        )
        return

    match_id = int(context.args[0])
    team_name = context.args[1]
    image_url = context.args[2]

    result = process_prematch_lineup(match_id, team_name, image_url)
    if result:
        players_text = "\n".join([f"{p['name']} ({p['confidence']}%)" for p in result['players']])
        await update.message.reply_text(
            f"📋 Pre-match lineup for {team_name}:\n\n{players_text}"
        )
    else:
        await update.message.reply_text("⚠️ Could not process lineup.")

# --- CALLBACKS ---
async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    client, db = get_db()

    if query.data == "next_fixtures":
        fixtures = get_next_fixtures(db)
        lines = ["📆 Upcoming fixtures:"]
        for ko, f in fixtures:
            home = get_team_name(db, f['team_h'])
            away = get_team_name(db, f['team_a'])
            lines.append(f"• {home} vs {away} — {ko.strftime('%d %b %H:%M UTC')}")
        await query.edit_message_text("\n".join(lines))

    client.close()

# --- DEBUG COMMANDS ---
async def debug_teams(update: Update, context: CallbackContext):
    client, db = get_db()
    teams = list(db.teams.find().limit(20))
    client.close()
    lines = [f"{t['_id']} — {t['name']}" for t in teams]
    await update.message.reply_text("\n".join(lines))

async def debug_fixtures(update: Update, context: CallbackContext):
    client, db = get_db()
    fixtures = list(db.fixtures.find().limit(5))
    client.close()
    lines = [f"{f['fixture_id']} — {f['team_h']} vs {f['team_a']}" for f in fixtures]
    await update.message.reply_text("\n".join(lines))

# --- FLASK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot running"

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# --- MAIN ---
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debug_teams", debug_teams))
    app.add_handler(CommandHandler("debug_fixtures", debug_fixtures))
    app.add_handler(CommandHandler("prematch_lineup", prematch_lineup))  # <-- New
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.run_polling()
