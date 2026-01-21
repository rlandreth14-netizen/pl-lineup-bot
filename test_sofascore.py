import requests
import json

# Match ID from the URL you provided
# Marseille vs Liverpool - lineups already announced
match_id = 14566821

# SofaScore API endpoint
url = f"https://api.sofascore.com/api/v1/event/{match_id}/lineups"

# Headers to mimic a browser request
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': f'https://www.sofascore.com/',
    'Origin': 'https://www.sofascore.com'
}

print(f"Testing SofaScore API for match {match_id}...")
print(f"URL: {url}\n")

try:
    response = requests.get(url, headers=headers, timeout=10)
    
    print(f"Status Code: {response.status_code}\n")
    
    if response.status_code == 200:
        data = response.json()
        
        print("✅ SUCCESS! Lineup data retrieved\n")
        print("=" * 60)
        
        # Check if we have lineups
        if 'home' in data and 'away' in data:
            # Home team
            home_team = data.get('home', {})
            print(f"\n🏠 HOME TEAM")
            print(f"Formation: {home_team.get('formation', 'Unknown')}")
            
            if 'players' in home_team:
                print("\nStarting XI:")
                for player in home_team['players'][:11]:
                    player_info = player.get('player', {})
                    position = player.get('position', 'Unknown')
                    shirt_number = player.get('shirtNumber', 'N/A')
                    print(f"  #{shirt_number} {player_info.get('name', 'Unknown')} ({position})")
            
            # Away team
            away_team = data.get('away', {})
            print(f"\n✈️ AWAY TEAM")
            print(f"Formation: {away_team.get('formation', 'Unknown')}")
            
            if 'players' in away_team:
                print("\nStarting XI:")
                for player in away_team['players'][:11]:
                    player_info = player.get('player', {})
                    position = player.get('position', 'Unknown')
                    shirt_number = player.get('shirtNumber', 'N/A')
                    print(f"  #{shirt_number} {player_info.get('name', 'Unknown')} ({position})")
        
        else:
            print("⚠️ Lineup data structure unexpected")
            print("\nFull Response:")
            print(json.dumps(data, indent=2))
    
    elif response.status_code == 404:
        print("❌ Match not found or lineups not available yet")
    
    elif response.status_code == 403:
        print("❌ Access forbidden - SofaScore might be blocking requests")
        print("This could mean:")
        print("  - Need better headers")
        print("  - IP-based rate limiting")
        print("  - They detected automated requests")
    
    else:
        print(f"❌ Unexpected status code: {response.status_code}")
        print(f"Response: {response.text[:500]}")

except requests.exceptions.Timeout:
    print("❌ Request timed out")
except requests.exceptions.RequestException as e:
    print(f"❌ Request failed: {e}")
except json.JSONDecodeError:
    print("❌ Could not parse JSON response")
    print(f"Raw response: {response.text[:500]}")

print("\n" + "=" * 60)
print("\nTest complete!")
