# Configuration - CHANGE THESE
$RECIPIENT_EMAIL = "your-email@example.com"
$SENDER_EMAIL = "your-gmail-address@gmail.com"

# System Config
$PROJECT_ID = gcloud config get-value project
$REGION = "europe-west3"
$SA_NAME = "morgenpost-gmail-sa"
$JOB_NAME = "daily-text-briefing"

if (-not $PROJECT_ID) {
    Write-Error "No Project ID found. Run 'gcloud config set project ID' first."
    exit
}

Write-Host "Starting Deployment for Gmail Edition: $PROJECT_ID" -ForegroundColor Cyan

Write-Host "Enabling APIs..." -ForegroundColor Yellow
gcloud services enable --project $PROJECT_ID `
    run.googleapis.com `
    secretmanager.googleapis.com `
    cloudscheduler.googleapis.com `
    artifactregistry.googleapis.com `
    aiplatform.googleapis.com `
    cloudbuild.googleapis.com

Write-Host "Waiting 60s..." -ForegroundColor Yellow
Start-Sleep -Seconds 60

Write-Host "Creating Service Account..." -ForegroundColor Yellow
gcloud iam service-accounts create $SA_NAME --display-name="Morgenpost Gmail SA" --project $PROJECT_ID

Write-Host "Granting Permissions..." -ForegroundColor Yellow
$SA_EMAIL = "serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
$ROLES = @("roles/secretmanager.secretAccessor", "roles/aiplatform.user", "roles/run.admin")
foreach ($ROLE in $ROLES) {
    gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA_EMAIL --role=$ROLE --project $PROJECT_ID
}

Write-Host "Creating Secrets..." -ForegroundColor Yellow
gcloud secrets create NEWS_API_KEY --project $PROJECT_ID
gcloud secrets create GMAIL_APP_PASSWORD --project $PROJECT_ID

Write-Host "Building and Deploying..." -ForegroundColor Yellow
gcloud artifacts repositories create morgenpost-repo --repository-format=docker --location=$REGION --project $PROJECT_ID
$TAG = [Math]::Floor([decimal](Get-Date -UFormat %s))
$IMAGE_URL = "${REGION}-docker.pkg.dev/${PROJECT_ID}/morgenpost-repo/gmail-app:$TAG"
gcloud builds submit --tag $IMAGE_URL --project $PROJECT_ID

# Deploy Job
gcloud run jobs deploy $JOB_NAME --image $IMAGE_URL --region $REGION --project $PROJECT_ID `
    --service-account "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" `
    --set-env-vars "GCP_PROJECT=$PROJECT_ID,RECIPIENT_EMAIL=$RECIPIENT_EMAIL,SENDER_EMAIL=$SENDER_EMAIL" `
    --cpu=1 --memory=512Mi `
    --task-timeout=600s

Write-Host "Creating Scheduler..." -ForegroundColor Yellow
gcloud scheduler jobs delete ${JOB_NAME}-trigger --location $REGION --project $PROJECT_ID --quiet 2>$null
gcloud scheduler jobs create http ${JOB_NAME}-trigger --location $REGION --project $PROJECT_ID --schedule="0 6 * * *" --time-zone="Europe/Berlin" `
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" `
    --http-method POST --oauth-service-account-email "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

Write-Host "Done! Gmail Edition Deployment successful." -ForegroundColor Green
Write-Host "WICHTIG: Erstelle ein Gmail App-Passwort und hinterlege es im Secret Manager als 'GMAIL_APP_PASSWORD'." -ForegroundColor Yellow
