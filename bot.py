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
HIGH_OWNERSHIP_THRESHOLD = 16.0  # %
SOFASCORE_BASE_URL = "https://api.sofascore.com/api/v1"
PL_TOURNAMENT_ID = 17
PL_SEASON_ID = 76986

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

# --- MONGO HELPER ---
from pymongo import MongoClient
from datetime import datetime

# Connect to MongoDB
def get_db():
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    return client, db

# Save standings to MongoDB
def save_standings_to_mongo(db, rows):
    """
    Save the Premier League standings rows to MongoDB.
    Overwrites any existing standings.
    """
    collection = db.standings
    collection.delete_many({})  # clean overwrite

    for row in rows:
        team = row["team"]

        doc = {
            "team_id": team["id"],
            "team_name": team["name"],
            "position": row["position"],
            "played": row["matches"],
            "wins": row["wins"],
            "draws": row["draws"],
            "losses": row["losses"],
            "goals_for": row["scoresFor"],
            "goals_against": row["scoresAgainst"],
            "goal_diff": row["goalDifference"],
            "points": row["points"],
            "updated_at": datetime.utcnow()
        }
        collection.insert_one(doc)
        
# --- CORE FUNCTIONS ---
def fetch_sofascore_lineup(match_id, retries=2):
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"
    for attempt in range(retries):
        try:
            res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
            if res.status_code != 200:
                logging.warning(f"SofaScore fetch returned {res.status_code}")
                time.sleep(2)
                continue
            data = res.json()
            players = []
            for side in ['home', 'away']:
                team_data = data.get(side)
                if not team_data: continue
                team_name = team_data['team']['name']
                for entry in team_data.get('players', []):
                    p = entry.get('player')
                    if not p: continue
                    players.append({
                        "name": p.get('name', 'Unknown'),
                        "sofa_id": p.get('id'),
                        "tactical_pos": entry.get('position', 'Unknown'),
                        "team": team_name
                    })
            return players
        except Exception as e:
            logging.error(f"SofaScore Fetch Error (attempt {attempt+1}): {e}")
            time.sleep(2)
    return None

def get_today_sofascore_matches():
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10).json()
        return [e for e in res.get('events', []) if e.get('tournament', {}).get('uniqueTournament', {}).get('id') == 17]
    except Exception as e:
        logging.error(f"SofaScore Events Fetch Error: {e}")
        return []

