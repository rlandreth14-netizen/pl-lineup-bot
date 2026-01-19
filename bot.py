import os
import logging
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types

# --- 1. CONFIG & LOGGING ---
logging.basicConfig(level=logging.INFO)
GEMINI_CLIENT = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# --- 2. FLASK HEALTH-CHECK (The fix for Render) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    """Tells Render the bot is alive."""
    return "Football Scout Bot is Online!", 200

def run_flask():
    # Render provides the PORT variable automatically
    port = int(os.environ.get("PORT", 10000))
    # Must bind to 0.0.0.0 for Render to see it
    app.run(host='0.0.0.0', port=port)

# --- 3. TELEGRAM BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message."""
    welcome_text = (
        "📸 *Lineup Scout Ready!*\n\n"
        "Just send me a *screenshot* of a lineup (from Flashscore, ESPN, or Twitter) "
        "and I will analyze the tactics and find betting value for you."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes images sent to the bot."""
    # Get the highest resolution version of the photo
    photo_file = await update.message.photo[-1].get_file()
    
    # Download image into memory
    image_bytes = await photo_file.download_as_bytearray()
    
    status_msg = await update.message.reply_text("🧐 Lineup detected! Analyzing...")

    # The AI Prompt
    prompt = """
    Analyze this football lineup screenshot:
    1. Identify the team names and the formation.
    2. Spot any 'out of position' players or tactical surprises.
    3. Suggest 2 betting 'props' (e.g., Shots on Target, Fouls, or Cards) based on player roles.
    4. Briefly explain the tactical value.
    """
    
    try:
        # Use Gemini 2.0 Flash for vision
        response = GEMINI_CLIENT.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt
            ]
        )
        await status_msg.edit_text(f"📋 *Scout Report*\n\n{response.text}", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Gemini Error: {e}")
        await status_msg.edit_text("❌ Sorry, I couldn't read that image. Please make sure the text is clear!")

# --- 4. MAIN EXECUTION ---

if __name__ == '__main__':
    # Start the Health-Check server in a separate thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Start the Telegram Bot
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        print("Error: No BOT_TOKEN found in environment variables!")
    else:
        application = ApplicationBuilder().token(TOKEN).build()
        
        # Handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
        
        print("🚀 Bot is starting...")
        application.run_polling()
