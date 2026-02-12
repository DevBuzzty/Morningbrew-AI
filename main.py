import os
import json
import logging
import requests
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.cloud import secretmanager

# --- CONFIG & LOGGING ---
VERSION = "8.0-GMAIL-SMTP"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
SENDER_EMAIL = os.getenv("SENDER_EMAIL") # This should be your Gmail address

# --- UTILS ---
def get_secret(name):
    try:
        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"
        res = client.access_secret_version(request={"name": path})
        return res.payload.data.decode("UTF-8").strip()
    except Exception as e:
        logger.error(f"Secret {name} failed: {e}")
        return None

# --- STEP 1: NEWS FETCHING ---
def fetch_news():
    logger.info("Step 1: Fetching News Headlines...")
    api_key = get_secret("NEWS_API_KEY")
    if not api_key: return "Keine aktuellen Nachrichten gefunden."

    categories = ["general", "business", "technology", "science"]
    all_articles = []

    for cat in categories:
        try:
            url = f"https://newsapi.org/v2/top-headlines?country=de&category={cat}&apiKey={api_key}"
            r = requests.get(url, timeout=10)
            articles = r.json().get("articles", [])[:5]
            for a in articles:
                all_articles.append(f"[{cat.upper()}] {a['title']}: {a.get('description', '')}")
        except Exception as e:
            logger.warning(f"Failed category {cat}: {e}")

    return "\n".join(all_articles)

# --- STEP 2: AI NEWS PROCESSING ---
def generate_briefing(news_content):
    logger.info(f"Step 2: Generating Text Briefing (v{VERSION})...")

    regions = ["us-central1", "europe-west1", "europe-west3"]
    models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]

    system_instruction = """
    Du bist ein Redakteur für das tägliche "Morgenpost" Briefing.
    Deine Aufgabe ist es, die bereitgestellten Nachrichten zu analysieren und eine strukturierte, leicht lesbare Zusammenfassung zu erstellen.

    Sprache: Deutsch.
    Tonfall: Professionell, informativ und prägnant.

    Struktur:
    1. Kurze Begrüßung.
    2. Die wichtigsten 3-5 Schlagzeilen mit kurzer Einordnung.
    3. Ein kurzer Ausblick/Fazit.

    Nutze Markdown für die Formatierung (Überschriften, Aufzählungszeichen).
    """

    for region in regions:
        try:
            vertexai.init(project=PROJECT_ID, location=region)
            for model_name in models:
                try:
                    logger.info(f"Attempting {model_name} in {region}...")
                    model = GenerativeModel(model_name=model_name)
                    response = model.generate_content(
                        f"{system_instruction}\n\nNews-Daten:\n{news_content}",
                        generation_config=GenerationConfig(
                            temperature=0.7,
                            max_output_tokens=2048
                        )
                    )
                    if response.text:
                        logger.info(f"Briefing generated successfully using {model_name}.")
                        return response.text
                except Exception as e:
                    logger.warning(f"Model {model_name} in {region} failed: {e}")
        except Exception as e:
            logger.error(f"Region {region} init failed: {e}")

    raise Exception("Model discovery failed completely.")

# --- STEP 3: EMAIL DELIVERY (GMAIL SMTP) ---
def send_gmail(subject, body):
    logger.info("Step 3: Delivering via Gmail SMTP...")
    password = get_secret("GMAIL_APP_PASSWORD")

    if not password:
        logger.error("GMAIL_APP_PASSWORD missing in Secret Manager.")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECIPIENT_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, password)
            server.send_message(msg)

        logger.info("Email sent successfully via Gmail.")
        return True
    except Exception as e:
        logger.error(f"Gmail SMTP Error: {e}")
        return False

# --- MAIN EXECUTION ---
def main():
    start_time = time.time()
    logger.info(f"--- Morgenpost Gmail Edition v{VERSION} Started ---")

    if not all([PROJECT_ID, RECIPIENT_EMAIL, SENDER_EMAIL]):
        logger.error("Environment variables missing!")
        return

    try:
        # 1. Fetch
        news = fetch_news()

        # 2. Process with AI
        briefing_text = generate_briefing(news)

        # 3. Deliver
        subject = f"Dein Morgen-Briefing ({datetime.now().strftime('%d.%m.%Y')})"
        success = send_gmail(subject, briefing_text)

        if not success:
            logger.error("Failed to deliver briefing. Check logs for SMTP errors.")

        duration = time.time() - start_time
        logger.info(f"Total processing time: {duration:.2f}s")
        logger.info("--- Processing Complete ---")

    except Exception as e:
        logger.critical(f"FATAL ERROR: {e}", exc_info=True)
        raise e

if __name__ == "__main__":
    main()
