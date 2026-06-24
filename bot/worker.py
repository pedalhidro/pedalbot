"""Worker de Cloud Tasks (Cloud Run, scale-to-zero).

Recebe um job (POST /process-job) do Cloud Tasks e executa o trabalho lento (publish,
transcode, upload), respondendo o resultado ao usuário via sendMessage. Acesso restrito:
faça o deploy com `--no-allow-unauthenticated` e dê `run.invoker` à service account do Cloud
Tasks (o `oidc_token` em `tasks.py` cuida da autenticação) — a verificação fica na borda do
Cloud Run, não no app.

Subir local p/ teste:  uvicorn bot.worker:app --port 8081
"""
from __future__ import annotations

from fastapi import FastAPI, Request, Response
from telegram import Bot

from . import jobs
from .config import Config

app = FastAPI()


@app.on_event("startup")
async def _startup():
    Config.require_startup()


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/process-job")
async def process_job(request: Request):
    job = await request.json()
    async with Bot(Config.TELEGRAM_BOT_TOKEN) as bot:
        await jobs.execute_job(bot, job)
    return Response(status_code=200)
