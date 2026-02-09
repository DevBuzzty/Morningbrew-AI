import os
import io
import json
import logging
from datetime import datetime, timedelta
import requests
import google.generativeai as genai
from google.cloud import texttospeech
from google.cloud import storage
from google.cloud import secretmanager
from pydub import AudioSegment
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants (Configurable via environment variables)
PROJECT_ID = os.getenv("GCP_PROJECT")
REGION = os.getenv("GCP_REGION", "europe-west3")
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
SENDGRID_API_KEY_SECRET_NAME = os.getenv("SENDGRID_API_KEY_SECRET_NAME")
NEWS_API_KEY_SECRET_NAME = os.getenv("NEWS_API_KEY_SECRET_NAME")
GOOGLE_API_KEY_SECRET_NAME = os.getenv("GOOGLE_API_KEY_SECRET_NAME")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")

def get_secret(secret_name):
    """Fetches a secret from Google Cloud Secret Manager."""
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret = response.payload.data.decode("UTF-8").strip()
        logger.info(f"Successfully retrieved secret: {secret_name} (length: {len(secret)})")
        return secret
    except Exception as e:
        logger.error(f"Failed to retrieve secret {secret_name}: {e}")
        raise

def fetch_news():
    """Fetches real-time news headlines using NewsAPI.org."""
    logger.info("Fetching news from NewsAPI...")
    api_key = get_secret(NEWS_API_KEY_SECRET_NAME)
    if not api_key:
        logger.error("NewsAPI key is empty!")
        return ""
    base_url = "https://newsapi.org/v2/top-headlines"

    news_summary = []

    # 1. Politics & General DE
    params_de = {
        "country": "de",
        "category": "general",
        "apiKey": api_key,
        "pageSize": 5
    }
    # 2. Business/Economy DE
    params_econ = {
        "country": "de",
        "category": "business",
        "apiKey": api_key,
        "pageSize": 5
    }
    # 3. Tech/AI International
    params_tech = {
        "category": "technology",
        "language": "en",
        "apiKey": api_key,
        "pageSize": 5
    }

    queries = [
        ("German Politics/General", params_de),
        ("German Economy", params_econ),
        ("Tech & AI International", params_tech)
    ]

    for label, params in queries:
        try:
            response = requests.get(base_url, params=params)
            data = response.json()
            if data.get("status") == "ok":
                articles = data.get("articles", [])
                news_summary.append(f"--- {label} ---")
                for art in articles:
                    news_summary.append(f"Title: {art.get('title')}\nDescription: {art.get('description')}\n")
            else:
                logger.error(f"Error fetching {label}: {data.get('message')}")
        except Exception as e:
            logger.error(f"Exception fetching {label}: {e}")

    return "\n".join(news_summary)

def generate_script(news_content):
    """Generates a podcast script using Google AI Studio (Gemini API) with System Instructions."""
    logger.info("Generating script with Google AI Studio (Gemini 1.5 Pro)...")

    api_key = get_secret(GOOGLE_API_KEY_SECRET_NAME)
    if not api_key:
        raise ValueError("Google API Key is missing or empty!")

    # Using transport='rest' to avoid gRPC 'Illegal metadata' errors in some Cloud environments
    genai.configure(api_key=api_key, transport='rest')

    system_instruction = """
Du bist ein erstklassiger Podcast-Redakteur für das Format "Daily Briefing".
Deine Aufgabe ist es, ein Skript für ein 15-20 minütiges Gespräch (ca. 2000-2500 Wörter) zu erstellen.

Sprecher-Profile:
1. Jules: Weiblich, KI-Expertin, Tech-Optimistin. Sie spricht mit Begeisterung, nutzt moderne Begriffe, ist energiegeladen und sieht in fast jedem Problem eine technologische Lösung.
2. Basti: Männlich, Journalist alter Schule, kritischer Beobachter. Er hinterfragt den Hype, sorgt sich um Datenschutz, Ethik und soziale Auswirkungen. Er ist nicht technikfeindlich, aber sehr skeptisch.

Gesprächsdynamik:
- Die beiden sollen INTERAGIEREN, nicht nur nacheinander vorlesen.
- Basti darf Jules sanft unterbrechen oder ihre Begeisterung hinterfragen.
- Jules versucht Basti mit Fakten zu überzeugen.
- Nutze natürliche Füllwörter (äh, weißt du, na ja) sehr sparsam, um das Skript lebendig zu machen.
- Sprache: Deutsch. Tonfall: Professionell, aber sehr konversationsorientiert (wie ein echtes Gespräch unter Kollegen).

Struktur: Intro -> Politik -> Wirtschaft -> Deep Dive Tech/AI -> Outro.
Ausgabeformat: Ein JSON-Array von Objekten mit "speaker" ("Jules" oder "Basti") und "text".
"""

    model = genai.GenerativeModel(
        model_name="gemini-1.5-pro",
        system_instruction=system_instruction
    )

    prompt = f"""
Hier sind die heutigen Nachrichten-Headlines:
{news_content}

Erstelle basierend auf diesen Informationen das Podcast-Skript. Achte darauf, dass das Gespräch flüssig ist und Jules und Basti wirklich miteinander debattieren, besonders im AI Deep Dive.
Ziele auf eine Wortzahl von insgesamt 2000-2500 Wörtern ab.
"""

    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            temperature=0.8,
        )
    )

    try:
        script = json.loads(response.text)
        return script
    except Exception as e:
        logger.error(f"Failed to parse Gemini response as JSON: {e}")
        logger.debug(f"Raw response: {response.text}")
        # Fallback: simple text parsing or error
        raise

