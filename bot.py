import os
import requests
import logging
import asyncio
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from google import genai

# --- 1. INITIAL SETUP ---
logging.basicConfig(level=logging.INFO)
GEMINI_CLIENT = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# API Keys
TSDB_KEY = "1"  # Public test key for TheSportsDB
SM_KEY = os.environ.get("SPORTMONKS_API_KEY")

# Flask for Render Health Checks
app = Flask(__name__)
@app.route('/')
def health(): return "Bot Online", 200

# --- 2. DATA FETCHING LOGIC ---

def get_tsdb_fixtures(league_name):
    """Fetch today's games from TheSportsDB."""
    # Note: English Premier League, Spanish La Liga, etc.
    url = f"https://www.thesportsdb.com/api/v1/json/{TSDB_KEY}/eventsday.php?d=2026-01-19&l={league_name}"
    try:
        r = requests.get(url, timeout=10).json()
        return r.get('events') or []
    except Exception as e:
        logging.error(f"TSDB Error: {e}")
        return []

def get_sm_lineup_data(match_query):
    """Fetch lineup and coordinate data from Sportmonks."""
    url = f"https://api.sportmonks.com/v3/football/fixtures/search/{match_query}"
    params = {
        "api_token": SM_KEY,
        "include": "lineups.player;lineups.position;lineups.details.type;formations"
    }
    try:
        r = requests.get(url, params=params, timeout=15).json()
        return r.get('data')
    except Exception as e:
        logging.error(f"Sportmonks Error: {e}")
        return None

# --- 3. TELEGRAM BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Select a League."""
    keyboard = [
        [InlineKeyboardButton("Premier League", callback_data='league:English Premier League')],
        [InlineKeyboardButton("La Liga", callback_data='league:Spanish La Liga')],
        [InlineKeyboardButton("Serie A", callback_data='league:Italian Serie A')]
    ]
    text = "⚽ *Football AI Scout*\nSelect a league to see today's fixtures:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # --- ACTION 1: LIST MATCHES ---
    if data.startswith('league:'):
        league_name = data.split(':')[1]
        await query.edit_message_text(f"📡 Searching for {league_name} matches...")
        
        matches = get_tsdb_fixtures(league_name)
        
        if not matches:
            # Fallback for TSDB '1' Key limitations
            keyboard = [[InlineKeyboardButton("Manual Test: Brighton vs Bournemouth", callback_data="match:Brighton vs Bournemouth")]]
            await query.edit_message_text("❌ No live data for Key 1. Try a manual test?", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        keyboard = []
        for m in matches:
            m_name = m['strEvent']
            keyboard.append([InlineKeyboardButton(m_name, callback_data=f"match:{m_name}")])
        
        await query.edit_message_text("📅 *Today's Fixtures:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # --- ACTION 2: ANALYZE WITH AI ---
    elif data.startswith('match:'):
        match_name = data.split(':')[1]
        await query.edit_message_text(f"🔬 Pulling Sportmonks data for {match_name}...")
        
        # 1. Fetch Real Data
        raw_data = get_sm_lineup_data(match_name)
        
        if not raw_data:
            await query.edit_message_text("⚠️ Sportmonks couldn't find this match. Ensure the league is in your subscription.")
            return

        # 2. AI Analysis (The only AI call)
        await query.edit_message_text("🧠 Gemini is analyzing tactical shifts...")
        
        # Mandatory gap to avoid Gemini 429
        await asyncio.sleep(3)
        
        prompt = f"""
        Analyze this football match data: {raw_data}
        1. Identify any out-of-position players based on 'formation_field' or 'position'.
        2. Give 2 betting tips (e.g. Shots on Target) for players in attacking roles.
        3. Explain the tactical value briefly.
        """
        
        try:
            response = GEMINI_CLIENT.models.generate_content(model="gemini-2.0-flash", contents=prompt)
            final_text = f"📋 *AI Scout: {match_name}*\n\n{response.text}"
        except Exception as e:
            final_text = "⏳ AI is busy. Please click the match again in 10 seconds."

        keyboard = [[InlineKeyboardButton("⬅️ Back to Leagues", callback_data="back")]]
        await query.edit_message_text(final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "back":
        await start(update, context)

# --- 4. STARTUP ---

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

if __name__ == '__main__':
    # Start Health Check Thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Start Telegram Bot
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    print("🚀 Scout Bot is Live with Dual-API support!")
    application.run_polling()
