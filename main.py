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
import asyncpraw
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

reddit = asyncpraw.Reddit(
    client_id=os.getenv("REDDIT_CLIENT_ID"),
    client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
    user_agent=os.getenv("REDDIT_USER_AGENT")
)

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

async def reddit_search(query, limit=5):
    posts = []

    # Use Reddit's search API
    async for submission in reddit.subreddit("all").search(query, limit=limit):
        # Fetch top 3 comments
        await submission.load()
        comments = []
        submission.comments.replace_more(limit=0)
        for c in submission.comments[:3]:
            comments.append(c.body)

        posts.append({
            "url": f"https://reddit.com{submission.permalink}",
            "title": submission.title,
            "post_text": submission.selftext,
            "comments": comments
        })

    return posts



# -----------------------------
#  STEP 3: SUMMARIZE USING GEMINI
# -----------------------------
async def summarize_reddit_results(query, reddit_data):
    # Filter out posts that have no comments or no post_text
    valid_posts = [d for d in reddit_data if d.get("comments") or d.get("post_text")]

    if not valid_posts:
        return "No meaningful Reddit data found to summarize."

    # Sort by amount of text (prefer longer posts + comments)
    valid_posts.sort(key=lambda d: len(" ".join(d.get("comments", [])) + d.get("post_text", "")), reverse=True)

    # Only keep top 2 for summarization
    top_posts = valid_posts[:2]

    # Build text block for Gemini
    text_block = ""
    for d in top_posts:
        text_block += (
            f"\n---\n"
            f"Title: {d['title']}\n"
            f"Post: {d.get('post_text','')}\n"
            f"Comments:\n" + "\n".join(d.get('comments', [])) + "\n"
        )

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
    reddit_data = await reddit_api_search(query, limit=5)

    print(f"\nFound {len(reddit_data)} Reddit posts:")
    for post in reddit_data:
        print(" -", post["url"])

    print("\n🤖 Summarizing using Gemini...")
    summary = await summarize_reddit_results(query, reddit_data)  # your Gemini function can stay mostly the same

    formatted_urls = "\n".join([p["url"] for p in reddit_data]) if reddit_data else "No URLs found."
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
