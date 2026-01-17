import os
import requests
import logging
import json
import threading
from datetime import datetime
from pymongo import MongoClient
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
PORT = int(os.getenv("PORT", 8000))

client = MongoClient(MONGO_URI)
db = client['football_bot']
player_collection = db['player_history']

LEAGUE_MAP = {"pl": 47, "championship": 48, "laliga": 87, "seriea": 55, "bundesliga": 54, "ligue1": 53}
POSITION_GROUPS = {'GK': 'G', 'CB': 'D', 'LCB': 'D', 'RCB': 'D', 'LB': 'D', 'RB': 'D', 'LWB': 'W', 'RWB': 'W', 'LM': 'W', 'RM': 'W', 'LW': 'W', 'RW': 'W', 'CDM': 'M', 'LDM': 'M', 'RDM': 'M', 'CM': 'M', 'LCM': 'M', 'RCM': 'M', 'CAM': 'M', 'AM': 'M', 'ST': 'A', 'CF': 'A'}

# --- HEALTH SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy")

def run_health_server():
    HTTPServer(('0.0.0.0', PORT), HealthCheckHandler).serve_forever()

# --- DATABASE ---
def update_player_knowledge(lineup_data):
    for p in lineup_data:
        player_collection.update_one({"name": p['name']}, {"$inc": {f"positions.{p['pos']}": 1}}, upsert=True)

def get_usual_position(player_name):
    player = player_collection.find_one({"name": player_name})
    if player and 'positions' in player:
        return max(player['positions'], key=player['positions'].get)
    return None

# --- RECURSIVE PLAYER EXTRACTOR ---
def find_players_in_json(obj):
    players = []
    if isinstance(obj, dict):
        # Look for objects that have both a name and a position
        has_name = 'name' in obj
        has_pos = 'position' in obj or 'positionShort' in obj
        
        if has_name and has_pos:
            name_val = obj.get('name')
            # Extract name from string or dict
            name = name_val.get('fullName') if isinstance(name_val, dict) else name_val
            pos = obj.get('positionShort
