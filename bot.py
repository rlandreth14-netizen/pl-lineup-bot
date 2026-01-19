import os
import logging
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types

# --- 1. LOGGING SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Gemini Client
GEMINI_CLIENT = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# --- 2. FLASK SERVER (Render Health Check) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is Online!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- 3. THE SCOUTING LOGIC ---

async def analyze_lineup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """General handler for both Photos and Image Files."""
    
    # Check if user sent a Photo OR a Document (Image)
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        logger.info("Detected regular photo upload.")
    elif update.message.document and update.message.document.mime_type.startswith('image/'):
        file_id = update.message.document.file_id
        logger.info("Detected image sent as a file/document.")
    else:
        # If it's something else (video, text, etc.), ignore it
        return

    status_msg = await update.message.reply_text("🔎 *Scouting the lineup...* (Processing high-res)", parse_mode="Markdown")

    try:
        # Download the file
        new_file = await context.bot.get_file(file_id)
        image_bytes = await new_file.download_as_bytearray()
        
        # Expert Prompt
        prompt = """
        You are a football scout. Analyze this lineup screenshot:
        1. Name the teams and the formation (e.g. 4-4-2).
        2. Identify any surprising tactical shifts (e.g. a winger playing wing-back).
        3. Suggest 2 betting 'props' (shots, fouls, cards) based on today's roles.
        """

        response = GEMINI_CLIENT.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt
            ]
        )

        await status_msg.edit_text(f"📋 *SCOUT REPORT*\n\n{response.text}", parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        await status_msg.edit_text("❌ *Error:* I couldn't analyze this image. Ensure it's a clear screenshot of a lineup.")

# --- 4. STARTUP ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚽ *Lineup Scout is Live!*\nSend me any football lineup screenshot.")

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    
    TOKEN = os.environ.get("BOT_TOKEN")
    application = ApplicationBuilder().token(TOKEN).build()
    
    # HANDLERS: This now listens for Photos AND Documents that are images
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, analyze_lineup))
    
    logger.info("Bot is polling...")
    application.run_polling()
