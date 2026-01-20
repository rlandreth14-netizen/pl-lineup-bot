import requests
import pandas as pd
from pymongo import MongoClient
import os

# Your MongoDB URI (from env for security)
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb+srv://youruser:yourpass@cluster0.abc.mongodb.net/')  # Replace with yours

def fetch_fpl_data():
    base_url = "https://fantasy.premierleague.com/api/"
    bootstrap = requests.get(base_url + "bootstrap-static/").json()
    fixtures = requests.get(base_url + "fixtures/").json()
    
    # Players: Simplify for demo
    players = pd.DataFrame(bootstrap['elements'])
    players = players[['id', 'web_name', 'element_type', 'minutes', 'goals_scored', 'assists', 'yellow_cards', 'total_points']]
    pos_map = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    players['position'] = players['element_type'].map(pos_map)
    players_dict = players.to_dict('records')
    
    # Fixtures
    fixtures_df = pd.DataFrame(fixtures)
    fixtures_df = fixtures_df[['id', 'event', 'team_h', 'team_a', 'kickoff_time']]
    fixtures_dict = fixtures_df.to_dict('records')
    
    return players_dict, fixtures_dict

def init_db(players, fixtures):
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    
    # Insert players and fixtures
    db.players.delete_many({})  # Clear for fresh insert (testing)
    db.players.insert_many(players)
    db.fixtures.delete_many({})
    db.fixtures.insert_many(fixtures)
    
    # Ensure lineups collection exists (add manually via Compass for now)
    # Example insert: db.lineups.insert_one({'match_id': 1, 'player_id': 123, 'position': 'RB', 'formation': '4-3-3'})
    
    client.close()

if __name__ == "__main__":
    players, fixtures = fetch_fpl_data()
    init_db(players, fixtures)
    print("Data collected and DB initialized!")
