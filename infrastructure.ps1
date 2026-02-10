# Configuration - Replace these or set them as environment variables
$PROJECT_ID = gcloud config get-value project

if (-not $PROJECT_ID) {
    Write-Host "ERROR: No Google Cloud project detected. Please run 'gcloud config set project YOUR_PROJECT_ID' first." -ForegroundColor Red
    exit
}

$REGION = "europe-west3"
$BUCKET_NAME = "${PROJECT_ID}-morning-briefing-podcasts"
$SERVICE_ACCOUNT_NAME = "podcast-generator-sa"
$JOB_NAME = "daily-morning-briefing"
$RECIPIENT_EMAIL = "your-email@example.com" # CHANGE THIS
$SENDER_EMAIL = "briefing@example.com"     # CHANGE THIS

Write-Host "Starting deployment for project: $PROJECT_ID in region: $REGION" -ForegroundColor Cyan

# 1. Enable APIs
Write-Host "Enabling necessary APIs..." -ForegroundColor Yellow
gcloud services enable `
    aiplatform.googleapis.com `
    texttospeech.googleapis.com `
    storage.googleapis.com `
    secretmanager.googleapis.com `
    run.googleapis.com `
    cloudscheduler.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com

Write-Host "Waiting for APIs to propagate..." -ForegroundColor Yellow
Start-Sleep -Seconds 30

# 2. Create Service Account
Write-Host "Creating service account..." -ForegroundColor Yellow
gcloud iam service-accounts create $SERVICE_ACCOUNT_NAME `
    --display-name="Service Account for Podcast Generator" 2>$null

# 3. Assign Roles to Service Account
Write-Host "Assigning roles to service account..." -ForegroundColor Yellow
$ROLES = @(
    "roles/aiplatform.user",
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectAdmin",
    "roles/texttospeech.user",
    "roles/iam.serviceAccountTokenCreator",
    "roles/run.jobRunner"
)

foreach ($ROLE in $ROLES) {
    gcloud projects add-iam-policy-binding $PROJECT_ID `
        --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" `
        --role="$ROLE"
}

# 4. Create GCS Bucket
Write-Host "Creating GCS bucket..." -ForegroundColor Yellow
gsutil mb -l $REGION gs://$BUCKET_NAME/ 2>$null

# 5. Create Secrets in Secret Manager (Placeholders)
Write-Host "Creating secrets (placeholders)..." -ForegroundColor Yellow
gcloud secrets create NEWS_API_KEY --replication-policy="automatic" 2>$null
gcloud secrets create SENDGRID_API_KEY --replication-policy="automatic" 2>$null
Write-Host "IMPORTANT: Please add your API keys to the secrets using these commands:" -ForegroundColor Magenta
Write-Host "echo -n 'YOUR_NEWS_API_KEY' | gcloud secrets versions add NEWS_API_KEY --data-file=-"
Write-Host "echo -n 'YOUR_SENDGRID_API_KEY' | gcloud secrets versions add SENDGRID_API_KEY --data-file=-"

# 6. Create Artifact Registry Repository
Write-Host "Creating Artifact Registry repository..." -ForegroundColor Yellow
$REPO_NAME = "podcast-repo"
gcloud artifacts repositories create $REPO_NAME `
    --repository-format=docker `
    --location=$REGION `
    --description="Repository for Podcast Generator images" 2>$null

# 7. Build and Deploy Cloud Run Job
Write-Host "Building and deploying Cloud Run Job..." -ForegroundColor Yellow

# VERIFICATION: Print the version from main.py
Write-Host "--- DEPLOYMENT VERIFICATION ---" -ForegroundColor Cyan
Select-String "VERSION =" main.py
Write-Host "------------------------------" -ForegroundColor Cyan

# Generate a unique tag to force a fresh pull
$TAG = Get-Date -Format "yyyyMMddHHmmss"
$IMAGE_URL = "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}:${TAG}"

Write-Host "Building image with tag: $TAG" -ForegroundColor Yellow
gcloud builds submit --tag $IMAGE_URL

# Delete the job first to ensure a clean state
gcloud run jobs delete $JOB_NAME --region $REGION --quiet 2>$null

gcloud run jobs deploy $JOB_NAME `
    --image $IMAGE_URL `
    --region $REGION `
    --service-account "${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" `
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,GCS_BUCKET_NAME=$BUCKET_NAME,RECIPIENT_EMAIL=$RECIPIENT_EMAIL,SENDER_EMAIL=$SENDER_EMAIL,SENDGRID_API_KEY_SECRET_NAME=SENDGRID_API_KEY,NEWS_API_KEY_SECRET_NAME=NEWS_API_KEY" `
    --max-retries 0 `
    --task-timeout 1200s

# 8. Create Cloud Scheduler Trigger (6:00 AM CET)
Write-Host "Creating Cloud Scheduler trigger..." -ForegroundColor Yellow
# Delete old scheduler job if it exists
gcloud scheduler jobs delete ${JOB_NAME}-trigger --location $REGION --quiet 2>$null

# Note: 6:00 AM CET is handled by the Europe/Berlin timezone.
gcloud scheduler jobs create http ${JOB_NAME}-trigger `
    --location $REGION `
    --schedule="0 6 * * *" `
    --time-zone="Europe/Berlin" `
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" `
    --http-method POST `
    --oauth-service-account-email "${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

Write-Host "Deployment complete!" -ForegroundColor Green
