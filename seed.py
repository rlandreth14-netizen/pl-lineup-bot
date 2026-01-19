import requests
from pymongo import MongoClient
from datetime import datetime, timedelta

# --- CONFIG ---
MONGO_URI = mongodb+srv://rlandreth14_db_user:J5r1iKoAXgPjsxVW@pl-lineup-bot.tuw2970.mongodb.net/?appName=pl-lineup-bot
client = MongoClient(MONGO_URI)
db = client['football_bot']
player_collection = db['player_history']

LEAGUES = [47, 48, 87, 55, 54, 53] # PL, Championship, La Liga, etc.

def find_players_in_json(obj):
    players = []
    if isinstance(obj, dict):
        name_data = obj.get('name')
        pos = obj.get('positionShort') or obj.get('position')
        if name_data and pos:
            full_name = name_data.get('fullName') if isinstance(name_data, dict) else name_data
            if isinstance(full_name, str) and len(str(pos)) <= 3:
                players.append({'name': full_name, 'pos': pos})
        for v in obj.values():
            players.extend(find_players_in_json(v))
    elif isinstance(obj, list):
        for item in obj:
            players.extend(find_players_in_json(item))
    return players

def seed_past_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # We will look at the last 14 days of matches
    for i in range(14):
        date_str = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
        print(f"📅 Processing Date: {date_str}...")
        
        for league_id in LEAGUES:
            url = f"https://www.fotmob.com/api/leagues?id={league_id}&date={date_str}"
            try:
                data = requests.get(url, headers=headers).json()
                
                # Extract Match IDs
                match_ids = []
                def get_ids(obj):
                    if isinstance(obj, dict):
                        if 'id' in obj and 'home' in obj and 'away' in obj:
                            match_ids.append(obj['id'])
                        for v in obj.values(): get_ids(v)
                    elif isinstance(obj, list):
                        for item in obj: get_ids(item)
                
                get_ids(data)
                
                for m_id in match_ids[:10]: # Limit to 10 matches per league per day to avoid rate limits
                    m_url = f"https://www.fotmob.com/api/matchDetails?matchId={m_id}"
                    m_data = requests.get(m_url, headers=headers).json()
                    players = find_players_in_json(m_data.get('content', {}).get('lineup', {}))
                    
                    for p in players:
                        player_collection.update_one(
                            {"name": p['name']}, 
                            {"$inc": {f"positions.{p['pos']}": 1}}, 
                            upsert=True
                        )
                    print(f"   ✅ Processed Match {m_id}")
            except Exception as e:
                print(f"   ❌ Error on league {league_id}: {e}")

if __name__ == "__main__":
    seed_past_data()
    print("🚀 Database Seeded! Your bot is now an expert.")
