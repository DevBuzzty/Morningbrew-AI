#!/bin/bash

# Configuration - Replace these or set them as environment variables
PROJECT_ID=$(gcloud config get-value project)
REGION="europe-west3"
BUCKET_NAME="${PROJECT_ID}-morning-briefing-podcasts"
SERVICE_ACCOUNT_NAME="podcast-generator-sa"
JOB_NAME="daily-morning-briefing"
RECIPIENT_EMAIL="your-email@example.com" # CHANGE THIS
SENDER_EMAIL="briefing@example.com"     # CHANGE THIS

echo "Starting deployment for project: $PROJECT_ID in region: $REGION"

# 1. Enable APIs
echo "Enabling necessary APIs..."
gcloud services enable \
    aiplatform.googleapis.com \
    texttospeech.googleapis.com \
    storage.googleapis.com \
    secretmanager.googleapis.com \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    cloudbuild.googleapis.com

# 2. Create Service Account
echo "Creating service account..."
gcloud iam service-accounts create $SERVICE_ACCOUNT_NAME \
    --display-name="Service Account for Podcast Generator"

# 3. Assign Roles to Service Account
echo "Assigning roles to service account..."
ROLES=(
    "roles/aiplatform.user"
    "roles/secretmanager.secretAccessor"
    "roles/storage.objectAdmin"
    "roles/texttospeech.user"
    "roles/iam.serviceAccountTokenCreator"
)

for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding $PROJECT_ID \
        --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
        --role="$ROLE"
done

# 4. Create GCS Bucket
echo "Creating GCS bucket..."
gsutil mb -l $REGION gs://$BUCKET_NAME/

# 5. Create Secrets in Secret Manager (Placeholders)
echo "Creating secrets (placeholders)..."
# Note: You will need to add the actual values in the GCP Console or via gcloud
gcloud secrets create NEWS_API_KEY --replication-policy="automatic"
gcloud secrets create SENDGRID_API_KEY --replication-policy="automatic"

echo "IMPORTANT: Please add your API keys to the secrets:"
echo "echo -n 'YOUR_NEWS_API_KEY' | gcloud secrets versions add NEWS_API_KEY --data-file=-"
echo "echo -n 'YOUR_SENDGRID_API_KEY' | gcloud secrets versions add SENDGRID_API_KEY --data-file=-"

# 6. Create Artifact Registry Repository
echo "Creating Artifact Registry repository..."
REPO_NAME="podcast-repo"
gcloud artifacts repositories create $REPO_NAME \
    --repository-format=docker \
    --location=$REGION \
    --description="Repository for Podcast Generator images" || true

# 6. Build and Deploy Cloud Run Job
echo "Building and deploying Cloud Run Job..."
IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}:latest"
gcloud builds submit --tag $IMAGE_URL

gcloud run jobs deploy $JOB_NAME \
    --image $IMAGE_URL \
    --region $REGION \
    --service-account "${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,GCS_BUCKET_NAME=$BUCKET_NAME,RECIPIENT_EMAIL=$RECIPIENT_EMAIL,SENDER_EMAIL=$SENDER_EMAIL,SENDGRID_API_KEY_SECRET_NAME=SENDGRID_API_KEY,NEWS_API_KEY_SECRET_NAME=NEWS_API_KEY" \
    --max-retries 0 \
    --task-timeout 1200s # 20 minutes

# 7. Create Cloud Scheduler Trigger (7:00 AM CET)
echo "Creating Cloud Scheduler trigger..."
# Note: 7:00 AM CET is handled by the Europe/Berlin timezone.
gcloud scheduler jobs create run ${JOB_NAME}-trigger \
    --location $REGION \
    --schedule="0 7 * * *" \
    --time-zone="Europe/Berlin" \
    --job $JOB_NAME

echo "Deployment complete!"
