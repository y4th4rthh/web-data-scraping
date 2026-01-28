from groq import Groq
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
import httpx

load_dotenv()
app = FastAPI()
CSV_FILE = "prompts.csv"
PR_CSV_FILE = "daily-prompts.csv"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_SEARCH_ENGINE_ID = os.getenv("GOOGLE_SEARCH_ENGINE_ID")

genai.configure(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
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
    allow_origins=["https://neura-ai.netlify.app", "http://localhost:3000", "http://localhost:5173", "https://neura-explore-ai.netlify.app/","https://neura-explore-ai.netlify.app",
                   "https://neura-share.netlify.app","https://dev-neura-ai.netlify.app" ,"https://admin-neura.netlify.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TextRequest(BaseModel):
    text: str
    model: str = "web.search1.o"
    user_id: Optional[str] = None
    sessionId: Optional[str] = None


MODEL = genai.GenerativeModel("gemini-2.5-flash")


# -----------------------------
#  GOOGLE SEARCH FUNCTIONS
# -----------------------------
async def google_search(query: str, num_results: int = 5):
    """Search Google using Custom Search API"""
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_SEARCH_ENGINE_ID,
        "q": query,
        "num": num_results,
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        
        results = []
        for item in data.get("items", []):
            results.append({
                "title": item.get("title"),
                "snippet": item.get("snippet"),
                "link": item.get("link")
            })
        
        print(f"✅ Google Search returned {len(results)} results")
        return results
    
    except httpx.HTTPStatusError as e:
        print(f"❌ Google Search HTTP error: {e.response.status_code}")
        return None
    except httpx.RequestError as e:
        print(f"❌ Google Search request error: {e}")
        return None
    except Exception as e:
        print(f"❌ Google Search unexpected error: {e}")
        return None


async def summarize_google_results(query, search_data):
    """Summarize Google search results using Gemini"""
    if not search_data:
        return "No search results found."
    
    text_block = "\n\n".join(
        f"Title: {d['title']}\nSnippet: {d['snippet']}\n"
        for d in search_data
    )
    
    system_prompt = """
You are a helpful assistant that summarizes Google search results.

Rules:
- Focus on useful, factual, and up-to-date information
- Keep the summary in thorough and clear manner (maximum 2–3 paragraphs)
- Provide only the most relevant insights
- Avoid speculation or filler
- Use emojis sparingly where they add clarity or emphasis

Output format (STRICT):
1. Write the summary in paragraphs.
2. After the summary, add TWO line breaks.
3. On a NEW line, write the TL;DR in italics, starting exactly with '*TL;DR:* '.

Do not merge the TL;DR with the summary.
"""

    user_prompt = f"""
     Search query:
     {query}

     Google search results:
     {text_block}
    """
    
    try:
        chat_completion = groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                model="llama-3.3-70b-versatile",
                max_tokens=2000,
                temperature=0.9
            )
        response = chat_completion.choices[0].message.content
        # response = MODEL.generate_content(prompt)
        return response.strip()
    
    except Exception as e:
        print(f"❌ Gemini summarization error: {e}")
        return "Your daily quota has expired. Please switch to another model or try later :("


async def google_ai_answer(query):
    """Main function for Google search and summarization"""
    print(f"🔍 Searching Google for: {query}")
    search_data = await google_search(query)
    
    if not search_data:
        return None  # Signal failure to fallback to Reddit
    
    summary = await summarize_google_results(query, search_data)
    formatted_urls = "\n\n".join([f"- {d['link']}" for d in search_data]) if search_data else "No links found."
    
    return f"📝 **Summary:**\n\n {" "} \n\n{summary}\n\n {" "} \n\n🔗 **Sources:**\n\n {" "} \n\n{formatted_urls}"


# -----------------------------
#  REDDIT SEARCH FUNCTIONS (FALLBACK)
# -----------------------------
async def reddit_search(query, limit=5):
    """Search Reddit posts and comments"""
    posts = []

    try:
        subreddit = await reddit.subreddit("all")
        async for submission in subreddit.search(query, limit=limit):
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
    except Exception as e:
        print(f"❌ Reddit search error: {e}")
        return []

    return posts


async def summarize_reddit_results(query, reddit_data):
    """Summarize Reddit results using Gemini"""
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

    try:
        response = MODEL.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"❌ Reddit summarization error: {e}")
        return "Summarization failed."


async def reddit_ai_answer(query):
    """Main function for Reddit search and summarization (fallback)"""
    print(f"🔍 Searching Reddit for: {query}")
    reddit_data = await reddit_search(query, limit=5)

    if not reddit_data:
        return "No results found from either Google or Reddit."

    print(f"\nFound {len(reddit_data)} Reddit posts:")
    for post in reddit_data:
        print(" -", post["url"])

    print("\n🤖 Summarizing using Gemini...")
    summary = await summarize_reddit_results(query, reddit_data)

    formatted_urls = "\n".join([p["url"] for p in reddit_data]) if reddit_data else "No URLs found."
    final_answer = f"\n🔎 **Final Answer (Reddit):**\n\n{summary}\n\n🔗 **Related Reddit Sources:**\n\n{formatted_urls}"

    return final_answer


# -----------------------------
#  UNIFIED SEARCH WITH FALLBACK
# -----------------------------
async def unified_search(query: str):
    """
    Try Google Search first, fallback to Reddit if Google fails
    """
    # Try Google first
    google_result = await google_ai_answer(query)
    
    if google_result:
        print("✅ Using Google Search results")
        return google_result, "google"
    
    # Fallback to Reddit
    print("⚠️ Google Search failed, falling back to Reddit...")
    reddit_result = await reddit_ai_answer(query)
    return reddit_result, "reddit"


@app.get("/ping")
async def ping():
    """Keep-alive / health check endpoint"""
    return {"status": "ok"}


@app.get("/search")
async def search_and_scrape(query: str = Query(..., min_length=3), userId: str = Query(...), incognito: str = Query(...)):
    """
    Search endpoint with Google as primary and Reddit as fallback
    """
    result, source = await unified_search(query)

    session_id = "web" + str(uuid.uuid4())
    if incognito == "false":
        chat_doc = {
            "session_id": session_id,
            "timestamp": datetime.datetime.utcnow(),
            "user_text": query,
            "user_id": userId,
            "model": "neura.vista1.o",
            "ai_response": result,
            "search_source": source  # Track which source was used
        }

        await chats_collection.insert_one(chat_doc)

    return {
        "result": result,
        "source": source  # Return which search engine was used
    }
