from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
from motor.motor_asyncio import AsyncIOMotorClient
from fake_useragent import UserAgent
import urllib.parse
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import Optional
import uuid
import time
import csv
import os
import datetime
from fastapi.responses import PlainTextResponse
from difflib import SequenceMatcher
import spacy
import string


load_dotenv()
app = FastAPI()
CSV_FILE = "prompts.csv"

MONGO_URI = os.getenv("MONGO_URI")
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["neuraai"]
chats_collection = db["chats"]

nlp = spacy.load("en_core_web_sm")

# Optional CORS if using frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TextRequest(BaseModel):
    text: str
    model: str = "web.search1.o"
    user_id: Optional[str] = None
    sessionId: Optional[str] = None

STOPWORDS = nlp.Defaults.stop_words
PUNCTUATION = set(string.punctuation)

def extract_prompt_phrase(title):
    doc = nlp(title)
    keywords = [
        token.text for token in doc
        if token.text.lower() not in STOPWORDS and token.text not in PUNCTUATION and token.is_alpha
    ]
    if len(keywords) >= 3:
        return " ".join(keywords[:3])
    return " ".join(keywords)

def is_relevant(content: str, query: str, threshold=0.3):
    return SequenceMatcher(None, content.lower(), query.lower()).ratio() > threshold

def fetch_news_titles():
    ua = UserAgent()
    headers = {'User-Agent': ua.random}
    url = "https://www.bing.com/news"
    prompt_phrases = set()

    print("🔁 Fetching news...")

    selectors = ["a.title", "h2 > a", "a[href^='/news/']", ".title a"]

    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')

        for selector in selectors:
            items = soup.select(selector)
            for item in items:
                text = item.get("title") or item.get("aria-label") or item.get_text(strip=True)
                if text and not text.endswith(("…", "...")):
                    phrase = extract_prompt_phrase(text.strip())
                    if phrase:
                        prompt_phrases.add(phrase)

        print(f"📈 Got {len(prompt_phrases)} phrases")
        print(prompt_phrases)

    except Exception as e:
        print(f"❌ Error during fetch: {e}")
        return

    top_phrases = list(prompt_phrases)[:15]

    # Save to CSV
    with open(CSV_FILE, "w", newline='', encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Prompt"])
        for phrase in top_phrases:
            writer.writerow([phrase])

    print("✅ prompts.csv updated.")


# fetch_news_titles()


@app.get("/fetch-news-now")
def manual_news_fetch():
    fetch_news_titles()
    return {"status": "✅ News updated manually"}


# Call it once on startup to initialize file
if not os.path.exists(CSV_FILE):
    fetch_news_titles()


def bing_search(query: str, max_results=100):
    ua = UserAgent()
    headers = {'User-Agent': ua.random}

    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded_query}"
    print(url)

    res = requests.get(url, headers=headers)
    soup = BeautifulSoup(res.text, 'html.parser')

    links = []
    for item in soup.find_all('li', {'class': 'b_algo'}):
        a_tag = item.find('a')
        if a_tag and a_tag['href'].startswith('http'):
            links.append(a_tag['href'])
            if len(links) >= max_results:
                break

    return links


def scrape_page(url: str, query: str):
    try:
        ua = UserAgent()
        headers = {'User-Agent': ua.random}
        res = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')
        paras = soup.find_all('p')
        content = "\n".join(p.get_text() for p in paras[:30])

        # Basic relevance check
        if is_relevant(content, query):
            return content
        else:
            return content

    except Exception as e:
        return f"⚠️ Error fetching content: {e}"


@app.get("/top-news-csv", response_class=PlainTextResponse)
async def get_top_news_csv():
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"⚠️ Failed to load CSV: {str(e)}"


@app.get("/search")
async def search_and_scrape(query: str = Query(..., min_length=3), userId: str = Query(...)):
    links = bing_search(query)
    results = []

    for url in links:
        content = scrape_page(url,query)
        results.append({
            "url": url,
            "content": content
        })

    print(results)

    text = f"🔗 [SOURCE]({results[0]['url']})\n\n{results[0]['content']}"

    session_id = "web" + str(uuid.uuid4())
    chat_doc = {
        "session_id": session_id,
        "timestamp": datetime.datetime.utcnow(),
        "user_text": query,
        "user_id": userId,
        "model": "neura.vista1.o",
        "ai_response": text
    }

    await chats_collection.insert_one(chat_doc)

    return {"results": results}
