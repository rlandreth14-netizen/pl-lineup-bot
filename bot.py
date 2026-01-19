import os
import requests
import logging
import asyncio
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from google import genai

# --- 1. CONFIG ---
logging.basicConfig(level=logging.INFO)
GEMINI_CLIENT = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

SM_KEY = os.environ.get("SPORTMONKS_API_KEY")
SM_BASE_URL = "https://api.sportmonks.com/v3/football"

# Flask for Render health check
app = Flask(__name__)
@app.route('/')
def health(): return "Bot Online", 200

# --- 2. SPORTMONKS HELPERS ---

def get_sm_data(endpoint, params=None):
    """Generic helper for Sportmonks API v3."""
    if params is None: params = {}
    params['api_token'] = SM_KEY
    try:
        url = f"{SM_BASE_URL}/{endpoint}"
        r = requests.get(url, params=params, timeout=15)
        return r.json().get('data', [])
    except Exception as e:
        logging.error(f"Sportmonks Error: {e}")
        return []

# --- 3. BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # These are the two major leagues usually free on Sportmonks
    keyboard = [
        [InlineKeyboardButton("Scottish Premiership", callback_data='league:501')],
        [InlineKeyboardButton("Danish Superliga", callback_data='league:271')]
    ]
    text = "⚽ *Sportmonks Free Scout*\nSelect a league to analyze today's matches:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # STEP 1: Show today's matches for the selected league
    if data.startswith('league:'):
        league_id = data.split(':')[1]
        await query.edit_message_text("📅 Fetching today's fixtures...")
        
        # Endpoint for fixtures by league and today's date
        fixtures = get_sm_data(f"fixtures/date/2026-01-19", params={"leagues": league_id})
        
        if not fixtures:
            await query.edit_message_text("No matches scheduled for today in this league.")
            return

        keyboard = []
        for f in fixtures:
            name = f.get('name', 'Unknown Match')
            keyboard.append([InlineKeyboardButton(name, callback_data=f"match:{f['id']}")])
        
        await query.edit_message_text("🏟 *Today's Games:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # STEP 2: Analyze Lineups with Gemini
    elif data.startswith('match:'):
        fixture_id = data.split(':')[1]
        await query.edit_message_text("🔬 Pulling lineups & player stats...")

        # We "include" lineups, player details, and their position coordinates
        match_details = get_sm_data(f"fixtures/{fixture_id}", params={
            "include": "lineups.player;lineups.position;scores"
        })

        if not match_details:
            await query.edit_message_text("⚠️ Could not load lineup data for this match.")
            return

        await query.edit_message_text("🧠 Gemini is scout-reporting...")
        
        # Analysis Prompt
        prompt = f"""
        Analyze this Sportmonks fixture data: {match_details}
        1. Identify the tactical formation for both teams.
        2. Spot 2 players who are key attacking threats based on their positions.
        3. Provide 2 'Prop' betting suggestions (e.g. Shots on Target or Fouls).
        Keep it concise and professional.
        """
        
        try:
            # 5s buffer for Free Tier Gemini
            await asyncio.sleep(5)
            response = GEMINI_CLIENT.models.generate_content(model="gemini-2.0-flash", contents=prompt)
            result = f"📋 *AI Scout Report*\n\n{response.text}"
        except Exception:
            result = "⏳ AI is rate-limited. Try clicking the match again in 10 seconds."

        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
        await query.edit_message_text(result, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "back":
        await start(update, context)

# --- 4. RUN ---
def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.run_polling()
