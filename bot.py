from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext
from pymongo import MongoClient
import os
import pandas as pd
from flask import Flask
import threading
import requests
import logging

logging.basicConfig(level=logging.INFO)

MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')

# ---------- Helper: Detect OOP (simple heuristic) ----------
def detect_oop(match_id):
    """
    Simple heuristic:
      - Flags defenders (DEF) who have attacking output (goals/assists)
        in the season data as *possible* OOP for the given match.
    This is intentionally conservative — you can expand it later.
    """
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    # pull lineups for the match
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    if not lineups:
        client.close()
        return "No lineups found for this match. Run /update to populate lineups."

    lineup_df = pd.DataFrame(lineups)
    player_ids = lineup_df['player_id'].tolist()

    # pull player metadata
    players = list(db.players.find({'id': {'$in': player_ids}}))
    if not players:
        client.close()
        return "No player metadata found for these players. Run /update to populate players."

    players_df = pd.DataFrame(players)

    # merge to combine lineup info with player base position & season stats
    merged = lineup_df.merge(players_df[['id', 'web_name', 'position', 'goals_scored', 'assists', 'total_points']],
                             left_on='player_id', right_on='id', how='left')

    insights = []
    for _, row in merged.iterrows():
        web_name = row.get('web_name', 'Unknown')
        base_pos = row.get('position', 'UNK')  # 'DEF','MID','FWD','GK'
        goals = int(row.get('goals_scored', 0) or 0)
        assists = int(row.get('assists', 0) or 0)
        minutes = int(row.get('minutes', 0) or 0)

        # Heuristic examples:
        #  - DEF with attacking output flagged as possible OOP
        #  - MID/FWD not flagged here (can be extended)
        oop_reasons = []
        if base_pos == 'DEF' and (goals > 0 or assists > 0):
            oop_reasons.append(f"season output G={goals}, A={assists}")

        # If player played very few minutes (sub) and has attacking outputs in season, still note
        if base_pos == 'DEF' and minutes < 60 and (goals > 0 or assists > 0):
            oop_reasons.append("(sub minutes this match — role uncertain)")

        if oop_reasons:
            insights.append(f"🔎 {web_name} ({base_pos}) — possible OOP: {'; '.join(oop_reasons)}")

    client.close()

    if not insights:
        return "No clear OOP players detected by the simple heuristic."
    return "\n".join(insights)


# ---------- Command: update_data (pull players, fixtures, lineups) ----------
async def update_data(update: Update, context: CallbackContext):
    """
    Pulls:
      - bootstrap-static -> players
      - fixtures -> fixtures + extracts 'minutes' stats into lineups collection
    Inserts into MongoDB collections: players, fixtures, lineups
    """
    await update.message.reply_text("Pulling latest FPL data (players, fixtures, lineups)...")

    base_url = "https://fantasy.premierleague.com/api/"
    try:
        # 1) Get bootstrap and fixtures
        bootstrap = requests.get(base_url + "bootstrap-static/").json()
        fixtures = requests.get(base_url + "fixtures/").json()

        # 2) Build players collection
        players = pd.DataFrame(bootstrap['elements'])
        # keep key season stats + map position
        pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
        players['position'] = players['element_type'].map(pos_map)
        players_dict = players[['id', 'web_name', 'position', 'minutes', 'goals_scored', 'assists', 'yellow_cards', 'total_points']].to_dict('records')

        # 3) Build fixtures collection (basic fields)
        fixtures_df = pd.DataFrame(fixtures)
        fixtures_df = fixtures_df[['id', 'event', 'team_h', 'team_a', 'kickoff_time', 'started', 'finished']]
        fixtures_dict = fixtures_df.to_dict('records')

        # 4) Extract lineups from fixtures: find 'minutes' stat in each fixture stats array
        lineup_entries = []
        for f in fixtures:
            # 'stats' can be empty or contain many identifiers (minutes, goals, etc.)
            if f.get('stats'):
                minute_stats = next((item for item in f['stats'] if item.get("identifier") == "minutes"), None)
                if minute_stats:
                    # minute_stats has two keys typically: 'h' and 'a' each is a list of dicts
                    for side in ('h', 'a'):
                        for player_stat in minute_stats.get(side, []):
                            # each player_stat = {'element': <player_id>, 'value': <minutes>}
                            lineup_entries.append({
                                "match_id": f['id'],
                                "player_id": int(player_stat['element']),
                                "team_side": side,
                                "minutes": int(player_stat['value'])
                            })

        # 5) Write to MongoDB (replace existing collections)
        client = MongoClient(MONGODB_URI)
        db = client['premier_league']

        db.players.delete_many({})
        if players_dict:
            db.players.insert_many(players_dict)

        db.fixtures.delete_many({})
        if fixtures_dict:
            db.fixtures.insert_many(fixtures_dict)

        db.lineups.delete_many({})
        if lineup_entries:
            db.lineups.insert_many(lineup_entries)

        client.close()

        await update.message.reply_text(
            f"✅ FPL update complete. Players: {len(players_dict)}. Lineup entries: {len(lineup_entries)}."
        )
    except Exception as e:
        logging.exception("Error in update_data")
        await update.message.reply_text(f"Error pulling data: {str(e)}")


# ---------- Command: start ----------
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "Welcome! Use /update to fetch FPL players/fixtures/lineups. Use /check <match_id> to detect OOP (or just /check for latest)."
    )


# ---------- Command: check (auto-find latest if no arg) ----------
async def check(update: Update, context: CallbackContext):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    # If user didn't send a match_id, pick the most recent match that has any lineup entry
    if not context.args:
        latest_match = db.lineups.find_one(sort=[("match_id", -1)])
        if not latest_match:
            client.close()
            await update.message.reply_text("No match data found. Run /update first.")
            return
        match_id = latest_match['match_id']
    else:
        match_id = context.args[0]

    client.close()

    # call detect_oop and reply
    insights = detect_oop(match_id)
    await update.message.reply_text(f"📊 Match ID: {match_id}\n\n{insights}")


# ---------- Flask health (for hosting) ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)


# ---------- Main ----------
if __name__ == "__main__":
    # Run Flask in a background thread (so hosting has a port)
    threading.Thread(target=run_flask, daemon=True).start()

    # Run Telegram bot polling
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.run_polling()
