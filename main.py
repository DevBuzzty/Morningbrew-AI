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
VERSION = "4.6-VERTEX-AUTO"

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Config
PROJECT_ID = os.getenv("GCP_PROJECT")
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")

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
    logger.info(f"Generating script (v{VERSION}) with Vertex AI...")

    # Priority List: 2.0 then 1.5
    models_to_try = [
        "gemini-2.0-flash-exp",
        "gemini-1.5-flash-002",
        "gemini-1.5-flash",
        "gemini-1.5-pro-002"
    ]

    # We use us-central1 for AI calls to bypass regional restrictions
    target_location = "us-central1"
    vertexai.init(project=PROJECT_ID, location=target_location)

    system_instruction = """
    Du bist ein erstklassiger Podcast-Redakteur. Erstelle ein Skript für ein 15-20 minütiges Gespräch.
    Sprecher:
    - Jules: Weiblich, Tech-Optimistin, energiegeladen.
    - Basti: Männlich, kritischer Beobachter, hinterfragt Hypes.
    Sprache: Deutsch. Tonfall: Professionell aber konversationsorientiert.
    Format: AUSSCHLIESSLICH ein JSON-Array von Objekten mit "speaker" und "text".
    """

    last_err = None
    for model_name in models_to_try:
        try:
            logger.info(f"Attempting AI call with {model_name} in {target_location}...")
            model = GenerativeModel(model_name=model_name)
            res = model.generate_content(
                f"{system_instruction}\n\nNews: {news}",
                generation_config=GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.8
                )
            )
            if res and res.text:
                logger.info(f"Success with {model_name}")
                return json.loads(res.text)
        except Exception as e:
            logger.warning(f"Model {model_name} failed: {e}")
            last_err = e
            time.sleep(2)

    raise Exception(f"All models in {target_location} failed. Last error: {last_err}")

def synthesize(script):
    logger.info("Synthesizing audio...")
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
        chunks = [text[i:i+4500] for i in range(0, len(text), 4500)]
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
    logger.info(f"Starting Podcast Generator v{VERSION}")
    if not all([PROJECT_ID, BUCKET_NAME, RECIPIENT_EMAIL, SENDER_EMAIL]):
        logger.error("Missing ENV VARS!")
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
        url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=24), method="GET", service_account_email=sa_email)

        sg_key = get_secret("SENDGRID_API_KEY")
        if sg_key:
            sg = SendGridAPIClient(sg_key)
            mail = Mail(from_email=SENDER_EMAIL, to_emails=RECIPIENT_EMAIL,
                        subject=f"Dein Audio-Briefing ({datetime.now().strftime('%d.%m.%Y')})",
                        plain_text_content=f"Guten Morgen! Hier ist dein Podcast: {url}")
            sg.send(mail)

        logger.info("Process completed successfully.")
    except Exception as e:
        logger.error(f"Fatal crash: {e}")
        raise e

if __name__ == "__main__":
    main()
