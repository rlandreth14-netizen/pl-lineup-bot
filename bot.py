import os
import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
from collections import defaultdict
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "YOUR_API_FOOTBALL_KEY")
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
HEALTH_CHECK_PORT = int(os.getenv("PORT", "8000"))
CURRENT_SEASON = 2025

# League IDs
LEAGUES = {
    'pl': {'id': 39, 'name': 'Premier League', 'emoji': '⚽'},
    'ucl': {'id': 2, 'name': 'Champions League', 'emoji': '⭐'},
    'laliga': {'id': 140, 'name': 'La Liga', 'emoji': '🇪🇸'},
    'seriea': {'id': 135, 'name': 'Serie A', 'emoji': '🇮🇹'},
    'bundesliga': {'id': 78, 'name': 'Bundesliga', 'emoji': '🇩🇪'},
    'ligue1': {'id': 61, 'name': 'Ligue 1', 'emoji': '🇫🇷'}
}

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Data storage
player_history: Dict[int, 'PlayerStats'] = {}
user_preferences: Dict[int, Set[str]] = {}

POSITION_MAP = {
    'G': 'Goalkeeper',
    'D': 'Defender',
    'M': 'Midfielder',
    'F': 'Forward'
}

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running!')
    
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(('0.0.0.0', HEALTH_CHECK_PORT), HealthCheckHandler)
    logger.info(f"Health check server running on port {HEALTH_CHECK_PORT}")
    server.serve_forever()

class PlayerStats:
    def __init__(self, player_id: int, name: str):
        self.player_id = player_id
        self.name = name
        self.positions = []
        self.fouls_per_90 = 0.0
        self.cards_per_90 = 0.0
        self.shots_per_90 = 0.0
        
    def add_position(self, position: str):
        self.positions.append(position)
        if len(self.positions) > 10:
            self.positions.pop(0)
    
    def get_usual_position(self) -> str:
        if not self.positions:
            return "Unknown"
        return max(set(self.positions), key=self.positions.count)
    
    def is_out_of_position(self, current_pos: str) -> bool:
        if len(self.positions) < 3:
            return False
        usual = self.get_usual_position()
        return usual != current_pos and usual != "Unknown"
    
    def get_position_change_impact(self, new_position: str) -> Dict:
        usual = self.get_usual_position()
        impact = {'fouls': 'neutral', 'cards': 'neutral', 'shots': 'neutral', 'confidence': 'low'}
        
        if len(self.positions) < 3:
            return impact
        
        if usual == 'D' and new_position in ['M', 'F']:
            impact.update({'fouls': 'decrease', 'cards': 'decrease', 'shots': 'increase', 'confidence': 'high'})
        elif usual == 'M' and new_position == 'D':
            impact.update({'fouls': 'increase', 'cards': 'increase', 'shots': 'decrease', 'confidence': 'high'})
        elif usual == 'M' and new_position == 'F':
            impact.update({'shots': 'increase', 'fouls': 'slight_decrease', 'confidence': 'medium'})
        elif usual == 'F' and new_position == 'M':
            impact.update({'shots': 'decrease', 'fouls': 'slight_increase', 'confidence': 'medium'})
        elif usual == 'F' and new_position == 'D':
            impact.update({'fouls': 'increase', 'cards': 'increase', 'shots': 'significant_decrease', 'confidence': 'high'})
        
        return impact

