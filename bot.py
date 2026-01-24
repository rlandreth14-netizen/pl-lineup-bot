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

# === ADD THE HELPERS HERE ===
def get_home_form(team_id, db, last_n=6):
    """Points from last N home games for the team."""
    try:
        home_games = list(db.fixtures.find({
            'team_h': team_id,
            'finished': True
        }).sort('kickoff_time', -1).limit(last_n))
        
        points = 0
        for f in home_games:
            h_score = f.get('team_h_score')
            a_score = f.get('team_a_score')
            if h_score is None or a_score is None:
                continue
            if h_score > a_score:
                points += 3
            elif h_score == a_score:
                points += 1
        return points
    except Exception as e:
        logging.error(f"Home form error for team {team_id}: {e}")
        return 0

def get_away_form(team_id, db, last_n=6):
    """Points from last N away games for the team."""
    try:
        away_games = list(db.fixtures.find({
            'team_a': team_id,
            'finished': True
        }).sort('kickoff_time', -1).limit(last_n))
        
        points = 0
        for f in away_games:
            h_score = f.get('team_h_score')
            a_score = f.get('team_a_score')
            if h_score is None or a_score is None:
                continue
            if a_score > h_score:
                points += 3
            elif a_score == h_score:
                points += 1
        return points
    except Exception as e:
        logging.error(f"Away form error for team {team_id}: {e}")
        return 0

from understatapi import UnderstatClient

def fetch_pl_standings():
    """
    Fetch Premier League standings from Understat with extra stats:
    - Full season xG/xGA/xGD/xPTS
    - Recent (last 6 matches) xG/xGA/xPTS
    """
    try:
        understat = UnderstatClient()
        league = understat.league(league="EPL")
        team_data = league.get_team_data(season="2025")  # 2025/26

        rows = []
        for team_id, team_info in team_data.items():
            name = team_info.get('title', 'Unknown')
            history = team_info.get('history', [])

            if not history:
                continue

            M = len(history)
            W = sum(1 for h in history if h.get('wins') == 1)
            D = sum(1 for h in history if h.get('draws') == 1)
            L = sum(1 for h in history if h.get('loses') == 1)
            G = sum(h.get('scored', 0) for h in history)
            GA = sum(h.get('missed', 0) for h in history)
            PTS = sum(h.get('pts', 0) for h in history)

            # Full season totals
            xG_total = sum(float(h.get('xG', 0)) for h in history)
            xGA_total = sum(float(h.get('xGA', 0)) for h in history)
            xGD = xG_total - xGA_total
            xPTS_total = sum(float(h.get('xpts', 0)) for h in history)

            # Recent (last 6 matches) - more predictive
            recent_history = history[-6:] if len(history) >= 6 else history
            recent_M = len(recent_history)
            recent_xG = sum(float(h.get('xG', 0)) for h in recent_history)
            recent_xGA = sum(float(h.get('xGA', 0)) for h in recent_history)
            recent_xPTS = sum(float(h.get('xpts', 0)) for h in recent_history)

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
                # Recent stats
                "xG_recent": round(recent_xG, 2),
                "xGA_recent": round(recent_xGA, 2),
                "xPTS_recent": round(recent_xPTS, 2),
                "played": M  # for per-game calcs
            })

        # Sort by points desc, GD desc, GF desc
        rows.sort(key=lambda r: (-r["points"], -r["goal_diff"], -r["goals_for"]))

        for pos, row in enumerate(rows, start=1):
            row["position"] = pos

        return rows
    except Exception as e:
        logging.error(f"Understat Fetch Error: {e}")
        raise

