from pythainlp import word_tokenize
import sqlite3
import json
import os
import random
from difflib import SequenceMatcher
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_logs.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

CONFIG_FILE = 'config.json'

def load_config():
    default_config = {
        "intents": {
            "unknown": {
                "responses": ["ขอโทษค่ะ ฉันไม่แน่ใจว่าเข้าใจถูกมั้ย ลองบอกใหม่ได้มั้ยคะ? 😊"]
            }
        },
        "products": []
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.info("Loaded config.json successfully")
                if "intents" not in config:
                    config["intents"] = default_config["intents"]
                elif "unknown" not in config["intents"]:
                    config["intents"]["unknown"] = default_config["intents"]["unknown"]
                return config
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding config.json: {e}")
            return default_config
        except Exception as e:
            logger.error(f"Unexpected error loading config.json: {e}")
            return default_config
    logger.warning("config.json not found, using default config")
    return default_config

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        logger.info("Saved config.json successfully")
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")

def initialize_database():
    conn = sqlite3.connect('chat_history.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            message TEXT,
            response TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("PRAGMA table_info(chat_history)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'intent' not in columns:
        cursor.execute('ALTER TABLE chat_history ADD COLUMN intent TEXT')
        logger.info("Added intent column to chat_history")
    if 'product_id' not in columns:
        cursor.execute('ALTER TABLE chat_history ADD COLUMN product_id TEXT')
        logger.info("Added product_id column to chat_history")
    conn.commit()
    conn.close()

def save_chat_message(user_id, message, response, intent=None, product_id=None):
    try:
        conn = sqlite3.connect('chat_history.db')
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO chat_history (user_id, message, response, intent, product_id) VALUES (?, ?, ?, ?, ?)',
            (user_id, message, response, intent, product_id)
        )
        conn.commit()
        logger.info(f"Saved chat message for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving chat message: {e}")
    finally:
        conn.close()

def get_recent_context(user_id, limit=3):
    try:
        conn = sqlite3.connect('chat_history.db')
        cursor = conn.cursor()
        cursor.execute(
            'SELECT message, intent, product_id FROM chat_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?',
            (user_id, limit)
        )
        context = cursor.fetchall()
        return context
    except Exception as e:
        logger.error(f"Error getting recent context: {e}")
        return []
    finally:
        conn.close()

def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def find_best_product(tokens, products):
    best_product = None
    best_score = 0
    for product in products:
        score = 0
        for token in tokens:
            for keyword in product["keywords"]:
                if similarity(token, keyword) > 0.8:
                    score += 1
            if product["name"].lower() in token.lower():
                score += 2
        if score > best_score:
            best_score = score
            best_product = product
    return best_product

def update_intent_keywords_and_responses(config, intent, tokens, response):
    if intent == "unknown":
        return
    intent_data = config["intents"].get(intent, {"keywords": [], "responses": []})
    # อัปเดต keywords
    for token in tokens:
        if len(token) > 2 and token not in intent_data["keywords"]:
            intent_data["keywords"].append(token)
            logger.info(f"Added keyword '{token}' to intent '{intent}'")
    # อัปเดต responses (ถ้ามีการเรียนรู้จากบริบท)
    if response and response not in intent_data["responses"]:
        intent_data["responses"].append(response)
        logger.info(f"Added response '{response}' to intent '{intent}'")
    config["intents"][intent] = intent_data
    save_config(config)

def process_text(text, user_id='web_user'):
    initialize_database()
    config = load_config()
    intents = config.get("intents", {})
    products = config.get("products", [])

    tokens = word_tokenize(text, engine='newmm')
    logger.info(f"Tokenized input: {tokens}")

    context = get_recent_context(user_id)
    previous_intent = context[0][1] if context else None
    previous_product_id = context[0][2] if context else None

    matched_product = find_best_product(tokens, products)
    if not matched_product and previous_product_id:
        matched_product = next((p for p in products if p["id"] == previous_product_id), None)

    best_intent = "unknown"
    best_score = 0
    for intent, data in intents.items():
        if intent == "unknown":
            continue
        score = 0
        keywords = data.get("keywords", [])
        for token in tokens:
            for keyword in keywords:
                if similarity(token, keyword) > 0.8:
                    score += 1
        if score > best_score:
            best_score = score
            best_intent = intent
        if intent == previous_intent and score >= best_score * 0.8:
            best_intent = intent
            best_score = score

    default_response = "ขอโทษค่ะ ฉันไม่แน่ใจว่าเข้าใจถูกมั้ย ลองบอกใหม่ได้มั้ยคะ? 😊"
    responses = intents.get(best_intent, {}).get("responses", [default_response])
    if not responses:
        responses = [default_response]
    response = random.choice(responses)
    logger.info(f"Selected intent: {best_intent}, response: {response}")

    if matched_product:
        response = response.format(
            product_name=matched_product["name"],
            product_price=matched_product["price"],
            product_description=matched_product["description"]
        )
    else:
        response = response.replace("{product_name}", "สินค้าของเรา")
        response = response.replace("{product_price}", "ราคาดีๆ")
        response = response.replace("{product_description}", "ของเจ๋งๆ")

    # อัปเดต keywords ทันที
    update_intent_keywords_and_responses(config, best_intent, tokens, None)

    save_chat_message(
        user_id, text, response, best_intent,
        matched_product["id"] if matched_product else None
    )

    return {
        "response": response,
        "tokens": tokens,
        "intent": best_intent,
        "product_id": matched_product["id"] if matched_product else None
    }