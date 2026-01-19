import os
import threading
import logging
import requests
import time
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from google import genai

# --- 1. SETUP ---
logging.basicConfig(level=logging.INFO)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

app = Flask(__name__)
@app.route('/')
def health(): return "Bot Active", 200

LEAGUES = {
    "4328": "Premier League",
    "4329": "Championship",
    "4335": "La Liga",
    "4331": "Bundesliga",
    "4332": "Serie A"
}

# --- 2. AI SCOUT LOGIC ---

async def get_single_match_report(match_name):
    """Analyze one specific match."""
    prompt = (
        f"You are a football scout. Analyze the match: {match_name}. "
        "1. Identify players starting in unusual or more attacking positions today. "
        "2. Provide their last 5 game stats for 'Shots on Target' and 'Fouls'. "
        "3. Keep it concise and use bullet points."
    )
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"❌ AI Error: {str(e)}"

# --- 3. BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f'league:{lid}')] for lid, name in LEAGUES.items()]
    await update.message.reply_text("⚽ *Select a League:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # Step 1: User selected a League -> Show Matches
    if data.startswith('league:'):
        league_id = data.split(':')[1]
        await query.edit_message_text(f"⏳ Fetching {LEAGUES[league_id]} fixtures...")
        
        url = f"https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php?id={league_id}"
        res = requests.get(url).json()
        matches = res.get('events', [])[:8] # Show up to 8 matches

        if not matches:
            await query.edit_message_text("❌ No upcoming matches found.")
            return

        # Create buttons for each match
        keyboard = []
        for m in matches:
            match_name = m['strEvent']
            # We pass the match name in the callback data (shortened to stay under 64 chars)
            keyboard.append([InlineKeyboardButton(match_name, callback_data=f"match:{match_name[:40]}")])
        
        await query.edit_message_text("👉 *Select a match to analyze:*", 
                                      reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # Step 2: User selected a Match -> Run AI Analysis
    elif data.startswith('match:'):
        match_selected = data.split(':', 1)[1]
        await query.edit_message_text(f"🔍 Analyzing {match_selected}...")
        
        report = await get_single_match_report(match_selected)
        
        # Add a 'Back' button to return to league selection
        back_kb = [[InlineKeyboardButton("⬅️ Back to Leagues", callback_data="back_to_start")]]
        await query.edit_message_text(f"📋 *Scout Report: {match_selected}*\n\n{report}", 
                                      reply_markup=InlineKeyboardMarkup(back_kb), parse_mode="Markdown")

    elif data == "back_to_start":
        keyboard = [[InlineKeyboardButton(name, callback_data=f'league:{lid}')] for lid, name in LEAGUES.items()]
        await query.edit_message_text("⚽ *Select a League:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# --- 4. START ---

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Start Flask for Render
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    
    print("Bot is live and waiting for selection...")
    application.run_polling()

if __name__ == '__main__':
    main()
