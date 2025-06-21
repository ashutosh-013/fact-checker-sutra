from flask import Flask, render_template, request, jsonify
import requests
import json
import re
import time
import os
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from threading import Lock

app = Flask(__name__)

# Your real API keys here (replace with your actual keys)
GROQ_API_KEY = "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"
NEWS_API_KEY = "57b87c337d88476f81145feefceb0634"

# Cache config
CACHE_TTL = 300  # seconds (5 minutes)
cache = {}
cache_lock = Lock()

# Quota config
QUOTA_LIMIT = 100  # max requests per client IP
quota = {}
quota_lock = Lock()

# Rate limiter setup (limits per IP)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["10 per minute"]  # default limit (10 requests per minute per IP)
)

def get_client_ip():
    return get_remote_address()

def clean_cache():
    """Remove expired cache entries."""
    now = time.time()
    with cache_lock:
        keys_to_delete = [key for key, val in cache.items() if now - val['time'] > CACHE_TTL]
        for key in keys_to_delete:
            del cache[key]

def increment_quota(ip):
    """Increment request count for IP and return current count."""
    with quota_lock:
        quota[ip] = quota.get(ip, 0) + 1
        return quota[ip]

def check_quota(ip):
    """Check if IP is still under quota."""
    with quota_lock:
        return quota.get(ip, 0) < QUOTA_LIMIT

def search_newsapi(query):
    """Search newsapi for a query with caching."""
    clean_cache()

    with cache_lock:
        cached = cache.get(query)
        if cached and time.time() - cached['time'] <= CACHE_TTL:
            return cached['result']

    url = f"https://newsapi.org/v2/everything?q={query}&language=en&sortBy=publishedAt&apiKey={NEWS_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            articles = response.json().get("articles", [])
            found = bool(articles)
            title = articles[0]["title"] if found else None
        else:
            found, title = False, None
    except requests.RequestException:
        found, title = False, None

    with cache_lock:
        cache[query] = {'result': (found, title), 'time': time.time()}

    return found, title

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/check_news', methods=['POST'])
@limiter.limit("5 per minute")  # additional rate limit on this endpoint
def check_news():
    ip = get_client_ip()

    if not check_quota(ip):
        return jsonify({"error": "Quota exceeded. Please try again later."}), 429

    used = increment_quota(ip)

    data = request.json or {}
    news_text = data.get('news', '').strip()

    if not news_text:
        return jsonify({"error": "No news text provided"}), 400

    # Step 1: Check NewsAPI cache or live
    found, title = search_newsapi(news_text)
    if found:
        return jsonify({
            "status": "Real",
            "corrected_news": title,
            "explanation": "Verified from live news sources (NewsAPI)",
            "quota_used": used,
            "quota_limit": QUOTA_LIMIT
        })

    # Step 2: Use AI fallback via Groq (LLaMA 3)
    prompt = f"""
You are a fact-checking AI. Analyze the following news and determine if it is Real, Fake, or Unverified.

News:
\"{news_text}\"

Respond only in JSON like this:
{{
  "status": "Fake/Real/Unverified",
  "corrected_news": "Corrected or true version (if fake or unclear)",
  "explanation": "Short reasoning or evidence"
}}
"""

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-70b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3
            },
            timeout=15
        )

        if response.status_code != 200:
            return jsonify({"error": "Groq API error", "details": response.text}), 500

        reply = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()

        # Extract JSON from AI response safely
        match = re.search(r"\{.*\}", reply, re.DOTALL)
        if not match:
            return jsonify({"error": "AI response did not contain valid JSON", "raw_response": reply}), 500

        result_json = json.loads(match.group(0))

        # Add quota info to AI result
        result_json["quota_used"] = used
        result_json["quota_limit"] = QUOTA_LIMIT

        return jsonify(result_json)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/healthz")
def healthz():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Dynamic port for Render
    app.run(host="0.0.0.0", port=port)
