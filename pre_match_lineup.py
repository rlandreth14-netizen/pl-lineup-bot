import requests
import pytesseract
from PIL import Image
from io import BytesIO
from pymongo import MongoClient
from datetime import datetime, timezone
import logging
import os

logging.basicConfig(level=logging.INFO)

# --- ENV VARIABLES ---
MONGODB_URI = os.getenv('MONGODB_URI')

# --- MONGO HELPER ---
def get_db():
    client = MongoClient(MONGODB_URI)
    db = client['premier_league']
    return client, db

# --- OCR & LINEUP PROCESSING ---
def process_prematch_lineup(match_id, team_name, image_url):
    """
    Downloads the lineup image, runs OCR, and saves lineup to MongoDB.
    
    Args:
        match_id (int): FPL fixture ID
        team_name (str): Home or away team name
        image_url (str): URL of the lineup image
    Returns:
        dict: OCR result with player names and confidence
    """
    logging.info(f"Processing lineup for match {match_id}, team {team_name}")

    try:
        # --- Download the image ---
        response = requests.get(image_url)
        img = Image.open(BytesIO(response.content))

        # --- OCR ---
        ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        num_items = len(ocr_data['text'])
        players = []

        for i in range(num_items):
            text = ocr_data['text'][i].strip()
            conf = int(ocr_data['conf'][i])
            if text and conf > 50:  # Confidence threshold
                players.append({'name': text, 'confidence': conf})

        if not players:
            logging.warning("No players detected with sufficient confidence")
            return None

        # --- Save to MongoDB ---
        client, db = get_db()
        collection = db['prematch_lineups']
        doc = {
            'match_id': match_id,
            'team_name': team_name,
            'timestamp': datetime.now(timezone.utc),
            'players': players
        }
        collection.replace_one(
            {'match_id': match_id, 'team_name': team_name},
            doc,
            upsert=True
        )
        client.close()

        logging.info(f"Saved lineup for {team_name}, {len(players)} players detected")
        return doc

    except Exception as e:
        logging.error(f"Error processing lineup: {e}")
        return None

# --- EXAMPLE USAGE ---
if __name__ == "__main__":
    # Quick test (replace with real fixture ID and image URL)
    test_doc = process_prematch_lineup(
        match_id=123,
        team_name="Newcastle",
        image_url="https://example.com/nufc_lineup.png"
    )
    print(test_doc)
