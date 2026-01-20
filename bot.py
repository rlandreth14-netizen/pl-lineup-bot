import os
import threading
import logging
from datetime import datetime, timezone
from contextlib import contextmanager
import time
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
API_BASE_URL = "https://fantasy.premierleague.com/api/"

# --- MONGO CONNECTION MANAGER ---
_mongo_client = None

def get_mongo_client():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGODB_URI)
    return _mongo_client

@contextmanager
def get_db():
    """Context manager for database connections"""
    client = get_mongo_client()
    db = client['premier_league']
    try:
        yield db
    finally:
        pass  # Connection pooling handles cleanup

# --- HISTORICAL STORAGE HELPER ---
def save_gameweek_stats(gameweek, players_df, fixtures_df, lineups):
    """Save gameweek statistics to local CSV files"""
    try:
        os.makedirs("historical_stats", exist_ok=True)
        file_path = f"historical_stats/gameweek_{gameweek}.csv"
        players_df.to_csv(file_path, index=False)
        logging.info(f"Saved historical stats to {file_path}")
    except Exception as e:
        logging.error(f"Failed to save historical stats: {e}")

# --- DETECT ABNORMAL PLAYER PERFORMANCE ---
def detect_abnormal(match_id):
    """Detect players with abnormal performance in a specific match"""
    try:
        with get_db() as db:
            lineups = list(db.lineups.find({'match_id': int(match_id)}))
            if not lineups:
                return "No lineup data available yet."
            
            lineup_df = pd.DataFrame(lineups)
            player_ids = lineup_df['player_id'].tolist()
            players = list(db.players.find({'id': {'$in': player_ids}}))
            
            if not players:
                return "No player data available."
            
            players_df = pd.DataFrame(players)
            
            # Merge lineup data with player stats
            merged = lineup_df.merge(
                players_df[['id', 'web_name', 'position', 'minutes', 'goals_scored', 'assists']],
                left_on='player_id',
                right_on='id',
                how='left',
                suffixes=('_match', '_season')
            )

            insights = []
            for _, row in merged.iterrows():
                # Season minutes
                season_mins = row.get('minutes_season', 0) or 0
                if season_mins < 300:  # Skip if less than ~3 full matches played
                    continue
                
                # Calculate season average per 90
                season_goals = row.get('goals_scored', 0) or 0
                season_assists = row.get('assists', 0) or 0
                avg_per_90 = (season_goals + season_assists) / (season_mins / 90)
                
                # Match performance
                match_mins = row.get('minutes_match', 0) or 0
                if match_mins == 0:
                    continue
                
                # For this match, we'd need actual match goals/assists
                # Since lineup only has minutes, we'll use a simplified check
                # In a real implementation, you'd need match-specific stats
                
                # Check if player played significantly more than usual
                avg_mins = season_mins / max(1, len(players))  # Rough estimate
                if match_mins >= 90 and avg_mins < 60:
                    insights.append(
                        f"⚠️ {row['web_name']} ({row['position']}) — rare starter (usually {avg_mins:.0f} mins/game)"
                    )
            
            return "\n".join(insights) if insights else "No abnormal player behaviour detected."
    
    except Exception as e:
        logging.error(f"Error detecting abnormal performance: {e}")
        return f"Error analyzing match: {str(e)}"

# --- DETECT HIGH-OWNERSHIP BENCHED PLAYERS ---
def detect_high_ownership_benched(match_id):
    """Detect high-ownership players who aren't starting"""
    try:
        with get_db() as db:
            # Get players who started (played any minutes)
            lineups = list(db.lineups.find({'match_id': int(match_id)}))
            started_ids = {l['player_id'] for l in lineups if l.get('minutes', 0) > 0}
            
            # Get fixture to determine teams
            fixture = db.fixtures.find_one({'id': int(match_id)})
            if not fixture:
                return "Fixture not found."
            
            team_h = fixture['team_h']
            team_a = fixture['team_a']
            
            # Get high-ownership players from these teams
            high_ownership = list(db.players.find({
                'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD},
                'team': {'$in': [team_h, team_a]}
            }))
            
            alerts = []
            for player in high_ownership:
                if player['id'] not in started_ids:
                    alerts.append(
                        f"🚨 {player['web_name']} ({player['selected_by_percent']}%) — NOT STARTING"
                    )
            
            return "\n".join(alerts) if alerts else "No high-ownership benchings."
    
    except Exception as e:
        logging.error(f"Error detecting benched players: {e}")
        return f"Error checking lineups: {str(e)}"