# --- FIXTURE BET BUILDER FUNCTIONS ---
def generate_fixture_bet_builder(fixture, db):
    try:
        builder = []

        # Fetch team data once here (used for both prediction and display)
        home_team_name = fixture['team_h_name']
        away_team_name = fixture['team_a_name']

        home_data = db.standings.find_one({"team_name": home_team_name})
        away_data = db.standings.find_one({"team_name": away_team_name})

        # Use real xG for predictions (as in previous suggestion)
        result = evaluate_team_result(fixture, db)   # assumes you already updated this function
        builder.append(f"• Result: {result}")

        btts = evaluate_btts(fixture, db)
        builder.append(f"• BTTS: {btts}")

        # Add the xG context line – only if we have the data
        if home_data and away_data:
            builder.append(
                f"• Season xG: {home_team_name} {home_data['xG']} "
                f"({home_data['xGD']:+.2f}) vs {away_team_name} {away_data['xG']} "
                f"({away_data['xGD']:+.2f})"
            )

        sofa_data = db.tactical_data.find_one({"match_id": fixture.get('sofascore_id')})
        if not sofa_data:
            return "\n".join(builder)

        home_player = select_shot_player(fixture['team_h_name'], sofa_data.get('players', []), db)
        away_player = select_shot_player(fixture['team_a_name'], sofa_data.get('players', []), db)

        if home_player:
            builder.append(f"• {home_player} 1+ SOT")
        if away_player:
            builder.append(f"• {away_player} 1+ SOT")

        return "\n".join(builder)
    except Exception as e:
        logging.error(f"Bet Builder Error: {e}")
        return "Could not generate builder."

def evaluate_btts(fixture, db):
    try:
        home_team_name = fixture['team_h_name']
        away_team_name = fixture['team_a_name']

        home_data = db.standings.find_one({"team_name": home_team_name})
        away_data = db.standings.find_one({"team_name": away_team_name})

        if not home_data or not away_data:
            return "Skip (no xG data)"

        home_matches = home_data.get('played', 1)
        away_matches = away_data.get('played', 1)

        home_xg_per_game = home_data.get('xG', 1.0) / max(home_matches, 1)
        away_xg_per_game = away_data.get('xG', 1.0) / max(away_matches, 1)

        # BTTS likely if both teams create decent chances
        if home_xg_per_game >= 1.3 and away_xg_per_game >= 1.3:
            return "Yes"
        elif home_xg_per_game < 0.9 or away_xg_per_game < 0.9:
            return "No"
        else:
            return "Skip"
    except Exception as e:
        logging.error(f"BTTS eval error: {e}")
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

        result = evaluate_team_result(fixture, db)  # Now uses DB xG
        builder.append(f"• Result: {result}")

        btts = evaluate_btts(fixture, db)  # Now uses DB xG
        builder.append(f"• BTTS: {btts}")

        sofa_data = db.tactical_data.find_one({"match_id": fixture.get('sofascore_id')})
        if not sofa_data:
            return "\n".join(builder)

        home_player = select_shot_player(fixture['team_h_name'], sofa_data.get('players', []), db)
        away_player = select_shot_player(fixture['team_a_name'], sofa_data.get('players', []), db)

        if home_player:
            builder.append(f"• {home_player} 1+ SOT")
        if away_player:
            builder.append(f"• {away_player} 1+ SOT")

        return "\n".join(builder)
    except Exception as e:
        logging.error(f"Bet Builder Error: {e}")
        return "Could not generate builder."

