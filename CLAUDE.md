# pedalbot — map for Claude

Telegram bot do **Pedal Hidrográfico**: um frontend de chat (wizards) para dois
backends tratados como **clientes HTTP** (o bot não importa o código deles).
Leia o `README.md` primeiro — ele tem os comandos, a arquitetura (Cloud Run
webhook + worker, ou polling local) e a config. Este arquivo só registra o que
não é óbvio pelo código.

## Backends são repos vizinhos (mesmo `pedalhidro/`)

- **sabiá** → `../sabia` — compõe/publica no Instagram (`/api/publish`,
  `/api/posts`). Apontado por `SABIA_BASE_URL`.
- **amora** → `../amora` — mapa + censo: passeios (`/upload-tour`), fotos/vídeos
  (`/upload-image`/`/upload-video`), deletes. Apontado por `AMORA_BASE_URL`.

São **repos separados**; o bot fala com eles por HTTP, nunca importa.
`tests/verify.py` valida os builders TTL contra os **shapes SHACL reais do
amora** em `../amora/web/data` (`shapes.ttl` + `ontology.ttl`) — se mover/renomear
o amora, ajuste o caminho lá.

## Dois modos, mesmo código

- **Polling local** (`python -m bot`): estado da conversa **em memória**, sem
  GCP. É o que roda quando `WORKER_URL` está vazio (`Config.using_cloud_run()`).
- **Cloud Run webhook** (`bot.webhook` + `bot.worker`): estado da conversa no
  **Firestore** (`FirestorePersistence`), trabalho lento via Cloud Tasks.

## Invariante: o bot PRECISA estar inscrito em `callback_query`

Botão inline em "Loading…" pra sempre = o update de `callback_query` **nem chega**
ao bot. O Telegram lembra a última `allowed_updates` passada num `getUpdates`/
`setWebhook` e, se você **omite**, mantém a anterior — uma assinatura velha de
`["message"]` (de um deploy/setWebhook anterior) entrega mensagens mas **descarta
os cliques**. Sintoma cruel: o wizard anda (mensagens), mas todo botão trava; e aí
`/anuncio` "não responde" porque você fica preso no passo do botão (entry point é
ignorado no meio de uma conversa — use `/cancelar`, ou `allow_reentry=True` que já
está ligado). Confira sempre com `getWebhookInfo`.

Por isso:

- **Polling** (`bot/polling.py`): `run_polling(allowed_updates=Update.ALL_TYPES)` —
  reescreve a assinatura a cada `getUpdates`. **Não** volte a omitir `allowed_updates`.
- **Webhook**: o `setWebhook` (ver `deploy-cloudrun.sh`) precisa de
  `allowed_updates=["message","callback_query"]` — omitir herda a assinatura velha.
- Wizards com `allow_reentry=True`: re-digitar o comando reinicia em vez de ser
  engolido no meio da conversa.

## Rede de segurança p/ botões órfãos (defesa em profundidade)

Mesmo inscrito, um callback pode ficar sem dono se o **estado** da conversa some
(restart no polling = estado em memória; `conversation_timeout`; troca de instância
no Cloud Run sem restaurar persistência). Então `handlers.register()` adiciona um
`CallbackQueryHandler(orphan_callback)` **por último no grupo 0**: dentro do grupo só
o 1º handler que casa roda, então as conversas têm prioridade quando há estado e o que
sobra (órfão) é sempre respondido (limpa o spinner). **Não remova esse catch-all nem
registre callback depois dele.** Coberto por `test_orphan_callback` em
`tests/verify.py`.

## Verificação

`.venv/bin/python tests/verify.py` — offline (sem creds/rede): `py_compile`,
pHash/vHash, EXIF/GPS real, transcode ffmpeg, builders TTL vs SHACL do amora, e a
regressão do callback órfão.
