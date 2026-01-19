import os
import threading
import requests
import logging
from datetime import datetime, timedelta
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
import time

# --- 1. SETUP & CONFIG ---
logging.basicConfig(level=logging.INFO)

# MongoDB Setup
MONGO_URI = os.environ.get("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client['football_bot']
player_collection = db['player_history']
cache_collection = db['player_stats_cache']

# Flask for Render Health Checks
app = Flask(__name__)
@app.route('/')
def health():
    return "Bot Active", 200

# League mappings (TheSportsDB)
target_leagues = [4328, 4329, 4335, 4331, 4332]
league_names = {
    4328: "Premier League",
    4329: "Championship",
    4335: "La Liga",
    4331: "Bundesliga",
    4332: "Serie A"
}

# --- 2. POSITION MAPPING ---

def map_position(pos):
    mapping = {
        'Centre Back': 'CB',
        'Right Back': 'RB',
        'Left Back': 'LB',
        'Defensive Midfield': 'DM',
        'Central Midfield': 'CM',
        'Right Midfield': 'RM',
        'Left Midfield': 'LM',
        'Attacking Midfield': 'AM',
        'Right Wing': 'RW',
        'Left Wing': 'LW',
        'Striker': 'ST',
        'Forward': 'ST',
        'Midfielder': 'CM',
        'Defender': 'CB',
    }
    return mapping.get(pos, '??')

# --- 3. PLAYER FORM (CACHED) ---

async def get_player_form(player_id):
    cached = cache_collection.find_one({"player_id": player_id})
    if cached and cached["timestamp"] > datetime.now() - timedelta(hours=24):
        return cached["stats_text"]

    url = f"https://www.thesportsdb.com/api/v1/json/3/lookupplayer.php?id={player_id}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return "⚠️ Stats unavailable."

        p = r.json().get("players", [{}])[0]
        stats = (
            f"Season Goals: {p.get('strGoals', 'N/A')} | "
            f"Cards: {p.get('strYellowCards', 'N/A')}/{p.get('strRedCards', 'N/A')}"
        )

        cache_collection.update_one(
            {"player_id": player_id},
            {"$set": {"stats_text": stats, "timestamp": datetime.now()}},
            upsert=True
        )
        return stats
    except Exception as e:
        logging.error(e)
        return "⚠️ Stats unavailable."

# --- 4. LINEUP ANALYSIS (FIXED DATA SOURCE) ---

async def analyze_lineups(query, league_id):
    today = datetime.now().date()

    # STRONGER ENDPOINT — league-based
    url = f"https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php?id={league_id}"

    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            await query.edit_message_text("⚠️ Fixture API error.")
            return
        events = r.json().get("events", [])
    except Exception as e:
        logging.error(e)
        await query.edit_message_text("❌ Failed to fetch fixtures.")
        return

    # Filter to TODAY only
    todays_matches = []
    for m in events:
        try:
            match_date = datetime.strptime(m["dateEvent"], "%Y-%m-%d").date()
            if match_date == today:
                todays_matches.append(m)
        except Exception:
            continue

    if not todays_matches:
        await query.edit_message_text(
            "❌ No matches scheduled for today in this league.\n"
            "ℹ️ Try again on matchday evenings or weekends."
        )
        return

    alerts = []

    MARKETS = {
        "ATTACKING": "🎯 *Target: Over 0.5/1.5 Shots on Target*",
        "DEFENSIVE": "⚠️ *Target: Over 1.5 Fouls Committed*",
        "CONTROL": "🔄 *Target: Over 50.5/70.5 Passes*"
    }

    for match in todays_matches:
        match_id = match["idEvent"]

        try:
            lineup_url = f"https://www.thesportsdb.com/api/v1/json/3/lookuplineup.php?id={match_id}"
            lr = requests.get(lineup_url, timeout=10)
            if lr.status_code != 200:
                continue

            lineup = lr.json().get("lineup", [])
            for p in lineup:
                name = p.get("strPlayer")
                pid = p.get("idPlayer")
                team = p.get("strTeam")
                current_pos = map_position(p.get("strPosition"))

                hist = player_collection.find_one({"name": name})
                if not hist or "positions" not in hist:
                    continue

                usual = max(hist["positions"], key=hist["positions"].get)
                alert = None
                market = None

                if usual in ["CB", "RB", "LB"] and current_pos in ["DM", "CM", "AM", "RW", "LW", "ST"]:
                    alert = f"🚀 *FORWARD SHIFT* ({team})\n*{name}* at *{current_pos}* (Usual: {usual})"
                    market = MARKETS["ATTACKING"]

                elif usual in ["ST", "RW", "LW", "AM"] and current_pos in ["CM", "DM", "RB", "LB"]:
                    alert = f"🛡️ *DEFENSIVE SHIFT* ({team})\n*{name}* at *{current_pos}* (Usual: {usual})"
                    market = MARKETS["DEFENSIVE"]

                if alert:
                    form = await get_player_form(pid)
                    alerts.append(f"{alert}\n{market}\n*Form:*\n{form}")

            time.sleep(1.5)
        except Exception as e:
            logging.error(e)

    if not alerts:
        await query.edit_message_text("✅ No major positional changes detected.")
    else:
        msg = "📊 *SCOUT REPORT*\n\n" + "\n---\n".join(alerts)
        await query.edit_message_text(msg[:4090], parse_mode="Markdown")

# --- 5. BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton(league_names[l], callback_data=f"league:{l}")]
        for l in target_leagues
    ]
    await update.message.reply_text(
        "⚽ *Football IQ Bot*\nSelect a league:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("league:"):
        league_id = query.data.split(":")[1]
        kb = [[InlineKeyboardButton("🔍 Analyze Today's Lineups", callback_data=f"analyze:{league_id}")]]
        await query.edit_message_text(
            f"Selected *{league_names[int(league_id)]}*.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif query.data.startswith("analyze:"):
        league_id = query.data.split(":")[1]
        await query.edit_message_text("⏳ Scanning lineups...")
        await analyze_lineups(query, league_id)

# --- 6. SERVER ---

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    app_bot = ApplicationBuilder().token(TOKEN).build()

    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(handle_callback))

    threading.Thread(target=run_flask, daemon=True).start()
    app_bot.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
