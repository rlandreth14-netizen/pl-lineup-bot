import os
import time
import threading
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from contextlib import contextmanager
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- CONFIG ---
MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')
HIGH_OWNERSHIP_THRESHOLD = 20.0
SOFASCORE_PL_ID = 17
LINEUP_CHECK_WINDOW = (59, 61)  # minutes before kickoff

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

# --- SINGLETON MONGO CLIENT ---
_mongo_client = None

def get_mongo_client():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGODB_URI)
    return _mongo_client

@contextmanager
def get_db():
    """Context manager for database access"""
    client = get_mongo_client()
    db = client['premier_league']
    try:
        yield db
    except Exception as e:
        logging.error(f"Database error: {e}")
        raise

# --- SOFASCORE API ---
def fetch_sofascore_lineup(match_id, retries=2):
    """Fetch lineup from SofaScore with retry logic"""
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"
    
    for attempt in range(retries):
        try:
            res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
            if res.status_code == 200:
                data = res.json()
                players = []
                
                for side in ['home', 'away']:
                    if team_data := data.get(side):
                        team_name = team_data['team']['name']
                        for entry in team_data.get('players', []):
                            if p := entry.get('player'):
                                players.append({
                                    "name": p.get('name', 'Unknown'),
                                    "sofa_id": p.get('id'),
                                    "tactical_pos": entry.get('position', 'Unknown'),
                                    "team": team_name
                                })
                
                logging.info(f"Fetched {len(players)} players for match {match_id}")
                return players
            
            logging.warning(f"SofaScore returned {res.status_code}, retry {attempt + 1}/{retries}")
            time.sleep(2)
            
        except Exception as e:
            logging.error(f"SofaScore error (attempt {attempt + 1}): {e}")
            if attempt < retries - 1:
                time.sleep(2)
    
    return None

