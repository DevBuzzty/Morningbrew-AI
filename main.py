import os
import io
import json
import logging
import requests
import time
from datetime import datetime, timedelta
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.cloud import texttospeech
from google.cloud import storage
from google.cloud import secretmanager
from pydub import AudioSegment
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# Version for easy debugging
VERSION = "4.2-VERTEX-STABLE"

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Config
PROJECT_ID = os.getenv("GCP_PROJECT")
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
REGION = "europe-west3"

def get_secret(name):
    try:
        client = secretmanager.SecretManagerServiceClient()
        res = client.access_secret_version(request={"name": f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"})
        return res.payload.data.decode("UTF-8").strip()
    except Exception as e:
        logger.error(f"Error fetching secret {name}: {e}")
        return None

def fetch_news():
    logger.info("Fetching news from NewsAPI...")
    api_key = get_secret("NEWS_API_KEY")
    if not api_key: return "Keine Nachrichten verfügbar."

    summary = []
    categories = ["general", "business", "technology"]
    for cat in categories:
        url = f"https://newsapi.org/v2/top-headlines?country=de&category={cat}&apiKey={api_key}"
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            articles = data.get("articles", [])[:5]
            summary.append(f"--- Category: {cat} ---")
            summary.extend([f"- {a['title']}: {a['description']}" for a in articles])
        except Exception:
            continue
    return "\n".join(summary)

def generate_script(news):
    logger.info("Generating script with Vertex AI (Gemini 1.5 Flash)...")

    # us-central1 is the most stable region for Gemini availability
    vertexai.init(project=PROJECT_ID, location="us-central1")

    model = GenerativeModel("gemini-1.5-flash")

    system_instruction = """
    Du bist ein erfahrener Podcast-Produzent. Erstelle ein Skript für ein 15-20 minütiges Gespräch (ca. 2500 Wörter).
    Sprecher:
    1. Jules: Weiblich, KI-Expertin, sehr optimistisch und energiegeladen.
    2. Basti: Männlich, kritischer Beobachter, hinterfragt Trends, eher ruhig.

    Struktur:
    - Intro: Begrüßung.
    - Politik: Analyse der Schlagzeilen.
    - Wirtschaft: Trends und Auswirkungen.
    - Deep Dive AI: Fokus auf Technologie.
    - Outro: Verabschiedung.

    Tonfall: Professionell aber locker, wie ein echtes Gespräch. Die beiden sollen wirklich debattieren.
    Sprache: Deutsch.
    Format: AUSSCHLIESSLICH ein JSON-Array von Objekten mit "speaker" ("Jules" oder "Basti") und "text".
    """

    prompt = f"Hier sind die News des Tages:\n{news}\n\nErstelle das Skript basierend auf der Systemanweisung."

    try:
        res = model.generate_content(
            prompt,
            generation_config=GenerationConfig(
                response_mime_type="application/json",
                temperature=0.8
            )
        )
        return json.loads(res.text)
    except Exception as e:
        logger.error(f"AI Error: {e}")
        # Log available models for debugging if it fails
        logger.info("Tip: Ensure Vertex AI API is enabled in your project.")
        raise e

def synthesize(script):
    logger.info("Synthesizing audio with Cloud TTS...")
    client = texttospeech.TextToSpeechClient()
    combined = AudioSegment.empty()

    voices = {
        "Jules": texttospeech.VoiceSelectionParams(language_code="de-DE", name="de-DE-Neural2-F"),
        "Basti": texttospeech.VoiceSelectionParams(language_code="de-DE", name="de-DE-Neural2-B")
    }
    cfg = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)

    for line in script:
        speaker = line.get("speaker", "Jules")
        text = line.get("text", "")
        if not text: continue

        voice = voices.get(speaker, voices["Jules"])
        chunks = [text[i:i+4800] for i in range(0, len(text), 4800)]
        for chunk in chunks:
            s_input = texttospeech.SynthesisInput(text=chunk)
            response = client.synthesize_speech(input=s_input, voice=voice, audio_config=cfg)
            segment = AudioSegment.from_file(io.BytesIO(response.audio_content), format="mp3")
            combined += segment

        combined += AudioSegment.silent(duration=600)

    out = io.BytesIO()
    combined.export(out, format="mp3")
    return out.getvalue()

def main():
    logger.info(f"Starting Podcast Briefing version {VERSION}")
    if not all([PROJECT_ID, BUCKET_NAME, RECIPIENT_EMAIL, SENDER_EMAIL]):
        logger.error("Missing required Environment Variables!")
        return

    try:
        news = fetch_news()
        script = generate_script(news)
        audio = synthesize(script)

        filename = f"briefing_{datetime.now().strftime('%Y%m%d_%H%M')}.mp3"
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(audio, content_type="audio/mpeg")

        sa_email = f"podcast-generator-sa@{PROJECT_ID}.iam.gserviceaccount.com"
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=24),
            method="GET",
            service_account_email=sa_email
        )

        sg_key = get_secret("SENDGRID_API_KEY")
        if sg_key:
            sg = SendGridAPIClient(sg_key)
            mail = Mail(from_email=SENDER_EMAIL, to_emails=RECIPIENT_EMAIL,
                        subject=f"Dein Audio-Briefing ({datetime.now().strftime('%d.%m.%Y')})",
                        plain_text_content=f"Guten Morgen!\n\nHier ist dein tägliches Podcast-Briefing: {url}\n\nDer Link ist 24 Stunden gültig.")
            sg.send(mail)

        logger.info("Process completed successfully.")
    except Exception as e:
        logger.error(f"Fatal crash: {e}")
        raise e

if __name__ == "__main__":
    main()