def detect_high_ownership_benched(match_id, db):
    try:
        lineups = list(db.lineups.find({'match_id': int(match_id)}))
        if not lineups: return None
        started_ids = {l['player_id'] for l in lineups if l.get('minutes', 0) > 0}
        players = list(db.players.find({'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}}))
        alerts = [f"🚨 {p['web_name']} — NOT STARTING" for p in players if p['id'] not in started_ids]
        return "\n".join(alerts) if alerts else None
    except Exception as e:
        logging.error(f"High Ownership Benched Check Error: {e}")
        return None

def detect_tactical_oop(db, match_id_filter=None):
    try:
        query = {"match_id": match_id_filter} if match_id_filter else {}
        latest = db.tactical_data.find_one(query, sort=[("last_updated", -1)])
        if not latest: return None
        insights = []
        fpl_map = {'GK': 'GK', 'DEF': 'DEF', 'MID': 'MID', 'FWD': 'FWD'}
        for p_sofa in latest.get('players', []):
            fpl_p = db.players.find_one({"web_name": {"$regex": f"^{p_sofa['name']}$", "$options": "i"}})
            if fpl_p:
                sofa_pos = p_sofa.get('tactical_pos', 'Unknown')
                fpl_pos = fpl_map.get(fpl_p.get('position', ''), None)
                if not fpl_pos: continue
                is_oop = False
                if fpl_pos == 'DEF' and sofa_pos not in ['DEF', 'GK']: is_oop = True
                elif fpl_pos == 'MID' and sofa_pos in ['FWD']: is_oop = True
                elif fpl_pos == 'FWD' and sofa_pos in ['MID', 'DEF']: is_oop = True
                if is_oop:
                    insights.append(f"🔥 {p_sofa['name']} ({p_sofa['team']}): {fpl_pos} ➡️ {sofa_pos}")
        return "\n".join(insights) if insights else None
    except Exception as e:
        logging.error(f"Tactical OOP Detection Error: {e}")
        return None

def get_next_fixtures(db, limit=5):
    now = datetime.now(timezone.utc)
    upcoming = []
    for f in db.fixtures.find({'started': False, 'finished': False}):
        ko_time = f.get('kickoff_time')
        if not ko_time: continue
        ko = datetime.fromisoformat(ko_time.replace('Z', '+00:00'))
        if ko > now:
            upcoming.append((ko, f))
    upcoming.sort(key=lambda x: x[0])
    return upcoming[:limit]

def fetch_pl_standings():
    """
    Fetch Premier League standings from SofaScore
    """
    url = (
        f"{SOFASCORE_BASE_URL}/unique-tournament/"
        f"{PL_TOURNAMENT_ID}/season/{PL_SEASON_ID}/standings/total"
    )

    response = requests.get(url, timeout=15)
    response.raise_for_status()

    data = response.json()
    return data["standings"][0]["rows"]

# --- FIXTURE BET BUILDER FUNCTIONS ---
def evaluate_team_result(fixture):
    try:
        home_xg = fixture.get('home_xg', 1.2)
        away_xg = fixture.get('away_xg', 1.0)
        home_team = fixture['team_h_name']
        away_team = fixture['team_a_name']

        if home_xg - away_xg >= 0.5:
            return f"{home_team} to Win"
        elif away_xg - home_xg >= 0.5:
            return f"{away_team} to Win"
        else:
            return "Draw / Skip"
    except:
        return "Draw / Skip"

def evaluate_btts(fixture):
    try:
        home_xg = fixture.get('home_xg', 1.2)
        away_xg = fixture.get('away_xg', 1.2)
        if home_xg >= 1.2 and away_xg >= 1.2:
            return "Yes"
        elif home_xg < 1.0 or away_xg < 1.0:
            return "No"
        else:
            return "Skip"
    except:
        return "Skip"

def select_shot_player(team_name, lineup, db):
    try:
        candidates = []
        for p in lineup:
            if p['team'] != team_name: continue
            fpl_p = db.players.find_one({"web_name": {"$regex": f"^{p['name']}$", "$options": "i"}})
            if not fpl_p: continue
            sofa_pos = p.get('tactical_pos', '')
            fpl_pos = fpl_p.get('position', '')
            if fpl_p.get('minutes',0) == 0: continue
            if fpl_pos not in ['FWD', 'MID']: continue
            if sofa_pos in ['FWD', 'MID']:
                candidates.append(p['name'])
        return candidates[0] if candidates else None
    except:
        return None

def generate_fixture_bet_builder(fixture, db):
    try:
        builder = []
        result = evaluate_team_result(fixture)
        builder.append(f"• Result: {result}")
        btts = evaluate_btts(fixture)
        builder.append(f"• BTTS: {btts}")

        sofa_data = db.tactical_data.find_one({"match_id": fixture.get('sofascore_id')})
        if not sofa_data: return "\n".join(builder)

        home_player = select_shot_player(fixture['team_h_name'], sofa_data.get('players', []), db)
        away_player = select_shot_player(fixture['team_a_name'], sofa_data.get('players', []), db)

        if home_player: builder.append(f"• {home_player} 1+ SOT")
        if away_player: builder.append(f"• {away_player} 1+ SOT")
        return "\n".join(builder)
    except Exception as e:
        logging.error(f"Bet Builder Error: {e}")
        return "Could not generate builder."

# --- GAMEWEEK ACCUMULATOR ---
def get_team_form(team_id, db, last_n=5):
    """Calculate form points from last_n matches for a team."""
    try:
        results = list(db.fixtures.find({
            '$or': [{'team_h': team_id}, {'team_a': team_id}],
            'finished': True
        }).sort('kickoff_time', -1).limit(last_n))
        points = 0
        for f in results:
            h_score = f.get('team_h_score')
            a_score = f.get('team_a_score')
            if h_score is None or a_score is None:
                continue
            if f['team_h'] == team_id:
                if h_score > a_score: points += 3
                elif h_score == a_score: points += 1
            else:
                if a_score > h_score: points += 3
                elif a_score == h_score: points += 1
        return points
    except Exception as e:
        logging.error(f"Team form error: {e}")
        return 0

def get_h2h_edge(home_id, away_id, db, last_n=3):
    """Return H2H edge for home team based on last_n matches."""
    try:
        h2h_games = list(db.fixtures.find({
            '$or': [
                {'team_h': home_id, 'team_a': away_id},
                {'team_h': away_id, 'team_a': home_id}
            ],
            'finished': True
        }).sort('kickoff_time', -1).limit(last_n))
        edge = 0
        for f in h2h_games:
            h_score = f.get('team_h_score')
            a_score = f.get('team_a_score')
            if h_score is None or a_score is None:
                continue
            if f['team_h'] == home_id and h_score > a_score:
                edge += 0.5
            elif f['team_a'] == home_id and a_score > h_score:
                edge += 0.5
            elif h_score == a_score:
                edge += 0.0
            else:
                edge -= 0.5
        return edge
    except Exception as e:
        logging.error(f"H2H edge error: {e}")
        return 0

def generate_gw_accumulator(db, top_n=5):
    """Generate strongest bets for the current gameweek with xG threshold filter."""
    try:
        # Get current gameweek from fixtures with event number
        upcoming = list(db.fixtures.find({'started': False, 'finished': False, 'event': {'$ne': None}}))
        accumulator = []

        for f in upcoming:
            try:
                # Skip if xG data missing
                home_xg = f.get('home_xg')
                away_xg = f.get('away_xg')
                if home_xg is None or away_xg is None:
                    continue

                home_id = f['team_h']
                away_id = f['team_a']
                home_form = get_team_form(home_id, db)
                away_form = get_team_form(away_id, db)
                table_diff = f.get('away_table_pos', 10) - f.get('home_table_pos', 10)
                h2h = get_h2h_edge(home_id, away_id, db)

                final_strength = (home_xg - away_xg) + 0.1*(home_form - away_form) + 0.05*table_diff + h2h

                # Apply threshold for "strong bets"
                if final_strength >= 0.5:
                    pick = f"{f['team_h_name']} to Win"
                elif final_strength <= -0.5:
                    pick = f"{f['team_a_name']} to Win"
                else:
                    continue  # Skip weak bets

                accumulator.append((abs(final_strength), f"{f['team_h_name']} vs {f['team_a_name']}: {pick}"))

            except Exception as e:
                logging.error(f"GW Accumulator Error for {f.get('team_h_name')} vs {f.get('team_a_name')}: {e}")

        # Sort strongest bets first
        accumulator.sort(reverse=True, key=lambda x: x[0])
        top_bets = [x[1] for x in accumulator[:top_n]]
        return "\n".join(top_bets) if top_bets else "No strong bets found."
    except Exception as e:
        logging.error(f"Generate accumulator error: {e}")
        return "Error generating accumulator."

# --- FIXTURE MENU SYSTEM ---
def show_fixture_menu(db):
    fixtures = get_next_fixtures(db, limit=10)
    keyboard = []
    for _, f in fixtures:
        keyboard.append([InlineKeyboardButton(f"{f['team_h_name']} vs {f['team_a_name']}", callback_data=f"select_{f['id']}")])
    return keyboard if keyboard else [[InlineKeyboardButton("No upcoming fixtures", callback_data="none")]]

# --- BACKGROUND MONITOR ---
def run_monitor():
    while True:
        try:
            time.sleep(60)
            client, db = get_db()
            now = datetime.now(timezone.utc)
            upcoming = db.fixtures.find({'kickoff_time': {'$exists': True}, 'finished': False, 'alert_sent': {'$ne': True}})
            for f in upcoming:
                ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
                diff_mins = (ko - now).total_seconds() / 60
                if 59 <= diff_mins <= 61:
                    logging.info(f"Auto-checking match: {f['team_h_name']} vs {f['team_a_name']}")
                    sofa_events = get_today_sofascore_matches()
                    target_event = next((e for e in sofa_events if e.get('homeTeam', {}).get('name') == f['team_h_name'] 
                                         or e.get('awayTeam', {}).get('name') == f['team_a_name']), None)
                    msg_parts = [f"📢 *Lineups Out: {f['team_h_name']} vs {f['team_a_name']}*"]
                    if target_event:
                        sofa_lineup = fetch_sofascore_lineup(target_event['id'])
                        if sofa_lineup:
                            db.tactical_data.update_one(
                                {"match_id": target_event['id']},
                                {"$set": {"home_team": target_event['homeTeam']['name'],
                                          "away_team": target_event['awayTeam']['name'],
                                          "players": sofa_lineup,
                                          "last_updated": datetime.now(timezone.utc)}},
                                upsert=True
                            )
                            db.fixtures.update_one({'id': f['id']}, {'$set': {'sofascore_id': target_event['id']}})
                            oop = detect_tactical_oop(db, target_event['id'])
                            if oop: msg_parts.append(f"\n*Tactical Shifts:*\n{oop}")
                    benched = detect_high_ownership_benched(f['id'], db)
                    if benched: msg_parts.append(f"\n*Benched Assets:*\n{benched}")
                    final_msg = "\n".join(msg_parts)
                    users = db.users.find()
                    for u in users:
                        try:
                            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                          json={"chat_id": u['chat_id'], "text": final_msg, "parse_mode": "Markdown"})
                        except Exception as e:
                            logging.error(f"Failed to send alert: {e}")
                    db.fixtures.update_one({'id': f['id']}, {'$set': {'alert_sent': True}})
            client.close()
        except Exception as e:
            logging.error(f"Monitor Loop Error: {e}")

# --- TELEGRAM COMMANDS ---
async def update_standings_command(update: Update, context: CallbackContext):
    client, db = get_db()
    try:
        rows = fetch_pl_standings()
        save_standings_to_mongo(db, rows)
        await update.message.reply_text("✅ Premier League standings updated.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to update standings:\n{e}")
    finally:
        client.close()
        
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    user_id = update.effective_chat.id
    db.users.update_one({'chat_id': user_id}, {'$set': {'chat_id': user_id, 'joined': datetime.now()}}, upsert=True)
    welcome_msg = (
        "👋 Welcome to the Premier League Lineup Bot!\n\n"
        "This bot monitors lineups and alerts you 60 mins before kickoff when tactical shifts or benched high-ownership players occur.\n\n"
        "Commands:\n"
        "/start - Show this message\n"
        "/update - Sync latest FPL & SofaScore data\n"
        "/check - View latest tactical analysis\n"
        "/builder - Generate Fixture Bet Builder\n"
        "/gw_accumulator - View gameweek accumulator\n"
        "/status - Check bot status and last update info\n\n"
        "Tip: Use the 📆 Next fixtures button below to see upcoming matches."
    )
    keyboard = show_fixture_menu(db)
    await update.message.reply_text(welcome_msg, reply_markup=InlineKeyboardMarkup(keyboard))
    client.close()

async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("🔄 Syncing FPL & SofaScore Data...")
    client, db = get_db()
    try:
        base_url = "https://fantasy.premierleague.com/api/"
        bootstrap = requests.get(base_url + "bootstrap-static/", timeout=30).json()
        players = pd.DataFrame(bootstrap['elements'])
        pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
        players['position'] = players['element_type'].map(pos_map)
        players_dict = players[['id','web_name','position','minutes','team','goals_scored','assists','total_points','selected_by_percent']].to_dict('records')
        db.players.delete_many({})
        db.players.insert_many(players_dict)

        fixtures = requests.get(base_url + "fixtures/", timeout=30).json()
        teams_df = pd.DataFrame(bootstrap['teams'])
        team_map = dict(zip(teams_df['id'], teams_df['name']))
        fixtures_dict = []
        for f in fixtures:
            fixtures_dict.append({
                'id': f['id'], 'event': f.get('event'), 'team_h': f['team_h'], 'team_a': f['team_a'],
                'team_h_name': team_map.get(f['team_h'], str(f['team_h'])),
                'team_a_name': team_map.get(f['team_a'], str(f['team_a'])),
                'kickoff_time': f.get('kickoff_time'), 'started': f.get('started', False), 
                'finished': f.get('finished', False),
                'team_h_score': f.get('team_h_score'), 'team_a_score': f.get('team_a_score')
            })
        db.fixtures.delete_many({})
        db.fixtures.insert_many(fixtures_dict)

        lineup_entries = []
        for f in fixtures:
            for s in f.get('stats', []):
                if s.get('identifier') == 'minutes':
                    for side in ('h','a'):
                        for p in s.get(side, []):
                            lineup_entries.append({"match_id": f['id'], "player_id": p['element'], "minutes": p['value']})
        db.lineups.delete_many({})
        if lineup_entries: db.lineups.insert_many(lineup_entries)

        today_events = get_today_sofascore_matches()
        for event in today_events:
            sofa_lineup = fetch_sofascore_lineup(event['id'])
            if sofa_lineup:
                db.tactical_data.update_one(
                    {"match_id": event['id']},
                    {"$set": {"home_team": event['homeTeam']['name'],
                              "away_team": event['awayTeam']['name'],
                              "players": sofa_lineup,
                              "last_updated": datetime.now(timezone.utc)}},
                    upsert=True
                )
        await update.message.reply_text("✅ Sync Complete.")
    except Exception as e:
        logging.error(f"/update error: {e}")
        await update.message.reply_text("⚠️ Failed to sync data.")
    finally:
        client.close()

async def check(update: Update, context: CallbackContext):
    client, db = get_db()
    latest_tactical = db.tactical_data.find_one(sort=[("last_updated", -1)])
    if not latest_tactical:
        client.close()
        await update.message.reply_text("No tactical data found. Run /update.")
        return
    msg = f"📊 *Analysis: {latest_tactical['home_team']} vs {latest_tactical['away_team']}*\n"
    oop = detect_tactical_oop(db, latest_tactical['match_id'])
    msg += oop if oop else "✅ No tactical OOP shifts."
    client.close()
    await update.message.reply_text(msg, parse_mode="Markdown")

async def builder(update: Update, context: CallbackContext):
    client, db = get_db()
    keyboard = show_fixture_menu(db)
    await update.message.reply_text("📊 Select a fixture for bet builder:", reply_markup=InlineKeyboardMarkup(keyboard))
    client.close()

async def gw_accumulator(update: Update, context: CallbackContext):
    client, db = get_db()
    msg = "📊 *Gameweek Accumulator:*\n\n"
    msg += generate_gw_accumulator(db)
    client.close()
    await update.message.reply_text(msg, parse_mode="Markdown")

async def status(update: Update, context: CallbackContext):
    client, db = get_db()
    player_count = db.players.count_documents({})
    fixture_count = db.fixtures.count_documents({'started': False, 'finished': False})
    tactical_count = db.tactical_data.count_documents({})
    user_count = db.users.count_documents({})
    
    latest_tactical = db.tactical_data.find_one(sort=[("last_updated", -1)])
    last_update = "Never"
    if latest_tactical:
        last_update = latest_tactical['last_updated'].strftime("%Y-%m-%d %H:%M UTC")
    
    msg = (
        f"🤖 *Bot Status*\n\n"
        f"Players in DB: {player_count}\n"
        f"Upcoming fixtures: {fixture_count}\n"
        f"Lineups cached: {tactical_count}\n"
        f"Registered users: {user_count}\n"
        f"Last lineup update: {last_update}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")
    client.close()

async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    client, db = get_db()
    if query.data == "next_fixtures":
        keyboard = show_fixture_menu(db)
        await query.edit_message_text("📆 Select a fixture:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data.startswith("select_"):
        fixture_id = int(query.data.split("_")[1])
        fixture = db.fixtures.find_one({"id": fixture_id})
        if fixture:
            msg = f"📊 *Fixture Bet Builder: {fixture['team_h_name']} vs {fixture['team_a_name']}*\n\n"
            msg += generate_fixture_bet_builder(fixture, db)
            await query.edit_message_text(msg, parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Fixture not found.")
    client.close()

# --- FLASK APP (for Render) ---
app = Flask(__name__)
@app.route('/')
def index(): return "Bot Running!"

# --- MAIN ---
if __name__ == "__main__":
    if not MONGODB_URI or not TELEGRAM_TOKEN:
        raise ValueError("MONGODB_URI and BOT_TOKEN environment variables required")
    
    # Start Telegram Bot
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("update", update_data))
    application.add_handler(CommandHandler("check", check))
    application.add_handler(CommandHandler("builder", builder))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("gw_accumulator", gw_accumulator))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_handler(CommandHandler("update_standings", update_standings_command))
    
    # Start monitor in background
    monitor_thread = threading.Thread(target=run_monitor, daemon=True)
    monitor_thread.start()

    # Start Flask app for ping
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000))), daemon=True).start()
    
    # Run Telegram bot
    logging.info("Starting PL Lineup Bot...")
    application.run_polling()
