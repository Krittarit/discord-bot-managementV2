import sqlite3
import json
import logging
from sentence_transformers import SentenceTransformer, util
from pythainlp import word_tokenize

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_chat_history():
    conn = sqlite3.connect('chat_history.db')
    cursor = conn.cursor()
    cursor.execute('SELECT message, intent FROM chat_history WHERE intent IS NOT NULL')
    data = cursor.fetchall()
    conn.close()
    return [{"message": msg, "intent": intent} for msg, intent in data]

def update_config_with_new_keywords(config_file='config.json'):
    model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
    chat_data = load_chat_history()
    if not chat_data:
        logger.warning("No chat history found")
        return

    with open(config_file, 'r', encoding='utf-8') as f:
        config = json.load(f)

    intent_keywords = {intent: data.get("keywords", []) for intent, data in config.get("intents", {}).items()}
    for chat in chat_data:
        message = chat["message"]
        intent = chat["intent"]
        if intent == "unknown":
            continue
        embedding = model.encode(message)
        new_keywords = word_tokenize(message, engine='newmm')
        for keyword in new_keywords:
            if len(keyword) > 2 and keyword not in intent_keywords.get(intent, []):
                intent_keywords.setdefault(intent, []).append(keyword)
                logger.info(f"Added keyword '{keyword}' to intent '{intent}'")

    for intent, keywords in intent_keywords.items():
        if intent in config["intents"]:
            config["intents"][intent]["keywords"] = list(set(keywords))

    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    logger.info("Updated config.json with new keywords")

if __name__ == "__main__":
    update_config_with_new_keywords()