import os
import json
import logging
import requests
import time
from datetime import datetime

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.cloud import secretmanager
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# --- CONFIG & LOGGING ---
VERSION = "7.0-TEXT-ONLY"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")

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

# --- MAIN EXECUTION ---
def main():
    start_time = time.time()
    logger.info(f"--- Morgenpost Text Briefing v{VERSION} Started ---")

    if not all([PROJECT_ID, RECIPIENT_EMAIL, SENDER_EMAIL]):
        logger.error("Environment variables missing!")
        return

    try:
        # 1. Fetch
        news = fetch_news()

        # 2. Process with AI
        briefing_text = generate_briefing(news)

        # 3. Deliver via Email
        sg_key = get_secret("SENDGRID_API_KEY")
        if sg_key:
            try:
                sg = SendGridAPIClient(sg_key)
                mail = Mail(
                    from_email=SENDER_EMAIL,
                    to_emails=RECIPIENT_EMAIL,
                    subject=f"Dein Morgen-Briefing ({datetime.now().strftime('%d.%m.%Y')})",
                    plain_text_content=briefing_text
                )
                sg.send(mail)
                logger.info("Email sent successfully.")
            except Exception as sg_err:
                if "403" in str(sg_err):
                    logger.error("SENDGRID ERROR 403 (Forbidden): Dies liegt meist an einer fehlenden 'Sender Authentication'.")
                    logger.error(f"Bitte stelle sicher, dass '{SENDER_EMAIL}' in deinem SendGrid Account als 'Single Sender' verifiziert ist.")
                elif "401" in str(sg_err):
                    logger.error("SENDGRID ERROR 401 (Unauthorized): Der API Key ist ungültig oder hat keine Berechtigung.")
                else:
                    logger.error(f"SendGrid Error: {sg_err}")
                # We don't want to crash the whole job if only the mail fails, but we log it clearly
        else:
            logger.error("SENDGRID_API_KEY missing. Printing briefing to logs:")
            print(briefing_text)

        duration = time.time() - start_time
        logger.info(f"Total processing time: {duration:.2f}s")
        logger.info("--- Processing Complete ---")

    except Exception as e:
        logger.critical(f"FATAL ERROR: {e}", exc_info=True)
        raise e

if __name__ == "__main__":
    main()
