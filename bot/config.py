"""Configuração por ambiente do pedalbot.

Carrega `.env` (best-effort) e expõe `Config`. Dois modos, mesmo código:
- **Cloud Run (webhook)** — primário: precisa de GCP_PROJECT/REGION, fila Cloud Tasks,
  WORKER_URL e o segredo do webhook.
- **Long-polling local** — fallback de dev: basta token + allowlist.

A allowlist é um **portão duro (fail-closed)**: sem `TELEGRAM_ALLOWED_USERS` o bot NÃO sobe
(`require_startup`). Mesma filosofia do APP_PASSWORD do sabiá, mas o bot nunca roda aberto.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


_load_dotenv()


def _csv_ints(raw: str) -> "frozenset[int]":
    out: set[int] = set()
    for tok in (raw or "").replace(",", " ").split():
        try:
            out.add(int(tok))
        except ValueError:
            pass
    return frozenset(out)


class Config:
    # ── Telegram ──────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    ALLOWED_USERS = _csv_ints(os.environ.get("TELEGRAM_ALLOWED_USERS", ""))
    WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

    # ── Backends (clientes HTTP) ──────────────────────────────────────────
    SABIA_BASE_URL = os.environ.get("SABIA_BASE_URL", "http://localhost:8080").rstrip("/")
    SABIA_APP_PASSWORD = os.environ.get("SABIA_APP_PASSWORD", "")
    AMORA_BASE_URL = os.environ.get("AMORA_BASE_URL", "https://amora.pedalhidrografi.co").rstrip("/")

    # ── GCP / Cloud Run ───────────────────────────────────────────────────
    GCP_PROJECT = os.environ.get("GCP_PROJECT", "pedal-hidrografico")
    GCP_REGION = os.environ.get("GCP_REGION", "southamerica-east1")
    CLOUD_TASKS_QUEUE = os.environ.get("CLOUD_TASKS_QUEUE", "phbot-jobs")
    WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
    WORKER_SA_EMAIL = os.environ.get("WORKER_SA_EMAIL", "")  # OIDC p/ Tasks→worker
    FIRESTORE_PREFIX = os.environ.get("FIRESTORE_PREFIX", "phbot_")

    # ── Limites / política ────────────────────────────────────────────────
    MAX_TELEGRAM_FILE_MB = 20          # teto do getFile na cloud Bot API
    FFMPEG_TIMEOUT_S = 300
    CONVERSATION_TIMEOUT_S = 3600
    DEBOUNCE_S = 30
    UPDATE_DEDUP_TTL_S = 24 * 3600     # janela p/ dedup de update_id (retries do Telegram)

    @classmethod
    def is_allowed(cls, user_id: "int | None") -> bool:
        return user_id is not None and user_id in cls.ALLOWED_USERS

    @classmethod
    def using_cloud_run(cls) -> bool:
        """Modo cloud (webhook + Firestore + Cloud Tasks).

        Verdadeiro quando: (a) há WORKER_URL — é o webhook, que despacha jobs; ou (b) rodamos
        DENTRO do Cloud Run — o `K_SERVICE` é injetado pela plataforma. O (b) é essencial pro
        WORKER: ele NÃO tem WORKER_URL, então sem isso cairia no Store em memória e perderia o
        marcador de idempotência do publish entre instâncias/reentregas do Cloud Tasks — risco de
        post duplicado no Instagram. No polling local não há K_SERVICE nem WORKER_URL → False.
        """
        return bool(cls.WORKER_URL) or bool(os.environ.get("K_SERVICE"))

    @classmethod
    def require_startup(cls) -> None:
        """Fail-closed: aborta o processo se faltar o essencial."""
        problems: list[str] = []
        if not cls.TELEGRAM_BOT_TOKEN:
            problems.append("TELEGRAM_BOT_TOKEN ausente")
        if not cls.ALLOWED_USERS:
            problems.append(
                "TELEGRAM_ALLOWED_USERS vazio — defina a lista de IDs permitidos "
                "(o bot nunca roda aberto)"
            )
        if problems:
            sys.stderr.write("ERRO de configuração:\n  - " + "\n  - ".join(problems) + "\n")
            raise SystemExit(2)
