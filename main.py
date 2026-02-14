import os
import io
import json
import logging
import requests
import time
import smtplib
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
try:
    from vertexai.vision_models import ImageGenerationModel
except ImportError:
    from vertexai.preview.vision_models import ImageGenerationModel
from google.cloud import texttospeech
from google.cloud import storage
from google.cloud import secretmanager
from pydub import AudioSegment

# --- CONFIG & LOGGING ---
VERSION = "11.0-MULTIMEDIA-EDITION"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT")
BUCKET_NAME = f"{PROJECT_ID}-briefing-podcasts"
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

# --- STEP 1: NEWS FETCHING ---
def fetch_news():
    logger.info("Step 1: Fetching Extensive News Pool...")
    api_key = get_secret("NEWS_API_KEY")
    if not api_key: return []

    headers = {"User-Agent": "MorgenpostMultimedia/1.0 (GoogleCloudRun)"}
    categories = ["general", "business", "technology", "science", "health"]
    news_data = []

    for cat in categories:
        try:
            url = f"https://newsapi.org/v2/top-headlines?country=de&category={cat}&pageSize=20&apiKey={api_key}"
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            articles = data.get("articles", [])

            if not articles:
                url = f"https://newsapi.org/v2/top-headlines?language=en&category={cat}&pageSize=20&apiKey={api_key}"
                r = requests.get(url, headers=headers, timeout=10)
                data = r.json()
                articles = data.get("articles", [])

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

# --- STEP 2: IMAGE GENERATION ---
def generate_hero_image(topic_summary):
    logger.info("Step 2: Generating AI Hero Image...")
    try:
        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-001")
        prompt = f"Professional cinematic editorial illustration for a deep news magazine cover. Subject: {topic_summary}. Digital art, high resolution, soft balanced colors."
        images = model.generate_images(prompt=prompt, number_of_images=1, aspect_ratio="16:9")
        if images:
            img_obj = images[0]
            if hasattr(img_obj, "_image_bytes"): return img_obj._image_bytes
            temp_path = "/tmp/hero.png"
            img_obj.save(temp_path, include_generation_parameters=False)
            with open(temp_path, "rb") as f: return f.read()
    except Exception as e: logger.warning(f"Imagen failed: {e}")
    return None

# --- STEP 3: AI MAGAZINE & PODCAST SCRIPT ---
def generate_content(news_data):
    logger.info("Step 3: Generating HTML Briefing & Podcast Script...")
    if not news_data: return None, None

    news_context = ""
    for i, n in enumerate(news_data, 1):
        news_context += f"[{i}] {n['category']} - {n['title']}: {n['description']} (Source: {n['source']}, URL: {n['url']})\n\n"

    system_instruction = """
    Rolle: Chefredakteur & Podcast-Produzent.
    AUFGABE 1 (HTML): Erstelle ein extrem detailliertes E-Mail Briefing (200-300 Wörter pro Top-Nachricht).
    Struktur: Politik, Wirtschaft, Technik & KI, Wissenschaft. Jede News mit <h3> Überschrift. Nutze [1], [2] für Quellen.

    AUFGABE 2 (Podcast Script): Erstelle ein Gespräch zwischen Jules (Tech-Fan) und Basti (Kritiker) im NotebookLM-Stil.
    Das Gespräch soll die oben geschriebenen Inhalte lebendig diskutieren.
    Format für Podcast: JSON-Array von Objekten {"speaker": "Jules"|"Basti", "text": "..."}.

    Ausgabeformat: Ein JSON mit den Schlüsseln "html" und "podcast_script".
    Sprache: Deutsch.
    """

    regions = ["us-central1", "europe-west1", "europe-west3"]
    for region in regions:
        try:
            vertexai.init(project=PROJECT_ID, location=region)
            model = GenerativeModel(model_name="gemini-2.0-flash")
            res = model.generate_content(
                f"{system_instruction}\n\nDaten:\n{news_context}",
                generation_config=GenerationConfig(response_mime_type="application/json", temperature=0.75, max_output_tokens=8192)
            )
            data = json.loads(res.text)
            return data.get("html"), data.get("podcast_script")
        except Exception: continue
    return None, None

