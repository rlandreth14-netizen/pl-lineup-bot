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

# Available leagues for selection
LEAGUES = ["Premier League", "Championship", "La Liga", "Bundesliga", "Serie A"]

# --- 2. AI SCOUT BRAIN (With Fix for Error 429) ---

async def get_ai_response(prompt):
    """
    Asks Gemini for info. If it hits the 'Too Many Requests' limit, 
    it waits and tries again automatically.
    """
    for attempt in range(3):
        try:
            # Tell the AI exactly what today's date is for accuracy
            full_prompt = f"Today is Monday, January 19, 2026. {prompt}"
            
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=full_prompt
            )
            return response.text
        except Exception as e:
            # Check if it's a rate limit error (429)
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait_time = 10 * (attempt + 1)
                logging.warning(f"Limit hit. Waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            return f"❌ AI Error: {str(e)}"
    
    return "⚠️ AI is currently busy (Rate Limit). Please wait 30 seconds and try again."

# --- 3. BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f'league:{name}')] for name in LEAGUES]
    text = "⚽ *AI Football Scout*\nSelect a league to see tonight's fixtures (Jan 19):"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # Step 1: User selects a League
    if data.startswith('league:'):
        league_name = data.split(':')[1]
        await query.edit_message_text(f"🔍 Searching for {league_name} games today...")
        
        prompt = f"List the matches happening today (Jan 19, 2026) in the {league_name}. Return ONLY the match names separated by commas."
        match_data = await get_ai_response(prompt)
        
        # Split the comma-separated list into buttons
        matches = [m.strip() for m in match_data.split(',') if len(m.strip()) > 5]
        
        if not matches or "❌" in match_data:
            await query.edit_message_text(f"📝 No {league_name} matches found for tonight.")
            return

        keyboard = [[InlineKeyboardButton(m, callback_data=f"match:{m[:40]}")] for m in matches]
        await query.edit_message_text(f"📅 *{league_name} - Jan 19*\nSelect a match to scout:", 
                                      reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # Step 2: User selects a Match
    elif data.startswith('match:'):
        match_name = data.split(':', 1)[1]
        await query.edit_message_text(f"🧠 Scouting lineups for {match_name}...")
        
        prompt = (
            f"Provide a scout report for {match_name} today. "
            "1. Find any players in the starting lineup playing a more attacking role than usual. "
            "2. Give their last 5 game averages for 'Shots on Target' and 'Fouls'. "
            "3. Suggest a betting value based on this shift."
        )
        report = await get_ai_response(prompt)
        
        back_kb = [[InlineKeyboardButton("⬅️ Back to Leagues", callback_data="back_to_start")]]
        await query.edit_message_text(f"📋 *Scout Report*\n\n{report}", 
                                      reply_markup=InlineKeyboardMarkup(back_kb), parse_mode="Markdown")

    elif data == "back_to_start":
        # Return to main league list
        keyboard = [[InlineKeyboardButton(name, callback_data=f'league:{name}')] for name in LEAGUES]
        await query.edit_message_text("⚽ *Select a League:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# --- 4. EXECUTION ---

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Run a simple server for Render's health check
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    
    print("Bot is online...")
    application.run_polling()

if __name__ == '__main__':
    main()
