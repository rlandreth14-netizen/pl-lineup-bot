from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext
from pymongo import MongoClient
import os
import pandas as pd
from flask import Flask
import threading
import requests

MONGODB_URI = os.getenv('MONGODB_URI')
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')

# Your detect_oop function (unchanged)
def detect_oop(match_id):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    
    lineups = list(db.lineups.find({'match_id': int(match_id)}))
    if not lineups:
        return "No lineups found for this match. Add via MongoDB Compass."
    
    lineup_df = pd.DataFrame(lineups)
    
    players = list(db.players.find({'id': {'$in': lineup_df['player_id'].tolist()}}))
    players_df = pd.DataFrame(players)
    
    merged = lineup_df.merge(players_df[['id', 'web_name', 'position']], left_on='player_id', right_on='id')
    merged['is_oop'] = merged['position_x'] != merged['position_y']
    oop_players = merged[merged['is_oop']]
    
    if oop_players.empty:
        return "No OOP players in this match."
    
    insights = []
    for _, row in oop_players.iterrows():
        foul_delta = "+1.2 (more defensive duties)" if 'DEF' in row['position_x'] else "+0.5"
        shot_delta = "+0.8 (new role opportunities)" if 'MID' in row['position_x'] else "+0.3"
        insights.append(f"{row['web_name']} OOP ({row['position_y']} -> {row['position_x']}): Likely {foul_delta} fouls, {shot_delta} shots on target.")
    
    client.close()
    return "\n".join(insights)

async def update_data(update: Update, context: CallbackContext):
    await update.message.reply_text("Pulling latest FPL data... this might take 30-60 seconds.")
    
    base_url = "https://fantasy.premierleague.com/api/"
    try:
        bootstrap = requests.get(base_url + "bootstrap-static/").json()
        fixtures = requests.get(base_url + "fixtures/").json()
        
        players = pd.DataFrame(bootstrap['elements'])
        players = players[['id', 'web_name', 'element_type', 'minutes', 'goals_scored', 'assists', 'yellow_cards', 'total_points']]
        pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
        players['position'] = players['element_type'].map(pos_map)
        players_dict = players.to_dict('records')
        
        fixtures_df = pd.DataFrame(fixtures)
        fixtures_df = fixtures_df[['id', 'event', 'team_h', 'team_a', 'kickoff_time']]
        fixtures_dict = fixtures_df.to_dict('records')
        
        client = MongoClient(MONGODB_URI)
        db = client['premier_league']
        
        db.players.delete_many({})
        db.players.insert_many(players_dict)
        db.fixtures.delete_many({})
        db.fixtures.insert_many(fixtures_dict)
        
        client.close()
        
        await update.message.reply_text("FPL data updated! Players and fixtures pulled successfully. Now add lineups manually for testing.")
    except Exception as e:
        await update.message.reply_text(f"Error pulling data: {str(e)}")

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("Welcome, Ryan! Use /check <match_id> for OOP insights (e.g., /check 1). Ensure lineups are added to DB.")

async def check(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Provide match_id, e.g., /check 1")
        return
    match_id = context.args[0]
    insights = detect_oop(match_id)
    await update.message.reply_text(insights)

# Dummy Flask app to bind port for Render Web Service
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    # Run Flask in a thread
    threading.Thread(target=run_flask).start()
    
    # Run Telegram bot polling
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_data))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.run_polling()
