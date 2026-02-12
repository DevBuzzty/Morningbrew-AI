import os
import json
import logging
import requests
import time
import smtplib
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
try:
    from vertexai.vision_models import ImageGenerationModel
except ImportError:
    from vertexai.preview.vision_models import ImageGenerationModel
from google.cloud import secretmanager

# --- CONFIG & LOGGING ---
VERSION = "10.0-DEEP-DIVE"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT")
RECIPIENT_RAW = str(os.getenv("RECIPIENT_EMAIL", "")).strip()
RECIPIENT_LIST = [e.strip() for e in RECIPIENT_RAW.split(",") if "example.com" not in e and e.strip()]
RECIPIENT_EMAIL = ",".join(RECIPIENT_LIST)
SENDER_EMAIL = str(os.getenv("SENDER_EMAIL", "")).strip()

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

# --- STEP 1: NEWS FETCHING (EXPANDED) ---
def fetch_news():
    logger.info("Step 1: Fetching Extensive News Pool...")
    api_key = get_secret("NEWS_API_KEY")
    if not api_key:
        return []

    headers = {"User-Agent": "MorgenpostDeepDive/1.0 (GoogleCloudRun)"}
    # We fetch more categories to give Gemini a broader choice
    categories = ["general", "business", "technology", "science", "health"]
    news_data = []

    for cat in categories:
        try:
            # Increase pageSize to get more raw material
            url = f"https://newsapi.org/v2/top-headlines?country=de&category={cat}&pageSize=20&apiKey={api_key}"
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            articles = data.get("articles", [])

            # Fallback for small categories or empty feeds
            if not articles:
                url = f"https://newsapi.org/v2/top-headlines?language=en&category={cat}&pageSize=20&apiKey={api_key}"
                r = requests.get(url, headers=headers, timeout=10)
                data = r.json()
                articles = data.get("articles", [])

            logger.info(f"Fetched {len(articles)} for {cat}")
            for a in articles:
                news_data.append({
                    "category": cat.upper(),
                    "title": a.get("title"),
                    "description": a.get("description"),
                    "source": a.get("source", {}).get("name"),
                    "url": a.get("url"),
                    "image": a.get("urlToImage")
                })
        except Exception as e:
            logger.warning(f"Failed {cat}: {e}")

    return news_data

# --- STEP 2: IMAGE GENERATION (IMAGEN) ---
def generate_hero_image(topic_summary):
    logger.info("Step 2: Generating AI Hero Image...")
    try:
        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-001")
        prompt = f"A high-end cinematic editorial digital art illustration for a deep news magazine cover. Subject: {topic_summary}. Style: Modern, professional, balanced colors, 4k resolution."

        images = model.generate_images(
            prompt=prompt,
            number_of_images=1,
            aspect_ratio="16:9",
            guidance_scale=21.0
        )
        if images:
            img_obj = images[0]
            if hasattr(img_obj, "_image_bytes"): return img_obj._image_bytes
            if hasattr(img_obj, "image_bytes"): return img_obj.image_bytes
            temp_path = "/tmp/hero.png"
            img_obj.save(temp_path, include_generation_parameters=False)
            with open(temp_path, "rb") as f:
                return f.read()
    except Exception as e:
        logger.warning(f"Imagen failed: {e}")
    return None

