import os
import time
import threading
import logging
import requests
import pandas as pd
from bs4 import BeautifulSoup
import json
import base64
from datetime import datetime, timezone
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler
from understatapi import UnderstatClient

logging.basicConfig(level=logging.INFO)

# --- CONFIG ---
MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')
HIGH_OWNERSHIP_THRESHOLD = 16.0
SOFASCORE_BASE_URL = "https://api.sofascore.com/api/v1"
PL_TOURNAMENT_ID = 17
PL_SEASON_ID = 76986

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

TEAM_NAME_MAP = {
    "Man City": "Manchester City",
    "Man Utd": "Manchester United",
    "Newcastle": "Newcastle United",
    "Spurs": "Tottenham",
    "Nott’m Forest": "Nottingham Forest",
    "Wolves": "Wolverhampton Wanderers",
}

# --- MONGO HELPER ---
def get_db():
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    return client, db

def save_standings_to_mongo(db, rows):
    """Save PL standings to MongoDB"""
    collection = db.standings
    collection.delete_many({})

    for row in rows:
        team = row["team"]
        doc = {
            "team_id": team["id"],
            "team_name": team["name"],
            "position": row["position"],
            "played": row["played"],
            "wins": row["wins"],
            "draws": row["draws"],
            "losses": row["losses"],
            "goals_for": row["goals_for"],
            "goals_against": row["goals_against"],
            "goal_diff": row["goal_diff"],
            "points": row["points"],
            "xG": row.get("xG", 0.0),
            "xGA": row.get("xGA", 0.0),
            "xGD": row.get("xGD", 0.0),
            "xPTS": row.get("xPTS", 0.0),
            "xG_recent": row.get("xG_recent", 0.0),
            "xGA_recent": row.get("xGA_recent", 0.0),
            "xPTS_recent": row.get("xPTS_recent", 0.0),
            "updated_at": datetime.utcnow(),
            "ppda_avg": row.get("ppda_avg", 20.0),
            "home_xG_pg": row.get("home_xG_pg", 1.0),
            "away_xG_pg": row.get("away_xG_pg", 1.0),
        }
        collection.insert_one(doc)

# --- SOFASCORE FUNCTIONS ---
def fetch_sofascore_lineup(match_id, retries=2):
    url = f"{SOFASCORE_BASE_URL}/event/{match_id}/lineups"
    for attempt in range(retries):
        try:
            res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
            if res.status_code != 200:
                logging.warning(f"SofaScore returned {res.status_code}")
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
            logging.error(f"SofaScore error (attempt {attempt+1}): {e}")
            time.sleep(2)
    return None

def get_today_sofascore_matches():
    date_str = datetime.now().strftime("%Y-%m-%d")
    url = f"{SOFASCORE_BASE_URL}/sport/football/scheduled-events/{date_str}"
    try:
        res = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10).json()
        return [e for e in res.get('events', []) 
                if e.get('tournament', {}).get('uniqueTournament', {}).get('id') == PL_TOURNAMENT_ID]
    except Exception as e:
        logging.error(f"Error fetching matches: {e}")
        return []

# --- ANALYSIS FUNCTIONS ---
def detect_high_ownership_benched(match_id, db):
    try:
        lineups = list(db.lineups.find({'match_id': int(match_id)}))
        if not lineups: return None
        started_ids = {l['player_id'] for l in lineups if l.get('minutes', 0) > 0}
        players = list(db.players.find({'selected_by_percent': {'$gte': HIGH_OWNERSHIP_THRESHOLD}}))
        alerts = [f"🚨 {p['web_name']} — NOT STARTING" for p in players if p['id'] not in started_ids]
        return "\n".join(alerts) if alerts else None
    except Exception as e:
        logging.error(f"Benched check error: {e}")
        return None

def detect_tactical_oop(db, match_id_filter=None):
    try:
        query = {"match_id": match_id_filter} if match_id_filter else {}
        latest = db.tactical_data.find_one(query, sort=[("last_updated", -1)])
        if not latest: return None
        insights = []
        
        for p_sofa in latest.get('players', []):
            fpl_p = db.players.find_one({"web_name": {"$regex": f"^{p_sofa['name']}$", "$options": "i"}})
            if fpl_p:
                sofa_pos = p_sofa.get('tactical_pos', 'Unknown')
                fpl_pos = fpl_p.get('position')
                
                is_oop = False
                if fpl_pos == 'DEF' and sofa_pos not in ['DEF', 'GK']: is_oop = True
                elif fpl_pos == 'MID' and sofa_pos == 'FWD': is_oop = True
                elif fpl_pos == 'FWD' and sofa_pos in ['MID', 'DEF']: is_oop = True
                
                if is_oop:
                    insights.append(f"🔥 {p_sofa['name']} ({p_sofa['team']}): {fpl_pos} ➡️ {sofa_pos}")
        return "\n".join(insights) if insights else None
    except Exception as e:
        logging.error(f"OOP detection error: {e}")
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

