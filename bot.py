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

# ---------- Helper: Detect OOP (simple heuristic) ----------
def detect_oop(match_id):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    if not lineups:
        client.close()
        return "No lineups found for this match. Run /update to populate lineups."

    lineup_df = pd.DataFrame(lineups)
    player_ids = lineup_df['player_id'].tolist()

    players = list(db.players.find({'id': {'$in': player_ids}}))
    if not players:
        client.close()
        return "No player metadata found. Run /update."

    players_df = pd.DataFrame(players)

    merged = lineup_df.merge(
        players_df[['id', 'web_name', 'position', 'goals_scored', 'assists', 'total_points']],
        left_on='player_id', right_on='id', how='left'
    )

    insights = []
    for _, row in merged.iterrows():
        if row['position'] == 'DEF' and (row['goals_scored'] > 0 or row['assists'] > 0):
            insights.append(
                f"🔎 {row['web_name']} (DEF) — possible OOP (G:{row['goals_scored']} A:{row['assists']})"
            )

    client.close()
    return "\n".join(insights) if insights else "No clear OOP players detected."


# ---------- Helper: Get next fixtures ----------
def get_next_fixtures(db, limit=5):
    now_utc = datetime.now(timezone.utc)
    upcoming = []

    for f in db.fixtures.find({'started': False, 'finished': False}):
        if not f.get('kickoff_time'):
            continue

        kickoff = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
        if kickoff > now_utc:
            upcoming.append((kickoff, f))

    upcoming.sort(key=lambda x: x[0])
    return upcoming[:limit]


# ---------- Command: update ----------
async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("Pulling latest FPL data...")

    base_url = "https://fantasy.premierleague.com/api/"
    try:
        bootstrap = requests.get(base_url + "bootstrap-static/").json()
        fixtures = requests.get(base_url + "fixtures/").json()

        players = pd.DataFrame(bootstrap['elements'])
        pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
        players['position'] = players['element_type'].map(pos_map)
        players_dict = players[['id', 'web_name', 'position', 'minutes',
                                'goals_scored', 'assists', 'yellow_cards', 'total_points']].to_dict('records')

        fixtures_df = pd.DataFrame(fixtures)
        fixtures_df = fixtures_df[['id', 'event', 'team_h', 'team_a',
                                   'kickoff_time', 'started', 'finished']]
        fixtures_dict = fixtures_df.to_dict('records')

        lineup_entries = []
        for f in fixtures:
            if f.get('stats'):
                minute_stats = next(
                    (s for s in f['stats'] if s.get('identifier') == 'minutes'), None
                )
                if minute_stats:
                    for side in ('h', 'a'):
                        for p in minute_stats.get(side, []):
                            lineup_entries.append({
                                "match_id": f['id'],
                                "player_id": int(p['element']),
                                "team_side": side,
                                "minutes": int(p['value'])
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

        await update.message.reply_text(
            f"✅ Update complete. Players: {len(players_dict)} | Lineups: {len(lineup_entries)}"
        )

    except Exception as e:
        logging.exception("Update failed")
        await update.message.reply_text(f"❌ Error: {str(e)}")


# ---------- Command: start ----------
async def start(update: Update, context: CallbackContext):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    today = datetime.now(timezone.utc).date()
    fixtures_today = []

    for f in db.fixtures.find():
        if not f.get('kickoff_time'):
            continue
        kickoff = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
        if kickoff.date() == today:
            fixtures_today.append((kickoff, f))

    client.close()

    if not fixtures_today:
        keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
        await update.message.reply_text(
            "📅 No Premier League matches today.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    lines = ["⚽ *Premier League matches today:*"]
    for kickoff, f in fixtures_today:
        lines.append(f"• Match ID {f['id']} — {kickoff.strftime('%H:%M UTC')}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- Callback: Next fixtures ----------
async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    if query.data == "next_fixtures":
        client = MongoClient(MONGODB_URI)
        db = client['premier_league']
        fixtures = get_next_fixtures(db, limit=5)
        client.close()

        if not fixtures:
            await query.edit_message_text("No upcoming fixtures found.")
            return

        lines = ["📆 *Next Premier League fixtures:*"]
        for kickoff, f in fixtures:
            lines.append(
                f"• Match ID {f['id']} — {kickoff.strftime('%a %d %b %H:%M UTC')}"
            )

        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")


# ---------- Command: check ----------
async def check(update: Update, context: CallbackContext):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']

    if not context.args:
        latest = db.lineups.find_one(sort=[("match_id", -1)])
        if not latest:
            client.close()
            await update.message.reply_text("No match data found. Run /update first.")
            return
        match_id = latest['match_id']
    else:
        match_id = context.args[0]

    client.close()
    insights = detect_oop(match_id)
    await update.message.reply_text(f"📊 Match ID {match_id}\n\n{insights}")


# ---------- Flask health ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)


# ---------- Main ----------
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.run_polling()
