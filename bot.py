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
player_history: Dict[int, Dict] = {}
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
        impact = {
            'fouls': 'neutral',
            'cards': 'neutral',
            'shots': 'neutral',
            'confidence': 'low'
        }
        
        if len(self.positions) < 3:
            return impact
        
        if usual == 'D' and new_position in ['M', 'F']:
            impact['fouls'] = 'decrease'
            impact['cards'] = 'decrease'
            impact['shots'] = 'increase'
            impact['confidence'] = 'high'
        elif usual == 'M' and new_position == 'D':
            impact['fouls'] = 'increase'
            impact['cards'] = 'increase'
            impact['shots'] = 'decrease'
            impact['confidence'] = 'high'
        elif usual == 'M' and new_position == 'F':
            impact['shots'] = 'increase'
            impact['fouls'] = 'slight_decrease'
            impact['confidence'] = 'medium'
        elif usual == 'F' and new_position == 'M':
            impact['shots'] = 'decrease'
            impact['fouls'] = 'slight_increase'
            impact['confidence'] = 'medium'
        elif usual == 'F' and new_position == 'D':
            impact['fouls'] = 'increase'
            impact['cards'] = 'increase'
            impact['shots'] = 'significant_decrease'
            impact['confidence'] = 'high'
        
        return impact