# --- FORM HELPERS ---
def get_home_form(team_id, db, last_n=6):
    """Points from last N home games"""
    try:
        home_games = list(db.fixtures.find({
            'team_h': team_id,
            'finished': True
        }).sort('kickoff_time', -1).limit(last_n))
        
        points = 0
        for f in home_games:
            h_score, a_score = f.get('team_h_score'), f.get('team_a_score')
            if h_score is None or a_score is None: continue
            if h_score > a_score: points += 3
            elif h_score == a_score: points += 1
        return points
    except Exception as e:
        logging.error(f"Home form error: {e}")
        return 0

def get_away_form(team_id, db, last_n=6):
    """Points from last N away games"""
    try:
        away_games = list(db.fixtures.find({
            'team_a': team_id,
            'finished': True
        }).sort('kickoff_time', -1).limit(last_n))
        
        points = 0
        for f in away_games:
            h_score, a_score = f.get('team_h_score'), f.get('team_a_score')
            if h_score is None or a_score is None: continue
            if a_score > h_score: points += 3
            elif a_score == h_score: points += 1
        return points
    except Exception as e:
        logging.error(f"Away form error: {e}")
        return 0

def get_h2h_edge(home_id, away_id, db, last_n=5):
    """H2H edge for home team"""
    try:
        h2h = db.fixtures.find({
            '$or': [
                {'team_h': home_id, 'team_a': away_id},
                {'team_h': away_id, 'team_a': home_id}
            ],
            'finished': True
        }).sort('kickoff_time', -1).limit(last_n)
        
        edge = 0
        for f in h2h:
            h_score, a_score = f.get('team_h_score'), f.get('team_a_score')
            if h_score is None or a_score is None: continue
            
            if (f['team_h'] == home_id and h_score > a_score) or \
               (f['team_a'] == home_id and a_score > h_score):
                edge += 0.5
            elif h_score != a_score:
                edge -= 0.5
        return edge
    except Exception as e:
        logging.error(f"H2H error: {e}")
        return 0

# --- UNDERSTAT STANDINGS ---
def fetch_pl_standings():
    """Fetch PL standings from Understat with xG stats"""
    try:
        understat = UnderstatClient()
        league = understat.league(league="EPL")
        team_data = league.get_team_data(season="2025")

        rows = []
        for team_id, team_info in team_data.items():
            name = team_info.get('title', 'Unknown')
            history = team_info.get('history', [])
            if not history: continue

            M = len(history)
            W = sum(1 for h in history if h.get('wins') == 1)
            D = sum(1 for h in history if h.get('draws') == 1)
            L = sum(1 for h in history if h.get('loses') == 1)
            G = sum(h.get('scored', 0) for h in history)
            GA = sum(h.get('missed', 0) for h in history)
            PTS = sum(h.get('pts', 0) for h in history)

            xG_total = sum(float(h.get('xG', 0)) for h in history)
            xGA_total = sum(float(h.get('xGA', 0)) for h in history)
            xGD = xG_total - xGA_total
            xPTS_total = sum(float(h.get('xpts', 0)) for h in history)
            ppda_avg = sum(float(h.get('ppda', {}).get('def', 20.0)) for h in history) / max(M, 1)

            recent_history = history[-6:] if len(history) >= 6 else history
            recent_xG = sum(float(h.get('xG', 0)) for h in recent_history)
            recent_xGA = sum(float(h.get('xGA', 0)) for h in recent_history)
            recent_xPTS = sum(float(h.get('xpts', 0)) for h in recent_history)

            home_history = [h for h in history if h.get('h_a') == 'h']
            away_history = [h for h in history if h.get('h_a') == 'a']
            home_xG = sum(float(h.get('xG', 0)) for h in home_history)
            away_xG = sum(float(h.get('xG', 0)) for h in away_history)
            home_matches = len(home_history) or 1
            away_matches = len(away_history) or 1

            rows.append({
                "team": {"id": team_id, "name": name},
                "matches": M,
                "wins": W,
                "draws": D,
                "losses": L,
                "goals_for": G,
                "goals_against": GA,
                "goal_diff": G - GA,
                "points": PTS,
                "xG": round(xG_total, 2),
                "xGA": round(xGA_total, 2),
                "xGD": round(xGD, 2),
                "xPTS": round(xPTS_total, 2),
                "ppda_avg": round(ppda_avg, 2),
                "xG_recent": round(recent_xG, 2),
                "xGA_recent": round(recent_xGA, 2),
                "xPTS_recent": round(recent_xPTS, 2),
                "home_xG_pg": round(home_xG / home_matches, 2),
                "away_xG_pg": round(away_xG / away_matches, 2),
                "played": M
            })

        rows.sort(key=lambda r: (-r["points"], -r["goal_diff"], -r["goals_for"]))
        for pos, row in enumerate(rows, start=1):
            row["position"] = pos

        return rows
    except Exception as e:
        logging.error(f"Understat error: {e}")
        raise

