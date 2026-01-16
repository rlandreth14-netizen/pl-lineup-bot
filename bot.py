import os
import requests
import logging
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("API_FOOTBALL_KEY")
SEASON = "2025"

# --- KOYEB HEALTH CHECK ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.getenv("PORT", 8000))
    HTTPServer(('0.0.0.0', port), HealthCheckHandler).serve_forever()

# --- API LOGIC ---
logging.basicConfig(level=logging.INFO)
LEAGUE_MAP = {
    "pl": 39,
    "championship": 40,
    "laliga": 140,
    "seriea": 135,
    "bundesliga": 78,
    "ligue1": 61,
    "ucl": 2
}

# Player position history (simple in-memory cache)
player_positions = {}

def get_api_data(endpoint):
    """Call API-Football with correct headers"""
    headers = {
        'x-apisports-key': API_KEY  # FIXED: Was x-rapidapi-key
    }
    url = f"https://v3.football.api-sports.io/{endpoint}"
    
    logging.info(f"API Call: {endpoint}")
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            results = data.get('response', [])
            logging.info(f"API returned {len(results)} results")
            return results
        else:
            logging.error(f"API Error: {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"API Exception: {e}")
        return []

def analyze_position_change(player_id, current_pos, player_name):
    """Check if player is out of position and generate betting insights"""
    if player_id not in player_positions:
        player_positions[player_id] = {'name': player_name, 'positions': []}
    
    player_positions[player_id]['positions'].append(current_pos)
    
    # Keep last 5 positions
    if len(player_positions[player_id]['positions']) > 5:
        player_positions[player_id]['positions'].pop(0)
    
    positions = player_positions[player_id]['positions']
    
    # Need at least 2 positions to compare
    if len(positions) < 2:
        return None
    
    # Get most common position
    usual_pos = max(set(positions[:-1]), key=positions[:-1].count)
    
    # If different from current
    if usual_pos != current_pos:
        insights = []
        
        # Defender playing forward
        if usual_pos == 'D' and current_pos in ['M', 'F']:
            insights.append("✅ BACK: Player shots on target (attacking role)")
            insights.append("❌ AVOID: Player fouls (less defensive work)")
        
        # Midfielder playing defender
        elif usual_pos == 'M' and current_pos == 'D':
            insights.append("✅ BACK: Player to commit 2+ fouls (defensive role)")
            insights.append("✅ BACK: Player to be booked (unfamiliar position)")
            insights.append("❌ AVOID: Player shots markets")
        
        # Forward playing midfield
        elif usual_pos == 'F' and current_pos == 'M':
            insights.append("❌ AVOID: Player goalscorer markets (deeper role)")
            insights.append("✅ CONSIDER: Player assists (playmaking role)")
        
        # Midfielder playing forward
        elif usual_pos == 'M' and current_pos == 'F':
            insights.append("✅ BACK: Player shots on target")
            insights.append("✅ CONSIDER: Anytime goalscorer")
        
        if insights:
            return {
                'player': player_name,
                'usual': usual_pos,
                'current': current_pos,
                'insights': insights
            }
    
    return None

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⚽ Premier League", callback_data="list_39")],
        [InlineKeyboardButton("⭐ Champions League", callback_data="list_2")],
        [InlineKeyboardButton("🇪🇸 La Liga", callback_data="list_140")],
        [InlineKeyboardButton("🇮🇹 Serie A", callback_data="list_135")],
        [InlineKeyboardButton("🇩🇪 Bundesliga", callback_data="list_78")],
        [InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="list_61")],
    ]
    await update.message.reply_text(
        "⚽ **Football Position Analyzer**\n\n"
        "Select a league to find betting opportunities:\n\n"
        "Commands:\n"
        "/test - Test API connection\n"
        "/pl /ucl /laliga - Quick access",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test if API is working"""
    await update.message.reply_text("🔍 Testing API connection...")
    
    result = get_api_data(f"fixtures?league=39&season={SEASON}")
    
    if result:
        await update.message.reply_text(
            f"✅ API Connected!\n"
            f"Key: {API_KEY[:10]}...\n"
            f"Season: {SEASON}\n"
            f"Found {len(result)} fixtures"
        )
    else:
        await update.message.reply_text(
            f"❌ API Failed\n"
            f"Key: {API_KEY[:10]}...\n"
            f"Check your API key in Koyeb"
        )

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pl, /ucl etc commands"""
    cmd = update.message.text.lower().replace("/", "")
    if cmd in LEAGUE_MAP:
        league_id = LEAGUE_MAP[cmd]
        await list_fixtures_func(update, league_id, cmd.upper(), from_message=True)

async def list_fixtures_func(update, league_id, name, from_message=False):
    """List today's fixtures for a league"""
    today = datetime.now().strftime('%Y-%m-%d')
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    
    # Check today and tomorrow
    fixtures = get_api_data(f"fixtures?league={league_id}&season={SEASON}&from={today}&to={tomorrow}")
    
    if not fixtures:
        msg = f"ℹ️ No matches found for {name} today/tomorrow.\n\nTry /test to check API connection."
        if from_message:
            await update.message.reply_text(msg)
        else:
            await update.callback_query.edit_message_text(msg)
        return

    msg = f"📅 **{name} Fixtures:**\n\n"
    buttons = []
    
    for f in fixtures[:5]:  # Limit to 5 matches
        f_id = f['fixture']['id']
        home = f['teams']['home']['name']
        away = f['teams']['away']['name']
        date = f['fixture']['date']
        status = f['fixture']['status']['short']
        
        # Parse date
        match_time = datetime.fromisoformat(date.replace('Z', '+00:00'))
        time_str = match_time.strftime('%H:%M')
        
        msg += f"⚽ {home} vs {away}\n"
        msg += f"   🕐 {time_str} | Status: {status}\n\n"
        
        buttons.append([InlineKeyboardButton(
            f"📋 {home} vs {away}",
            callback_data=f"lineup_{f_id}"
        )])
    
    if from_message:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))