# --- GET NEXT FIXTURES ---
def get_next_fixtures(db, limit=5):
    """Get upcoming fixtures sorted by kickoff time"""
    try:
        now = datetime.now(timezone.utc)
        upcoming = []
        
        for f in db.fixtures.find({'started': False, 'finished': False}):
            kickoff = f.get('kickoff_time')
            if not kickoff:
                continue
            
            ko_time = datetime.fromisoformat(kickoff.replace('Z', '+00:00'))
            if ko_time > now:
                upcoming.append((ko_time, f))
        
        upcoming.sort(key=lambda x: x[0])
        return upcoming[:limit]
    
    except Exception as e:
        logging.error(f"Error getting fixtures: {e}")
        return []

# --- UPDATE DATA ---
async def update_data(update: Update, context: CallbackContext):
    """Pull latest FPL data and update database"""
    await update.message.reply_text("Pulling latest FPL data...")
    
    try:
        # Rate limiting - be nice to FPL API
        time.sleep(1)
        
        # --- FETCH BOOTSTRAP DATA ---
        response = requests.get(f"{API_BASE_URL}bootstrap-static/", timeout=30)
        response.raise_for_status()
        bootstrap = response.json()
        
        # --- PROCESS PLAYERS ---
        players = pd.DataFrame(bootstrap['elements'])
        pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
        players['position'] = players['element_type'].map(pos_map)
        
        players_dict = players[[
            'id', 'web_name', 'position', 'minutes', 'team',
            'goals_scored', 'assists', 'total_points',
            'selected_by_percent'
        ]].to_dict('records')

        # --- TEAM MAPPING ---
        teams_df = pd.DataFrame(bootstrap['teams'])
        team_map = dict(zip(teams_df['id'], teams_df['name']))

        # --- FETCH FIXTURES ---
        time.sleep(1)  # Rate limiting
        response = requests.get(f"{API_BASE_URL}fixtures/", timeout=30)
        response.raise_for_status()
        fixtures = response.json()
        
        fixtures_dict = []
        for f in fixtures:
            fixtures_dict.append({
                'id': f['id'],
                'event': f.get('event'),
                'team_h': f['team_h'],
                'team_a': f['team_a'],
                'team_h_name': team_map.get(f['team_h'], str(f['team_h'])),
                'team_a_name': team_map.get(f['team_a'], str(f['team_a'])),
                'kickoff_time': f.get('kickoff_time'),
                'started': f.get('started', False),
                'finished': f.get('finished', False)
            })

        # --- PROCESS LINEUPS ---
        lineup_entries = []
        for f in fixtures:
            for s in f.get('stats', []):
                if s.get('identifier') == 'minutes':
                    for side in ('h', 'a'):
                        for p in s.get(side, []):
                            lineup_entries.append({
                                "match_id": f['id'],
                                "player_id": p['element'],
                                "minutes": p['value']
                            })

        # --- SAVE TO MONGO ---
        with get_db() as db:
            db.players.delete_many({})
            if players_dict:
                db.players.insert_many(players_dict)
            
            db.fixtures.delete_many({})
            if fixtures_dict:
                db.fixtures.insert_many(fixtures_dict)
            
            db.lineups.delete_many({})
            if lineup_entries:
                db.lineups.insert_many(lineup_entries)

            # --- FIND CURRENT GAMEWEEK ---
            current_gw = None
            for event in bootstrap.get('events', []):
                if event.get('is_current'):
                    current_gw = event['id']
                    break
            
            if not current_gw:
                # Fallback to first event
                current_gw = bootstrap['events'][0]['id'] if bootstrap.get('events') else 1

            # --- SAVE HISTORICAL STATS ---
            season_name = f"season_{datetime.now().year}_{datetime.now().year + 1}"
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

        # --- SAVE TO LOCAL CSV ---
        save_gameweek_stats(current_gw, players, pd.DataFrame(fixtures_dict), lineup_entries)

        await update.message.reply_text("✅ Update complete and historical stats saved.")
    
    except requests.exceptions.RequestException as e:
        logging.error(f"API request failed: {e}")
        await update.message.reply_text(f"❌ Failed to fetch FPL data: {str(e)}")
    except Exception as e:
        logging.error(f"Update failed: {e}")
        await update.message.reply_text(f"❌ Update failed: {str(e)}")

