import os
import threading
import logging
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

# We keep this list just to give the user buttons to click
LEAGUES = ["Premier League", "Championship", "La Liga", "Bundesliga", "Serie A"]

# --- 2. AI BRAIN ---

async def get_ai_response(prompt):
    """Simple wrapper to talk to Gemini with retry logic for the Free Tier."""
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=f"Current Date: Monday, Jan 19, 2026. {prompt}"
            )
            return response.text
        except Exception as e:
            if "429" in str(e):
                time.sleep(5)
                continue
            return f"❌ AI Error: {str(e)}"
    return "❌ Rate limit exceeded. Try again in a moment."

# --- 3. BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f'league:{name}')] for name in LEAGUES]
    await update.message.reply_text("⚽ *AI Football Scout*\nChoose a league to see today's matches:", 
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # Step 1: Find Today's Matches via AI
    if data.startswith('league:'):
        league_name = data.split(':')[1]
        await query.edit_message_text(f"🔍 AI is searching for today's {league_name} fixtures...")
        
        prompt = f"List the matches happening today in the {league_name}. Return ONLY the match names separated by commas (e.g. Team A vs Team B, Team C vs Team D)."
        match_data = await get_ai_response(prompt)
        
        matches = [m.strip() for m in match_data.split(',') if len(m.strip()) > 5]
        
        if not matches or "❌" in match_data:
            await query.edit_message_text(f"📝 No {league_name} matches found for today (Jan 19).")
            return

        keyboard = [[InlineKeyboardButton(m, callback_data=f"match:{m[:40]}")] for m in matches]
        await query.edit_message_text(f"📅 *{league_name} Fixtures (Jan 19)*\nSelect a match to scout:", 
                                      reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # Step 2: Analyze the Specific Match via AI
    elif data.startswith('match:'):
        match_name = data.split(':', 1)[1]
        await query.edit_message_text(f"🧠 AI is scouting lineups for {match_name}...")
        
        prompt = (
            f"Analyze the match {match_name} for today. "
            "1. Identify any out-of-position players (e.g. a CB playing DM, or a standard Fullback playing as a Winger). "
            "2. For these players, provide their average 'Shots on Target' and 'Fouls' from their last 5 games. "
            "3. Explain why this shift provides betting value."
        )
        report = await get_ai_response(prompt)
        
        back_kb = [[InlineKeyboardButton("⬅️ Back to Leagues", callback_data="back_to_start")]]
        await query.edit_message_text(f"📋 *Scout Report*\n\n{report}", 
                                      reply_markup=InlineKeyboardMarkup(back_kb), parse_mode="Markdown")

    elif data == "back_to_start":
        await start(update, context)

# --- 4. START ---

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    application.run_polling()

if __name__ == '__main__':
    main()
