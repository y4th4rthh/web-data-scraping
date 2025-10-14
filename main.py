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
import re
import time
import csv
import os
import datetime
import google.generativeai as genai
from fastapi.responses import PlainTextResponse
from difflib import SequenceMatcher

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


MODEL = genai.GenerativeModel("gemini-2.5-flash")  # or gemini-pro

# -----------------------------
#  STEP 1: SCRAPE REDDIT LINKS FROM GOOGLE
# -----------------------------
async def google_reddit_search(query, limit=10):  # keeping the same name for compatibility
    ua = UserAgent()
    headers = {"User-Agent": ua.random}
    q = urllib.parse.quote_plus(f"site:reddit.com {query}")
    url = f"https://www.bing.com/search?q={q}&count={limit}"

    res = requests.get(url, headers=headers, timeout=10)
    soup = BeautifulSoup(res.text, "html.parser")

    links = []
    for li in soup.find_all("li", {"class": "b_algo"}):
        a_tag = li.find("a", href=True)
        if a_tag:
            href = a_tag["href"]
            if "reddit.com/r/" in href and "/comments/" in href:
                links.append(href)
        if len(links) >= limit:
            break

    # --- Fallback if Bing finds nothing ---
    if len(links) == 0:
        print("⚠️ Bing returned no Reddit results. Using Reddit search instead...")
        reddit_search_url = f"https://www.reddit.com/search/?q={urllib.parse.quote_plus(query)}"
        res2 = requests.get(reddit_search_url, headers=headers, timeout=10)
        soup2 = BeautifulSoup(res2.text, "html.parser")

        for a in soup2.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/r/") and "/comments/" in href:
                links.append("https://www.reddit.com" + href)
            if len(links) >= limit:
                break

    return links



async def scrape_reddit_post(url):
    """Scrape Reddit post and top comments from old.reddit.com"""
    ua = UserAgent()
    headers = {"User-Agent": ua.random}

    try:
        # Convert to old Reddit
        old_url = re.sub(r"www\.reddit\.com", "old.reddit.com", url)
        res = requests.get(old_url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")

        # Title
        title_tag = soup.find("a", class_="title")
        title = title_tag.text.strip() if title_tag else "No title found"

        # Post text
        post_body = soup.find("div", class_="expando")
        post_text = post_body.get_text(strip=True) if post_body else ""

        # Top 3 comments
        comments = []
        for c in soup.select("div.entry .md")[:3]:
            txt = c.get_text(strip=True)
            if txt and len(txt) > 30:
                comments.append(txt)

        # Combine data
        return {
            "url": url,
            "title": title,
            "post": post_text,
            "comments": comments
        }

    except Exception as e:
        return {"url": url, "error": str(e)}


# -----------------------------
#  STEP 3: SUMMARIZE USING GEMINI
# -----------------------------
async def summarize_reddit_results(query, reddit_data):
    # Filter out empty or error posts
    valid_posts = [d for d in reddit_data if "error" not in d and (d.get("comments") or d.get("post"))]

    if not valid_posts:
        return "No meaningful Reddit data found to summarize."

    # Sort by amount of text (prefer longer posts)
    valid_posts.sort(key=lambda d: len(" ".join(d.get("comments", [])) + d.get("post", "")), reverse=True)

    # Only keep top 2 for Gemini summarization
    top_posts = valid_posts[:2]

    text_block = ""
    for d in top_posts:
        text_block += f"\n---\nTitle: {d['title']}\nPost: {d.get('post','')}\nComments:\n" + "\n".join(d['comments']) + "\n"

    prompt = f"""
You are an intelligent Reddit summarizer.
Given the following Reddit posts and their top comments, summarize and extract the most relevant, informative insights
to the query: "{query}".

Respond in short bullet points or a short paragraph.
Avoid URLs, and only include useful insights.

Reddit data:
{text_block}
    """

    response = MODEL.generate_content(prompt)
    return response.text.strip()



# -----------------------------
#  STEP 4: MAIN FUNCTION
# -----------------------------
async def reddit_ai_answer(query):
    print(f"🔍 Searching Reddit for: {query}")
    urls = await google_reddit_search(query, limit=5)

    print(f"\nFound {len(urls)} Reddit posts:")
    for u in urls:
        print(" -", u)

    print("\n📥 Scraping posts...")
    reddit_data = []
    for u in urls:
        post = await scrape_reddit_post(u)
        reddit_data.append(post)
        time.sleep(2)  # avoid hitting too fast

    print("\n🤖 Summarizing using Gemini...")
    summary = await summarize_reddit_results(query, reddit_data)
    
    formatted_urls = "\n".join(urls) if urls else "No URLs found."
    final_answer = f"\n🔎 Final Answer:\n{summary}\n\n🔗 Related Reddit Posts:\n{formatted_urls}"

    return final_answer
     


@app.get("/search")
async def search_and_scrape(query: str = Query(..., min_length=3), userId: str = Query(...), incognito: str = Query(...)):
    
    result = await reddit_ai_answer(query)

    session_id = "web" + str(uuid.uuid4())
    if incognito == "false":
       chat_doc = {
        "session_id": session_id,
        "timestamp": datetime.datetime.utcnow(),
        "user_text": query,
        "user_id": userId,
        "model": "neura.vista1.o",
        "ai_response": result
       }

       await chats_collection.insert_one(chat_doc)

    return {"result": result}
