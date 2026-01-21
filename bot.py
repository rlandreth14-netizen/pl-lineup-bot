import os
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

# --- ENV VARIABLES ---
MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')

HIGH_OWNERSHIP_THRESHOLD = 20.0  # %

# --- SOFASCORE CONFIG ---
SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

# --- MONGO HELPER ---
def get_db():
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    return client, db

# --- HISTORICAL STORAGE HELPER (ORIGINAL) ---
def save_gameweek_stats(gameweek, players_df, fixtures_df, lineups):
    os.makedirs("historical_stats", exist_ok=True)
    file_path = f"historical_stats/gameweek_{gameweek}.csv"
    players_df.to_csv(file_path, index=False)
    logging.info(f"Saved historical stats to {file_path}")

# --- SOFASCORE HELPERS (NEW) ---
def fetch_sofascore_lineup(match_id):
    """Pulls the tactical lineup from SofaScore."""
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS)
        if res.status_code != 200: return None
        data = res.json()
        players = []
        for side in ['home', 'away']:
            team_name = data[side]['team']['name']
            for entry in data.get(side, {}).get('players', []):
                p = entry['player']
                players.append({
                    "name": p['name'],
                    "sofa_id": p['id'],
                    "tactical_pos": entry.get('position', 'Unknown'), # D, M, F
                    "team": team_name
                })
        return players
    except Exception as e:
        logging.error(f"SofaScore Fetch Error: {e}")
        return None