async def show_lineups(update: Update, fixture_id: str):
    """Show lineups and detect position changes"""
    query = update.callback_query
    
    await query.edit_message_text("🔍 Fetching lineups and analyzing positions...")
    
    lineups = get_api_data(f"fixtures/lineups?fixture={fixture_id}")
    
    if not lineups:
        await query.edit_message_text(
            "⚠️ Lineups not yet announced.\n\n"
            "💡 Lineups usually drop 60-90 minutes before kickoff.\n\n"
            "Try again closer to match time!"
        )
        return

    opportunities = []
    lineup_text = "📊 **LINEUPS & ANALYSIS**\n\n"
    
    for team in lineups:
        team_name = team['team']['name']
        formation = team['formation']
        
        lineup_text += f"**{team_name}** ({formation})\n"
        
        for player in team['startXI']:
            p = player['player']
            player_id = p['id']
            player_name = p['name']
            current_pos = p['pos']
            number = p.get('number', '?')
            
            lineup_text += f"• {number}. {player_name} ({current_pos})\n"
            
            # Check for position changes
            insight = analyze_position_change(player_id, current_pos, player_name)
            if insight:
                opportunities.append(insight)
        
        lineup_text += "\n"
    
    # Add betting opportunities
    if opportunities:
        lineup_text += "━━━━━━━━━━━━━━━━━━━━\n"
        lineup_text += "🚨 **BETTING OPPORTUNITIES**\n\n"
        
        for i, opp in enumerate(opportunities, 1):
            lineup_text += f"**#{i} {opp['player']}**\n"
            lineup_text += f"📍 {opp['usual']} → {opp['current']} (OUT OF POSITION)\n"
            for insight in opp['insights']:
                lineup_text += f"{insight}\n"
            lineup_text += "\n"
        
        lineup_text += "⚠️ *Always gamble responsibly*"
    else:
        lineup_text += "✅ All players in usual positions\n"
        lineup_text += "No significant opportunities detected"
    
    # Add button to check live stats
    btn = [[InlineKeyboardButton("🎯 Live Stats (Once Match Starts)", callback_data=f"stats_{fixture_id}")]]
    await query.edit_message_text(lineup_text, reply_markup=InlineKeyboardMarkup(btn), parse_mode='Markdown')

async def show_stats(update: Update, fixture_id: str):
    """Show live player statistics during match"""
    query = update.callback_query
    
    await query.edit_message_text("🔍 Fetching live stats...")
    
    player_data = get_api_data(f"fixtures/players?fixture={fixture_id}")
    
    if not player_data:
        await query.edit_message_text(
            "ℹ️ Live stats available once match begins.\n\n"
            "Check back after kickoff!"
        )
        return

    msg = "🎯 **LIVE PLAYER STATS**\n\n"
    
    for team in player_data:
        msg += f"**{team['team']['name']}**\n"
        
        # Sort by shots on target
        players = sorted(
            team['players'],
            key=lambda x: x['statistics'][0].get('shots', {}).get('on', 0),
            reverse=True
        )
        
        for p in players[:8]:  # Top 8 players
            s = p['statistics'][0]
            name = p['player']['name']
            shots_on = s.get('shots', {}).get('on', 0)
            fouls = s.get('fouls', {}).get('committed', 0)
            cards_y = s.get('cards', {}).get('yellow', 0)
            cards_r = s.get('cards', {}).get('red', 0)
            
            msg += f"• {name}\n"
            msg += f"  🎯 {shots_on} SOT | 🚫 {fouls} Fouls"
            
            if cards_y:
                msg += f" | 🟨 {cards_y}"
            if cards_r:
                msg += f" | 🟥 {cards_r}"
            
            msg += "\n"
        
        msg += "\n"

    await query.edit_message_text(msg, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all button clicks"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("list_"):
        league_id = int(query.data.split("_")[1])
        league_name = "League"
        # Find league name
        for name, lid in LEAGUE_MAP.items():
            if lid == league_id:
                league_name = name.upper()
                break
        await list_fixtures_func(update, league_id, league_name, from_message=False)
    
    elif query.data.startswith("lineup_"):
        await show_lineups(update, query.data.split("_")[1])
    
    elif query.data.startswith("stats_"):
        await show_stats(update, query.data.split("_")[1])

def main():
    """Start the bot"""
    logging.info("Starting Football Position Analyzer Bot...")
    
    # Start health check server
    threading.Thread(target=run_health_server, daemon=True).start()
    logging.info("✅ Health check server started")
    
    # Build bot
    app = Application.builder().token(TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_command))
    
    # League commands
    for cmd in LEAGUE_MAP.keys():
        app.add_handler(CommandHandler(cmd, handle_text_command))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logging.info("✅ Bot started - Season 2025")
    app.run_polling()

if __name__ == '__main__':
    main()