# --- BET BUILDER FUNCTIONS ---
def evaluate_team_result(fixture, db):
    """ADDED: Missing function to evaluate match result"""
    try:
        home_name = fixture['team_h_name']
        away_name = fixture['team_a_name']
        
        home_ustat = TEAM_NAME_MAP.get(home_name, home_name)
        away_ustat = TEAM_NAME_MAP.get(away_name, away_name)
        
        home_data = db.standings.find_one({"team_name": home_ustat})
        away_data = db.standings.find_one({"team_name": away_ustat})
        
        if not home_data or not away_data:
            return "Skip (no data)"
        
        home_played = home_data.get('played', 1)
        away_played = away_data.get('played', 1)
        
        home_xg_pg = home_data.get('xG', 1.0) / max(home_played, 1)
        away_xg_pg = away_data.get('xG', 1.0) / max(away_played, 1)
        
        # Add home advantage
        home_xg_expected = home_xg_pg + 0.3
        away_xg_expected = away_xg_pg
        
        diff = home_xg_expected - away_xg_expected
        
        if diff >= 0.5:
            return f"{home_name} to Win"
        elif diff <= -0.5:
            return f"{away_name} to Win"
        return "Draw / Skip"
    except Exception as e:
        logging.error(f"Result eval error: {e}")
        return "Skip"

def evaluate_btts(fixture, db):
    try:
        home_name = fixture['team_h_name']
        away_name = fixture['team_a_name']
        
        home_ustat = TEAM_NAME_MAP.get(home_name, home_name)
        away_ustat = TEAM_NAME_MAP.get(away_name, away_name)
        
        home_data = db.standings.find_one({"team_name": home_ustat})
        away_data = db.standings.find_one({"team_name": away_ustat})
        
        if not home_data or not away_data:
            return "Skip (no xG data)"
        
        home_matches = home_data.get('played', 1)
        away_matches = away_data.get('played', 1)
        
        home_xg_pg = home_data.get('xG', 1.0) / max(home_matches, 1)
        away_xg_pg = away_data.get('xG', 1.0) / max(away_matches, 1)
        
        if home_xg_pg >= 1.3 and away_xg_pg >= 1.3:
            return "Yes"
        elif home_xg_pg < 0.9 or away_xg_pg < 0.9:
            return "No"
        return "Skip"
    except Exception as e:
        logging.error(f"BTTS error: {e}")
        return "Skip"

def select_shot_player(team_name, lineup, db):
    for p in lineup:
        if p['team'] == team_name:
            fpl_p = db.players.find_one({"web_name": {"$regex": f"^{p['name']}$", "$options": "i"}})
            if fpl_p and fpl_p.get('position') in ['FWD', 'MID'] and fpl_p.get('minutes', 0) > 0:
                if p.get('tactical_pos') in ['FWD', 'MID']:
                    return p['name']
    return None

