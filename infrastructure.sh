#!/bin/bash
# Configuration - CHANGE THESE
RECIPIENT_EMAIL="your-email@example.com"
SENDER_EMAIL="your-gmail-address@gmail.com"

# System Config
PROJECT_ID=$(gcloud config get-value project)
REGION="europe-west3"
SA_NAME="morgenpost-gmail-sa"
JOB_NAME="daily-text-briefing"
BUCKET_NAME="${PROJECT_ID}-briefing-podcasts"

if [ -z "$PROJECT_ID" ]; then
    echo "ERROR: No Project ID found. Run 'gcloud config set project ID' first."
    exit 1
fi

echo "Starting Deployment for Multimedia Edition (v11.0): $PROJECT_ID"

echo "Enabling necessary APIs..."
gcloud services enable --project $PROJECT_ID \
    run.googleapis.com \
    secretmanager.googleapis.com \
    cloudscheduler.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    texttospeech.googleapis.com \
    storage.googleapis.com \
    cloudbuild.googleapis.com

echo "Waiting 60 seconds for API synchronization..."
sleep 60

echo "Creating GCS Bucket for Podcasts..."
gsutil mb -p $PROJECT_ID -l $REGION gs://$BUCKET_NAME/ || true

echo "Creating Service Account..."
gcloud iam service-accounts create $SA_NAME --display-name="Morgenpost Gmail SA" --project $PROJECT_ID || true

echo "Granting Permissions..."
SA_EMAIL="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
ROLES=(
    "roles/secretmanager.secretAccessor"
    "roles/aiplatform.user"
    "roles/run.admin"
    "roles/storage.objectAdmin"
    "roles/texttospeech.user"
    "roles/iam.serviceAccountTokenCreator"
)
for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role="$ROLE" --project $PROJECT_ID
done

echo "Creating Secrets (if they don't exist)..."
gcloud secrets create NEWS_API_KEY --project $PROJECT_ID || true
gcloud secrets create GMAIL_APP_PASSWORD --project $PROJECT_ID || true

echo "Building and Deploying Image..."
gcloud artifacts repositories create morgenpost-repo --repository-format=docker --location=$REGION --project $PROJECT_ID || true
TAG=$(date +%Y%m%d%H%M%S)
IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/morgenpost-repo/multimedia-app:$TAG"
gcloud builds submit --tag $IMAGE_URL --project $PROJECT_ID

# Deploy Job with multimedia resources
gcloud run jobs deploy $JOB_NAME --image $IMAGE_URL --region $REGION --project $PROJECT_ID \
    --service-account "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --set-env-vars "GCP_PROJECT=$PROJECT_ID,RECIPIENT_EMAIL=$RECIPIENT_EMAIL,SENDER_EMAIL=$SENDER_EMAIL" \
    --cpu=2 --memory=2Gi \
    --task-timeout=1800s

echo "Creating Scheduler (7:00 AM CET)..."
gcloud scheduler jobs delete ${JOB_NAME}-trigger --location $REGION --project $PROJECT_ID --quiet || true
gcloud scheduler jobs create http ${JOB_NAME}-trigger --location $REGION --project $PROJECT_ID --schedule="0 6 * * *" --time-zone="Europe/Berlin" \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
    --http-method POST --oauth-service-account-email "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Done! Morgenpost Multimedia Edition Deployment successful."