# --- START COMMAND ---
async def start(update: Update, context: CallbackContext):
    """Show today's matches or next fixtures"""
    try:
        with get_db() as db:
            today = datetime.now(timezone.utc).date()
            todays = []

            for f in db.fixtures.find():
                kickoff = f.get('kickoff_time')
                if kickoff:
                    ko_time = datetime.fromisoformat(kickoff.replace('Z', '+00:00'))
                    if ko_time.date() == today:
                        todays.append((ko_time, f))
            
            todays.sort(key=lambda x: x[0])

        if not todays:
            keyboard = [[InlineKeyboardButton("📆 Next fixtures", callback_data="next_fixtures")]]
            await update.message.reply_text(
                "No games today.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        msg = ["⚽ Matches today:"]
        for ko, f in todays:
            msg.append(
                f"• {f['team_h_name']} vs {f['team_a_name']} — {ko.strftime('%H:%M UTC')}"
            )
        await update.message.reply_text("\n".join(msg))
    
    except Exception as e:
        logging.error(f"Start command failed: {e}")
        await update.message.reply_text("❌ Error loading fixtures.")

# --- CALLBACKS ---
async def handle_callbacks(update: Update, context: CallbackContext):
    """Handle inline keyboard button callbacks"""
    query = update.callback_query
    await query.answer()

    try:
        with get_db() as db:
            if query.data == "next_fixtures":
                fixtures = get_next_fixtures(db)
                if not fixtures:
                    await query.edit_message_text("No upcoming fixtures found.")
                    return
                
                lines = ["📆 Upcoming fixtures:"]
                for ko, f in fixtures:
                    lines.append(
                        f"• {f['team_h_name']} vs {f['team_a_name']} — "
                        f"{ko.strftime('%d %b %H:%M UTC')}"
                    )
                await query.edit_message_text("\n".join(lines))

            elif query.data.startswith("fixture_"):
                try:
                    match_id = int(query.data.split("_")[1])
                except (IndexError, ValueError):
                    await query.edit_message_text("Invalid fixture ID.")
                    return
                
                fixture = db.fixtures.find_one({'id': match_id})
                if not fixture:
                    await query.edit_message_text("Fixture not found.")
                    return
                
                abnormal = detect_abnormal(match_id)
                benched = detect_high_ownership_benched(match_id)
                
                msg = f"⚽ {fixture['team_h_name']} vs {fixture['team_a_name']}\n\n{abnormal}"
                if benched:
                    msg += f"\n\n{benched}"
                
                await query.edit_message_text(msg)
    
    except Exception as e:
        logging.error(f"Callback handler failed: {e}")
        await query.edit_message_text("❌ Error processing request.")

# --- CHECK COMMAND ---
async def check(update: Update, context: CallbackContext):
    """Check latest match for abnormalities"""
    try:
        with get_db() as db:
            latest = db.lineups.find_one(sort=[("match_id", -1)])
        
        if not latest:
            await update.message.reply_text("No lineup data found. Run /update first.")
            return
        
        match_id = latest['match_id']
        abnormal = detect_abnormal(match_id)
        benched = detect_high_ownership_benched(match_id)
        
        msg_parts = [f"📊 Match ID {match_id}", abnormal]
        if benched:
            msg_parts.append(benched)
        
        await update.message.reply_text("\n\n".join(msg_parts))
    
    except Exception as e:
        logging.error(f"Check command failed: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

# --- FLASK ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot running"

@flask_app.route('/health')
def health():
    """Health check endpoint"""
    try:
        with get_db() as db:
            db.command('ping')
        return {"status": "healthy", "database": "connected"}, 200
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}, 500

def run_flask():
    """Run Flask server in background thread"""
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# --- MAIN ---
if __name__ == "__main__":
    # Validate environment variables
    if not MONGODB_URI:
        raise ValueError("MONGODB_URI environment variable not set")
    if not TELEGRAM_TOKEN:
        raise ValueError("BOT_TOKEN environment variable not set")
    
    # Start Flask server
    threading.Thread(target=run_flask, daemon=True).start()

    # Start Telegram bot
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    
    logging.info("Starting bot...")
    app.run_polling()
