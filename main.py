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
VERSION = "9.0-MAGAZINE-EDITION"
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

# --- STEP 1: NEWS FETCHING (EXTENDED) ---
def fetch_news():
    logger.info("Step 1: Fetching News with Metadata...")
    api_key = get_secret("NEWS_API_KEY")
    if not api_key:
        return []

    headers = {"User-Agent": "MorgenpostMagazine/1.0 (GoogleCloudRun)"}
    categories = ["general", "business", "technology"]
    news_data = []

    for cat in categories:
        try:
            url = f"https://newsapi.org/v2/top-headlines?country=de&category={cat}&apiKey={api_key}"
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            articles = data.get("articles", [])

            # Fallback
            if not articles:
                url = f"https://newsapi.org/v2/top-headlines?language=en&category={cat}&apiKey={api_key}"
                r = requests.get(url, headers=headers, timeout=10)
                data = r.json()
                articles = data.get("articles", [])

            logger.info(f"Fetched {len(articles)} for {cat}")
            for a in articles[:3]: # 3 per category for depth
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
        prompt = f"A professional, high-quality editorial illustration for a morning news briefing about: {topic_summary}. Digital art style, clean, informative, soft lighting."

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
            # Fallback if we can't find the attribute easily
            temp_path = "/tmp/hero.png"
            img_obj.save(temp_path, include_generation_parameters=False)
            with open(temp_path, "rb") as f:
                return f.read()
    except Exception as e:
        logger.warning(f"Imagen failed: {e}")
    return None

# --- STEP 3: AI MAGAZINE GENERATION ---
def generate_magazine_html(news_data):
    logger.info("Step 3: Generating Detailed HTML Briefing...")
    if not news_data:
        return "<h1>Guten Morgen</h1><p>Heute gibt es leider keine News.</p>"

    # Prepare context for Gemini
    news_context = ""
    for i, n in enumerate(news_data, 1):
        news_context += f"[{i}] {n['category']} - {n['title']}: {n['description']} (Source: {n['source']}, URL: {n['url']}, ImageURL: {n['image']})\n\n"

    regions = ["us-central1", "europe-west1", "europe-west3"]
    models = ["gemini-2.0-flash", "gemini-1.5-pro"]

    system_instruction = """
    Du bist ein Chefredakteur für ein exklusives digitales Morgenmagazin.
    AUFGABE: Erstelle ein extrem detailliertes Briefing (200-300 Wörter pro Hauptthema).

    REGELN:
    1. Ausgabe in sauberem HTML (nutze <h2>, <p>, <ul>, <li>). Keine <html>/<body> Tags.
    2. Strukturiere nach Themen: Politik, Internationale Wirtschaft, Nationale Wirtschaft, Tech.
    3. Wenn eine 'ImageURL' vorhanden ist, binde sie dezent mit <img src='...' style='width: 100%; max-width: 400px; border-radius: 8px; margin: 10px 0;'> ein.
    4. Referenziere Quellen im Text mit [1], [2] etc.
    5. Erstelle am Ende eine Sektion 'Quellen' mit klickbaren Links (<a href='...'>).
    6. Schreibe packend, analytisch und tiefgründig.
    7. Wenn ein Thema sehr wichtig ist, schreibe mehr als 300 Wörter.
    """

    for region in regions:
        try:
            vertexai.init(project=PROJECT_ID, location=region)
            for model_name in models:
                try:
                    model = GenerativeModel(model_name=model_name)
                    res = model.generate_content(
                        f"{system_instruction}\n\nDaten:\n{news_context}",
                        generation_config=GenerationConfig(temperature=0.8, max_output_tokens=8192)
                    )
                    if res.text:
                        return res.text
                except Exception: continue
        except Exception: continue
    return "<p>Fehler bei der Generierung.</p>"

# --- STEP 4: EMAIL DELIVERY (HTML + IMAGE) ---
def send_magazine(html_body, image_bytes):
    logger.info("Step 4: Sending Magazine Email...")
    password = get_secret("GMAIL_APP_PASSWORD")
    if not password: return

    try:
        msg = MIMEMultipart('related')
        msg['Subject'] = f"Dein Morgen-Magazin ({datetime.now().strftime('%d.%m.%Y')})"
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECIPIENT_EMAIL

        # Alternative for clients that don't like HTML
        msg_alt = MIMEMultipart('alternative')
        msg.attach(msg_alt)

        # HTML Content
        full_html = f"""
        <html>
            <body style='font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: auto;'>
                <div style='background-color: #f4f4f4; padding: 20px; text-align: center; border-radius: 8px 8px 0 0;'>
                    <h1 style='color: #2c3e50; margin-bottom: 5px;'>Morgenpost Magazin</h1>
                    <p style='margin-top: 0; color: #777;'>{datetime.now().strftime('%A, %d. %B %Y')}</p>
                </div>
                {'<div style="text-align: center;"><img src="cid:hero_image" style="width: 100%; height: auto; border-bottom: 4px solid #f4f4f4;"></div>' if image_bytes else ''}
                <div style='padding: 30px; background-color: white; border: 1px solid #eee;'>
                    {html_body}
                </div>
                <div style='background-color: #f4f4f4; padding: 20px; text-align: center; font-size: 12px; color: #777; border-radius: 0 0 8px 8px;'>
                    <p>Dies ist dein automatisiertes KI-Briefing. Bleib informiert!</p>
                </div>
            </body>
        </html>
        """

        html_part = MIMEText(full_html, 'html')
        msg_alt.attach(html_part)

        # Attach Image
        if image_bytes:
            img = MIMEImage(image_bytes)
            img.add_header('Content-ID', '<hero_image>')
            msg.attach(img)

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, password)
            server.send_message(msg)
        logger.info("Magazine sent successfully.")
    except Exception as e:
        logger.error(f"Email Error: {e}")

# --- MAIN ---
def main():
    logger.info(f"--- Morgenpost Magazine v{VERSION} ---")
    if not all([PROJECT_ID, SENDER_EMAIL]):
        logger.error("Basic Config (Project/Sender) missing!")
        return

    if not RECIPIENT_EMAIL:
        logger.error("No valid RECIPIENT_EMAIL found (Check if you still have 'example.com' in the config).")
        return

    # Initialize Vertex AI globally for the main region first
    vertexai.init(project=PROJECT_ID, location="us-central1")

    news = fetch_news()
    # Use the titles of the first 3 news items for the image prompt
    summary = ", ".join([n['title'] for n in news[:3]])

    image = generate_hero_image(summary)
    magazine = generate_magazine_html(news)

    send_magazine(magazine, image)

if __name__ == "__main__":
    main()
