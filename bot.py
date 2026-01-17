import os
import requests
import logging
import json
from bs4 import BeautifulSoup
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LEAGUE_MAP = {
    "pl": 47, "championship": 48, "laliga": 87, 
    "seriea": 55, "bundesliga": 54, "ligue1": 53
}

# --- SCRAPER LOGIC ---
def get_league_matches(league_id):
    """Automatically finds today's match links for a specific league on FotMob."""
    url = f"https://www.fotmob.com/api/leagues?id={league_id}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        matches = data.get('matches', {}).get('allMatches', [])
        
        today = datetime.now().strftime('%Y-%m-%d')
        today_matches = []
        
        for m in matches:
            # FotMob date format is usually 'Sun, Aug 11' or ISO strings
            # We filter for 'today' or 'tomorrow' matches
            if today in m.get('status', {}).get('utcTime', ''):
                today_matches.append({
                    'home': m['home']['name'],
                    'away': m['away']['name'],
                    'id': m['id'],
                    'time': m['status']['utcTime'][11:16]
                })
        return today_matches
    except Exception as e:
        logging.error(f"League Fetch Error: {e}")
        return []

def scrape_lineup(match_id):
    """Scrapes the actual lineup using the match ID found above."""
    url = f"https://www.fotmob.com/matches/{match_id}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.content, 'html.parser')
        script = soup.find('script', id='__NEXT_DATA__')
        data = json.loads(script.string)
        content = data['props']['pageProps']['content']
        
        if 'lineup' not in content: return None
        
        results = []
        for side in ['home', 'away']:
            players = content['lineup'][side]['starting']
            for p in players:
                results.append({
                    'name': p['name']['fullName'],
                    'pos': p.get('positionStringShort', '??')
                })
        return results
    except:
        return None

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"⚽ {k.upper()}", callback_data=f"list_{v}")] for k, v in LEAGUE_MAP.items()]
    await update.message.reply_text("🔍 **Select a League to find today's edges:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("list_"):
        l_id = query.data.split("_")[1]
        matches = get_league_matches(l_id)
        if not matches:
            await query.edit_message_text("📭 No matches found for today in this league.")
            return
        
        for m in matches:
            text = f"🏟 {m['home']} vs {m['away']}\n⏰ {m['time']} UTC"
            btn = [[InlineKeyboardButton("📋 Analyze Lineup", callback_data=f"analyze_{m['id']}")]]
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btn))

    elif query.data.startswith("analyze_"):
        m_id = query.data.split("_")[1]
        lineup = scrape_lineup(m_id)
        if not lineup:
            await query.message.reply_text("⏳ Lineups not out yet (Check 60m before KO).")
        else:
            # Insert your analysis logic here (usual vs current position)
            res = "🚨 **Analysis Result:**\n" + "\n".join([f"• {p['name']} ({p['pos']})" for p in lineup[:5]]) # showing first 5 for brevity
            await query.message.reply_text(res)

# (Add your HealthCheck and main() boilerplate here as before)
