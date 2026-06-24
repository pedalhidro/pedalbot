"""Offload do trabalho lento p/ o Cloud Tasks → serviço worker.

O webhook não pode rodar o publish (~40–80 s) nem o transcode dentro da resposta (o Telegram
reentrega após ~60 s e o Cloud Run estrangula a CPU depois do 200). Então enfileira um job e
responde rápido; o worker executa e manda o resultado por `sendMessage`.

Honestidade: o Cloud Tasks **não** garante FIFO por usuário (isso é Pub/Sub ordenado). A
segurança contra duplicar vem dos marcadores de idempotência (`persistence.Store`), não da
ordem da fila. A fila roda com concorrência baixa no worker.
"""
from __future__ import annotations

import json

from .config import Config

_client = None


def _tasks_client():
    global _client
    if _client is None:
        from google.cloud import tasks_v2

        _client = tasks_v2.CloudTasksClient()
    return _client


def enqueue(job: dict) -> bool:
    """Enfileira um job no Cloud Tasks. Devolve True se enfileirou; False se não estamos no
    modo Cloud Run (aí o chamador roda o job inline, ex.: no polling local)."""
    if not Config.using_cloud_run():
        return False

    from google.cloud import tasks_v2

    client = _tasks_client()
    parent = client.queue_path(Config.GCP_PROJECT, Config.GCP_REGION, Config.CLOUD_TASKS_QUEUE)
    http_request = {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": f"{Config.WORKER_URL}/process-job",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(job).encode("utf-8"),
    }
    if Config.WORKER_SA_EMAIL:
        # OIDC: o worker valida o token e rejeita quem não vier do Cloud Tasks.
        http_request["oidc_token"] = {
            "service_account_email": Config.WORKER_SA_EMAIL,
            "audience": Config.WORKER_URL,
        }
    client.create_task(parent=parent, task={"http_request": http_request})
    return True
