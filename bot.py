import os
import io
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types

# --- 1. SETUP ---
logging.basicConfig(level=logging.INFO)
GEMINI_CLIENT = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# --- 2. VISION HANDLER ---

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when you send a photo to the bot."""
    # Get the photo with the highest resolution
    photo_file = await update.message.photo[-1].get_file()
    
    # Download the image into memory (no need to save to disk)
    image_bytes = await photo_file.download_as_bytearray()
    
    await update.message.reply_text("🧐 I see the lineup! Analyzing tactics and betting value...")

    # Send to Gemini
    prompt = """
    I have provided a screenshot of a football lineup. 
    1. List the key players and the formation.
    2. Identify any 'out of position' players or tactical surprises.
    3. Suggest 2 betting props (Shots, Fouls, or Cards) based on these roles.
    4. Mention if any major star is missing from the XI.
    """
    
    try:
        # Gemini 2.0 Flash is incredibly fast at reading text from images
        response = GEMINI_CLIENT.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt
            ]
        )
        await update.message.reply_text(f"📋 *Scout Report*\n\n{response.text}", parse_mode="Markdown")
    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ Sorry, I couldn't process that image. Try a clearer screenshot!")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 *Lineup Scout Ready!*\nJust send me a screenshot of any lineup (from an app or Twitter) and I'll analyze it for you.")

# --- 3. RUN ---
if __name__ == '__main__':
    application = ApplicationBuilder().token(os.environ.get("BOT_TOKEN")).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    application.run_polling()
