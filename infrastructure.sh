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

echo "Enabling all necessary APIs (this takes time)..."
gcloud services enable --project $PROJECT_ID \
    run.googleapis.com \
    secretmanager.googleapis.com \
    texttospeech.googleapis.com \
    storage.googleapis.com \
    cloudscheduler.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    cloudbuild.googleapis.com

echo "Waiting 90 seconds for API synchronization..."
sleep 90

echo "Creating Service Account..."
gcloud iam service-accounts create $SA_NAME --display-name="Podcast Service Account" --project $PROJECT_ID || true

echo "Granting Permissions..."
SA_EMAIL="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role="roles/secretmanager.secretAccessor" --project $PROJECT_ID
gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role="roles/storage.objectAdmin" --project $PROJECT_ID
gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role="roles/iam.serviceAccountTokenCreator" --project $PROJECT_ID
gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role="roles/run.jobRunner" --project $PROJECT_ID
gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role="roles/texttospeech.admin" --project $PROJECT_ID
gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role="roles/aiplatform.user" --project $PROJECT_ID

echo "Creating Bucket..."
gsutil mb -p $PROJECT_ID -l $REGION gs://$BUCKET_NAME/ || true

echo "Creating Secrets..."
gcloud secrets create NEWS_API_KEY --project $PROJECT_ID || true
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
    --task-timeout=1200s

echo "Creating Scheduler..."
gcloud scheduler jobs delete ${JOB_NAME}-trigger --location $REGION --project $PROJECT_ID --quiet || true
gcloud scheduler jobs create http ${JOB_NAME}-trigger --location $REGION --project $PROJECT_ID --schedule="0 6 * * *" --time-zone="Europe/Berlin" \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
    --http-method POST --oauth-service-account-email "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Done! Deployment successful."
echo "IMPORTANT: Add your secrets now:"
echo "echo -n 'KEY' | gcloud secrets versions add NEWS_API_KEY --data-file=-"
echo "echo -n 'KEY' | gcloud secrets versions add SENDGRID_API_KEY --data-file=-"
