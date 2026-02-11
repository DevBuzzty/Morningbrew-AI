import os
import io
import json
import logging
import requests
import time
import html
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.cloud import texttospeech
from google.cloud import storage
from google.cloud import secretmanager
from pydub import AudioSegment
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# --- CONFIG & LOGGING ---
VERSION = "6.0-ULTRA-OPTIMIZED"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT")
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
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
            articles = r.json().get("articles", [])[:4]
            for a in articles:
                all_articles.append(f"[{cat.upper()}] {a['title']}: {a.get('description', '')}")
        except Exception as e:
            logger.warning(f"Failed category {cat}: {e}")

    return "\n".join(all_articles)

# --- STEP 2: AI SCRIPT GENERATION (CHAIN OF THOUGHT) ---
def generate_script(news_content):
    logger.info(f"Step 2: Generating CoT Script (v{VERSION})...")

    regions = ["us-central1", "europe-west1", "europe-west3"]
    models = ["gemini-2.0-flash", "gemini-2.0-pro", "gemini-1.5-pro"]

    system_instruction = """
    Rolle: Du bist ein Podcast-Produzent für "Morgenpost".
    Sprecher:
    - Jules: Enthusiastisch, schnell, liebt Fortschritt. (Weiblich)
    - Basti: Kritisch, bedacht, hinterfragt Konsequenzen. (Männlich)

    Aufgabe: Erstelle ein 15-20 minütiges Gespräch (ca. 2000-2500 Wörter).
    Format: AUSSCHLIESSLICH ein JSON-Array von Objekten: [{"speaker": "Jules", "text": "..."}]

    Workflow:
    1. Analysiere die News.
    2. Erstelle einen roten Faden (Intro -> News -> Deep Dive -> Outro).
    3. Schreibe lebendige Dialoge in natürlichem Deutsch. Vermeide Roboter-Sprache.
    """

    for region in regions:
        logger.info(f"Connecting to Vertex AI in {region}...")
        try:
            vertexai.init(project=PROJECT_ID, location=region)
            for model_name in models:
                try:
                    logger.info(f"Attempting {model_name}...")
                    model = GenerativeModel(model_name=model_name)
                    # Use a high max_output_tokens for the long script
                    response = model.generate_content(
                        f"{system_instruction}\n\nNews:\n{news_content}",
                        generation_config=GenerationConfig(
                            response_mime_type="application/json",
                            temperature=0.85,
                            max_output_tokens=8192
                        )
                    )
                    if response.text:
                        script = json.loads(response.text)
                        logger.info(f"Script generated successfully ({len(script)} segments).")
                        return script
                except Exception as e:
                    logger.warning(f"Model {model_name} in {region} failed: {e}")
        except Exception as e:
            logger.error(f"Region {region} init failed: {e}")

    raise Exception("Model discovery failed completely.")

# --- STEP 3: PARALLEL AUDIO SYNTHESIS (SSML) ---
def tts_worker(segment_index, speaker, text, client):
    """Worker function for parallel TTS calls."""
    # Build SSML for better quality
    voice_map = {
        "Jules": {"name": "de-DE-Neural2-F", "pitch": "+1st", "rate": "1.05"},
        "Basti": {"name": "de-DE-Neural2-B", "pitch": "-1st", "rate": "0.95"}
    }
    v = voice_map.get(speaker, voice_map["Jules"])

    # Escape special characters for SSML (XML)
    escaped_text = html.escape(text)

    ssml = f"""
    <speak>
        <prosody rate='{v['rate']}' pitch='{v['pitch']}'>
            {escaped_text}
        </prosody>
    </speak>
    """

    s_input = texttospeech.SynthesisInput(ssml=ssml)
    voice_params = texttospeech.VoiceSelectionParams(language_code="de-DE", name=v["name"])
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)

    try:
        response = client.synthesize_speech(input=s_input, voice=voice_params, audio_config=audio_config)
        return (segment_index, response.audio_content)
    except Exception as e:
        logger.error(f"TTS Segment {segment_index} failed: {e}")
        return (segment_index, None)

def synthesize_parallel(script):
    logger.info("Step 3: Parallel Audio Synthesis...")
    client = texttospeech.TextToSpeechClient()

    # We use a ThreadPool for I/O bound TTS calls
    results = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(tts_worker, i, s["speaker"], s["text"], client): i for i, s in enumerate(script)}
        for future in as_completed(futures):
            idx, audio = future.result()
            if audio:
                results[idx] = audio

    # Stitching
    logger.info("Stitching segments...")
    combined = AudioSegment.empty()
    for i in range(len(script)):
        if i in results:
            segment = AudioSegment.from_file(io.BytesIO(results[i]), format="mp3")
            combined += segment
            # Add natural pause between speakers
            combined += AudioSegment.silent(duration=700)

    out_buffer = io.BytesIO()
    combined.export(out_buffer, format="mp3", bitrate="128k")
    return out_buffer.getvalue()

# --- MAIN EXECUTION ---
def main():
    start_time = time.time()
    logger.info(f"--- Morgenpost Generator v{VERSION} Started ---")

    if not all([PROJECT_ID, BUCKET_NAME, RECIPIENT_EMAIL, SENDER_EMAIL]):
        logger.error("Environment variables missing!")
        return

    try:
        # 1. Fetch
        news = fetch_news()

        # 2. Generate
        script = generate_script(news)

        # 3. Synthesize
        audio_data = synthesize_parallel(script)

        # 4. Store
        filename = f"morgenpost_{datetime.now().strftime('%Y%m%d_%H%M')}.mp3"
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(audio_data, content_type="audio/mpeg")

        # 5. Deliver
        sa_email = f"podcast-generator-sa@{PROJECT_ID}.iam.gserviceaccount.com"
        url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=24), method="GET", service_account_email=sa_email)

        sg_key = get_secret("SENDGRID_API_KEY")
        if sg_key:
            sg = SendGridAPIClient(sg_key)
            mail = Mail(
                from_email=SENDER_EMAIL,
                to_emails=RECIPIENT_EMAIL,
                subject=f"Dein Morgen-Briefing ({datetime.now().strftime('%d.%m.%Y')})",
                plain_text_content=f"Guten Morgen!\n\nDein heutiger Podcast ist fertig: {url}\n\nViel Spaß beim Hören!"
            )
            sg.send(mail)
            logger.info("Email sent successfully.")

        duration = time.time() - start_time
        logger.info(f"Total processing time: {duration:.2f}s")
        logger.info("--- Processing Complete ---")

    except Exception as e:
        logger.critical(f"FATAL ERROR: {e}", exc_info=True)
        raise e

if __name__ == "__main__":
    main()
