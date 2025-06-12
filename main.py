from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
import urllib.parse

app = FastAPI()

# Optional CORS if using frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
def search_and_scrape(query: str = Query(..., min_length=3)):
    links = bing_search(query)
    results = []
    
    for url in links:
        content = scrape_page(url)
        results.append({
            "url": url,
            "content": content
        })
    
    return {"results": results}
