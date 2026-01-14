import os
import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
from collections import defaultdict
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Configuration - Use environment variables for deployment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "YOUR_API_FOOTBALL_KEY")
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
PREMIER_LEAGUE_ID = 39
CHECK_INTERVAL = 1800  # 30 minutes
CURRENT_SEASON = 2024  # Update each season

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory storage (in production, use a database)
subscribed_users: Set[int] = set()
player_history: Dict[int, Dict] = {}  # player_id -> historical data
analyzed_fixtures: Set[int] = set()  # Track analyzed fixtures to avoid duplicates

POSITION_MAP = {
    'G': 'Goalkeeper',
    'D': 'Defender',
    'M': 'Midfielder',
    'F': 'Forward'
}

class PlayerStats:
    """Store and analyze player statistics"""
    def __init__(self, player_id: int, name: str):
        self.player_id = player_id
        self.name = name
        self.positions = []  # List of positions in recent matches
        self.fouls_per_90 = 0.0
        self.cards_per_90 = 0.0
        self.shots_per_90 = 0.0
        self.usual_position = None
        
    def add_position(self, position: str):
        """Add a position from a match"""
        self.positions.append(position)
        if len(self.positions) > 10:
            self.positions.pop(0)  # Keep last 10 matches
    
    def get_usual_position(self) -> str:
        """Get most common position"""
        if not self.positions:
            return "Unknown"
        return max(set(self.positions), key=self.positions.count)
    
    def is_out_of_position(self, current_pos: str) -> bool:
        """Check if current position differs from usual"""
        if len(self.positions) < 3:  # Need at least 3 matches
            return False
        usual = self.get_usual_position()
        return usual != current_pos and usual != "Unknown"
    
    def get_position_change_impact(self, new_position: str) -> Dict:
        """Analyze impact of position change on betting markets"""
        usual = self.get_usual_position()
        impact = {
            'fouls': 'neutral',
            'cards': 'neutral',
            'shots': 'neutral',
            'confidence': 'low'
        }
        
        if len(self.positions) < 3:
            return impact
        
        # Defender -> Midfielder/Forward
        if usual == 'D' and new_position in ['M', 'F']:
            impact['fouls'] = 'decrease'
            impact['cards'] = 'decrease'
            impact['shots'] = 'increase'
            impact['confidence'] = 'high'
        
        # Midfielder -> Defender
        elif usual == 'M' and new_position == 'D':
            impact['fouls'] = 'increase'
            impact['cards'] = 'increase'
            impact['shots'] = 'decrease'
            impact['confidence'] = 'high'
        
        # Midfielder -> Forward
        elif usual == 'M' and new_position == 'F':
            impact['shots'] = 'increase'
            impact['fouls'] = 'slight_decrease'
            impact['confidence'] = 'medium'
        
        # Forward -> Midfielder
        elif usual == 'F' and new_position == 'M':
            impact['shots'] = 'decrease'
            impact['fouls'] = 'slight_increase'
            impact['confidence'] = 'medium'
        
        # Forward -> Defender (rare but possible)
        elif usual == 'F' and new_position == 'D':
            impact['fouls'] = 'increase'
            impact['cards'] = 'increase'
            impact['shots'] = 'significant_decrease'
            impact['confidence'] = 'high'
        
        return impact

