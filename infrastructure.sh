#!/bin/bash
# Configuration - CHANGE THESE
RECIPIENT_EMAIL="your-email@example.com"
SENDER_EMAIL="your-verified-sender@example.com"

# System Config
PROJECT_ID=$(gcloud config get-value project)
REGION="europe-west3"
BUCKET_NAME="${PROJECT_ID}-briefing-podcasts"
SA_NAME="podcast-generator-sa"
JOB_NAME="daily-morning-briefing"

if [ -z "$PROJECT_ID" ]; then
    echo "ERROR: No Project ID found. Run 'gcloud config set project ID' first."
    exit 1
fi

echo "Starting Deployment for Project: $PROJECT_ID"

echo "Enabling all necessary APIs (this takes up to 2 minutes)..."
gcloud services enable --project $PROJECT_ID \
    run.googleapis.com \
    secretmanager.googleapis.com \
    texttospeech.googleapis.com \
    storage.googleapis.com \
    cloudscheduler.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    generativelanguage.googleapis.com \
    ml.googleapis.com \
    cloudbuild.googleapis.com

echo "NOTE: 'gcloud ai models list' returns 0 items for foundation models by design."
echo "Foundation models (Gemini) are managed by Google and don't appear as user-deployed models."

echo "Waiting 120 seconds for API synchronization across regions..."
sleep 120

echo "Creating Service Account..."
gcloud iam service-accounts create $SA_NAME --display-name="Podcast Service Account" --project $PROJECT_ID || true

echo "Granting Permissions..."
SA_EMAIL="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
ROLES=(
    "roles/secretmanager.secretAccessor"
    "roles/storage.objectAdmin"
    "roles/iam.serviceAccountTokenCreator"
    "roles/run.admin"
    "roles/texttospeech.user"
    "roles/aiplatform.user"
)
for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role="$ROLE" --project $PROJECT_ID
done

echo "Creating Bucket..."
gsutil mb -p $PROJECT_ID -l $REGION gs://$BUCKET_NAME/ || true

echo "Creating Secrets..."
gcloud secrets create NEWS_API_KEY --project $PROJECT_ID || true
gcloud secrets create GEMINI_API_KEY --project $PROJECT_ID || true
gcloud secrets create SENDGRID_API_KEY --project $PROJECT_ID || true

echo "Building and Deploying..."
gcloud artifacts repositories create podcast-repo --repository-format=docker --location=$REGION --project $PROJECT_ID || true
IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/podcast-repo/app:$(date +%s)"
gcloud builds submit --tag $IMAGE_URL --project $PROJECT_ID

# Delete old job to force refresh
gcloud run jobs delete $JOB_NAME --region $REGION --project $PROJECT_ID --quiet || true

gcloud run jobs deploy $JOB_NAME --image $IMAGE_URL --region $REGION --project $PROJECT_ID \
    --service-account "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --set-env-vars "GCP_PROJECT=$PROJECT_ID,GCS_BUCKET_NAME=$BUCKET_NAME,RECIPIENT_EMAIL=$RECIPIENT_EMAIL,SENDER_EMAIL=$SENDER_EMAIL" \
    --cpu=2 --memory=2Gi \
    --task-timeout=1800s

echo "Creating Scheduler..."
gcloud scheduler jobs delete ${JOB_NAME}-trigger --location $REGION --project $PROJECT_ID --quiet || true
gcloud scheduler jobs create http ${JOB_NAME}-trigger --location $REGION --project $PROJECT_ID --schedule="0 6 * * *" --time-zone="Europe/Berlin" \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
    --http-method POST --oauth-service-account-email "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Done! Deployment of Morgenpost v6.0 successful."
echo "Optimizations active: Parallel synthesis, SSML quality, and CoT scripting."