def generate_fixture_bet_builder(fixture, db):
    """FIXED: Removed duplicate, kept enhanced version"""
    try:
        builder = []
        home_name = fixture['team_h_name']
        away_name = fixture['team_a_name']
        
        home_ustat = TEAM_NAME_MAP.get(home_name, home_name)
        away_ustat = TEAM_NAME_MAP.get(away_name, away_name)
        
        home_data = db.standings.find_one({"team_name": home_ustat})
        away_data = db.standings.find_one({"team_name": away_ustat})
        
        result = evaluate_team_result(fixture, db)
        builder.append(f"• Result: {result}")
        
        btts = evaluate_btts(fixture, db)
        builder.append(f"• BTTS: {btts}")
        
        if home_data and away_data:
            builder.append(
                f"• Season xG: {home_name} {home_data['xG']} "
                f"({home_data['xGD']:+.2f}) vs {away_name} {away_data['xG']} "
                f"({away_data['xGD']:+.2f})"
            )
        
        sofa_data = db.tactical_data.find_one({"match_id": fixture.get('sofascore_id')})
        if sofa_data:
            lineup = sofa_data.get('players', [])
            if home_player := select_shot_player(home_name, lineup, db):
                builder.append(f"• {home_player} 1+ SOT")
            if away_player := select_shot_player(away_name, lineup, db):
                builder.append(f"• {away_player} 1+ SOT")
        
        return "\n".join(builder)
    except Exception as e:
        logging.error(f"Bet builder error: {e}")
        return "Could not generate builder."

# --- GAMEWEEK ACCUMULATOR ---
def generate_gw_accumulator(db, top_n=6):
    """Generate top win bets using xG, form, H2H"""
    try:
        upcoming = list(db.fixtures.find({
            'started': False,
            'finished': False,
            'event': {'$ne': None}
        }).sort('kickoff_time', 1))
        
        if upcoming:
            next_event = min(f['event'] for f in upcoming if f['event'] is not None)
            upcoming = [f for f in upcoming if f['event'] == next_event]
        
        accumulator = []
        
        for f in upcoming:
            try:
                home_name = f['team_h_name']
                away_name = f['team_a_name']
                home_id = f['team_h']
                away_id = f['team_a']
                
                home_ustat = TEAM_NAME_MAP.get(home_name, home_name)
                away_ustat = TEAM_NAME_MAP.get(away_name, away_name)
                
                home_stand = db.standings.find_one({"team_name": home_ustat})
                away_stand = db.standings.find_one({"team_name": away_ustat})
                
                if not home_stand or not away_stand:
                    continue
                
                home_played = home_stand.get('played', 1)
                away_played = away_stand.get('played', 1)
                
                home_xg_pg = home_stand.get('xG_recent', home_stand.get('xG', 1.0)) / min(6, home_played)
                away_xg_pg = away_stand.get('xG_recent', away_stand.get('xG', 1.0)) / min(6, away_played)
                home_xga_pg = home_stand.get('xGA_recent', home_stand.get('xGA', 1.5)) / min(6, home_played)
                away_xga_pg = away_stand.get('xGA_recent', away_stand.get('xGA', 1.5)) / min(6, away_played)
                
                home_xg_pg = home_stand.get('home_xG_pg', home_xg_pg)
                away_xg_pg = away_stand.get('away_xG_pg', away_xg_pg)
                
                home_xg_expected = (home_xg_pg + 0.45) * (1 - (away_xga_pg / 2.0))
                away_xg_expected = away_xg_pg * (1 - (home_xga_pg / 2.0))
                xg_diff = home_xg_expected - away_xg_expected
                
                home_xpts_pg = home_stand.get('xPTS_recent', home_stand.get('xPTS', 0)) / min(6, home_played)
                away_xpts_pg = away_stand.get('xPTS_recent', away_stand.get('xPTS', 0)) / min(6, away_played)
                xpts_diff = home_xpts_pg - away_xpts_pg
                
                home_ppda = home_stand.get('ppda_avg', 20.0)
                away_ppda = away_stand.get('ppda_avg', 20.0)
                ppda_bonus = (away_ppda - home_ppda) * 0.05
                
                home_form = get_home_form(home_id, db)
                away_form = get_away_form(away_id, db)
                form_diff = home_form - away_form
                
                home_pos = home_stand.get('position', 10)
                away_pos = away_stand.get('position', 10)
                table_diff = away_pos - home_pos
                
                h2h = get_h2h_edge(home_id, away_id, db)
                
                final_strength = (
                    xg_diff * 1.5 +
                    xpts_diff * 1.0 +
                    form_diff * 0.7 +
                    table_diff * 0.5 +
                    h2h * 0.8 +
                    ppda_bonus
                )
                
                if final_strength >= 0.45:
                    stars = "⭐⭐⭐"
                elif final_strength >= 0.20:
                    stars = "⭐⭐"
                elif final_strength >= 0.05:
                    stars = "⭐"
                else:
                    continue
                
                pick = f"{home_name} to Win" if final_strength > 0 else f"{away_name} to Win"
                
                accumulator.append({
                    'strength': abs(final_strength),
                    'match': f"{home_name} vs {away_name}",
                    'pick': pick,
                    'stars': stars,
                    'details': f"xG diff: {xg_diff:.2f} | xPTS: {xpts_diff:.2f} | Form: {form_diff} | H2H: {h2h:.1f}"
                })
            
            except Exception as e:
                logging.error(f"Accumulator error: {e}")
                continue
        
        accumulator.sort(key=lambda x: x['strength'], reverse=True)
        
        if not accumulator:
            return "No upcoming matches or insufficient data."
        
        msg = "🔥 *Gameweek Accumulator*\n\n"
        for item in accumulator[:top_n]:
            msg += f"{item['stars']} **{item['match']}**: {item['pick']}**\n"
            msg += f"   {item['details']}\n\n"
        
        return msg
    
    except Exception as e:
        logging.error(f"Accumulator error: {e}")
        return "Error generating accumulator."

