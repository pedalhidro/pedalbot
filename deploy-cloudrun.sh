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

echo "==> service account de runtime + IAM (idempotente)"
gcloud iam service-accounts describe "$RUNTIME_SA" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${RUNTIME_SA%%@*}" --display-name="pedalbot Cloud Run runtime"

# Garante o service agent do Cloud Tasks (ele cunha o token OIDC na entrega) e pega o nº do projeto.
gcloud beta services identity create --service=cloudtasks.googleapis.com >/dev/null 2>&1 || true
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
TASKS_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

# Papéis no projeto: estado das conversas (Firestore) + enfileirar no Cloud Tasks.
for ROLE in roles/datastore.user roles/cloudtasks.enqueuer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$ROLE" --condition=None >/dev/null
done

# Ler os segredos montados nos dois serviços.
for S in telegram-bot-token telegram-webhook-secret sabia-app-password; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" \
    --condition=None >/dev/null
done

# OIDC do Cloud Tasks → worker. SEM ISTO o /anuncio trava em "Processando…" (create_task dá 403):
#  - o webhook roda como phbot-run e CRIA a task com oidc_token de phbot-run → actAs em si mesmo;
#  - o agente do Cloud Tasks CUNHA o token na entrega → tokenCreator sobre phbot-run.
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/iam.serviceAccountUser" --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:${TASKS_AGENT}" --role="roles/iam.serviceAccountTokenCreator" --condition=None >/dev/null

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
echo "SA + IAM (Firestore, Cloud Tasks, secrets, OIDC) já aplicados acima. Após o setWebhook,"
echo "mande um /anuncio de teste; se travar em 'Processando…', cheque os logs do ph-bot-webhook."
