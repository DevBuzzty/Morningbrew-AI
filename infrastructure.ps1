# Configuration - CHANGE THESE
$RECIPIENT_EMAIL = "your-email@example.com"
$SENDER_EMAIL = "your-verified-sender@example.com"

# System Config
$PROJECT_ID = gcloud config get-value project
$REGION = "europe-west3"
$BUCKET_NAME = "${PROJECT_ID}-briefing-podcasts"
$SA_NAME = "podcast-generator-sa"
$JOB_NAME = "daily-morning-briefing"

Write-Host "Starting Deployment for Project: $PROJECT_ID"

Write-Host "Enabling APIs..."
gcloud services enable `
    run.googleapis.com `
    secretmanager.googleapis.com `
    texttospeech.googleapis.com `
    storage.googleapis.com `
    cloudscheduler.googleapis.com `
    artifactregistry.googleapis.com `
    generativelanguage.googleapis.com

Write-Host "Waiting for APIs..."
Start-Sleep -Seconds 30

Write-Host "Creating Service Account..."
gcloud iam service-accounts create $SA_NAME 2>$null

Write-Host "Granting Permissions..."
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --role="roles/storage.objectAdmin"
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --role="roles/iam.serviceAccountTokenCreator"
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --role="roles/run.jobRunner"
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --role="roles/texttospeech.admin"

Write-Host "Creating Bucket..."
gsutil mb -l $REGION gs://$BUCKET_NAME 2>$null

Write-Host "Creating Secrets..."
gcloud secrets create NEWS_API_KEY 2>$null
gcloud secrets create GEMINI_API_KEY 2>$null
gcloud secrets create SENDGRID_API_KEY 2>$null

Write-Host "Building and Deploying..."
gcloud artifacts repositories create podcast-repo --repository-format=docker --location=$REGION 2>$null
$TAG = Get-Date -UFormat "%s"
$IMAGE_URL = "${REGION}-docker.pkg.dev/${PROJECT_ID}/podcast-repo/app:$TAG"
gcloud builds submit --tag $IMAGE_URL
gcloud run jobs deploy $JOB_NAME --image $IMAGE_URL --region $REGION `
    --service-account "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" `
    --set-env-vars "GCP_PROJECT=$PROJECT_ID,GCS_BUCKET_NAME=$BUCKET_NAME,RECIPIENT_EMAIL=$RECIPIENT_EMAIL,SENDER_EMAIL=$SENDER_EMAIL" `
    --task-timeout=1200s

Write-Host "Creating Scheduler..."
gcloud scheduler jobs delete ${JOB_NAME}-trigger --location $REGION --quiet 2>$null
gcloud scheduler jobs create http ${JOB_NAME}-trigger --location $REGION --schedule="0 6 * * *" --time-zone="Europe/Berlin" `
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" `
    --http-method POST --oauth-service-account-email "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

Write-Host "Done! Deployment successful."
Write-Host "Now run these to add your keys:"
Write-Host "echo -n 'KEY' | gcloud secrets versions add GEMINI_API_KEY --data-file=-"
