import os
import requests
import logging
import json
import threading
from bs4 import BeautifulSoup
from datetime import datetime
from pymongo import MongoClient
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")  # MongoDB Atlas connection string
PORT = int(os.getenv("PORT", 8000))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database Connection
try:
    if MONGO_URI:
        client = MongoClient(MONGO_URI)
        db = client['football_bot']
        player_collection = db['player_history']
        logger.info("✅ MongoDB connected")
    else:
        logger.warning("⚠️ No MONGO_URI - using in-memory storage")
        player_collection = None
        # Fallback to in-memory dict
        player_memory = {}
except Exception as e:
    logger.error(f"MongoDB connection failed: {e}")
    player_collection = None
    player_memory = {}

LEAGUE_MAP = {
    "pl": 47,           # Premier League
    "championship": 48, # Championship
    "laliga": 87,       # La Liga
    "seriea": 55,       # Serie A
    "bundesliga": 54,   # Bundesliga
    "ligue1": 53,       # Ligue 1
    "ucl": 42           # Champions League
}

# Enhanced position mapping
POSITION_GROUPS = {
    'GK': 'G', 'G': 'G',
    'CB': 'D', 'LCB': 'D', 'RCB': 'D', 'LB': 'D', 'RB': 'D', 'D': 'D',
    'LWB': 'W', 'RWB': 'W', 'WB': 'W',
    'LM': 'W', 'RM': 'W', 'LW': 'W', 'RW': 'W', 'W': 'W',
    'CDM': 'M', 'LDM': 'M', 'RDM': 'M', 'DM': 'M',
    'CM': 'M', 'LCM': 'M', 'RCM': 'M', 'M': 'M',
    'CAM': 'M', 'AM': 'M', 'LAM': 'M', 'RAM': 'M',
    'ST': 'A', 'CF': 'A', 'SS': 'A', 'F': 'A', 'A': 'A'
}

# --- HEALTH CHECK ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    logger.info(f"Health server running on port {PORT}")
    server.serve_forever()

# --- DATABASE LOGIC ---
def update_player_knowledge(lineup_data):
    """Store player position history"""
    if player_collection:
        for p in lineup_data:
            try:
                player_collection.update_one(
                    {"name": p['name']},
                    {"$inc": {f"positions.{p['pos']}": 1}},
                    upsert=True
                )
            except Exception as e:
                logger.error(f"DB update error: {e}")
    else:
        # In-memory fallback
        for p in lineup_data:
            if p['name'] not in player_memory:
                player_memory[p['name']] = {}
            if p['pos'] not in player_memory[p['name']]:
                player_memory[p['name']][p['pos']] = 0
            player_memory[p['name']][p['pos']] += 1

def get_usual_position(player_name):
    """Get player's most common position"""
    if player_collection:
        try:
            player = player_collection.find_one({"name": player_name})
            if player and 'positions' in player and player['positions']:
                return max(player['positions'], key=player['positions'].get)
        except Exception as e:
            logger.error(f"DB query error: {e}")
    else:
        # In-memory fallback
        if player_name in player_memory and player_memory[player_name]:
            return max(player_memory[player_name], key=player_memory[player_name].get)
    
    return None

# --- SCRAPER LOGIC ---
def get_league_matches(league_id):
    """Scrape FotMob for today's matches"""
    url = f"https://www.fotmob.com/api/leagues?id={league_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        logger.info(f"Fetching matches for league {league_id}")
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"FotMob returned {response.status_code}")
            return []
        
        data = response.json()
        
        # Different API structure - adapt as needed
        matches = data.get('matches', {}).get('allMatches', [])
        if not matches:
            # Try alternative structure
            matches = data.get('allMatches', [])
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Filter for today's matches
        today_matches = []
        for m in matches:
            match_date = m.get('status', {}).get('utcTime', '')
            if today in match_date:
                today_matches.append({
                    'id': m.get('id'),
                    'home': m.get('home', {}).get('name', 'Home'),
                    'away': m.get('away', {}).get('name', 'Away'),
                    'time': match_date
                })
        
        logger.info(f"Found {len(today_matches)} matches today")
        return today_matches
        
    except Exception as e:
        logger.error(f"Match scrape error: {e}")
        return []