def show_fixture_menu(db):
    fixtures = get_next_fixtures(db, limit=10)
    return [
        [InlineKeyboardButton(f"{f['team_h_name']} vs {f['team_a_name']}", callback_data=f"select_{f['id']}")]
        for _, f in fixtures
    ] or [[InlineKeyboardButton("No upcoming fixtures", callback_data="none")]]

# --- BACKGROUND MONITOR ---
def run_monitor():
    logging.info("Monitor started")
    while True:
        try:
            time.sleep(60)
            client, db = get_db()
            now = datetime.now(timezone.utc)
            
            for f in db.fixtures.find({'kickoff_time': {'$exists': True}, 'finished': False, 'alert_sent': {'$ne': True}}):
                ko = datetime.fromisoformat(f['kickoff_time'].replace('Z', '+00:00'))
                diff_mins = (ko - now).total_seconds() / 60
                
                if 59 <= diff_mins <= 61:
                    logging.info(f"Checking: {f['team_h_name']} vs {f['team_a_name']}")
                    sofa_events = get_today_sofascore_matches()
                    target_event = next((e for e in sofa_events 
                                       if f['team_h_name'] in e.get('homeTeam', {}).get('name', '') 
                                       or f['team_a_name'] in e.get('awayTeam', {}).get('name', '')), None)
                    
                    msg_parts = [f"📢 *Lineups Out: {f['team_h_name']} vs {f['team_a_name']}*"]
                    
                    if target_event:
                        sofa_lineup = fetch_sofascore_lineup(target_event['id'])
                        if sofa_lineup:
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
                            db.fixtures.update_one({'id': f['id']}, {'$set': {'sofascore_id': target_event['id']}})
                            
                            if oop := detect_tactical_oop(db, target_event['id']):
                                msg_parts.append(f"\n*Tactical Shifts:*\n{oop}")
                    
                    if benched := detect_high_ownership_benched(f['id'], db):
                        msg_parts.append(f"\n*Benched:*\n{benched}")
                    
                    for u in db.users.find():
                        try:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                json={"chat_id": u['chat_id'], "text": "\n".join(msg_parts), "parse_mode": "Markdown"},
                                timeout=5
                            )
                        except Exception as e:
                            logging.error(f"Send failed: {e}")
                    
                    db.fixtures.update_one({'id': f['id']}, {'$set': {'alert_sent': True}})
            
            client.close()
        except Exception as e:
            logging.error(f"Monitor error: {e}")

# --- TELEGRAM COMMANDS ---
async def start(update: Update, context: CallbackContext):
    client, db = get_db()
    user_id = update.effective_chat.id
    db.users.update_one({'chat_id': user_id}, {'$set': {'chat_id': user_id, 'joined': datetime.now()}}, upsert=True)
    
    await update.message.reply_text(
        "👋 *Welcome to PL Lineup Bot!*\n\n"
        "Commands:\n"
        "/update - Sync FPL data\n"
        "/check - Latest analysis\n"
        "/builder - Bet builder\n"
        "/gw_accumulator - Top bets\n"
        "/status - Bot status\n"
        "/update_standings - Update xG data",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(show_fixture_menu(db))
    )
    client.close()

