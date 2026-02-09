# Configuration - Replace these or set them as environment variables
$PROJECT_ID = gcloud config get-value project
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
    generativelanguage.googleapis.com `
    texttospeech.googleapis.com `
    storage.googleapis.com `
    secretmanager.googleapis.com `
    run.googleapis.com `
    cloudscheduler.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com

# 2. Create Service Account
Write-Host "Creating service account..." -ForegroundColor Yellow
gcloud iam service-accounts create $SERVICE_ACCOUNT_NAME `
    --display-name="Service Account for Podcast Generator"

# 3. Assign Roles to Service Account
Write-Host "Assigning roles to service account..." -ForegroundColor Yellow
$ROLES = @(
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectAdmin",
    "roles/texttospeech.user",
    "roles/iam.serviceAccountTokenCreator"
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
gcloud secrets create GOOGLE_API_KEY --replication-policy="automatic" 2>$null

Write-Host "IMPORTANT: Please add your API keys to the secrets using these commands:" -ForegroundColor Magenta
Write-Host "echo -n 'YOUR_NEWS_API_KEY' | gcloud secrets versions add NEWS_API_KEY --data-file=-"
Write-Host "echo -n 'YOUR_SENDGRID_API_KEY' | gcloud secrets versions add SENDGRID_API_KEY --data-file=-"
Write-Host "echo -n 'YOUR_GOOGLE_AI_STUDIO_API_KEY' | gcloud secrets versions add GOOGLE_API_KEY --data-file=-"

# 6. Create Artifact Registry Repository
Write-Host "Creating Artifact Registry repository..." -ForegroundColor Yellow
$REPO_NAME = "podcast-repo"
gcloud artifacts repositories create $REPO_NAME `
    --repository-format=docker `
    --location=$REGION `
    --description="Repository for Podcast Generator images" 2>$null

# 7. Build and Deploy Cloud Run Job
Write-Host "Building and deploying Cloud Run Job..." -ForegroundColor Yellow
$IMAGE_URL = "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}:latest"
gcloud builds submit --tag $IMAGE_URL

gcloud run jobs deploy $JOB_NAME `
    --image $IMAGE_URL `
    --region $REGION `
    --service-account "${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" `
    --set-env-vars="GCP_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,GCS_BUCKET_NAME=$BUCKET_NAME,RECIPIENT_EMAIL=$RECIPIENT_EMAIL,SENDER_EMAIL=$SENDER_EMAIL,SENDGRID_API_KEY_SECRET_NAME=SENDGRID_API_KEY,NEWS_API_KEY_SECRET_NAME=NEWS_API_KEY,GOOGLE_API_KEY_SECRET_NAME=GOOGLE_API_KEY" `
    --max-retries 0 `
    --task-timeout 1200s

# 8. Create Cloud Scheduler Trigger (6:00 AM CET)
Write-Host "Creating Cloud Scheduler trigger..." -ForegroundColor Yellow
gcloud scheduler jobs create run ${JOB_NAME}-trigger `
    --location $REGION `
    --schedule="0 6 * * *" `
    --time-zone="Europe/Berlin" `
    --job $JOB_NAME

Write-Host "Deployment complete!" -ForegroundColor Green