def scrape_lineup(match_id):
    """Scrape lineup from FotMob match page"""
    url = f"https://www.fotmob.com/matches/{match_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        logger.info(f"Scraping lineup for match {match_id}")
        res = requests.get(url, headers=headers, timeout=10)
        
        if res.status_code != 200:
            logger.error(f"FotMob match page returned {res.status_code}")
            return None
        
        soup = BeautifulSoup(res.content, 'html.parser')
        script = soup.find('script', id='__NEXT_DATA__')
        
        if not script:
            logger.error("Could not find __NEXT_DATA__ script")
            return None
        
        data = json.loads(script.string)
        content = data.get('props', {}).get('pageProps', {}).get('content', {})
        
        if 'lineup' not in content or not content['lineup']:
            logger.info("Lineup not available yet")
            return None
        
        players = []
        for side in ['home', 'away']:
            lineup_side = content['lineup'].get(side, {})
            starting = lineup_side.get('starting', [])
            
            for p in starting:
                name_info = p.get('name', {})
                position = p.get('positionStringShort', '??')
                
                players.append({
                    'name': name_info.get('fullName', name_info.get('firstName', 'Unknown')),
                    'pos': position,
                    'team': side
                })
        
        logger.info(f"Found {len(players)} players in lineup")
        return players
        
    except Exception as e:
        logger.error(f"Lineup scrape error: {e}")
        return None

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message with league buttons"""
    keyboard = [
        [InlineKeyboardButton("⚽ Premier League", callback_data="list_47")],
        [InlineKeyboardButton("⭐ Champions League", callback_data="list_42")],
        [InlineKeyboardButton("🇪🇸 La Liga", callback_data="list_87")],
        [InlineKeyboardButton("🇮🇹 Serie A", callback_data="list_55")],
        [InlineKeyboardButton("🇩🇪 Bundesliga", callback_data="list_54")],
        [InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="list_53")],
    ]
    
    msg = (
        "⚽ **Football Position Analyzer**\n\n"
        "🎯 Find betting edges from lineup changes!\n\n"
        "**How it works:**\n"
        "1. Select a league\n"
        "2. Choose a match\n"
        "3. Get betting insights\n\n"
        "💡 Lineups usually drop 60-90min before kickoff"
    )
    
    await update.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test FotMob scraping"""
    await update.message.reply_text("🔍 Testing FotMob connection...")
    
    matches = get_league_matches(47)  # Test Premier League
    
    msg = f"**Test Results:**\n\n"
    msg += f"✅ FotMob: {len(matches)} PL matches found\n"
    
    if MONGO_URI:
        msg += f"✅ MongoDB: Connected\n"
    else:
        msg += f"⚠️ MongoDB: Using in-memory storage\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks"""
    query = update.callback_query
    await query.answer()
    
    # LEAGUE LISTING
    if query.data.startswith("list_"):
        league_id = query.data.split("_")[1]
        matches = get_league_matches(league_id)
        
        if not matches:
            msg = (
                f"📭 No matches found for today.\n\n"
                f"⏰ Checked at: {datetime.now().strftime('%H:%M')}\n\n"
                f"💡 Try again later or check another league!"
            )
            try:
                await query.edit_message_text(msg, parse_mode='Markdown')
            except BadRequest:
                pass
            return

        try:
            await query.edit_message_text(
                f"📅 **Today's Matches:**\n\nSelect a match to analyze:",
                parse_mode='Markdown'
            )
        except BadRequest:
            pass

        for m in matches[:10]:  # Limit to 10 matches
            match_time = datetime.fromisoformat(m['time'].replace('Z', '+00:00'))
            time_str = match_time.strftime('%H:%M')
            
            btn = [[InlineKeyboardButton(
                "📋 Analyze Lineup",
                callback_data=f"an_{m['id']}"
            )]]
            
            match_label = f"⚽ {m['home']} vs {m['away']}\n🕐 {time_str}"
            await query.message.reply_text(
                match_label,
                reply_markup=InlineKeyboardMarkup(btn)
            )

    # MATCH ANALYSIS
    elif query.data.startswith("an_"):
        match_id = query.data.split("_")[1]
        
        await query.message.reply_text("🔍 Fetching lineup and analyzing positions...")
        
        lineup = scrape_lineup(match_id)
        
        if not lineup:
            await query.message.reply_text(
                "⏳ **Lineups not released yet**\n\n"
                "Lineups usually drop 60-90 minutes before kickoff.\n\n"
                "💡 Try again closer to match time!"
            )
            return
        
        # Update knowledge base
        update_player_knowledge(lineup)
        
        # Analyze position changes
        alerts = []
        normal_players = []
        
        for p in lineup:
            usual = get_usual_position(p['name'])
            
            if not usual:
                # First time seeing this player
                continue
            
            if usual != p['pos']:
                # Position changed
                usual_zone = POSITION_GROUPS.get(usual, 'M')
                current_zone = POSITION_GROUPS.get(p['pos'], 'M')
                
                insights = []
                confidence = "MEDIUM"
                
                # Defender playing forward
                if usual_zone == 'D' and current_zone in ['M', 'W', 'A']:
                    insights.append("✅ BACK: Shots on Target Over 0.5")
                    insights.append("✅ CONSIDER: Anytime Goalscorer")
                    insights.append("❌ AVOID: Fouls markets")
                    confidence = "HIGH"
                
                # Attacker/Winger playing defensive
                elif usual_zone in ['A', 'W'] and current_zone in ['D', 'M']:
                    insights.append("✅ BACK: Player to commit 2+ fouls")
                    insights.append("✅ CONSIDER: Player to be booked")
                    insights.append("❌ AVOID: Shots/Goals markets")
                    confidence = "HIGH"
                
                # Midfielder to attack
                elif usual_zone == 'M' and current_zone in ['A', 'W']:
                    insights.append("✅ BACK: Shots on Target")
                    insights.append("✅ CONSIDER: Anytime Goalscorer")
                    confidence = "MEDIUM"
                
                # Midfielder to defense
                elif usual_zone == 'M' and current_zone == 'D':
                    insights.append("✅ BACK: Fouls Over 1.5")
                    insights.append("❌ AVOID: Shots markets")
                    confidence = "MEDIUM"
                
                if insights:
                    alert_msg = (
                        f"🔥 **{confidence} CONFIDENCE**\n"
                        f"👤 **{p['name']}**\n"
                        f"📍 {usual} → {p['pos']} (OUT OF POSITION)\n\n"
                    )
                    for insight in insights:
                        alert_msg += f"{insight}\n"
                    
                    alerts.append(alert_msg)
            else:
                normal_players.append(p['name'])
        
        # Build response
        if alerts:
            result = "🚨 **BETTING OPPORTUNITIES DETECTED**\n\n"
            result += "\n━━━━━━━━━━━━━━━━━━━━\n\n".join(alerts)
            result += "\n━━━━━━━━━━━━━━━━━━━━\n\n"
            result += f"✅ {len(normal_players)} players in usual positions\n\n"
            result += "⚠️ *Always gamble responsibly*"
        else:
            result = (
                "✅ **All players in usual positions**\n\n"
                f"Analyzed {len(lineup)} players - no significant opportunities detected.\n\n"
                "💡 Position changes create the best betting edges!"
            )
        
        await query.message.reply_text(result, parse_mode='Markdown')

def main():
    """Start the bot"""
    logger.info("Starting Football Position Analyzer (FotMob Edition)...")
    
    # Start health check
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("✅ Health server started")
    
    # Build bot
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("✅ Bot started")
    app.run_polling()

if __name__ == '__main__':
    main()
