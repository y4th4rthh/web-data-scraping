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
import os
import random
import datetime

load_dotenv()
app = FastAPI()

MONGO_URI = os.getenv("MONGO_URI")
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["neuraai"]
chats_collection = db["chats"]

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


def bing_search(query: str, max_results=5):
    ua = UserAgent()
    headers = {'User-Agent': ua.random}

    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded_query}"

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


def scrape_page(url: str):
    try:
        ua = UserAgent()
        headers = {'User-Agent': ua.random}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        paras = soup.find_all('p')
        return "\n".join(p.get_text() for p in paras[:5])
    except Exception as e:
        return f"⚠️ Error fetching content: {e}"


@app.get("/search")
async def search_and_scrape(query: str = Query(..., min_length=3), userId: str = Query(...)):
    links = bing_search(query)
    results = []

    for url in links:
        content = scrape_page(url)
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
        "model": "web.search.1.o",
        "ai_response": text
    }

    await chats_collection.insert_one(chat_doc)

    return {"results": results}