# --- GAMEWEEK ACCUMULATOR ---
def generate_gw_accumulator(db, top_n=6):  # Show up to 6, enforce min 5
    """Generate strongest win bets using real xG, xGA, form (home/away), H2H, table position."""
    try:
        # Get upcoming fixtures in current/next gameweek
        upcoming = list(db.fixtures.find({
            'started': False,
            'finished': False,
            'event': {'$ne': None}
        }).sort('kickoff_time', 1))

        accumulator = []

        for f in upcoming:
            try:
                home_name = f['team_h_name']
                away_name = f['team_a_name']
                home_id = f['team_h']
                away_id = f['team_a']

                home_stand = db.standings.find_one({"team_name": home_name})
                away_stand = db.standings.find_one({"team_name": away_name})

                if not home_stand or not away_stand:
                    continue

                home_played = home_stand.get('played', 1)
                away_played = away_stand.get('played', 1)

                # Prefer recent xG/xGA (fallback to season)
                home_xg_pg = home_stand.get('xG_recent', home_stand.get('xG', 1.0)) / min(6, home_played)
                away_xg_pg = away_stand.get('xG_recent', away_stand.get('xG', 1.0)) / min(6, away_played)
                home_xga_pg = home_stand.get('xGA_recent', home_stand.get('xGA', 1.5)) / min(6, home_played)
                away_xga_pg = away_stand.get('xGA_recent', away_stand.get('xGA', 1.5)) / min(6, away_played)

                # Prefer home/away split if present
                home_xg_pg = home_stand.get('home_xG_pg', home_xg_pg)
                away_xg_pg = away_stand.get('away_xG_pg', away_xg_pg)

                # Home boost + defensive penalty
                home_xg_expected = home_xg_pg + 0.45
                away_xg_expected = away_xg_pg

                home_xg_expected *= (1 - (away_xga_pg / 2.0))
                away_xg_expected *= (1 - (home_xga_pg / 2.0))

                xg_diff = home_xg_expected - away_xg_expected

                # Recent xPTS per game diff
                home_xpts_pg = home_stand.get('xPTS_recent', home_stand.get('xPTS', 0)) / min(6, home_played)
                away_xpts_pg = away_stand.get('xPTS_recent', away_stand.get('xPTS', 0)) / min(6, away_played)
                xpts_diff = home_xpts_pg - away_xpts_pg

                # PPDA bonus
                home_ppda = home_stand.get('ppda_avg', 20.0)
                away_ppda = away_stand.get('ppda_avg', 20.0)
                ppda_bonus = (away_ppda - home_ppda) * 0.05

                # Home/away specific form
                home_form = get_home_form(home_id, db, last_n=6)
                away_form = get_away_form(away_id, db, last_n=6)
                form_diff = home_form - away_form

                home_pos = home_stand.get('position', 10)
                away_pos = away_stand.get('position', 10)
                table_diff = away_pos - home_pos

                h2h = get_h2h_edge(home_id, away_id, db, last_n=5)

                final_strength = (
                    xg_diff * 1.5 +
                    xpts_diff * 1.0 +     # unlucky teams due results
                    form_diff * 0.7 +     # home/away form boosted
                    table_diff * 0.5 +
                    h2h * 0.8 +
                    ppda_bonus
                )

                # Debug logging — very useful to see why picks are weak
                logging.info(f"{home_name} vs {away_name} | strength: {final_strength:.2f} | xG_diff: {xg_diff:.2f} | xPTS_diff: {xpts_diff:.2f} | form_diff: {form_diff} | table_diff: {table_diff} | H2H: {h2h:.1f} | PPDA_bonus: {ppda_bonus:.2f}")

                # Confidence & stars - pure win picks only
                if final_strength >= 0.45:
                    confidence = "High"
                    stars = "⭐⭐⭐"
                elif final_strength >= 0.20:
                    confidence = "Medium"
                    stars = "⭐⭐"
                elif final_strength >= 0.05:
                    confidence = "Low"
                    stars = "⭐"
                else:
                    continue

                pick = f"{home_name} to Win" if final_strength > 0 else f"{away_name} to Win"

                accumulator.append({
                    'strength': abs(final_strength),
                    'match': f"{home_name} vs {away_name}",
                    'pick': pick,
                    'stars': stars,
                    'details': f"xG diff: {xg_diff:.2f} | xPTS diff: {xpts_diff:.2f} | Form diff: {form_diff} | Table diff: {table_diff} | H2H: {h2h:.1f}"
                })

            except Exception as e:
                logging.error(f"Accumulator error for {home_name} vs {away_name}: {e}")
                continue

        # Enforce minimum 5 picks
        if len(accumulator) < 5 and upcoming:
            logging.info(f"Only {len(accumulator)} strong bets — forcing top {min(6, len(upcoming))} fallback win picks")
            remaining_needed = 5 - len(accumulator)
            fallback_matches = upcoming[len(accumulator): len(accumulator) + remaining_needed + 2]
            for f in fallback_matches:
                home_name = f['team_h_name']
                accumulator.append({
                    'strength': 0.05,
                    'match': f"{home_name} vs {f['team_a_name']}",
                    'pick': f"{home_name} to Win",
                    'stars': "⭐",
                    'details': "Fallback win pick (weak/no edge)"
                })

        accumulator.sort(key=lambda x: x['strength'], reverse=True)

        if not accumulator:
            return "No upcoming matches or insufficient data this gameweek."

        msg = "🔥 *Gameweek Accumulator – Strongest Win Bets*\n\n"
        for item in accumulator[:top_n]:
            msg += f"{item['stars']} **{item['match']}**: {item['pick']}**\n"
            msg += f"   {item['details']}\n\n"

        return msg

    except Exception as e:
        logging.error(f"Generate accumulator error: {e}")
        return "Error generating accumulator — check logs."
        
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
