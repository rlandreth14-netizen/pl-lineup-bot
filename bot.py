import os
import threading
import logging
import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
# New import for the modern SDK
from google import genai 

# --- 1. SETUP & CONFIG ---
logging.basicConfig(level=logging.INFO)

# Initialize the new Google Gen AI Client
# Ensure GEMINI_API_KEY is in your Render environment variables
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

# --- 2. THE AI BRAIN ---

async def get_ai_scout_report(match_name):
    """
    Uses the new SDK to consult Gemini.
    """
    prompt = f"Analyze the match {match_name}. Identify players starting in unusual positions today and give their last 5 game stats for Shots on Target and Fouls."
    
    try:
        # Using the new Client.models.generate_content method
        response = client.models.generate_content(
            model="gemini-2.0-flash", # Accessing the latest model
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"❌ AI Analysis Error: {str(e)}"

# --- 3. BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f'league:{lid}')] for lid, name in LEAGUES.items()]
    await update.message.reply_text(
        "📊 *Football IQ Scout Online*\nChoose a league to analyze:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith('league:'):
        league_id = query.data.split(':')[1]
        league_name = LEAGUES.get(league_id)
        await query.edit_message_text(f"🔍 Consulting Gemini 2.0 for {league_name}...")
        
        # Fetching data from TheSportsDB
        url = f"https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php?id={league_id}"
        try:
            res = requests.get(url).json()
            matches = res.get('events', [])[:2] # Analyze top 2 upcoming matches
            
            if not matches:
                await query.edit_message_text("❌ No matches found.")
                return

            full_report = f"📋 *SCOUT REPORT: {league_name}*\n\n"
            for m in matches:
                report = await get_ai_scout_report(m['strEvent'])
                full_report += f"*Match:* {m['strEvent']}\n{report}\n\n"
            
            await query.edit_message_text(full_report[:4000], parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"❌ Error fetching matches: {str(e)}")

# --- 4. STARTUP ---

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    application.run_polling()

if __name__ == '__main__':
    main()