def synthesize_audio(script_segments):
    """Synthesizes audio from script segments and stitches them."""
    logger.info("Synthesizing audio segments with Google Cloud TTS...")
    client = texttospeech.TextToSpeechClient()

    combined_audio = AudioSegment.empty()

    voice_map = {
        "Jules": texttospeech.VoiceSelectionParams(
            language_code="de-DE", name="de-DE-Neural2-F"
        ),
        "Basti": texttospeech.VoiceSelectionParams(
            language_code="de-DE", name="de-DE-Neural2-B"
        )
    }

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )

    for segment in script_segments:
        speaker = segment.get("speaker")
        text = segment.get("text")

        if not text:
            continue

        voice = voice_map.get(speaker, voice_map["Jules"]) # Default to Jules

        # Handle TTS character limit (5000 chars) safely by splitting on whitespace
        if len(text) < 4900:
            text_chunks = [text]
        else:
            text_chunks = []
            while text:
                if len(text) <= 4900:
                    text_chunks.append(text)
                    break
                split_idx = text.rfind(' ', 0, 4900)
                if split_idx == -1: split_idx = 4900
                text_chunks.append(text[:split_idx])
                text = text[split_idx:].strip()

        for chunk in text_chunks:
            if not chunk: continue
            synthesis_input = texttospeech.SynthesisInput(text=chunk)
            response = client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )

            # Convert bytes to AudioSegment
            segment_audio = AudioSegment.from_file(io.BytesIO(response.audio_content), format="mp3")
            combined_audio += segment_audio

        # Add a small pause between speakers
        pause = AudioSegment.silent(duration=500) # 500ms pause
        combined_audio += pause

    # Export combined audio to a buffer
    buffer = io.BytesIO()
    combined_audio.export(buffer, format="mp3")
    return buffer.getvalue()

def upload_to_gcs(audio_data, blob_name):
    """Uploads the MP3 file to Google Cloud Storage."""
    logger.info(f"Uploading {blob_name} to GCS bucket {BUCKET_NAME}...")
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(audio_data, content_type="audio/mpeg")
    return blob

def generate_signed_url(blob):
    """Generates a signed URL for the GCS blob (valid for 24h)."""
    logger.info(f"Generating signed URL for {blob.name}...")
    # For V4 signing, we use the storage client's credentials.
    # In Cloud Run, this is the Service Account attached to the job.
    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=24),
        method="GET",
    )
    return url

def send_email(signed_url):
    """Sends the signed URL via SendGrid."""
    logger.info(f"Sending email to {RECIPIENT_EMAIL}...")
    api_key = get_secret(SENDGRID_API_KEY_SECRET_NAME)
    sg = SendGridAPIClient(api_key)

    date_str = datetime.now().strftime('%d.%m.%Y')
    message = Mail(
        from_email=SENDER_EMAIL,
        to_emails=RECIPIENT_EMAIL,
        subject=f"Dein tägliches Audio-Briefing - {date_str}",
        plain_text_content=f"Guten Morgen! Hier ist dein Podcast-Briefing für heute: {signed_url}\nDer Link ist 24 Stunden gültig."
    )

    try:
        response = sg.send(message)
        logger.info(f"Email sent! Status code: {response.status_code}")
    except Exception as e:
        logger.error(f"Error sending email: {e}")

def main():
    try:
        # Check required env vars
        required_vars = [
            "GCP_PROJECT", "GCS_BUCKET_NAME", "RECIPIENT_EMAIL",
            "SENDER_EMAIL", "SENDGRID_API_KEY_SECRET_NAME",
            "NEWS_API_KEY_SECRET_NAME", "GOOGLE_API_KEY_SECRET_NAME"
        ]
        missing = [v for v in required_vars if not os.getenv(v)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        news = fetch_news()
        script = generate_script(news)
        audio_data = synthesize_audio(script)

        if audio_data:
            blob_name = f"briefing_{datetime.now().strftime('%Y%m%d')}.mp3"
            blob = upload_to_gcs(audio_data, blob_name)
            signed_url = generate_signed_url(blob)
            send_email(signed_url)
            logger.info("Daily briefing process completed successfully.")
        else:
            logger.error("No audio data generated.")

    except Exception as e:
        logger.error(f"An error occurred: {e}")
        raise # Re-raise for Cloud Run Job failure detection

if __name__ == "__main__":
    main()