async def get_api_football_data(endpoint: str, params: Dict = None, retries: int = 3):
    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    url = f"{API_FOOTBALL_BASE}/{endpoint}"
    
    async with aiohttp.ClientSession() as session:
        for attempt in range(retries):
            try:
                async with session.get(url, headers=headers, params=params, timeout=15) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        await asyncio.sleep(60)
                    else:
                        logger.error(f"API error {response.status} on {endpoint}")
            except Exception as e:
                logger.error(f"Request attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(2)
    return None

async def test_api_connection():
    result = await get_api_football_data('fixtures', {'league': 39, 'season': CURRENT_SEASON, 'last': 1})
    return result is not None

async def build_player_history(team_id: int, season: int = CURRENT_SEASON):
    data = await get_api_football_data('fixtures', {'team': team_id, 'season': season, 'last': 5})
    if not data or not data.get('response'):
        return
    
    for fixture in data['response']:
        f_id = fixture['fixture']['id']
        lineup_data = await get_api_football_data('fixtures/lineups', {'fixture': f_id})
        if lineup_data and lineup_data.get('response'):
            for team_lineup in lineup_data['response']:
                if team_lineup['team']['id'] == team_id:
                    players = team_lineup.get('startXI', []) + team_lineup.get('substitutes', [])
                    for p in players:
                        p_info = p.get('player', {})
                        p_id, p_name, pos = p_info.get('id'), p_info.get('name'), p_info.get('pos')
                        if p_id and pos:
                            if p_id not in player_history:
                                player_history[p_id] = PlayerStats(p_id, p_name)
                            player_history[p_id].add_position(pos)
        await asyncio.sleep(0.5)

async def get_player_statistics(player_id: int, season: int = CURRENT_SEASON):
    data = await get_api_football_data('players', {'id': player_id, 'season': season})
    if not data or not data.get('response'): return None
    
    stats_list = data['response'][0].get('statistics', [])
    if not stats_list: return None
    
    m, f, c, s = 0, 0, 0, 0
    for stat in stats_list:
        m += (stat.get('games', {}).get('minutes') or 0)
        f += (stat.get('fouls', {}).get('committed') or 0)
        c += ((stat.get('cards', {}).get('yellow') or 0) + (stat.get('cards', {}).get('red') or 0) * 2)
        s += (stat.get('shots', {}).get('total') or 0)
    
    if m < 90: return None
    per_90 = m / 90
    return {'fouls_per_90': round(f/per_90, 2), 'cards_per_90': round(c/per_90, 2), 'shots_per_90': round(s/per_90, 2)}

async def analyze_lineup_detailed(fixture_id: int, match_info: Dict) -> Optional[Dict]:
    data = await get_api_football_data('fixtures/lineups', {'fixture': fixture_id})
    if not data or not data.get('response'): return None
    
    analysis = {
        'league': match_info['league']['name'],
        'home_team': match_info['teams']['home']['name'],
        'away_team': match_info['teams']['away']['name'],
        'status': match_info['fixture']['status']['long'],
        'kickoff': match_info['fixture']['date'],
        'opportunities': []
    }
    
    for team_lineup in data['response']:
        for p_data in team_lineup.get('startXI', []):
            p = p_data.get('player', {})
            p_id, p_name, curr_pos = p.get('id'), p.get('name'), p.get('pos')
            if not p_id or curr_pos == 'Unknown': continue
            
            if p_id not in player_history:
                player_history[p_id] = PlayerStats(p_id, p_name)
                stats = await get_player_statistics(p_id)
                if stats:
                    player_history[p_id].fouls_per_90 = stats['fouls_per_90']
                    player_history[p_id].cards_per_90 = stats['cards_per_90']
                    player_history[p_id].shots_per_90 = stats['shots_per_90']
            
            p_stats = player_history[p_id]
            if p_stats.is_out_of_position(curr_pos):
                impact = p_stats.get_position_change_impact(curr_pos)
                if impact['confidence'] in ['high', 'medium']:
                    analysis['opportunities'].append({
                        'team': team_lineup['team']['name'],
                        'player': p_name,
                        'usual_position': POSITION_MAP.get(p_stats.get_usual_position(), "Unknown"),
                        'current_position': POSITION_MAP.get(curr_pos, curr_pos),
                        'impact': impact,
                        'stats': {'fouls': p_stats.fouls_per_90, 'cards': p_stats.cards_per_90, 'shots': p_stats.shots_per_90},
                        'betting_recommendations': [f"✅ BACK: {p_name} 2+ fouls" if impact['fouls'] == 'increase' else f"✅ BACK: {p_name} 1+ shot"]
                    })
    return analysis if analysis['opportunities'] else None

# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚽ **Football Analyzer 2025**\n/live - Live\n/next - Upcoming\n/check [id] - Analyze")

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    success = await test_api_connection()
    await update.message.reply_text("✅ API Connected!" if success else "❌ API Failed")

async def live_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches = []
    for code, info in LEAGUES.items():
        data = await get_api_football_data('fixtures', {'league': info['id'], 'season': CURRENT_SEASON, 'live': 'all'})
        if data and data.get('response'): matches.extend(data['response'])
    
    if not matches:
        await update.message.reply_text("No live matches.")
        return
    
    msg = "🔴 **LIVE:**\n"
    for m in matches[:10]:
        msg += f"• {m['teams']['home']['name']} vs {m['teams']['away']['name']} | ID: `{m['fixture']['id']}`\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def next_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await get_api_football_data('fixtures', {'league': 39, 'season': CURRENT_SEASON, 'next': 10})
    if not data or not data.get('response'):
        await update.message.reply_text("No matches found.")
        return
    msg = "📅 **Upcoming (PL):**\n"
    for m in data['response']:
        msg += f"• {m['teams']['home']['name']} vs {m['teams']['away']['name']} | ID: `{m['fixture']['id']}`\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def check_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check [ID]")
        return
    f_id = context.args[0]
    await update.message.reply_text(f"Analyzing {f_id}...")
    
    data = await get_api_football_data('fixtures', {'id': f_id})
    if data and data.get('response'):
        analysis = await analyze_lineup_detailed(int(f_id), data['response'][0])
        if analysis:
            await update.message.reply_text(f"🚨 **Opportunity!**\nMatch: {analysis['home_team']} vs {analysis['away_team']}")
        else:
            await update.message.reply_text("No unusual positions found.")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime('%Y-%m-%d')
    await update.message.reply_text(f"Debug: Checking PL fixtures for {today}...")
    data = await get_api_football_data('fixtures', {'league': 39, 'season': CURRENT_SEASON, 'date': today})
    count = len(data.get('response', [])) if data else 0
    await update.message.reply_text(f"Found {count} matches today.")

def main():
    # Start Health Server
    Thread(target=run_health_server, daemon=True).start()
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CommandHandler("live", live_matches))
    application.add_handler(CommandHandler("next", next_matches))
    application.add_handler(CommandHandler("check", check_match))
    application.add_handler(CommandHandler("debug", debug_command))
    
    logger.info("Bot Starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
