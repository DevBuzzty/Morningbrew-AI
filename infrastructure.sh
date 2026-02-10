#!/bin/bash

# Configuration - Replace these or set them as environment variables
PROJECT_ID=$(gcloud config get-value project)

if [ -z "$PROJECT_ID" ]; then
    echo "ERROR: No Google Cloud project detected. Please run 'gcloud config set project YOUR_PROJECT_ID' first."
    exit 1
fi

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
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com

echo "Waiting for APIs to propagate..."
sleep 30

# 2. Create Service Account
echo "Creating service account..."
gcloud iam service-accounts create $SERVICE_ACCOUNT_NAME \
    --display-name="Service Account for Podcast Generator" || true

# 3. Assign Roles to Service Account
echo "Assigning roles to service account..."
ROLES=(
    "roles/aiplatform.user"
    "roles/secretmanager.secretAccessor"
    "roles/storage.objectAdmin"
    "roles/texttospeech.user"
    "roles/iam.serviceAccountTokenCreator"
    "roles/run.jobRunner"
)

for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding $PROJECT_ID \
        --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
        --role="$ROLE"
done

# 4. Create GCS Bucket
echo "Creating GCS bucket..."
gsutil mb -l $REGION gs://$BUCKET_NAME/ || true

# 5. Create Secrets in Secret Manager (Placeholders)
echo "Creating secrets (placeholders)..."
# Note: You will need to add the actual values in the GCP Console or via gcloud
gcloud secrets create NEWS_API_KEY --replication-policy="automatic" || true
gcloud secrets create SENDGRID_API_KEY --replication-policy="automatic" || true
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

# 7. Build and Deploy Cloud Run Job
echo "Building and deploying Cloud Run Job..."

# VERIFICATION: Print the version from main.py
echo "--- DEPLOYMENT VERIFICATION ---"
grep "VERSION =" main.py
echo "------------------------------"

# Generate a unique tag to force a fresh pull
TAG=$(date +%Y%m%d%H%M%S)
IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}:${TAG}"

echo "Building image with tag: $TAG"
gcloud builds submit --tag $IMAGE_URL

# Delete the old job to ensure a clean state
gcloud run jobs delete $JOB_NAME --region $REGION --quiet || true

gcloud run jobs deploy $JOB_NAME \
    --image $IMAGE_URL \
    --region $REGION \
    --service-account "${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,GCS_BUCKET_NAME=$BUCKET_NAME,RECIPIENT_EMAIL=$RECIPIENT_EMAIL,SENDER_EMAIL=$SENDER_EMAIL,SENDGRID_API_KEY_SECRET_NAME=SENDGRID_API_KEY,NEWS_API_KEY_SECRET_NAME=NEWS_API_KEY" \
    --max-retries 0 \
    --task-timeout 1200s # 20 minutes

# 8. Create Cloud Scheduler Trigger (6:00 AM CET)
echo "Creating Cloud Scheduler trigger..."
# Delete old scheduler job if it exists
gcloud scheduler jobs delete ${JOB_NAME}-trigger --location $REGION --quiet || true

# Note: 6:00 AM CET is handled by the Europe/Berlin timezone.
gcloud scheduler jobs create http ${JOB_NAME}-trigger \
    --location $REGION \
    --schedule="0 6 * * *" \
    --time-zone="Europe/Berlin" \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
    --http-method POST \
    --oauth-service-account-email "${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Deployment complete!"
