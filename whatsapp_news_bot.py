import json
import os
import sqlite3
import requests
from bs4 import BeautifulSoup
from newsapi import NewsApiClient
from textblob import TextBlob
from google.cloud import texttospeech
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from aiohttp import web
import schedule
import time
import asyncio
import logging
import datetime
import pytz
import google.generativeai as genai
import random

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load configuration
with open('config.json', 'r') as f:
    config = json.load(f)

PHONE_NUMBER = config['phone_number']
TELEGRAM_TOKEN = config['telegram_token']
NEWSAPI_KEY = config['newsapi_key']
NEWS_URLS = config['news_urls']
INTERESTS = config['interests']
PREFERRED_LANGUAGE = config['preferred_language']
VOICE_ACCENT = config['voice_accent']
SUMMARY_LENGTH = config['summary_length']
NOTIFICATION_TIMES = config['notification_times']
LOCATION = config['location']
GEMINI_API_KEY = config['gemini_api_key']

# Initialize Gemini API
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

# Initialize Telegram bot
bot_app = Application.builder().token(TELEGRAM_TOKEN).build()

# Initialize database
conn = sqlite3.connect('news.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS news
             (id INTEGER PRIMARY KEY AUTOINCREMENT, headline TEXT, url TEXT, category TEXT, sentiment TEXT, timestamp TEXT)''')
conn.commit()

# NewsAPI client
newsapi = NewsApiClient(api_key=NEWSAPI_KEY)

# Scrape headlines from websites
def scrape_headlines(url):
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(response.text, 'html.parser')
        headlines = soup.find_all('h2')
        return [(h.text.strip(), url) for h in headlines if h.text.strip()]
    except Exception as e:
        logger.error(f"Error scraping {url}: {e}")
        return []

# Fetch headlines with Gemini API
async def fetch_headlines():
    all_headlines = []
    # NewsAPI
    for interest in INTERESTS:
        try:
            articles = newsapi.get_everything(q=interest, sources='bbc-news,cnn', language='en')
            for article in articles['articles'][:5]:
                headline = article['title']
                url = article['url']
                all_headlines.append((headline, url, interest))
        except Exception as e:
            logger.error(f"Error with NewsAPI for {interest}: {e}")

    # Scraping
    for url in NEWS_URLS:
        scraped = scrape_headlines(url)
        for headline, link in scraped[:5]:
            all_headlines.append((headline, link, 'general'))

    # Filter with Gemini API (batched)
    filtered_headlines = []
    batch_size = 10  # Process 10 headlines per Gemini request
    for i in range(0, len(all_headlines), batch_size):
        batch = all_headlines[i:i + batch_size]
        headlines_text = "\n".join([f"{j+1}. {h[0]}" for j, h in enumerate(batch)])
        prompt = f"Which of these headlines relate to {', '.join(INTERESTS)} in {LOCATION}? List the numbers of relevant headlines.\nHeadlines:\n{headlines_text}"
        
        for attempt in range(3):  # Retry up to 3 times
            try:
                response = await asyncio.to_thread(gemini_model.generate_content, prompt)
                relevant_numbers = [int(n) for n in response.text.split() if n.isdigit()]
                for num in relevant_numbers:
                    if 1 <= num <= len(batch):
                        filtered_headlines.append(batch[num - 1])
                break  # Success, exit retry loop
            except Exception as e:
                if "429" in str(e):  # Rate limit error
                    delay = (2 ** attempt) * 5 + random.uniform(0, 1)  # Exponential backoff
                    logger.warning(f"Rate limit hit, retrying in {delay:.2f}s (attempt {attempt + 1})")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Gemini API error for batch {i//batch_size + 1}: {e}")
                    break
        else:
            logger.error(f"Failed to process batch {i//batch_size + 1} after 3 retries")

    # Fallback: Use all headlines if none filtered
    if not filtered_headlines:
        logger.warning("No headlines filtered by Gemini, using all headlines as fallback")
        filtered_headlines = all_headlines[:10]  # Limit to 10 to avoid spam

    logger.info(f"Found {len(filtered_headlines)} filtered headlines")
    return filtered_headlines

# Analyze sentiment
def analyze_sentiment(text):
    blob = TextBlob(text)
    return 'positive' if blob.sentiment.polarity > 0 else 'negative' if blob.sentiment.polarity < 0 else 'neutral'

# Generate summary with Gemini API
async def generate_summary(headline, category):
    try:
        prompt = f"Generate a 30–60-second summary (50–100 words) of a news article with the headline '{headline}' related to {category} in {LOCATION}. Keep it concise and engaging."
        response = await asyncio.to_thread(gemini_model.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini summary error for {headline}: {e}")
        return f"This news from {LOCATION} is about {category}: {headline}."

# Create voice message
def create_voice_message(text):
    try:
        client = texttospeech.TextToSpeechClient()
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(language_code="en-US", name=VOICE_ACCENT)
        audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.OGG_OPUS)
        response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
        voice_file = "news_explanation.ogg"
        with open(voice_file, "wb") as out:
            out.write(response.audio_content)
        return voice_file
    except Exception as e:
        logger.error(f"Voice message error: {e}")
        return None

# Send Telegram message
async def send_telegram_message(chat_id, headline, article_url, category, sentiment):
    try:
        message = f"News ({category}, {sentiment}): {headline}\nLink: {article_url}\nReply 'More about {headline}' or 'Read full article'"
        logger.info(f"Attempting to send message to {chat_id}: {message}")
        await bot_app.bot.send_message(chat_id=chat_id, text=message)
        logger.info("Text message sent successfully")

        # Generate and send voice message
        summary = await generate_summary(headline, category)
        voice_file = create_voice_message(summary)
        if voice_file:
            with open(voice_file, 'rb') as audio:
                await bot_app.bot.send_voice(chat_id=chat_id, voice=audio, duration=30)
            os.remove(voice_file)
            logger.info("Voice message sent successfully")

        # Store in database
        c.execute("INSERT INTO news (headline, url, category, sentiment, timestamp) VALUES (?, ?, ?, ?, datetime('now'))",
                  (headline, article_url, category, sentiment))
        conn.commit()
        logger.info("Message stored in database")
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")

# Webhook handler using aiohttp
async def telegram_webhook(request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        if update.message:
            chat_id = update.message.chat_id
            text = update.message.text.lower()

            if 'my interests' in text:
                new_interests = text.replace('my interests', '').strip().split(',')
                INTERESTS.clear()
                INTERESTS.extend([i.strip() for i in new_interests if i.strip()])
                await bot_app.bot.send_message(chat_id=chat_id, text=f"Updated interests to {INTERESTS}")
            elif 'more about' in text:
                headline = text.replace('more about', '').strip()
                summary = await generate_summary(headline, 'general')
                await bot_app.bot.send_message(chat_id=chat_id, text=summary)
            elif 'read full article' in text:
                c.execute("SELECT url FROM news ORDER BY id DESC LIMIT 1")
                url = c.fetchone()
                if url:
                    await bot_app.bot.send_message(chat_id=chat_id, text=f"Full article: {url[0]}")
                else:
                    await bot_app.bot.send_message(chat_id=chat_id, text="No articles found.")

        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500)

# Schedule news sending
async def send_scheduled_news():
    logger.info("Starting send_scheduled_news")
    headlines = await fetch_headlines()
    if not headlines:
        logger.warning("No headlines found")
        return

    chat_id = "5556168938"
    logger.info(f"Sending to chat_id: {chat_id}")
    for headline, url, category in headlines:
        sentiment = analyze_sentiment(headline)
        logger.info(f"Sending headline: {headline}")
        await send_telegram_message(chat_id, headline, url, category, sentiment)

def schedule_notifications():
    for t in NOTIFICATION_TIMES:
        logger.info(f"Scheduling task at {t}")
        schedule.every().day.at(t).do(lambda: asyncio.create_task(send_scheduled_news()))

async def run_scheduler():
    while True:
        schedule.run_pending()
        await asyncio.sleep(60)

# Start Telegram bot and aiohttp server
async def main():
    logger.info(f"Current time: {datetime.datetime.now(pytz.timezone('Asia/Kolkata'))}")
    async def set_webhook():
        logger.info("Setting webhook")
        await bot_app.bot.set_webhook(url="https://1ca6b7b5cb2bb66d29308334e252b613.serveo.net/telegram")

    await set_webhook()

    # Schedule notifications
    schedule_notifications()

    # Start aiohttp server
    app = web.Application()
    app.router.add_post('/telegram', telegram_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 5000)
    await site.start()

    # Run scheduler
    await run_scheduler()

if __name__ == "__main__":
    asyncio.run(main())