async def get_api_football_data(endpoint: str, params: Dict = None, retries: int = 3):
    """Make request to API-Football with retry logic"""
    headers = {
        'x-apisports-key': API_FOOTBALL_KEY
    }
    url = f"{API_FOOTBALL_BASE}/{endpoint}"
    
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params, timeout=10) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:  # Rate limit
                        logger.warning("Rate limited, waiting...")
                        await asyncio.sleep(60)
                    else:
                        logger.error(f"API error: {response.status}")
        except Exception as e:
            logger.error(f"Request failed (attempt {attempt + 1}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(5)
    
    return None

async def build_player_history(team_id: int, season: int = CURRENT_SEASON):
    """Build historical position data for team players"""
    logger.info(f"Building player history for team {team_id}")
    
    # Get team's recent fixtures
    params = {
        'team': team_id,
        'season': season,
        'last': 15  # Last 15 matches
    }
    
    data = await get_api_football_data('fixtures', params)
    if not data or not data.get('response'):
        return
    
    fixtures = data['response']
    
    for fixture in fixtures:
        fixture_id = fixture['fixture']['id']
        
        # Get lineup for this fixture
        lineup_data = await get_api_football_data('fixtures/lineups', {'fixture': fixture_id})
        if not lineup_data or not lineup_data.get('response'):
            continue
        
        for team_lineup in lineup_data['response']:
            if team_lineup['team']['id'] != team_id:
                continue
            
            # Process all players (starters + subs)
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
        
        await asyncio.sleep(0.5)  # Rate limiting

async def get_player_statistics(player_id: int, season: int = CURRENT_SEASON):
    """Get detailed player statistics"""
    params = {
        'id': player_id,
        'season': season
    }
    
    data = await get_api_football_data('players', params)
    if not data or not data.get('response'):
        return None
    
    player_data = data['response'][0]
    statistics = player_data.get('statistics', [])
    
    if not statistics:
        return None
    
    # Aggregate stats across all competitions
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
        total_cards += (yellow + red * 2)  # Weight red cards
        total_shots += shots_total
    
    if total_minutes == 0:
        return None
    
    # Calculate per 90 stats
    matches_90 = total_minutes / 90
    
    return {
        'fouls_per_90': round(total_fouls / matches_90, 2) if matches_90 > 0 else 0,
        'cards_per_90': round(total_cards / matches_90, 2) if matches_90 > 0 else 0,
        'shots_per_90': round(total_shots / matches_90, 2) if matches_90 > 0 else 0
    }

async def get_upcoming_matches(hours_ahead: int = 48):
    """Get upcoming Premier League matches"""
    today = datetime.now()
    future = today + timedelta(hours=hours_ahead)
    
    params = {
        'league': PREMIER_LEAGUE_ID,
        'season': CURRENT_SEASON,
        'from': today.strftime('%Y-%m-%d'),
        'to': future.strftime('%Y-%m-%d')
    }
    
    data = await get_api_football_data('fixtures', params)
    if data and data.get('response'):
        return data['response']
    return []

async def analyze_lineup_detailed(fixture_id: int, match_info: Dict) -> Optional[Dict]:
    """Comprehensive lineup analysis with statistics"""
    params = {'fixture': fixture_id}
    data = await get_api_football_data('fixtures/lineups', params)
    
    if not data or not data.get('response'):
        return None
    
    lineups = data['response']
    home_team = match_info['teams']['home']
    away_team = match_info['teams']['away']
    
    # Build history for both teams if not already done
    if not any(p.player_id for p in player_history.values() if p.positions):
        logger.info("Building player histories...")
        await build_player_history(home_team['id'])
        await build_player_history(away_team['id'])
    
    analysis = {
        'fixture_id': fixture_id,
        'home_team': home_team['name'],
        'away_team': away_team['name'],
        'kickoff': match_info['fixture']['date'],
        'opportunities': []
    }
    
    for team_lineup in lineups:
        team_name = team_lineup.get('team', {}).get('name', 'Unknown')
        team_id = team_lineup.get('team', {}).get('id')
        starters = team_lineup.get('startXI', [])
        
        for player_data in starters:
            player = player_data.get('player', {})
            player_id = player.get('id')
            player_name = player.get('name', 'Unknown')
            current_position = player.get('pos', 'Unknown')
            
            if current_position == 'Unknown' or not player_id:
                continue
            
            # Get or create player stats
            if player_id not in player_history:
                player_history[player_id] = PlayerStats(player_id, player_name)
                # Try to get stats
                stats = await get_player_statistics(player_id)
                if stats:
                    player_history[player_id].fouls_per_90 = stats['fouls_per_90']
                    player_history[player_id].cards_per_90 = stats['cards_per_90']
                    player_history[player_id].shots_per_90 = stats['shots_per_90']
            
            player_stats = player_history[player_id]
            
            # Check if out of position
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
                
                # Generate betting recommendations
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
    """Format comprehensive analysis message"""
    msg = f"🚨 **BETTING OPPORTUNITY DETECTED** 🚨\n\n"
    msg += f"⚽ **{analysis['home_team']} vs {analysis['away_team']}**\n"
    msg += f"🕐 {analysis['kickoff']}\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for i, opp in enumerate(analysis['opportunities'], 1):
        confidence_emoji = {
            'high': '🔥',
            'medium': '⚡',
            'low': '💡'
        }
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
    
    msg += "⚠️ *Always gamble responsibly. These are analytical insights, not guarantees.*"
    
    return msg

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    chat_id = update.effective_chat.id
    subscribed_users.add(chat_id)
    
    await update.message.reply_text(
        "⚽ **Premier League Position Analyzer Bot**\n\n"
        "🎯 I analyze player lineups to find betting opportunities!\n\n"
        "**Features:**\n"
        "✅ Automatic lineup monitoring\n"
        "✅ Historical position tracking\n"
        "✅ Statistical analysis\n"
        "✅ Betting recommendations\n\n"
        "**Commands:**\n"
        "/next - View upcoming matches\n"
        "/check [id] - Analyze specific match\n"
        "/stats - Bot statistics\n"
        "/stop - Unsubscribe\n"
        "/help - Show help\n\n"
        "You're now subscribed to automatic alerts! 🔔"
    )

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command"""
    chat_id = update.effective_chat.id
    subscribed_users.discard(chat_id)
    await update.message.reply_text("✅ Unsubscribed from alerts. Use /start to resubscribe.")

async def next_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /next command"""
    await update.message.reply_text("🔍 Fetching upcoming Premier League matches...")
    
    matches = await get_upcoming_matches()
    
    if not matches:
        await update.message.reply_text("No upcoming matches found in the next 48 hours.")
        return
    
    msg = "📅 **Upcoming Premier League Matches:**\n\n"
    for match in matches[:8]:
        home = match['teams']['home']['name']
        away = match['teams']['away']['name']
        date_str = match['fixture']['date']
        fixture_id = match['fixture']['id']
        status = match['fixture']['status']['long']
        
        # Parse and format date
        kickoff = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        time_str = kickoff.strftime('%a %d %b, %H:%M')
        
        msg += f"⚽ **{home} vs {away}**\n"
        msg += f"   🕐 {time_str}\n"
        msg += f"   🆔 ID: `{fixture_id}`\n"
        msg += f"   📊 Status: {status}\n\n"
    
    msg += "💡 Use `/check [ID]` to analyze a match"
    await update.message.reply_text(msg)

async def check_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /check command"""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a match ID\n\n"
            "Usage: `/check [match_id]`\n"
            "Use /next to see available matches"
        )
        return
    
    try:
        fixture_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid match ID. Must be a number.")
        return
    
    await update.message.reply_text(f"🔍 Analyzing match {fixture_id}...\nThis may take 30-60 seconds...")
    
    # Get match info
    params = {'id': fixture_id}
    match_data = await get_api_football_data('fixtures', params)
    
    if not match_data or not match_data.get('response'):
        await update.message.reply_text("❌ Match not found.")
        return
    
    match_info = match_data['response'][0]
    
    # Analyze lineup
    analysis = await analyze_lineup_detailed(fixture_id, match_info)
    
    if analysis:
        message = format_detailed_analysis(analysis)
        await update.message.reply_text(message, parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "ℹ️ Lineup available but no significant opportunities detected.\n\n"
            "Possible reasons:\n"
            "• All players in usual positions\n"
            "• Insufficient historical data\n"
            "• Lineups not yet released\n\n"
            "Check back closer to kickoff! ⏰"
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics"""
    msg = "📊 **Bot Statistics**\n\n"
    msg += f"👥 Subscribed users: {len(subscribed_users)}\n"
    msg += f"🎯 Tracked players: {len(player_history)}\n"
    msg += f"⚽ Analyzed fixtures: {len(analyzed_fixtures)}\n"
    
    await update.message.reply_text(msg)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    await update.message.reply_text(
        "⚽ **Premier League Position Analyzer**\n\n"
        "**How it works:**\n"
        "1. Bot monitors upcoming PL matches\n"
        "2. When lineups drop (60-90min before kickoff)\n"
        "3. Compares current vs historical positions\n"
        "4. Analyzes betting market impact\n"
        "5. Sends alerts with recommendations\n\n"
        "**Commands:**\n"
        "/start - Subscribe to alerts\n"
        "/next - Upcoming matches\n"
        "/check [id] - Analyze match\n"
        "/stats - Bot statistics\n"
        "/stop - Unsubscribe\n\n"
        "**Markets to consider:**\n"
        "🟡 Player bookings\n"
        "⚽ Shots on target\n"
        "🚫 Fouls committed\n"
        "🎯 Anytime goalscorer\n\n"
        "⚠️ Always gamble responsibly!"
    )

async def auto_check_lineups(context: ContextTypes.DEFAULT_TYPE):
    """Background task to check for new lineups"""
    try:
        logger.info("Running automatic lineup check...")
        matches = await get_upcoming_matches(hours_ahead=6)
        
        for match in matches:
            fixture_id = match['fixture']['id']
            
            # Skip if already analyzed
            if fixture_id in analyzed_fixtures:
                continue
            
            # Check if match is within analysis window (30min - 3 hours before kickoff)
            kickoff_str = match['fixture']['date']
            kickoff = datetime.fromisoformat(kickoff_str.replace('Z', '+00:00'))
            now = datetime.now(kickoff.tzinfo)
            time_until = kickoff - now
            
            if timedelta(minutes=30) < time_until < timedelta(hours=3):
                logger.info(f"Analyzing fixture {fixture_id}")
                
                analysis = await analyze_lineup_detailed(fixture_id, match)
                
                if analysis:
                    analyzed_fixtures.add(fixture_id)
                    message = format_detailed_analysis(analysis)
                    
                    # Send to all subscribers
                    for chat_id in list(subscribed_users):
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=message,
                                parse_mode='Markdown'
                            )
                            await asyncio.sleep(0.5)  # Rate limiting
                        except Exception as e:
                            logger.error(f"Failed to send to {chat_id}: {e}")
                            if "bot was blocked" in str(e).lower():
                                subscribed_users.discard(chat_id)
                
                await asyncio.sleep(2)  # Rate limiting between matches
        
    except Exception as e:
        logger.error(f"Error in auto_check_lineups: {e}")

def main():
    """Start the bot"""
    logger.info("Starting Premier League Position Analyzer Bot...")
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("next", next_matches))
    application.add_handler(CommandHandler("check", check_match))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Schedule automatic lineup checks
    application.job_queue.run_repeating(
        auto_check_lineups,
        interval=CHECK_INTERVAL,
        first=10
    )
    
    logger.info("✅ Bot started successfully!")
    logger.info(f"📊 Checking lineups every {CHECK_INTERVAL/60} minutes")
    
    # Start polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