async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("🔄 Syncing data...")
    client, db = get_db()
    try:
        base_url = "https://fantasy.premierleague.com/api/"
        bootstrap = requests.get(f"{base_url}bootstrap-static/", timeout=30).json()
        
        players = pd.DataFrame(bootstrap['elements'])
        players['position'] = players['element_type'].map({1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'})
        
        db.players.delete_many({})
        db.players.insert_many(
            players[['id', 'web_name', 'position', 'minutes', 'team', 
                    'goals_scored', 'assists', 'total_points', 'selected_by_percent']].to_dict('records')
        )
        
        teams_df = pd.DataFrame(bootstrap['teams'])
        team_map = dict(zip(teams_df['id'], teams_df['name']))
        
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
        
        lineup_entries = []
        for f in fixtures_data:
            for s in f.get('stats', []):
                if s.get('identifier') == 'minutes':
                    for side in ('h', 'a'):
                        for p in s.get(side, []):
                            lineup_entries.append({
                                "match_id": f['id'],
                                "player_id": p['element'],
                                "minutes": p['value']
                            })
        
        db.lineups.delete_many({})
        if lineup_entries:
            db.lineups.insert_many(lineup_entries)
        
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
        
        await update.message.reply_text("✅ Sync complete!")
    except Exception as e:
        logging.error(f"Update error: {e}")
        await update.message.reply_text(f"❌ Failed: {str(e)}")
    finally:
        client.close()

async def check(update: Update, context: CallbackContext):
    client, db = get_db()
    if tactical := db.tactical_data.find_one(sort=[("last_updated", -1)]):
        msg = f"📊 *{tactical['home_team']} vs {tactical['away_team']}*\n\n"
        msg += detect_tactical_oop(db, tactical['match_id']) or "✅ No shifts"
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("No data yet. Run /update first.")
    client.close()

async def builder(update: Update, context: CallbackContext):
    client, db = get_db()
    await update.message.reply_text("📊 Select fixture:", reply_markup=InlineKeyboardMarkup(show_fixture_menu(db)))
    client.close()

async def gw_accumulator(update: Update, context: CallbackContext):
    client, db = get_db()
    msg = generate_gw_accumulator(db)
    await update.message.reply_text(msg, parse_mode="Markdown")
    client.close()

async def status(update: Update, context: CallbackContext):
    client, db = get_db()
    latest = db.tactical_data.find_one(sort=[("last_updated", -1)])
    last_update = latest['last_updated'].strftime("%Y-%m-%d %H:%M UTC") if latest else "Never"
    
    await update.message.reply_text(
        f"🤖 *Bot Status*\n\n"
        f"Players: {db.players.count_documents({})}\n"
        f"Upcoming: {db.fixtures.count_documents({'started': False, 'finished': False})}\n"
        f"Users: {db.users.count_documents({})}\n"
        f"Last update: {last_update}",
        parse_mode="Markdown"
    )
    client.close()

async def update_standings_command(update: Update, context: CallbackContext):
    client, db = get_db()
    try:
        await update.message.reply_text("🔄 Fetching xG data from Understat...")
        rows = fetch_pl_standings()
        save_standings_to_mongo(db, rows)
        await update.message.reply_text("✅ Standings updated with xG data!")
    except Exception as e:
        logging.error(f"Standings update error: {e}")
        await update.message.reply_text(f"❌ Failed: {str(e)}")
    finally:
        client.close()

async def handle_callbacks(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    client, db = get_db()
    
    if query.data.startswith("select_"):
        fixture_id = int(query.data.split("_")[1])
        if fixture := db.fixtures.find_one({"id": fixture_id}):
            msg = f"📊 *{fixture['team_h_name']} vs {fixture['team_a_name']}*\n\n"
            msg += generate_fixture_bet_builder(fixture, db)
            await query.edit_message_text(msg, parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Fixture not found")
    
    client.close()

# --- FLASK APP ---
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot Running!"

# --- MAIN ---
if __name__ == "__main__":
    if not all([MONGODB_URI, TELEGRAM_TOKEN]):
        raise ValueError("MONGODB_URI and BOT_TOKEN required")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("update", update_data))
    application.add_handler(CommandHandler("check", check))
    application.add_handler(CommandHandler("builder", builder))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("gw_accumulator", gw_accumulator))
    application.add_handler(CommandHandler("update_standings", update_standings_command))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    
    threading.Thread(target=run_monitor, daemon=True).start()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000))), daemon=True).start()
    
    logging.info("Starting PL Lineup Bot...")
    application.run_polling()