def get_today_sofascore_matches():
    """Get today's Premier League matches from SofaScore"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
        if res.status_code == 200:
            data = res.json()
            pl_matches = [e for e in data.get('events', []) 
                         if e.get('tournament', {}).get('uniqueTournament', {}).get('id') == SOFASCORE_PL_ID]
            logging.info(f"Found {len(pl_matches)} PL matches today")
            return pl_matches
    except Exception as e:
        logging.error(f"Error fetching matches: {e}")
    
    return []

# --- ANALYSIS FUNCTIONS ---
def match_fpl_player(player_name, db):
    """Match SofaScore player name to FPL database (cached lookup)"""
    return db.players.find_one({"web_name": {"$regex": f"^{player_name}$", "$options": "i"}})

def detect_tactical_oop(db, match_id_filter=None):
    """Detect players playing out of position"""
    query = {"match_id": match_id_filter} if match_id_filter else {}
    tactical_data = db.tactical_data.find_one(query, sort=[("last_updated", -1)])
    
    if not tactical_data:
        return None
    
    insights = []
    oop_rules = {
        'DEF': lambda pos: pos not in ['DEF', 'GK'],
        'MID': lambda pos: pos == 'FWD',
        'FWD': lambda pos: pos in ['MID', 'DEF']
    }
    
    for p_sofa in tactical_data.get('players', []):
        if fpl_p := match_fpl_player(p_sofa['name'], db):
            fpl_pos = fpl_p.get('position')
            sofa_pos = p_sofa.get('tactical_pos', 'Unknown')
            
            if fpl_pos in oop_rules and oop_rules[fpl_pos](sofa_pos):
                insights.append(f"🔥 {p_sofa['name']} ({p_sofa['team']}): {fpl_pos} ➡️ {sofa_pos}")
    
    return "\n".join(insights) if insights else None

def detect_high_ownership_benched(match_id, db):
    """Detect high-ownership FPL players not in starting lineup"""
    fixture = db.fixtures.find_one({'id': int(match_id)})
    if not fixture:
        return None
    
    # Get tactical data to see who's starting
    tactical = db.tactical_data.find_one({'match_id': match_id})
    if not tactical:
        return None
    
    starting_names = {p['name'].lower() for p in tactical.get('players', [])}
    
    # Find high-ownership players from these teams
    high_ownership = db.players.find({
        'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD},
        'team': {'$in': [fixture['team_h'], fixture['team_a']]}
    })
    
    alerts = [f"🚨 {p['web_name']} ({p['selected_by_percent']:.1f}%)" 
              for p in high_ownership if p['web_name'].lower() not in starting_names]
    
    return "\n".join(alerts) if alerts else None

# --- BET BUILDER FUNCTIONS ---
def evaluate_team_result(fixture):
    """Predict match result using xG"""
    home_xg = fixture.get('home_xg', 1.2)
    away_xg = fixture.get('away_xg', 1.0)
    diff = home_xg - away_xg
    
    if diff >= 0.5:
        return f"{fixture['team_h_name']} to Win"
    elif diff <= -0.5:
        return f"{fixture['team_a_name']} to Win"
    return "Draw / Skip"

def evaluate_btts(fixture):
    """Predict both teams to score"""
    home_xg = fixture.get('home_xg', 1.2)
    away_xg = fixture.get('away_xg', 1.2)
    
    if home_xg >= 1.2 and away_xg >= 1.2:
        return "Yes"
    elif home_xg < 1.0 or away_xg < 1.0:
        return "No"
    return "Skip"

def select_shot_player(team_name, lineup, db):
    """Select best player for shots on target bet"""
    for p in lineup:
        if p['team'] == team_name:
            if fpl_p := match_fpl_player(p['name'], db):
                if (fpl_p.get('position') in ['FWD', 'MID'] and 
                    fpl_p.get('minutes', 0) > 0 and
                    p.get('tactical_pos') in ['FWD', 'MID']):
                    return p['name']
    return None

def generate_fixture_bet_builder(fixture, db):
    """Generate bet builder for a fixture"""
    builder = [
        f"• Result: {evaluate_team_result(fixture)}",
        f"• BTTS: {evaluate_btts(fixture)}"
    ]
    
    if sofa_data := db.tactical_data.find_one({"match_id": fixture.get('sofascore_id')}):
        lineup = sofa_data.get('players', [])
        
        if home_player := select_shot_player(fixture['team_h_name'], lineup, db):
            builder.append(f"• {home_player} 1+ SOT")
        
        if away_player := select_shot_player(fixture['team_a_name'], lineup, db):
            builder.append(f"• {away_player} 1+ SOT")
    
    return "\n".join(builder)

# --- FORM & H2H ANALYSIS ---
def get_team_form(team_id, db, last_n=5):
    """Calculate team form points from last N matches"""
    results = db.fixtures.find({
        '$or': [{'team_h': team_id}, {'team_a': team_id}],
        'finished': True,
        'team_h_score': {'$ne': None},
        'team_a_score': {'$ne': None}
    }).sort('kickoff_time', -1).limit(last_n)
    
    points = 0
    for f in results:
        h_score, a_score = f['team_h_score'], f['team_a_score']
        is_home = f['team_h'] == team_id
        
        if (is_home and h_score > a_score) or (not is_home and a_score > h_score):
            points += 3
        elif h_score == a_score:
            points += 1
    
    return points

def get_h2h_edge(home_id, away_id, db, last_n=3):
    """Calculate head-to-head edge for home team"""
    h2h = db.fixtures.find({
        '$or': [
            {'team_h': home_id, 'team_a': away_id},
            {'team_h': away_id, 'team_a': home_id}
        ],
        'finished': True,
        'team_h_score': {'$ne': None}
    }).sort('kickoff_time', -1).limit(last_n)
    
    edge = 0
    for f in h2h:
        h_score, a_score = f['team_h_score'], f['team_a_score']
        
        if (f['team_h'] == home_id and h_score > a_score) or \
           (f['team_a'] == home_id and a_score > h_score):
            edge += 0.5
        elif h_score != a_score:
            edge -= 0.5
    
    return edge

def generate_gw_accumulator(db, top_n=5):
    """Generate top N strongest bets for gameweek"""
    upcoming = db.fixtures.find({
        'started': False,
        'finished': False,
        'event': {'$ne': None},
        'home_xg': {'$ne': None},
        'away_xg': {'$ne': None}
    })
    
    accumulator = []
    
    for f in upcoming:
        home_id, away_id = f['team_h'], f['team_a']
        home_xg, away_xg = f['home_xg'], f['away_xg']
        
        # Calculate strength score
        home_form = get_team_form(home_id, db)
        away_form = get_team_form(away_id, db)
        table_diff = f.get('away_table_pos', 10) - f.get('home_table_pos', 10)
        h2h = get_h2h_edge(home_id, away_id, db)
        
        strength = (home_xg - away_xg) + 0.1*(home_form - away_form) + 0.05*table_diff + h2h
        
        # Only include strong bets
        if strength >= 0.5:
            pick = f"{f['team_h_name']} to Win"
            accumulator.append((abs(strength), f"{f['team_h_name']} vs {f['team_a_name']}: {pick}"))
        elif strength <= -0.5:
            pick = f"{f['team_a_name']} to Win"
            accumulator.append((abs(strength), f"{f['team_h_name']} vs {f['team_a_name']}: {pick}"))
    
    accumulator.sort(reverse=True, key=lambda x: x[0])
    return "\n".join([x[1] for x in accumulator[:top_n]]) if accumulator else "No strong bets found."

# --- UI HELPERS ---
def get_next_fixtures(db, limit=5):
    """Get next N upcoming fixtures"""
    now = datetime.now(timezone.utc)
    fixtures = []
    
    for f in db.fixtures.find({'started': False, 'finished': False, 'kickoff_time': {'$ne': None}}):
        ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
        if ko > now:
            fixtures.append((ko, f))
    
    fixtures.sort(key=lambda x: x[0])
    return fixtures[:limit]

def show_fixture_menu(db):
    """Generate fixture selection keyboard"""
    fixtures = get_next_fixtures(db, limit=10)
    return [
        [InlineKeyboardButton(f"{f['team_h_name']} vs {f['team_a_name']}", callback_data=f"select_{f['id']}")]
        for _, f in fixtures
    ] or [[InlineKeyboardButton("No upcoming fixtures", callback_data="none")]]

# --- BACKGROUND MONITOR ---
def run_monitor():
    """Background thread to monitor lineups 60 mins before matches"""
    logging.info("Monitor thread started")
    
    while True:
        try:
            time.sleep(60)
            
            with get_db() as db:
                now = datetime.now(timezone.utc)
                
                for f in db.fixtures.find({
                    'kickoff_time': {'$exists': True},
                    'finished': False,
                    'alert_sent': {'$ne': True}
                }):
                    ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
                    diff_mins = (ko - now).total_seconds() / 60
                    
                    if LINEUP_CHECK_WINDOW[0] <= diff_mins <= LINEUP_CHECK_WINDOW[1]:
                        process_lineup_alert(f, db)
                        
        except Exception as e:
            logging.error(f"Monitor error: {e}")

def process_lineup_alert(fixture, db):
    """Process and send lineup alert for a fixture"""
    logging.info(f"Checking lineup: {fixture['team_h_name']} vs {fixture['team_a_name']}")
    
    # Find SofaScore match
    sofa_events = get_today_sofascore_matches()
    target_event = next((e for e in sofa_events 
                        if fixture['team_h_name'] in e.get('homeTeam', {}).get('name', '') 
                        or fixture['team_a_name'] in e.get('awayTeam', {}).get('name', '')), None)
    
    if not target_event:
        logging.warning(f"No SofaScore match found for {fixture['team_h_name']} vs {fixture['team_a_name']}")
        return
    
    # Fetch lineup
    sofa_lineup = fetch_sofascore_lineup(target_event['id'])
    if not sofa_lineup:
        return
    
    # Save to database
    db.tactical_data.update_one(
        {"match_id": target_event['id']},
        {"$set": {
            "home_team": target_event['homeTeam']['name'],
            "away_team": target_event['awayTeam']['name'],
            "players": sofa_lineup,
            "last_updated": datetime.now(timezone.utc)
        }},
        upsert=True
    )
    
    db.fixtures.update_one({'id': fixture['id']}, {'$set': {'sofascore_id': target_event['id']}})
    
    # Build alert message
    msg_parts = [f"📢 *Lineups Out: {fixture['team_h_name']} vs {fixture['team_a_name']}*"]
    
    if oop := detect_tactical_oop(db, target_event['id']):
        msg_parts.append(f"\n*Tactical Shifts:*\n{oop}")
    
    if benched := detect_high_ownership_benched(target_event['id'], db):
        msg_parts.append(f"\n*Benched Players:*\n{benched}")
    
    # Send to all users
    send_to_all_users("\n".join(msg_parts), db)
    
    # Mark as alerted
    db.fixtures.update_one({'id': fixture['id']}, {'$set': {'alert_sent': True}})

def send_to_all_users(message, db):
    """Send message to all registered users"""
    sent = 0
    for user in db.users.find():
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": user['chat_id'], "text": message, "parse_mode": "Markdown"},
                timeout=5
            )
            sent += 1
        except Exception as e:
            logging.error(f"Failed to send to {user['chat_id']}: {e}")
    
    logging.info(f"Sent alerts to {sent} users")

# --- TELEGRAM COMMANDS ---
async def start(update: Update, context: CallbackContext):
    """Handle /start command"""
    with get_db() as db:
        user_id = update.effective_chat.id
        db.users.update_one(
            {'chat_id': user_id},
            {'$set': {'chat_id': user_id, 'joined': datetime.now(timezone.utc)}},
            upsert=True
        )
        
        await update.message.reply_text(
            "👋 *Welcome to PL Lineup Bot!*\n\n"
            "Get alerts 60 mins before kickoff for:\n"
            "• Tactical position changes\n"
            "• High-ownership benched players\n\n"
            "*Commands:*\n"
            "/update - Sync FPL data\n"
            "/check - Latest analysis\n"
            "/builder - Bet builder\n"
            "/gw_accumulator - Top bets\n"
            "/status - Bot status",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(show_fixture_menu(db))
        )

async def update_data(update: Update, context: CallbackContext):
    """Handle /update command"""
    await update.message.reply_text("🔄 Syncing data...")
    
    with get_db() as db:
        try:
            # Fetch FPL data
            base_url = "https://fantasy.premierleague.com/api/"
            bootstrap = requests.get(f"{base_url}bootstrap-static/", timeout=30).json()
            
            # Process players
            players = pd.DataFrame(bootstrap['elements'])
            players['position'] = players['element_type'].map({1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'})
            
            db.players.delete_many({})
            db.players.insert_many(
                players[['id', 'web_name', 'position', 'minutes', 'team', 
                        'goals_scored', 'assists', 'total_points', 'selected_by_percent']].to_dict('records')
            )
            
            # Process fixtures
            teams = pd.DataFrame(bootstrap['teams'])
            team_map = dict(zip(teams['id'], teams['name']))
            
            fixtures_data = requests.get(f"{base_url}fixtures/", timeout=30).json()
            fixtures = [{
                'id': f['id'], 'event': f.get('event'),
                'team_h': f['team_h'], 'team_a': f['team_a'],
                'team_h_name': team_map.get(f['team_h'], str(f['team_h'])),
                'team_a_name': team_map.get(f['team_a'], str(f['team_a'])),
                'kickoff_time': f.get('kickoff_time'),
                'started': f.get('started', False),
                'finished': f.get('finished', False),
                'team_h_score': f.get('team_h_score'),
                'team_a_score': f.get('team_a_score')
            } for f in fixtures_data]
            
            db.fixtures.delete_many({})
            db.fixtures.insert_many(fixtures)
            
            await update.message.reply_text("✅ Sync complete!")
            
        except Exception as e:
            logging.error(f"Update error: {e}")
            await update.message.reply_text(f"❌ Sync failed: {str(e)}")

async def check(update: Update, context: CallbackContext):
    """Handle /check command"""
    with get_db() as db:
        if tactical := db.tactical_data.find_one(sort=[("last_updated", -1)]):
            msg = f"📊 *{tactical['home_team']} vs {tactical['away_team']}*\n\n"
            msg += detect_tactical_oop(db, tactical['match_id']) or "✅ No tactical shifts"
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text("No tactical data yet. Run /update first.")

async def builder(update: Update, context: CallbackContext):
    """Handle /builder command"""
    with get_db() as db:
        await update.message.reply_text(
            "📊 Select a fixture:",
            reply_markup=InlineKeyboardMarkup(show_fixture_menu(db))
        )

async def gw_accumulator(update: Update, context: CallbackContext):
    """Handle /gw_accumulator command"""
    with get_db() as db:
        msg = "📊 *Gameweek Accumulator*\n\n" + generate_gw_accumulator(db)
        await update.message.reply_text(msg, parse_mode="Markdown")

async def status(update: Update, context: CallbackContext):
    """Handle /status command"""
    with get_db() as db:
        latest = db.tactical_data.find_one(sort=[("last_updated", -1)])
        last_update = latest['last_updated'].strftime("%Y-%m-%d %H:%M UTC") if latest else "Never"
        
        await update.message.reply_text(
            f"🤖 *Bot Status*\n\n"
            f"Players: {db.players.count_documents({})}\n"
            f"Upcoming: {db.fixtures.count_documents({'started': False, 'finished': False})}\n"
            f"Lineups: {db.tactical_data.count_documents({})}\n"
            f"Users: {db.users.count_documents({})}\n"
            f"Last update: {last_update}",
            parse_mode="Markdown"
        )

async def handle_callbacks(update: Update, context: CallbackContext):
    """Handle inline keyboard callbacks"""
    query = update.callback_query
    await query.answer()
    
    with get_db() as db:
        if query.data.startswith("select_"):
            fixture_id = int(query.data.split("_")[1])
            
            if fixture := db.fixtures.find_one({"id": fixture_id}):
                msg = f"📊 *{fixture['team_h_name']} vs {fixture['team_a_name']}*\n\n"
                msg += generate_fixture_bet_builder(fixture, db)
                await query.edit_message_text(msg, parse_mode="Markdown")
            else:
                await query.edit_message_text("❌ Fixture not found")

# --- FLASK & MAIN ---
app = Flask(__name__)
@app.route('/')
def index():
    return "PL Lineup Bot Running!"

if __name__ == "__main__":
    if not all([MONGODB_URI, TELEGRAM_TOKEN]):
        raise ValueError("MONGODB_URI and BOT_TOKEN required")
    
    # Start Flask
    threading.Thread(target=lambda: app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    ), daemon=True).start()
    
    # Start monitor
    threading.Thread(target=run_monitor, daemon=True).start()
    
    # Start bot
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("update", update_data))
    application.add_handler(CommandHandler("check", check))
    application.add_handler(CommandHandler("builder", builder))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("gw_accumulator", gw_accumulator))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    
    logging.info("Starting PL Lineup Bot...")
    application.run_polling()
