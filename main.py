from fastapi import FastAPI, Query, File, UploadFile
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
import re
import time
import csv
import os
import datetime
import google.generativeai as genai
from fastapi.responses import PlainTextResponse
from difflib import SequenceMatcher
from transformers import BlipProcessor, BlipForConditionalGeneration
from PIL import Image
import io


load_dotenv()
app = FastAPI()
CSV_FILE = "prompts.csv"
PR_CSV_FILE = "daily-prompts.csv"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
MONGO_URI = os.getenv("MONGO_URI")
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["neuraai"]
chats_collection = db["chats"]

processor = BlipProcessor.from_pretrained("Salesforce/blip-image-captioning-base")
imgmodel = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-base")


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

def is_relevant(content: str, query: str, threshold=0.3):
    return SequenceMatcher(None, content.lower(), query.lower()).ratio() > threshold

def extract_keywords_from_titles(titles):
    model = genai.GenerativeModel("models/gemini-1.5-flash-8b")

    prompt = (
        "Convert each news headline into a short keyword-style summary. "
        "Remove location/event noise, keep main subject and action. "
        "Dont give headline or index value in output"
    )

    prompt += "\n" + "\n".join(titles)

    try:
        response = model.generate_content(prompt)
        result_text = response.text.strip()
        keywords = []
        print(result_text)

        for line in result_text.splitlines():
            line = line.strip("- ").strip()
            if line and not line.startswith("Headline"):
                keywords.append(line)
        
        # Ensure the count matches
        if len(keywords) != len(titles):
            print("⚠️ Warning: Keyword count mismatch. Falling back to original titles.")
            return keywords

        return keywords
    except Exception as e:
        print(f"❌ Gemini Flash error: {e}")
        return titles


def fetch_news_titles():
    ua = UserAgent()
    headers = {'User-Agent': ua.random}
    url = "https://www.bing.com/news"
    titles = set()

    print("🔁 Fetching news...")

    # Multiple selectors to increase hit rate
    selectors = ["a.title", "h2 > a", "a[href^='/news/']", ".title a"]

    while len(titles) < 20:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')

            # Loop through all selectors
            for selector in selectors:
              items = soup.select(selector)
              for item in items:
                text = item.get("title") or item.get("aria-label") or item.get_text(strip=True)
                if text and not text.endswith(("…", "...")):
                   titles.add(text.strip())

            print(f"📈 Got {len(titles)} titles")

            if len(titles) >= 15:
                break

            print("⏳ Retrying in 2s...")
            time.sleep(2)  # polite wait
        except Exception as e:
            print(f"❌ Error during fetch: {e}")
            time.sleep(5)

    top_30 = list(titles)
    keywords = extract_keywords_from_titles(top_30)
    print(keywords)

    # Save to CSV
    with open(CSV_FILE, "w", newline='', encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Prompt"])
        for keyword in keywords:
            writer.writerow([keyword])

    print("✅ prompts.csv updated.")

# fetch_news_titles()


@app.get("/fetch-news-now")
def manual_news_fetch():
    fetch_news_titles()
    get_titles()
    return {"status": "✅ News updated manually"}


# Call it once on startup to initialize file
if not os.path.exists(CSV_FILE):
    fetch_news_titles()


def get_titles():
    model = genai.GenerativeModel("models/gemini-1.5-flash-8b")

    prompt = (
        "You are a creative AI assistant.\n"
        "Given a list of festival-related news headlines, extract the core theme and rewrite each as a short, creative prompt.\n"
        "These prompts should inspire creative writing, tweets, memes, or AI responses.\n"
        "Do NOT number them, do NOT prefix with 'Headline', just return a clean list of prompts.\n"
        "Each prompt should be inspired by the headline, festival-themed, and phrased in an engaging way.\n\n"
        "Examples:\n"
        "- Write a haiku about Diwali lights.\n"
        "- Describe Holi from a color’s point of view.\n"
        "- Pitch a new food dish for Christmas.\n"
        "- What would Santa tweet after eating too many cookies?\n\n"
        "Now convert the following headlines:\n"
    )


    try:
        response = model.generate_content(prompt)
        result_text = response.text.strip()
        keywords = []
        print(result_text)

        for line in result_text.splitlines():
            line = line.strip("- ").strip()
            if line and not line.startswith("Headline"):
                keywords.append(line)


        with open(PR_CSV_FILE, "w", newline='', encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Prompt"])
            for keyword in keywords:
                writer.writerow([keyword])

        print("✅ prompts.csv updated.")

    except Exception as e:
        print(f"❌ Gemini Flash error: {e}")

        


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
    

@app.get("/prompt-csv", response_class=PlainTextResponse)
async def get_top_prompt_csv():
    try:
        with open(PR_CSV_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"⚠️ Failed to load CSV: {str(e)}"


@app.get("/search")
async def search_and_scrape(query: str = Query(..., min_length=3), userId: str = Query(...), incognito: str = Query(...)):
    links = bing_search(query)
    results = []

    for url in links:
        content = scrape_page(url,query)
        if content:
           model = genai.GenerativeModel("models/gemini-1.5-flash-8b")

           prompt = (
               "You are an AI assistant that helps check whether a given piece of content is related to a keyword.\n"
               "You must do the following:\n"
               "1. Carefully read the content.\n"
               "2. Check if the content contains or is clearly related to the keyword.\n"
               "3. If it is, respond with only: YES\n"
               "4. If not, respond with only: NO\n"
               "You MUST reply with only a single word: YES or NO. No explanations, no extra text.\n\n"
               f"Keyword: {query}\n\n"
               f"Content: {content}"
           )
           response = model.generate_content(prompt)
           result_text = response.text.strip()

           if "YES" in result_text:
               results.append({
               "url": url,
               "content": content
               })

    print(results)

    text = f"🔗 [SOURCE]({results[0]['url']})\n\n{results[0]['content']}"

    session_id = "web" + str(uuid.uuid4())
    if incognito == "false":
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


@app.post("/blip-caption")
async def generate_caption(file: UploadFile = File(...)):
    image_bytes = await file.read()
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    inputs = processor(image, return_tensors="pt")
    out = imgmodel.generate(**inputs)
    caption = processor.decode(out[0], skip_special_tokens=True)

    return {"caption": caption}
