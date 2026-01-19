import os
import threading
import logging
import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
import google.generativeai as genai

# --- 1. SETUP & CONFIG ---
logging.basicConfig(level=logging.INFO)

# Initialize Gemini AI
# Make sure GEMINI_API_KEY is set in your Render environment variables
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')

# Flask for Render Health Checks (prevents bot from sleeping)
app = Flask(__name__)
@app.route('/')
def health(): return "Bot Active", 200

# Supported Leagues
LEAGUES = {
    "4328": "Premier League",
    "4329": "Championship",
    "4335": "La Liga",
    "4331": "Bundesliga",
    "4332": "Serie A"
}

# --- 2. THE AI BRAIN ---

async def get_ai_scout_report(match_name, raw_data):
    """
    Sends raw match/lineup data to Gemini.
    Gemini uses its training data to identify positional shifts and player stats.
    """
    prompt = f"""
    You are a professional football betting scout. 
    Analyze the following lineup/match info for: {match_name}
    
    Data: {raw_data}
    
    Your Task:
    1. Identify any player starting in a position more ATTACKING than usual (e.g. CB playing DM, or CM playing LW/RW).
    2. For these players, provide their average 'Shots on Target' and 'Fouls' from their last 5 games.
    3. If no major shifts are found, suggest a 'Player to Watch' based on current form.
    4. Format the output clearly with bold headers and bullet points.
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"❌ AI Analysis Error: {str(e)}"

# --- 3. BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f'league:{lid}')] for lid, name in LEAGUES.items()]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📊 *Football IQ Scout Online*\nChoose a league to analyze today's lineups:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith('league:'):
        league_id = query.data.split(':')[1]
        league_name = LEAGUES.get(league_id)
        
        await query.edit_message_text(f"🔍 Fetching {league_name} data and consulting Gemini AI...")
        
        # We fetch current events for the league to get match names
        # You can use your existing TheSportsDB logic here to get 'raw_data'
        url = f"https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php?id={league_id}"
        try:
            res = requests.get(url).json()
            matches = res.get('events', [])[:3] # Analyze next 3 matches
            
            if not matches:
                await query.edit_message_text("❌ No upcoming matches found for this league today.")
                return

            full_report = f"📋 *SCOUT REPORT: {league_name}*\n\n"
            for m in matches:
                match_name = m['strEvent']
                # We send the match name to Gemini; it uses its internal knowledge for the analysis
                report = await get_ai_scout_report(match_name, "Analyze the confirmed/predicted lineup for this match.")
                full_report += f"--- \n*Match:* {match_name}\n{report}\n\n"
            
            await query.edit_message_text(full_report[:4000], parse_mode="Markdown")
            
        except Exception as e:
            await query.edit_message_text(f"❌ Error: Could not fetch match data. {str(e)}")

# --- 4. STARTUP ---

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Run Flask in a background thread for Render
    threading.Thread(target=run_flask, daemon=True).start()
    
    logging.info("Bot is running...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
