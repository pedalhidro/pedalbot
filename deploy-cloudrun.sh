#!/usr/bin/env bash
# Deploy do pedalbot no Cloud Run (modo webhook): serviço ph-bot-webhook (público) +
# ph-bot-worker (privado, só Cloud Tasks) + fila Cloud Tasks. Espelha o estilo de
# deploy do sabiá/amora (projeto pedal-hidrografico, região southamerica-east1,
# segredos no Secret Manager). Ajuste as variáveis no topo antes de rodar.
#
# Pré-requisitos (uma vez):
#   gcloud services enable run.googleapis.com cloudtasks.googleapis.com \
#       firestore.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com
#   gcloud firestore databases create --location=$REGION   # modo Native
#   # crie os segredos:
#   printf '%s' "$TOKEN"   | gcloud secrets create telegram-bot-token --data-file=-
#   printf '%s' "$SECRET"  | gcloud secrets create telegram-webhook-secret --data-file=-
#   printf '%s' "$PW"      | gcloud secrets create sabia-app-password --data-file=-
set -euo pipefail

PROJECT="${GCP_PROJECT:-pedal-hidrografico}"
REGION="${GCP_REGION:-southamerica-east1}"
QUEUE="${CLOUD_TASKS_QUEUE:-phbot-jobs}"
WEBHOOK_SVC="ph-bot-webhook"
WORKER_SVC="ph-bot-worker"
RUNTIME_SA="${RUNTIME_SA:-phbot-run@${PROJECT}.iam.gserviceaccount.com}"
AMORA_BASE_URL="${AMORA_BASE_URL:-https://amora.pedalhidrografi.co}"
SABIA_BASE_URL="${SABIA_BASE_URL:?defina SABIA_BASE_URL}"
ALLOWED="${TELEGRAM_ALLOWED_USERS:?defina TELEGRAM_ALLOWED_USERS (CSV de IDs)}"

gcloud config set project "$PROJECT" >/dev/null
gcloud config set run/region "$REGION" >/dev/null

echo "==> fila Cloud Tasks ($QUEUE)"
gcloud tasks queues describe "$QUEUE" --location="$REGION" >/dev/null 2>&1 \
  || gcloud tasks queues create "$QUEUE" --location="$REGION"

SECRETS="TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest,SABIA_APP_PASSWORD=sabia-app-password:latest"
ENVS="GCP_PROJECT=${PROJECT},GCP_REGION=${REGION},CLOUD_TASKS_QUEUE=${QUEUE},AMORA_BASE_URL=${AMORA_BASE_URL},SABIA_BASE_URL=${SABIA_BASE_URL},TELEGRAM_ALLOWED_USERS=${ALLOWED},FIRESTORE_PREFIX=phbot_"

echo "==> worker (privado; só Cloud Tasks invoca)"
gcloud run deploy "$WORKER_SVC" --source . \
  --no-allow-unauthenticated --min-instances=0 --max-instances=3 --concurrency=1 \
  --cpu=2 --memory=1Gi --timeout=600 --service-account="$RUNTIME_SA" \
  --set-secrets="$SECRETS" \
  --set-env-vars="APP_MODULE=bot.worker:app,${ENVS}"
WORKER_URL="$(gcloud run services describe "$WORKER_SVC" --format='value(status.url)')"
# o worker só aceita chamadas do Cloud Tasks (OIDC) com run.invoker:
gcloud run services add-iam-policy-binding "$WORKER_SVC" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/run.invoker" >/dev/null

echo "==> webhook (público; o Telegram precisa alcançar)"
gcloud run deploy "$WEBHOOK_SVC" --source . \
  --allow-unauthenticated --min-instances=0 --max-instances=5 --concurrency=80 \
  --cpu=1 --memory=512Mi --timeout=60 --service-account="$RUNTIME_SA" \
  --set-secrets="$SECRETS" \
  --set-env-vars="APP_MODULE=bot.webhook:app,WORKER_URL=${WORKER_URL},WORKER_SA_EMAIL=${RUNTIME_SA},${ENVS}"
WEBHOOK_URL="$(gcloud run services describe "$WEBHOOK_SVC" --format='value(status.url)')"

echo
echo "Deploy OK."
echo "  webhook: $WEBHOOK_URL"
echo "  worker:  $WORKER_URL"
echo
echo "Registre o webhook no Telegram (use o MESMO valor do secret telegram-webhook-secret):"
echo "  curl -fsS \"https://api.telegram.org/bot<TOKEN>/setWebhook\" \\"
echo "    -d url=\"${WEBHOOK_URL}/telegram\" -d secret_token=\"<TELEGRAM_WEBHOOK_SECRET>\" \\"
echo "    -d allowed_updates='[\"message\",\"callback_query\"]'"
echo "  # allowed_updates é OBRIGATÓRIO: sem callback_query os cliques nos botões inline não"
echo "  # chegam (ficam em 'Loading…'). Omitir mantém a assinatura anterior — confira com getWebhookInfo."
echo
echo "IAM extra p/ a runtime SA: roles/datastore.user (Firestore) e roles/cloudtasks.enqueuer."
