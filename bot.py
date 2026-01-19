import os
import logging
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types

# --- 1. CONFIGURATION & LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Initialize Gemini Client
GEMINI_CLIENT = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# --- 2. FLASK SERVER (For Render Health Checks) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Football Scout Bot is Online!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- 3. BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message."""
    welcome_text = (
        "⚽️ *Football Lineup Scout Ready!*\n\n"
        "Send me a *screenshot* from Flashscore, ESPN, or a pitch diagram.\n\n"
        "💡 *Tip:* For best results, send the image as a 'File' to avoid compression!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Downloads high-res image and sends to Gemini for scouting analysis."""
    
    # Send a placeholder message
    status_msg = await update.message.reply_text("🧐 *Analyzing lineup... please wait.*", parse_mode="Markdown")

    try:
        # Get the largest version of the photo (last in the list)
        photo_file = await update.message.photo[-1].get_file()
        
        # Download photo directly into memory
        image_bytes = await photo_file.download_as_bytearray()
        
        # The 'Scout Prompt' specifically tuned for football screenshots
        scout_prompt = """
        You are a Professional Football Tactical Analyst. 
        Attached is a lineup screenshot (likely from Flashscore or a sports app).
        
        1. Identify both teams and the match.
        2. Read the starting XI for both teams. 
        3. Describe the tactical formation (e.g., 4-3-3, 3-5-2).
        4. Provide 3 specific 'Betting Insights' based on individual player roles (e.g., 'Target player X for 2+ shots' or 'Midfielder Y for a card').
        
        If the image is blurry, extract as much as possible and warn the user.
        """

        # Call Gemini 2.0 Flash (Fast & Great Vision)
        response = GEMINI_CLIENT.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                scout_prompt
            ]
        )

        # Update the message with results
        await status_msg.edit_text(f"📝 *SCOUT REPORT*\n\n{response.text}", parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error processing image: {e}")
        await status_msg.edit_text("❌ *Error:* I couldn't read the image. Make sure the player names are visible and try again.")

# --- 4. MAIN EXECUTION ---

if __name__ == '__main__':
    # Run Flask in a background thread so Render is happy
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Start the Telegram Bot using Long Polling
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        logging.error("No BOT_TOKEN found! Check your environment variables.")
    else:
        application = ApplicationBuilder().token(TOKEN).build()
        
        # Add Handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        
        logging.info("Bot is starting...")
        application.run_polling()
