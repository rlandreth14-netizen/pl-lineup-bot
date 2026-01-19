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

LEAGUES = ["Premier League", "Championship", "La Liga", "Serie A"]

# --- 2. THE SLOW & STEADY AI BRAIN ---

async def ask_ai(prompt):
    """Simple, single-task request to Gemini."""
    try:
        # Give the AI a 2-second breather between any clicks to respect the free tier
        time.sleep(2) 
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=f"Today is Monday, Jan 19, 2026. {prompt}"
        )
        return response.text
    except Exception as e:
        if "429" in str(e):
            return "⚠️ System busy. Please wait 10 seconds and click again."
        return f"❌ Error: {str(e)}"

# --- 3. STEP-BY-STEP HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(l, callback_data=f'step1:{l}')] for l in LEAGUES]
    text = "⚽ *Step 1: Choose League*\n(Searching only for fixtures)"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_steps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # STEP 1: Get Fixtures Only
    if data.startswith('step1:'):
        league = data.split(':')[1]
        await query.edit_message_text(f"⏳ Finding {league} games...")
        
        prompt = f"List only the match names for today in the {league}. Separate by commas."
        res = await ask_ai(prompt)
        
        matches = [m.strip() for m in res.split(',') if len(m.strip()) > 5]
        if not matches:
            await query.edit_message_text("No games found. Try /start again.")
            return

        keyboard = [[InlineKeyboardButton(m, callback_data=f"step2:{m[:40]}")] for m in matches]
        await query.edit_message_text(f"⚽ *Step 2: Select Match*\nFound these games:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # STEP 2: Find Out-of-Position Players Only
    elif data.startswith('step2:'):
        match_name = data.split(':', 1)[1]
        await query.edit_message_text(f"🔎 Checking lineups for {match_name}...")
        
        prompt = f"Look at the confirmed lineups for {match_name}. List any players starting in a different or more attacking position than usual. If none, say 'No major shifts'."
        lineup_info = await ask_ai(prompt)
        
        # We store the match name in the button for the final step
        keyboard = [[InlineKeyboardButton("✅ Get Betting Insights", callback_data=f"step3:{match_name}")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="back")]]
        
        await query.edit_message_text(f"📋 *Positional Shifts*\n\n{lineup_info}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # STEP 3: Final Analysis & Betting Value
    elif data.startswith('step3:'):
        match_name = data.split(':', 1)[1]
        await query.edit_message_text(f"📊 Calculating value for {match_name}...")
        
        prompt = f"Based on the attacking shifts in {match_name}, provide 1-2 betting insights (Shots on Target or Fouls) using their recent stats."
        insights = await ask_ai(prompt)
        
        keyboard = [[InlineKeyboardButton("🔄 Start Over", callback_data="back")]]
        await query.edit_message_text(f"💰 *Betting Insights*\n\n{insights}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "back":
        await start(update, context)

# --- 4. RUN ---

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_steps))
    
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    application.run_polling()

if __name__ == '__main__':
    main()