# --- STEP 3: AI DEEP DIVE GENERATION ---
def generate_magazine_html(news_data):
    logger.info("Step 3: Generating Long-Form Deep Dive Briefing...")
    if not news_data:
        return "<h1>Guten Morgen</h1><p>Heute gibt es leider keine News.</p>"

    news_context = ""
    for i, n in enumerate(news_data, 1):
        news_context += f"[{i}] {n['category']} - {n['title']}: {n['description']} (Source: {n['source']}, URL: {n['url']}, ImageURL: {n['image']})\n\n"

    regions = ["us-central1", "europe-west1", "europe-west3"]
    # 2.0-Flash is great for long outputs, 1.5-Pro for reasoning
    models = ["gemini-2.0-flash", "gemini-1.5-pro"]

    system_instruction = """
    Rolle: Chefredakteur des 'Morgenpost Deep-Dive' Magazins.
    Aufgabe: Erstelle ein sehr langes, detailliertes und informatives E-Mail Briefing.

    STRUKTUR:
    1. Einleitung: Kurze Analyse der globalen Stimmung heute.
    2. Hauptsektionen:
       A. Politik & Weltgeschehen
       B. Internationale & Nationale Wirtschaft
       C. Technik & Künstliche Intelligenz (KI) - ESSENZIELL
       D. Wissenschaft & Gesellschaft

    REGELN PRO SEKTION:
    - Wähle die Top 3 Nachrichten pro Sektion aus den bereitgestellten Daten.
    - Gib JEDER Nachricht eine eigene prägnante Überschrift (<h3>).
    - Schreibe zu jeder Nachricht einen ausführlichen Text (200-300 Wörter). Analysiere Hintergründe und Folgen.
    - Fasse Nachrichten NICHT zusammen; jede der 3 Nachrichten braucht ihren eigenen Raum.
    - Wenn eine 'ImageURL' vorhanden ist, binde sie mit <img src='...' style='width: 100%; border-radius: 8px; margin: 15px 0;'> ein.
    - Referenziere Quellen im Text mit [1], [2] etc.

    FORMATIERUNG:
    - Nutze sauberes HTML (<h2> für Sektionen, <h3> für Nachrichten, <p> für Text).
    - Erstelle am Ende eine Sektion 'Quellenverzeichnis' mit klickbaren Links.
    - Sprache: Deutsch. Tonfall: Analytisch, professionell, fesselnd.
    - Ziel: Der Leser soll nach der Lektüre umfassend informiert sein.
    """

    for region in regions:
        try:
            vertexai.init(project=PROJECT_ID, location=region)
            for model_name in models:
                try:
                    model = GenerativeModel(model_name=model_name)
                    res = model.generate_content(
                        f"{system_instruction}\n\nRohdaten:\n{news_context}",
                        generation_config=GenerationConfig(temperature=0.75, max_output_tokens=8192)
                    )
                    if res.text:
                        return res.text
                except Exception: continue
        except Exception: continue
    return "<p>Fehler bei der Inhaltsgenerierung.</p>"

# --- STEP 4: EMAIL DELIVERY ---
def send_magazine(html_body, image_bytes):
    logger.info("Step 4: Sending Deep Dive Magazine...")
    password = get_secret("GMAIL_APP_PASSWORD")
    if not password: return

    try:
        msg = MIMEMultipart('related')
        msg['Subject'] = f"Morgenpost Deep-Dive ({datetime.now().strftime('%d.%m.%Y')})"
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECIPIENT_EMAIL

        msg_alt = MIMEMultipart('alternative')
        msg.attach(msg_alt)

        full_html = f"""
        <html>
            <body style='font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif; line-height: 1.7; color: #1a1a1a; max-width: 900px; margin: auto; background-color: #ffffff;'>
                <div style='background-color: #000000; padding: 40px 20px; text-align: center; color: #ffffff;'>
                    <h1 style='margin: 0; letter-spacing: 5px; text-transform: uppercase; font-size: 36px;'>Morgenpost</h1>
                    <p style='margin: 10px 0 0 0; opacity: 0.7;'>DEEP DIVE EDITION | {datetime.now().strftime('%A, %d. %B %Y')}</p>
                </div>
                {'<div style="text-align: center;"><img src="cid:hero_image" style="width: 100%; height: auto; display: block;"></div>' if image_bytes else ''}
                <div style='padding: 40px; border: 1px solid #eee; border-top: none;'>
                    {html_body}
                </div>
                <div style='background-color: #f9f9f9; padding: 30px; text-align: center; font-size: 13px; color: #666; border-top: 1px solid #eee;'>
                    <p>Du erhältst diesen Briefing, weil du dich für den täglichen Deep Dive angemeldet hast.</p>
                    <p>&copy; {datetime.now().year} Morgenpost Redaktion</p>
                </div>
            </body>
        </html>
        """

        html_part = MIMEText(full_html, 'html')
        msg_alt.attach(html_part)

        if image_bytes:
            img = MIMEImage(image_bytes)
            img.add_header('Content-ID', '<hero_image>')
            msg.attach(img)

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, password)
            server.send_message(msg)
        logger.info("Deep Dive Magazine sent successfully.")
    except Exception as e:
        logger.error(f"Email Error: {e}")

# --- MAIN ---
def main():
    logger.info(f"--- Morgenpost Magazine v{VERSION} ---")
    if not all([PROJECT_ID, SENDER_EMAIL]):
        logger.error("Basic Config missing!")
        return

    if not RECIPIENT_EMAIL:
        logger.error("No valid RECIPIENT_EMAIL found.")
        return

    vertexai.init(project=PROJECT_ID, location="us-central1")

    news = fetch_news()
    summary_topics = ", ".join([n['title'] for n in news[:5]])

    image = generate_hero_image(summary_topics)
    magazine = generate_magazine_html(news)

    send_magazine(magazine, image)

if __name__ == "__main__":
    main()
