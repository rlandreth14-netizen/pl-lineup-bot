mport os
import threading
import logging
from datetime import datetime, timezone
import requests
import pandas as pd
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler

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

# --- HISTORICAL STORAGE HELPER ---
def save_gameweek_stats(gameweek, players_df, fixtures_df, lineups):
    os.makedirs("historical_stats", exist_ok=True)
    file_path = f"historical_stats/gameweek_{gameweek}.csv"
    # Combine players + lineups into one CSV (simplified)
    players_df.to_csv(file_path, index=False)
    # Could also save fixtures or lineups in JSON for more detail
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
        mins = row['minutes_y'] or 0
        if mins < 300:
            continue
        avg_attack = (row['goals_scored'] + row['assists']) / (mins / 90)
        match_attack = (row['goals_scored'] + row['assists'])
        if match_attack >= avg_attack * 2 and match_attack > 0:
            insights.append(f"🔥 {row['web_name']} ({row['position']}) — abnormal attacking output")
    client.close()
    return "\n".join(insights) if insights else "No abnormal player behaviour detected."

# --- DETECT HIGH-OWNERSHIP BENCHED PLAYERS ---
def detect_high_ownership_benched(match_id):
    client, db = get_db()
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    started_ids = {l['player_id'] for l in lineups if l['minutes'] > 0}
    players = list(db.players.find({'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}}))
    alerts = [f"🚨 {p['web_name']} ({p['selected_by_percent']}%) — NOT STARTING" 
              for p in players if p['id'] not in started_ids]
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

    # --- PLAYERS ---
    bootstrap = requests.get(base_url + "bootstrap-static/").json()
    players = pd.DataFrame(bootstrap['elements'])
    pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    players['position'] = players['element_type'].map(pos_map)
    players_dict = players[[
        'id', 'web_name', 'position', 'minutes',
        'goals_scored', 'assists', 'total_points',
        'selected_by_percent'
    ]].to_dict('records')

    # --- TEAM MAPPING ---
    teams_df = pd.DataFrame(bootstrap['teams'])
    team_map = dict(zip(teams_df['id'], teams_df['name']))

    # --- FIXTURES ---
    fixtures = requests.get(base_url + "fixtures/").json()
    fixtures_dict = []
    for f in fixtures:
        fixtures_dict.append({
            'id': f['id'],
            'event': f['event'],
            'team_h': f['team_h'],
            'team_a': f['team_a'],
            'team_h_name': team_map.get(f['team_h'], str(f['team_h'])),
            'team_a_name': team_map.get(f['team_a'], str(f['team_a'])),
            'kickoff_time': f['kickoff_time'],
            'started': f['started'],
            'finished': f['finished']
        })

    # --- LINEUPS ---
    lineup_entries = []
    for f in fixtures:
        for s in f.get('stats', []):
            if s.get('identifier') == 'minutes':
                for side in ('h','a'):
                    for p in s.get(side, []):
                        lineup_entries.append({
                            "match_id": f['id'],
                            "player_id": p['element'],
                            "minutes": p['value']
                        })

    # --- SAVE TO MONGO ---
    client, db = get_db()
    db.players.delete_many({})
    db.players.insert_many(players_dict)
    db.fixtures.delete_many({})
    db.fixtures.insert_many(fixtures_dict)
    db.lineups.delete_many({})
    if lineup_entries:
        db.lineups.insert_many(lineup_entries)

    # --- Save historical stats for learning ---
    current_gw = bootstrap['events'][0]['id']  # Current gameweek
    season_name = f"season_{datetime.now().year}_{datetime.now().year+1}"  # Example: 2025_26
    hist_col = db[f"historical_stats_{season_name}"]
    hist_col.replace_one(
        {'game_week': current_gw},
        {
            'game_week': current_gw,
            'timestamp': datetime.now(timezone.utc),
            'players': players_dict,
            'fixtures': fixtures_dict,
            'lineups': lineup_entries
        },
        upsert=True
    )

    client.close()

    # --- Save to local CSV too ---
    save_gameweek_stats(current_gw, players, pd.DataFrame(fixtures_dict), lineup_entries)

    await update.message.reply_text("✅ Update complete and historical stats saved.")

# --- START COMMAND ---
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    today = datetime.now(timezone.utc).date()
    todays = []

    for f in db.fixtures.find():
        if f.get('kickoff_time'):
            ko = datetime.fromisoformat(f['kickoff_time'].replace('Z','+00:00'))
            if ko.date() == today:
                todays.append((ko,f))
    client.close()

    if not todays:
        keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
        await update.message.reply_text("No games today.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    msg = ["⚽ Matches today:"]
    for ko, f in todays:
        msg.append(f"• {f['team_h_name']} vs {f['team_a_name']} — {ko.strftime('%H:%M UTC')}")
    await update.message.reply_text("\n".join(msg))

# --- CALLBACKS ---
async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    client, db = get_db()

    if query.data == "next_fixtures":
        fixtures = get_next_fixtures(db)
        lines = ["📆 Upcoming fixtures:"]
        for ko, f in fixtures:
            lines.append(f"• {f['team_h_name']} vs {f['team_a_name']} — {ko.strftime('%d %b %H:%M UTC')}")
        await query.edit_message_text("\n".join(lines))

    elif query.data.startswith("fixture_"):
        match_id = int(query.data.split("_")[1])
        fixture = db.fixtures.find_one({'id': match_id})
        if not fixture:
            await query.edit_message_text("Fixture not found.")
        else:
            abnormal = detect_abnormal(match_id)
            benched = detect_high_ownership_benched(match_id)
            msg = f"⚽ {fixture['team_h_name']} vs {fixture['team_a_name']}\n\n{abnormal}"
            if benched:
                msg += f"\n\n{benched}"
            await query.edit_message_text(msg)

    client.close()

# --- CHECK COMMAND ---
async def check(update: Update, context: CallbackContext):
    client, db = get_db()
    latest = db.lineups.find_one(sort=[("match_id",-1)])
    client.close()
    if not latest:
        await update.message.reply_text("Run /update first.")
        return
    match_id = latest['match_id']
    abnormal = detect_abnormal(match_id)
    benched = detect_high_ownership_benched(match_id)
    await update.message.reply_text(f"📊 Match ID {match_id}\n\n{abnormal}\n\n{benched}")

# --- FLASK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot running"

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT',10000)))

# --- MAIN ---
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.run_polling()
