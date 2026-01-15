import os
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", 8000))  # Koyeb uses port 8000 by default

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- KOYEB HEALTH CHECK WORKAROUND ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    """Answers Koyeb's pings so the bot doesn't get killed."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")

    def log_message(self, format, *args):
        return # Keeps logs clean from constant pings

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    logger.info(f"Health check server started on port {PORT}")
    server.serve_forever()

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is online and bypassing Koyeb health checks!")

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This is where your football API logic goes
    await update.message.reply_text("Searching for live matches...")

# --- MAIN ENGINE ---
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN found in environment variables!")
        return

    # 1. Start the Health Server in a background thread
    threading.Thread(target=run_health_server, daemon=True).start()

    # 2. Start the Telegram Bot
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("live", live))

    logger.info("Bot is starting polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
