"""Receptor de webhook (Cloud Run, stateless, scale-to-zero).

Valida o secret-token, **deduplica update_id** (Telegram reentrega não-2xx), e processa o
update SÍNCRONO na requisição — assim a CPU do Cloud Run fica alocada durante o trabalho (os
passos do wizard são rápidos; o trabalho pesado já é offloaded p/ o Cloud Tasks). Depois,
**flush da persistência** pro Firestore, porque a instância pode congelar logo após o 200.

Subir local p/ teste:  uvicorn bot.webhook:app --port 8080
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request, Response
from telegram import Update

log = logging.getLogger("webhook")

from .config import Config
from .handlers import build_application, refresh_conversations
from .persistence import Store

_application = None
_store = Store()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _application
    Config.require_startup()
    _application = build_application()
    await _application.initialize()  # carrega persistência; NÃO start() (sem loop de fundo)
    yield
    await _application.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/telegram")
async def telegram(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if Config.WEBHOOK_SECRET and x_telegram_bot_api_secret_token != Config.WEBHOOK_SECRET:
        return Response(status_code=401)
    data = await request.json()
    update = Update.de_json(data, _application.bot)
    if update is None:
        return Response(status_code=200)
    uid = update.update_id

    # Dedup: marca DEPOIS do sucesso. Se algo falhar (process/flush/Firestore), devolvemos 5xx
    # e o Telegram reentrega — sem perder o update. O risco de reprocessar é coberto pelos
    # marcadores idempotentes a jusante (hash de mídia, replace por id, claim de publicação).
    try:
        if uid is not None and await _store.is_processed(uid):
            return Response(status_code=200)
        async with asyncio.timeout(50):  # < timeout do Telegram (~60 s)
            # Reidrata o estado da conversa deste update ANTES de despachar: a instância pode não
            # ser a que iniciou o wizard (o PTB só lê `conversations` no initialize()). Sem isto o
            # clique do botão cai no catch-all órfão mesmo com estado salvo — botões inline
            # (/excluir_post, /anuncio…) falhando de forma intermitente.
            await refresh_conversations(_application, update)
            await _application.process_update(update)
            if _application.persistence is not None and hasattr(_application, "update_persistence"):
                await _application.update_persistence()  # flush p/ Firestore antes de congelar
        if uid is not None:
            await _store.mark_processed(uid)
    except Exception:  # noqa: BLE001
        log.exception("falha processando update %s — devolvendo 500 p/ reentrega", uid)
        return Response(status_code=500)
    return Response(status_code=200)