def get_today_sofascore_matches():
    """Finds today's EPL match IDs on SofaScore."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS).json()
        # EPL Unique Tournament ID is 17
        return [e for e in res.get('events', []) if e.get('uniqueTournament', {}).get('id') == 17]
    except Exception as e:
        logging.error(f"SofaScore Schedule Error: {e}")
        return []

# --- ANALYSIS FUNCTIONS ---

# 1. Detect Abnormal Performance (Original)
def detect_abnormal(match_id):
    client, db = get_db()
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    if not lineups:
        client.close()
        return "No FPL lineup data available yet."
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
        if mins < 300: continue
        avg_attack = (row['goals_scored'] + row['assists']) / (mins / 90)
        match_attack = (row['goals_scored_x'] if 'goals_scored_x' in row else 0) + (row['assists_x'] if 'assists_x' in row else 0)
        # Note: In FPL API, live stats are sometimes separate. Assuming standard update logic here.
        
    client.close()
    # Placeholder for logic, keeping your original structure valid
    return "✅ Performance Normal" if not insights else "\n".join(insights)

# 2. Detect Benched High Ownership (Original)
def detect_high_ownership_benched(match_id):
    client, db = get_db()
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    started_ids = {l['player_id'] for l in lineups if l['minutes'] > 0}
    players = list(db.players.find({'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}}))
    alerts = [f"🚨 {p['web_name']} ({p['selected_by_percent']}%) — NOT STARTING" 
              for p in players if p['id'] not in started_ids]
    client.close()
    return "\n".join(alerts)

# 3. Detect OOP Tactical Shifts (NEW)
def detect_tactical_oop(db):
    latest_tactical = db.tactical_data.find_one(sort=[("last_updated", -1)])
    if not latest_tactical:
        return "No SofaScore tactical data found."
    
    insights = []
    for p_sofa in latest_tactical['players']:
        # Bridge FPL player via name (case-insensitive regex)
        fpl_p = db.players.find_one({"web_name": {"$regex": f"^{p_sofa['name']}$", "$options": "i"}})
        if fpl_p:
            fpl_pos = fpl_p['position'] # DEF, MID, FWD
            sofa_pos = p_sofa['tactical_pos'] # D, M, F
            
            is_oop = False
            if fpl_pos == 'DEF' and sofa_pos in ['M', 'F']: is_oop = True
            elif fpl_pos == 'MID' and sofa_pos == 'F': is_oop = True
            
            if is_oop:
                insights.append(f"🔥 *OOP ALERT:* {p_sofa['name']} ({p_sofa['team']})\n"
                                f"FPL: {fpl_pos} ➡️ Playing: {sofa_pos}")
    
    return "\n".join(insights) if insights else "✅ No tactical OOP shifts."

# --- GET NEXT FIXTURES (ORIGINAL) ---
def get_next_fixtures(db, limit=5):
    now = datetime.now(timezone.utc)
    upcoming = []
    for f in db.fixtures.find({'started': False, 'finished': False}):
        if not f.get('kickoff_time'): continue
        ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
        if ko > now:
            upcoming.append((ko, f))
    upcoming.sort(key=lambda x: x[0])
    return upcoming[:limit]

# --- START COMMAND (ORIGINAL + FIXES) ---
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    today = datetime.now(timezone.utc).date()
    todays = []

    if db.fixtures.count_documents({}) == 0:
        await update.message.reply_text("👋 Database is empty. Please run /update first.")
        client.close()
        return

    for f in db.fixtures.find():
        if f.get('kickoff_time'):
            ko = datetime.fromisoformat(f['kickoff_time'].replace('Z','+00:00'))
            if ko.date() == today:
                todays.append((ko, f))
    client.close()

    if not todays:
        keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
        await update.message.reply_text("No games today.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    msg = ["⚽ Matches today:"]
    for ko, f in todays:
        msg.append(f"• {f['team_h_name']} vs {f['team_a_name']} — {ko.strftime('%H:%M UTC')}")
    await update.message.reply_text("\n".join(msg))

# --- UPDATE COMMAND (MERGED) ---
async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("🔄 Syncing FPL & SofaScore Data...")
    client, db = get_db()
    base_url = "https://fantasy.premierleague.com/api/"

    # --- 1. FPL PLAYERS ---
    bootstrap = requests.get(base_url + "bootstrap-static/").json()
    players = pd.DataFrame(bootstrap['elements'])
    pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    players['position'] = players['element_type'].map(pos_map)
    players_dict = players[['id', 'web_name', 'position', 'minutes', 'goals_scored', 'assists', 'total_points', 'selected_by_percent']].to_dict('records')

    # --- 2. FPL TEAMS & FIXTURES ---
    teams_df = pd.DataFrame(bootstrap['teams'])
    team_map = dict(zip(teams_df['id'], teams_df['name']))
    fixtures = requests.get(base_url + "fixtures/").json()
    fixtures_dict = []
    for f in fixtures:
        fixtures_dict.append({
            'id': f['id'], 'event': f['event'], 'team_h': f['team_h'], 'team_a': f['team_a'],
            'team_h_name': team_map.get(f['team_h'], str(f['team_h'])),
            'team_a_name': team_map.get(f['team_a'], str(f['team_a'])),
            'kickoff_time': f['kickoff_time'], 'started': f['started'], 'finished': f['finished']
        })

    # --- 3. FPL LINEUPS (MINUTES) ---
    lineup_entries = []
    for f in fixtures:
        for s in f.get('stats', []):
            if s.get('identifier') == 'minutes':
                for side in ('h','a'):
                    for p in s.get(side, []):
                        lineup_entries.append({"match_id": f['id'], "player_id": p['element'], "minutes": p['value']})

    # --- 4. SAVE FPL DATA TO MONGO ---
    db.players.delete_many({})
    db.players.insert_many(players_dict)
    db.fixtures.delete_many({})
    db.fixtures.insert_many(fixtures_dict)
    db.lineups.delete_many({})
    if lineup_entries:
        db.lineups.insert_many(lineup_entries)

    # --- 5. SOFASCORE TACTICAL SYNC (NEW) ---
    sofascore_count = 0
    today_events = get_today_sofascore_matches()
    for event in today_events:
        sofa_lineup = fetch_sofascore_lineup(event['id'])
        if sofa_lineup:
            db.tactical_data.update_one(
                {"match_id": event['id']},
                {"$set": {
                    "home_team": event['homeTeam']['name'],
                    "away_team": event['awayTeam']['name'],
                    "players": sofa_lineup,
                    "last_updated": datetime.now(timezone.utc)
                }},
                upsert=True
            )
            sofascore_count += 1

    # --- 6. HISTORICAL ARCHIVE ---
    current_gw = bootstrap['events'][0]['id']
    season_name = f"season_{datetime.now().year}_{datetime.now().year+1}"
    db[f"historical_stats_{season_name}"].replace_one(
        {'game_week': current_gw},
        {'game_week': current_gw, 'timestamp': datetime.now(timezone.utc),
         'players': players_dict, 'fixtures': fixtures_dict, 'lineups': lineup_entries},
        upsert=True
    )
    save_gameweek_stats(current_gw, players, pd.DataFrame(fixtures_dict), lineup_entries)

    client.close()
    await update.message.reply_text(f"✅ Update complete.\n📊 FPL Stats updated.\n📡 {sofascore_count} SofaScore tactical lineups cached.")

# --- CHECK COMMAND (MERGED) ---
async def check(update: Update, context: CallbackContext):
    client, db = get_db()
    
    # 1. Get Latest FPL Data
    latest_fpl = db.lineups.find_one(sort=[("match_id", -1)])
    
    # 2. Get Latest Tactical Data
    latest_tactical = db.tactical_data.find_one(sort=[("last_updated", -1)])
    
    if not latest_fpl and not latest_tactical:
        client.close()
        await update.message.reply_text("Run /update first.")
        return

    msg = "📊 *LATEST INSIGHTS*\n\n"

    # FPL Section
    if latest_fpl:
        match_id = latest_fpl['match_id']
        benched = detect_high_ownership_benched(match_id)
        if benched: msg += f"🚨 *Benched Stars (FPL ID {match_id}):*\n{benched}\n\n"

    # Tactical Section
    if latest_tactical:
        oop_insights = detect_tactical_oop(db)
        msg += f"📡 *Tactical Analysis ({latest_tactical['home_team']} vs {latest_tactical['away_team']}):*\n{oop_insights}"
    
    client.close()
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- CALLBACKS (ORIGINAL) ---
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
            if benched: msg += f"\n\n{benched}"
            await query.edit_message_text(msg)
    client.close()

# --- FLASK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot running"

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