# --- STEP 4: PARALLEL AUDIO SYNTHESIS ---
def tts_worker(idx, speaker, text, client):
    voice_map = {
        "Jules": {"name": "de-DE-Neural2-F", "rate": "1.05"},
        "Basti": {"name": "de-DE-Neural2-B", "rate": "0.95"}
    }
    v = voice_map.get(speaker, voice_map["Jules"])
    s_input = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(language_code="de-DE", name=v["name"])
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3, speaking_rate=float(v["rate"]))
    try:
        res = client.synthesize_speech(input=s_input, voice=voice_params, audio_config=audio_config)
        return idx, res.audio_content
    except Exception: return idx, None

def synthesize_podcast(script):
    logger.info("Step 4: Synthesizing Audio Podcast...")
    client = texttospeech.TextToSpeechClient()
    results = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(tts_worker, i, s["speaker"], s["text"], client): i for i, s in enumerate(script)}
        for future in as_completed(futures):
            idx, audio = future.result()
            if audio: results[idx] = audio

    combined = AudioSegment.empty()
    for i in range(len(script)):
        if i in results:
            seg = AudioSegment.from_file(io.BytesIO(results[i]), format="mp3")
            combined += seg + AudioSegment.silent(duration=600)

    out = io.BytesIO()
    combined.export(out, format="mp3", bitrate="128k")
    return out.getvalue()

# --- STEP 5: EMAIL DELIVERY ---
def send_email(html_body, image_bytes, podcast_url):
    logger.info("Step 5: Sending Multimedia Magazine...")
    password = get_secret("GMAIL_APP_PASSWORD")
    if not password: return

    msg = MIMEMultipart('related')
    msg['Subject'] = f"Morgenpost Multimedia Edition ({datetime.now().strftime('%d.%m.%Y')})"
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECIPIENT_EMAIL

    msg_alt = MIMEMultipart('alternative')
    msg.attach(msg_alt)

    podcast_btn = ""
    if podcast_url:
        podcast_btn = f"""
        <div style='text-align: center; margin: 30px 0;'>
            <a href='{podcast_url}' style='background-color: #000; color: #fff; padding: 15px 30px; text-decoration: none; border-radius: 50px; font-weight: bold; font-size: 18px;'>
                ▶ PODCAST ABSPIELEN (Audio-Zusammenfassung)
            </a>
            <p style='font-size: 12px; color: #777; margin-top: 10px;'>Basierend auf deinen News & Quellen</p>
        </div>
        """

    full_html = f"""
    <html>
        <body style='font-family: Arial, sans-serif; line-height: 1.7; color: #1a1a1a; max-width: 850px; margin: auto;'>
            <div style='background-color: #000; padding: 40px; text-align: center; color: #fff;'>
                <h1 style='margin: 0; letter-spacing: 4px; text-transform: uppercase;'>Morgenpost</h1>
                <p style='opacity: 0.7;'>{datetime.now().strftime('%A, %d. %B %Y')}</p>
            </div>
            {'<img src="cid:hero_image" style="width: 100%; display: block;">' if image_bytes else ''}
            <div style='padding: 40px; border: 1px solid #eee;'>
                {podcast_btn}
                {html_body}
            </div>
        </body>
    </html>
    """
    msg_alt.attach(MIMEText(full_html, 'html'))

    if image_bytes:
        img = MIMEImage(image_bytes); img.add_header('Content-ID', '<hero_image>'); msg.attach(img)

    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.starttls(); server.login(SENDER_EMAIL, password); server.send_message(msg)
    logger.info("Email sent.")

# --- MAIN ---
def main():
    logger.info(f"--- Morgenpost v{VERSION} ---")
    if not all([PROJECT_ID, SENDER_EMAIL, RECIPIENT_EMAIL]): return

    news = fetch_news()
    if not news:
        logger.warning("No news found today.")
        return

    html_briefing, podcast_script = generate_content(news)
    if not html_briefing:
        logger.error("Failed to generate HTML content.")
        return

    hero_img = generate_hero_image(", ".join([n['title'] for n in news[:3]]))

    podcast_url = None
    if podcast_script:
        audio = synthesize_podcast(podcast_script)
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(f"podcast_{datetime.now().strftime('%Y%m%d_%H%M')}.mp3")
        blob.upload_from_string(audio, content_type="audio/mpeg")
        sa_email = f"morgenpost-gmail-sa@{PROJECT_ID}.iam.gserviceaccount.com"
        podcast_url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=24), method="GET", service_account_email=sa_email)

    send_email(html_briefing, hero_img, podcast_url)

if __name__ == "__main__":
    main()
