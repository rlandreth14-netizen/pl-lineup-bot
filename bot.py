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
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- CONFIG ----------------
MONGODB_URI = os.getenv("MONGODB_URI")
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
HIGH_OWNERSHIP_THRESHOLD = 20.0  # %

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

# ---------------- MONGO ----------------
def get_db():
    client = MongoClient(MONGODB_URI)
    return client, client["premier_league"]

# ---------------- SOFASCORE ----------------
def fetch_sofascore_lineup(match_id):
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
        if res.status_code != 200:
            return None

        data = res.json()
        players = []

        for side in ("home", "away"):
            team_data = data.get(side)
            if not team_data:
                continue

            team_name = team_data["team"]["name"]
            for entry in team_data.get("players", []):
                p = entry.get("player")
                if not p:
                    continue

                players.append({
                    "name": p.get("name"),
                    "sofa_id": p.get("id"),
                    "tactical_pos": entry.get("position"),
                    "team": team_name,
                })

        return players

    except Exception as e:
        logger.error(f"SofaScore lineup error: {e}")
        return None


def get_today_sofascore_matches():
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10).json()
        return [
            e for e in res.get("events", [])
            if e.get("uniqueTournament", {}).get("id") == 17
        ]
    except Exception as e:
        logger.error(f"SofaScore events error: {e}")
        return []

# ---------------- ANALYSIS ----------------
def detect_high_ownership_benched(match_id, db):
    lineups = list(db.lineups.find({"match_id": int(match_id)}))
    if not lineups:
        return None

    started_ids = {l["player_id"] for l in lineups if l.get("minutes", 0) > 0}
    players = db.players.find(
        {"selected_by_percent": {"$gte": HIGH_OWNERSHIP_THRESHOLD}}
    )

    alerts = [
        f"🚨 {p['web_name']} ({p['selected_by_percent']}%) — NOT STARTING"
        for p in players
        if p["id"] not in started_ids
    ]

    return "\n".join(alerts) if alerts else None


def detect_tactical_oop(db, match_id):
    latest = db.tactical_data.find_one({"match_id": match_id})
    if not latest:
        return None

    fpl_map = {"GK": "GK", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}
    insights = []

    for p in latest["players"]:
        fpl = db.players.find_one({
            "web_name": {"$regex": f"^{p['name']}$", "$options": "i"}
        })
        if not fpl:
            continue

        fpl_pos = fpl_map.get(fpl["position"])
        sofa_pos = p["tactical_pos"]

        if fpl_pos == "DEF" and sofa_pos not in ("DEF", "GK"):
            insights.append(f"🔥 {p['name']} ({p['team']}): DEF ➜ {sofa_pos}")
        elif fpl_pos == "MID" and sofa_pos == "FWD":
            insights.append(f"🔥 {p['name']} ({p['team']}): MID ➜ FWD")
        elif fpl_pos == "FWD" and sofa_pos in ("MID", "DEF"):
            insights.append(f"🔥 {p['name']} ({p['team']}): FWD ➜ {sofa_pos}")

    return "\n".join(insights) if insights else None

# ---------------- BACKGROUND MONITOR ----------------
def run_monitor():
    while True:
        try:
            time.sleep(60)
            client, db = get_db()
            now = datetime.now(timezone.utc)

            fixtures = db.fixtures.find({
                "finished": False,
                "alert_sent": {"$ne": True},
            })

            for f in fixtures:
                ko = datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00"))
                mins = (ko - now).total_seconds() / 60

                if 55 <= mins <= 65:
                    msg = [f"📢 *Lineups Out: {f['team_h_name']} vs {f['team_a_name']}*"]

                    events = get_today_sofascore_matches()
                    event = next(
                        (e for e in events if
                         e["homeTeam"]["name"] == f["team_h_name"] or
                         e["awayTeam"]["name"] == f["team_a_name"]),
                        None
                    )

                    if event:
                        lineup = fetch_sofascore_lineup(event["id"])
                        if lineup:
                            db.tactical_data.update_one(
                                {"match_id": event["id"]},
                                {"$set": {
                                    "home_team": event["homeTeam"]["name"],
                                    "away_team": event["awayTeam"]["name"],
                                    "players": lineup,
                                    "last_updated": datetime.now(timezone.utc),
                                }},
                                upsert=True,
                            )

                            oop = detect_tactical_oop(db, event["id"])
                            if oop:
                                msg.append(f"\n*Tactical Shifts:*\n{oop}")

                    benched = detect_high_ownership_benched(f["id"], db)
                    if benched:
                        msg.append(f"\n*Benched Assets:*\n{benched}")

                    final = "\n".join(msg)
                    for u in db.users.find():
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                            json={
                                "chat_id": u["chat_id"],
                                "text": final,
                                "parse_mode": "Markdown",
                            },
                            timeout=10,
                        )

                    db.fixtures.update_one(
                        {"id": f["id"]}, {"$set": {"alert_sent": True}}
                    )

            client.close()

        except Exception as e:
            logger.error(f"Monitor error: {e}")

# ---------------- TELEGRAM COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client, db = get_db()
    chat_id = update.effective_chat.id

    db.users.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "joined": datetime.utcnow()}},
        upsert=True,
    )

    today = datetime.now(timezone.utc).date()
    todays = []

    for f in db.fixtures.find():
        ko = datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00"))
        if ko.date() == today:
            todays.append((ko, f))

    if not todays:
        keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next")]]
        await update.message.reply_text(
            "No games today. You are registered for lineup alerts.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        client.close()
        return

    msg = ["⚽ *Matches today:*"]
    for ko, f in todays:
        msg.append(f"• {f['team_h_name']} vs {f['team_a_name']} — {ko:%H:%M UTC}")

    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")
    client.close()


async def update_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Syncing data...")
    client, db = get_db()

    try:
        base = "https://fantasy.premierleague.com/api/"
        bootstrap = requests.get(base + "bootstrap-static/").json()

        players = pd.DataFrame(bootstrap["elements"])
        pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
        players["position"] = players["element_type"].map(pos_map)

        db.players.delete_many({})
        db.players.insert_many(
            players[[
                "id", "web_name", "position",
                "minutes", "goals_scored",
                "assists", "total_points",
                "selected_by_percent"
            ]].to_dict("records")
        )

        fixtures = requests.get(base + "fixtures/").json()
        teams = {t["id"]: t["name"] for t in bootstrap["teams"]}

        db.fixtures.delete_many({})
        db.fixtures.insert_many([{
            "id": f["id"],
            "team_h_name": teams[f["team_h"]],
            "team_a_name": teams[f["team_a"]],
            "kickoff_time": f["kickoff_time"],
            "started": f["started"],
            "finished": f["finished"],
        } for f in fixtures])

        await update.message.reply_text("✅ Sync complete")

    except Exception as e:
        logger.error(e)
        await update.message.reply_text("❌ Sync failed")

    finally:
        client.close()


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "next":
        client, db = get_db()
        upcoming = sorted(
            db.fixtures.find({"finished": False}),
            key=lambda f: f["kickoff_time"]
        )[:5]

        msg = ["📆 *Upcoming Fixtures:*"]
        for f in upcoming:
            ko = datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00"))
            msg.append(f"• {f['team_h_name']} vs {f['team_a_name']} ({ko:%d %b %H:%M UTC})")

        await query.edit_message_text("\n".join(msg), parse_mode="Markdown")
        client.close()

# ---------------- FLASK ----------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is live"

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

# ---------------- MAIN ----------------
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=run_monitor, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CallbackQueryHandler(handle_callbacks))

    logger.info("Bot started successfully")
    app.run_polling()