async def get_api_football_data(endpoint: str, params: Dict = None, retries: int = 3):
    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    url = f"{API_FOOTBALL_BASE}/{endpoint}"
    
    logger.info(f"Making API request to: {endpoint} with params: {params}")
    
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params, timeout=10) as response:
                    logger.info(f"API Response Status: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        logger.info(f"API Response received: {len(data.get('response', []))} items")
                        return data
                    elif response.status == 429:
                        logger.warning("Rate limited, waiting...")
                        await asyncio.sleep(60)
                    else:
                        logger.error(f"API error: {response.status}")
                        response_text = await response.text()
                        logger.error(f"Response: {response_text}")
        except Exception as e:
            logger.error(f"Request failed (attempt {attempt + 1}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(5)
    return None

async def test_api_connection():
    logger.info("Testing API connection...")
    params = {'league': 39, 'season': 2025}
    result = await get_api_football_data('fixtures', params)
    if result:
        logger.info("API connection successful!")
        return True
    else:
        logger.error("API connection failed!")
        return False

async def build_player_history(team_id: int, season: int = CURRENT_SEASON):
    logger.info(f"Building player history for team {team_id}")
    
    params = {
        'team': team_id,
        'season': season,
        'last': 10
    }
    
    data = await get_api_football_data('fixtures', params)
    if not data or not data.get('response'):
        return
    
    fixtures = data['response']
    
    for fixture in fixtures[:5]:
        fixture_id = fixture['fixture']['id']
        lineup_data = await get_api_football_data('fixtures/lineups', {'fixture': fixture_id})
        
        if not lineup_data or not lineup_data.get('response'):
            continue
        
        for team_lineup in lineup_data['response']:
            if team_lineup['team']['id'] != team_id:
                continue
            
            all_players = team_lineup.get('startXI', []) + team_lineup.get('substitutes', [])
            
            for player_data in all_players:
                player = player_data.get('player', {})
                player_id = player.get('id')
                player_name = player.get('name')
                position = player.get('pos', 'Unknown')
                
                if player_id and position != 'Unknown':
                    if player_id not in player_history:
                        player_history[player_id] = PlayerStats(player_id, player_name)
                    player_history[player_id].add_position(position)
        
        await asyncio.sleep(0.5)

async def get_player_statistics(player_id: int, season: int = CURRENT_SEASON):
    params = {'id': player_id, 'season': season}
    data = await get_api_football_data('players', params)
    
    if not data or not data.get('response'):
        return None
    
    player_data = data['response'][0]
    statistics = player_data.get('statistics', [])
    
    if not statistics:
        return None
    
    total_minutes = 0
    total_fouls = 0
    total_cards = 0
    total_shots = 0
    
    for stat in statistics:
        games = stat.get('games', {})
        minutes = games.get('minutes', 0) or 0
        fouls_drawn = stat.get('fouls', {}).get('committed', 0) or 0
        yellow = stat.get('cards', {}).get('yellow', 0) or 0
        red = stat.get('cards', {}).get('red', 0) or 0
        shots_total = stat.get('shots', {}).get('total', 0) or 0
        
        total_minutes += minutes
        total_fouls += fouls_drawn
        total_cards += (yellow + red * 2)
        total_shots += shots_total
    
    if total_minutes == 0:
        return None
    
    matches_90 = total_minutes / 90
    
    return {
        'fouls_per_90': round(total_fouls / matches_90, 2) if matches_90 > 0 else 0,
        'cards_per_90': round(total_cards / matches_90, 2) if matches_90 > 0 else 0,
        'shots_per_90': round(total_shots / matches_90, 2) if matches_90 > 0 else 0
    }

async def get_live_matches(league_codes: List[str] = ['pl']):
    all_matches = []
    
    for code in league_codes:
        if code not in LEAGUES:
            continue
            
        league_id = LEAGUES[code]['id']
        
        params = {
            'league': league_id,
            'season': CURRENT_SEASON,
            'live': 'all'
        }
        
        logger.info(f"Fetching live matches for {code}")
        data = await get_api_football_data('fixtures', params)
        if data and data.get('response'):
            for match in data['response']:
                match['league_code'] = code
                all_matches.append(match)
        
        await asyncio.sleep(0.3)
    
    return all_matches

async def get_upcoming_matches(league_codes: List[str] = ['pl'], hours_ahead: int = 168):
    today = datetime.now()
    future = today + timedelta(hours=hours_ahead)
    
    all_matches = []
    
    for code in league_codes:
        if code not in LEAGUES:
            continue
            
        league_id = LEAGUES[code]['id']
        
        params = {
            'league': league_id,
            'season': CURRENT_SEASON,
            'from': today.strftime('%Y-%m-%d'),
            'to': future.strftime('%Y-%m-%d')
        }
        
        logger.info(f"Fetching upcoming matches for {code}")
        data = await get_api_football_data('fixtures', params)
        if data and data.get('response'):
            for match in data['response']:
                match['league_code'] = code
                all_matches.append(match)
        
        await asyncio.sleep(0.3)
    
    all_matches.sort(key=lambda x: x['fixture']['date'])
    return all_matches

async def analyze_lineup_detailed(fixture_id: int, match_info: Dict) -> Optional[Dict]:
    params = {'fixture': fixture_id}
    data = await get_api_football_data('fixtures/lineups', params)
    
    if not data or not data.get('response'):
        return None
    
    lineups = data['response']
    home_team = match_info['teams']['home']
    away_team = match_info['teams']['away']
    
    await build_player_history(home_team['id'])
    await build_player_history(away_team['id'])
    
    analysis = {
        'fixture_id': fixture_id,
        'home_team': home_team['name'],
        'away_team': away_team['name'],
        'kickoff': match_info['fixture']['date'],
        'league': match_info['league']['name'],
        'status': match_info['fixture']['status']['long'],
        'opportunities': []
    }
    
    for team_lineup in lineups:
        team_name = team_lineup.get('team', {}).get('name', 'Unknown')
        starters = team_lineup.get('startXI', [])
        
        for player_data in starters:
            player = player_data.get('player', {})
            player_id = player.get('id')
            player_name = player.get('name', 'Unknown')
            current_position = player.get('pos', 'Unknown')
            
            if current_position == 'Unknown' or not player_id:
                continue
            
            if player_id not in player_history:
                player_history[player_id] = PlayerStats(player_id, player_name)
                stats = await get_player_statistics(player_id)
                if stats:
                    player_history[player_id].fouls_per_90 = stats['fouls_per_90']
                    player_history[player_id].cards_per_90 = stats['cards_per_90']
                    player_history[player_id].shots_per_90 = stats['shots_per_90']
            
            player_stats = player_history[player_id]
            
            if player_stats.is_out_of_position(current_position):
                usual_pos = player_stats.get_usual_position()
                impact = player_stats.get_position_change_impact(current_position)
                
                opportunity = {
                    'team': team_name,
                    'player': player_name,
                    'usual_position': POSITION_MAP.get(usual_pos, usual_pos),
                    'current_position': POSITION_MAP.get(current_position, current_position),
                    'impact': impact,
                    'stats': {
                        'fouls_per_90': player_stats.fouls_per_90,
                        'cards_per_90': player_stats.cards_per_90,
                        'shots_per_90': player_stats.shots_per_90
                    },
                    'betting_recommendations': []
                }
                
                if impact['confidence'] in ['high', 'medium']:
                    if impact['fouls'] == 'increase':
                        opportunity['betting_recommendations'].append(
                            f"✅ BACK: Player to commit 2+ fouls (avg: {player_stats.fouls_per_90}/90)"
                        )
                    if impact['cards'] == 'increase' and player_stats.cards_per_90 > 0.3:
                        opportunity['betting_recommendations'].append(
                            f"✅ BACK: Player to be booked (avg: {player_stats.cards_per_90} cards/90)"
                        )
                    if impact['shots'] == 'increase':
                        opportunity['betting_recommendations'].append(
                            f"✅ BACK: Player 1+ shot on target"
                        )
                    if impact['shots'] in ['decrease', 'significant_decrease']:
                        opportunity['betting_recommendations'].append(
                            f"❌ AVOID: Player shots/goals markets"
                        )
                    if impact['fouls'] == 'decrease':
                        opportunity['betting_recommendations'].append(
                            f"❌ AVOID: Player fouls markets"
                        )
                
                if opportunity['betting_recommendations']:
                    analysis['opportunities'].append(opportunity)
    
    return analysis if analysis['opportunities'] else None

def format_detailed_analysis(analysis: Dict) -> str:
    status_emoji = "🔴" if "In Play" in analysis.get('status', '') else "⚽"
    
    msg = f"🚨 **BETTING OPPORTUNITY DETECTED** 🚨\n\n"
    msg += f"🏆 **{analysis['league']}**\n"
    msg += f"{status_emoji} **{analysis['home_team']} vs {analysis['away_team']}**\n"
    
    if "In Play" in analysis.get('status', ''):
        msg += f"🔴 **LIVE MATCH**\n"
    else:
        msg += f"🕐 {analysis['kickoff']}\n"
    
    msg += "\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for i, opp in enumerate(analysis['opportunities'], 1):
        confidence_emoji = {'high': '🔥', 'medium': '⚡', 'low': '💡'}
        confidence = opp['impact']['confidence']
        
        msg += f"{confidence_emoji.get(confidence, '💡')} **OPPORTUNITY #{i}** - {confidence.upper()} CONFIDENCE\n\n"
        msg += f"👤 **Player:** {opp['player']} ({opp['team']})\n"
        msg += f"📍 **Position Change:**\n"
        msg += f"   • Usual: {opp['usual_position']}\n"
        msg += f"   • Today: {opp['current_position']}\n\n"
        msg += f"📊 **Season Stats (per 90min):**\n"
        msg += f"   • Fouls: {opp['stats']['fouls_per_90']}\n"
        msg += f"   • Cards: {opp['stats']['cards_per_90']}\n"
        msg += f"   • Shots: {opp['stats']['shots_per_90']}\n\n"
        msg += f"💰 **Betting Recommendations:**\n"
        for rec in opp['betting_recommendations']:
            msg += f"{rec}\n"
        msg += "\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    msg += "⚠️ *Always gamble responsibly.*"
    return msg

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = {'pl', 'ucl'}
    
    await update.message.reply_text(
        "⚽ **Football Position Analyzer**\n\n"
        "🎯 Find betting opportunities!\n\n"
        "**Commands:**\n"
        "/test - Test API connection\n"
        "/live - Live matches 🔴\n"
        "/next - Upcoming matches\n"
        "/pl - Premier League\n"
        "/ucl - Champions League\n"
        "/check [id] - Analyze match\n"
        "/help - Full help\n\n"
        "✨ Season 2025/26"
    )

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Testing API connection...")
    
    success = await test_api_connection()
    
    if success:
        await update.message.reply_text(
            "✅ API Connected!\n\n"
            f"API Key configured: {API_FOOTBALL_KEY[:10]}...\n"
            f"Season: {CURRENT_SEASON}\n\n"
            "Try /live or /next to see matches!"
        )
    else:
        await update.message.reply_text(
            "❌ API Connection Failed\n\n"
            "Possible issues:\n"
            "• Check your API key in Koyeb\n"
            "• Verify it matches dashboard\n"
            "• Check API quota (100/day free)\n\n"
            f"Current key: {API_FOOTBALL_KEY[:10]}..."
        )

async def live_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = {'pl', 'ucl'}
    
    await update.message.reply_text("🔴 Checking for LIVE matches...")
    
    matches = await get_live_matches(list(user_preferences[user_id]))
    
    if not matches:
        await update.message.reply_text(
            "No live matches right now.\n\n"
            "💡 Try /next for upcoming matches!"
        )
        return
    
    msg = "🔴 **LIVE MATCHES:**\n\n"
    
    for match in matches:
        league_code = match.get('league_code', 'pl')
        league_info = LEAGUES.get(league_code, LEAGUES['pl'])
        
        home = match['teams']['home']['name']
        away = match['teams']['away']['name']
        fixture_id = match['fixture']['id']
        elapsed = match['fixture']['status'].get('elapsed', '?')
        home_score = match['goals']['home'] or 0
        away_score = match['goals']['away'] or 0
        
        msg += f"{league_info['emoji']} **{home} {home_score}-{away_score} {away}**\n"
        msg += f"   ⏱️ {elapsed}' | 🆔 `{fixture_id}`\n\n"
    
    msg += "💡 Use `/check [ID]` to analyze!"
    await update.message.reply_text(msg)

async def next_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = {'pl', 'ucl'}
    
    await update.message.reply_text("🔍 Fetching upcoming matches...")
    
    matches = await get_upcoming_matches(list(user_preferences[user_id]))
    
    if not matches:
        await update.message.reply_text("No upcoming matches in next 7 days.")
        return
    
    msg = "📅 **Upcoming Matches:**\n\n"
    current_league = None
    
    for match in matches[:15]:
        league_code = match.get('league_code', 'pl')
        league_info = LEAGUES.get(league_code, LEAGUES['pl'])
        
        if current_league != league_info['name']:
            current_league = league_info['name']
            msg += f"\n{league_info['emoji']} **{league_info['name']}**\n"
        
        home = match['teams']['home']['name']
        away = match['teams']['away']['name']
        date_str = match['fixture']['date']
        fixture_id = match['fixture']['id']
        
        kickoff = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        time_str = kickoff.strftime('%a %d %b, %H:%M')
        
        msg += f"⚽ {home} vs {away}\n"
        msg += f"   🕐 {time_str} | 🆔 `{fixture_id}`\n"
    
    msg += "\n💡 Use `/check [ID]` to analyze"
    await update.message.reply_text(msg)

async def league_specific(update: Update, context: ContextTypes.DEFAULT_TYPE, league_code: str):
    if league_code not in LEAGUES:
        await update.message.reply_text("❌ League not found")
        return
    
    league_info = LEAGUES[league_code]
    await update.message.reply_text(f"🔍 Fetching {league_info['name']}...")
    
    matches = await get_upcoming_matches([league_code])
    
    if not matches:
        await update.message.reply_text(f"No {league_info['name']} matches found.")
        return
    
    msg = f"{league_info['emoji']} **{league_info['name']}:**\n\n"
    
    for match in matches[:10]:
        home = match['teams']['home']['name']
        away = match['teams']['away']['name']
        date_str = match['fixture']['date']
        fixture_id = match['fixture']['id']
        kickoff = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        time_str = kickoff.strftime('%a %d %b, %H:%M')
        
        msg += f"⚽ {home} vs {away}\n"
        msg += f"   🕐 {time_str} | 🆔 `{fixture_id}`\n"
    
    msg += "\n💡 Use `/check [ID]`"
    await update.message.reply_text(msg)

async def check_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Provide match ID: `/check [id]`\n"
            "Use /next or /live to see matches"
        )
        return
    
    try:
        fixture_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")
        return
    
    await update.message.reply_text(f"🔍 Analyzing {fixture_id}...\n⏳ 30-60 seconds...")
    
    params = {'id': fixture_id}
    match_data = await get_api_football_data('fixtures', params)
    
    if not match_data or not match_data.get('response'):
        await update.message.reply_text("❌ Match not found")
        return
    
    match_info = match_data['response'][0]
    analysis = await analyze_lineup_detailed(fixture_id, match_info)
    
    if analysis:
        message = format_detailed_analysis(analysis)
        await update.message.reply_text(message, parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "ℹ️ No opportunities found\n\n"
            "Reasons:\n"
            "• Players in usual positions\n"
            "• Lineup not released yet\n"
            "• Insufficient data\n\n"
            "💡 Lineups drop 60-90min before kickoff"
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ **Football Position Analyzer**\n\n"
        "**Commands:**\n"
        "/test - Test API\n"
        "/live - Live matches\n"
        "/next - Upcoming\n"
        "/pl /ucl /laliga /seriea\n"
        "/bundesliga /ligue1\n"
        "/check [id] - Analyze\n\n"
        "**Season:** 2025/26\n\n"
        "⚠️ Gamble responsibly!"
    )

def main():
    logger.info("Starting Bot - Season 2025/26")
    
    health_thread = Thread(target=run_health_server, daemon=True)
    health_thread.start()
    logger.info("✅ Health server started")
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CommandHandler("live", live_matches))
    application.add_handler(CommandHandler("next", next_matches))
    application.add_handler(CommandHandler("check", check_match))
    
    application.add_handler(CommandHandler("pl", lambda u, c: league_specific(u, c, 'pl')))
    application.add_handler(CommandHandler("ucl", lambda u, c: league_specific(u, c, 'ucl')))
    application.add_handler(CommandHandler("laliga", lambda u, c: league_specific(u, c, 'laliga')))
    application.add_handler(CommandHandler("seriea", lambda u, c: league_specific(u, c, 'seriea')))
    application.add_handler(CommandHandler("bundesliga", lambda u, c: league_specific(u, c, 'bundesliga')))
    application.add_handler(CommandHandler("ligue1", lambda u, c: league_specific(u, c, 'ligue1')))
    
    logger.info("✅ Bot ready - Season 2025")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
