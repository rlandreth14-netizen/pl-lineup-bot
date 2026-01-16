import os, requests, logging, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("api_key")

# --- HEALTH CHECK (Required for Koyeb) ---
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def run_srv():
    HTTPServer(('0.0.0.0', int(os.getenv("PORT", 8000))), HealthCheck).serve_forever()
threading.Thread(target=run_srv, daemon=True).start()

logging.basicConfig(level=logging.INFO)

# --- THE FIX: Hardcoded Historical Data ---
def get_data(url_path):
    headers = {'x-apisports-key': API_KEY}
    url = f"https://v3.football.api-sports.io/{url_path}"
    logging.info(f"Calling: {url}")
    r = requests.get(url, headers=headers).json()
    return r.get('response', [])

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    # We only show PL for this specific test
    btn = [[InlineKeyboardButton("🎯 Load Jan 20, 2024 Matches", callback_data="test_list")]]
    await u.message.reply_text("🕒 **Time Machine Active**\nTesting logic on 2024 data.", reply_markup=InlineKeyboardMarkup(btn))

async def handle_btns(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    if q.data == "test_list":
        # MANUALLY FORCED URL TO BYPASS ANY ERRORS
        fixtures = get_
