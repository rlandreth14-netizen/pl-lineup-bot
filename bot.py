import os
import asyncio
import logging
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Configuration - CHECK THESE IN YOUR HOSTING DASHBOARD
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def debug_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """This function tests the raw connection and reports exactly what is wrong."""
    
    if not API_FOOTBALL_KEY or API_FOOTBALL_KEY == "YOUR_API_FOOTBALL_KEY":
        await update.message.reply_text("❌ ERROR: API Key is missing or set to default placeholder in Environment Variables.")
        return

    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    url = f"{API_FOOTBALL_BASE}/status" # Simple endpoint to check key health
    
    await update.message.reply_text(f"Attempting to contact: {url}\nKey (masked): {API_FOOTBALL_KEY[:5]}***")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                status = response.status
                text = await response.text()
                
                if status == 200:
                    await update.message.reply_text(f"✅ SUCCESS! API reached.\nResponse: {text[:100]}")
                else:
                    await update.message.reply_text(f"⚠️ API responded with Error {status}.\nBody: {text}")
    except Exception as e:
        await update.message.reply_text(f"🔥 CRITICAL ERROR: Could not even reach the API server.\nDetails: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is alive! Use /debug_api to see why the API isn't responding.")

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("No Telegram Token found!")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("debug_api", debug_api))
    
    print("Bot is polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
