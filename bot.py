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
import json

logging.basicConfig(level=logging.INFO)

MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')

HIGH_OWNERSHIP_THRESHOLD = 20.0  # %
DATA_DIR = "data"  # Folder to store historical season data

# ---------- Helper: Historical storage ----------
def save_historical_stats(players_list, season, gameweek):
    season_dir = os.path.join(DATA_DIR, season)
    os.makedirs(season_dir, exist_ok=True)
    file_path = os.path.join(season_dir, f"gw{gameweek}.json")
    with open(file_path, "w") as f:
        json.dump(players_list, f, indent=2)


def load_last_games(player_id, season, limit=5):
    season_dir = os.path.join(DATA_DIR, season)
    if not os.path.exists(season_dir):
        return []

    gws = sorted([f for f in os.listdir(season_dir) if f.startswith("gw")])
    recent_stats = []
    for gw_file in reversed(gws):
        with open(os.path.join(season_dir, gw_file)) as f:
            gw_data = json.load(f)
            for p in gw_data:
                if p['id'] == player_id:
                    recent_stats.append(p)
                    break
        if len(recent_stats) >= limit:
            break
    return recent_stats

# ---------- Helper: Abnormal behaviour ----------
def detect_abnormal(lineup, players_df):
    insights = []
    merged = lineup.merge(
        players_df[['id', 'web_name', 'position', 'minutes', 'goals_scored', 'assists']],
        left_on='player_id', right_on='id', how='left'
    )

    for _, row in merged.iterrows():
        mins = row['minutes_y'] or 0
        if mins < 300:
            continue

        avg_attack = (row['goals_scored'] + row['assists']) / (mins / 90)
        match_attack = (row['goals_scored'] + row['assists'])

        if match_attack >= avg_attack * 2 and match_attack > 0:
            insights.append(f"🔥 {row['web_name']} ({row['position']}) — abnormal attacking output")
    return "\n".join(insights) if insights else ""


# ---------- Helper: High ownership NOT starting ----------
def detect_high_ownership_benched(lineup, players_df):
    started_ids = set(lineup['player_id'].tolist())
    high_own = players_df[players_df['selected_by_percent'] >= HIGH_OWNERSHIP_THRESHOLD]

    alerts = []
    for _, p in high_own.iterrows():
        if p['id'] not in started_ids:
            alerts.append(f"🚨 {p['web_name']} ({p['selected_by_percent']}%) — NOT STARTING")
    return "\n".join(alerts)


# ---------- Helper: Trend detection ----------
def detect_trends(player_id, season):
    last_games = load_last_games(player_id, season)
    if len(last_games) < 2:
        return ""

    points_trend = [g['total_points'] for g in last_games]
    if points_trend[-1] > sum(points_trend[:-1])/len(points_trend[:-1]):
        return "📈 Improving performance"
    elif points_trend[-1] < sum(points_trend[:-1])/len(points_trend[:-1]):
        return "📉 Declining performance"
    return ""


# ---------- Helper: Out-of-position detection ----------
def detect_oop(player, lineup_position):
    if 'position' not in player:
        return ""
    if player['position'] != lineup_position:
        return f"⚠️ {player['web_name']} is OUT OF POSITION (Listed as {player['position']})"
    return ""


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
        'id', 'web_name', 'team', 'position', 'minutes',
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
                            "minutes": p['value'],
                            "lineup_position": side.upper()  # H/A for later OOP detection
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

    # Save historical per gameweek
    season = "2025-26"
    gameweek = bootstrap['events'][0]['id'] if bootstrap.get('events') else 1
    save_historical_stats(players_dict, season, gameweek)

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

    keyboard = []
    for ko, f in todays:
        text = f"{f['team_h']} vs {f['team_a']} — {ko.strftime('%H:%M UTC')}"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"fixture_{f['id']}")])

    await update.message.reply_text("⚽ Matches today:", reply_markup=InlineKeyboardMarkup(keyboard))


# ---------- Callback ----------
async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    if query.data == "next_fixtures":
        fixtures = get_next_fixtures(db)
        keyboard = []
        for ko, f in fixtures:
            text = f"{f['team_h']} vs {f['team_a']} — {ko.strftime('%d %b %H:%M UTC')}"
            keyboard.append([InlineKeyboardButton(text, callback_data=f"fixture_{f['id']}")])
        await query.edit_message_text("📆 Upcoming fixtures:", reply_markup=InlineKeyboardMarkup(keyboard))
        client.close()
        return

    # Fixture selected
    if query.data.startswith("fixture_"):
        match_id = int(query.data.split("_")[1])

        lineup_entries = list(db.lineups.find({'match_id': match_id}))
        if not lineup_entries:
            await query.edit_message_text("No lineup data available yet for this match.")
            client.close()
            return

        lineup_df = pd.DataFrame(lineup_entries)
        players = list(db.players.find({'id': {'$in': lineup_df['player_id'].tolist()}}))
        players_df = pd.DataFrame(players)

        abnormal = detect_abnormal(lineup_df, players_df)
        benched = detect_high_ownership_benched(lineup_df, players_df)

        season = "2025-26"
        player_messages = []

        for _, p in players_df.iterrows():
            # Last 5 GW stats
            last_games = load_last_games(p['id'], season)
            avg_points = sum([g['total_points'] for g in last_games])/len(last_games) if last_games else 0
            avg_minutes = sum([g['minutes'] for g in last_games])/len(last_games) if last_games else 0
            goals = sum([g['goals_scored'] for g in last_games]) if last_games else 0
            assists = sum([g['assists'] for g in last_games]) if last_games else 0
            clean_sheets = sum([g.get('clean_sheets',0) for g in last_games]) if last_games else 0

            # Trend
            trend = detect_trends(p['id'], season)

            # OOP
            lineup_pos = lineup_df.loc[lineup_df['player_id']==p['id'], 'lineup_position'].iloc[0]
            oop = detect_oop(p, lineup_pos)

            msg = f"{p['web_name']} — Avg Pts: {avg_points:.1f}, Mins: {avg_minutes:.0f}, G: {goals}, A: {assists}, CS: {clean_sheets}"
            if trend: msg += f" | {trend}"
            if oop: msg += f" | {oop}"
            player_messages.append(msg)

        message = f"📊 Match {players_df['team'].iloc[0]} vs {players_df['team'].iloc[1]}\n\n"
        if abnormal: message += f"{abnormal}\n\n"
        if benched: message += f"{benched}\n\n"
        message += "\n".join(player_messages)

        client.close()
        await query.edit_message_text(message)


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
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.run_polling()
