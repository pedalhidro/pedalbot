"""Fallback local: long-polling (`python -m bot`).

Processo único e longevo — estado em memória do PTB, trabalho lento roda inline (sem Cloud
Tasks), JobQueue do PTB cuida do conversation_timeout. Bom p/ dev/offline. Para produção use
o modo webhook (Cloud Run): `bot.webhook` + `bot.worker`.
"""
from __future__ import annotations

import logging

from telegram import Update

from .config import Config
from .handlers import build_application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # silencia o ruído do getUpdates
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    Config.require_startup()
    if Config.using_cloud_run():
        logging.warning("WORKER_URL definido — em produção prefira o modo webhook (bot.webhook).")
    app = build_application()
    # allowed_updates EXPLÍCITO: o Telegram lembra a última assinatura passada num getUpdates/
    # setWebhook e, se a gente omite, mantém a anterior. Uma assinatura velha de ["message"]
    # (de um setWebhook/deploy anterior) faz os updates de callback_query (cliques nos botões
    # inline) NUNCA chegarem — o botão fica em "Loading…" pra sempre. Update.ALL_TYPES reescreve
    # a assinatura a cada getUpdates e garante message + callback_query.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
