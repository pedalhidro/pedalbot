# pedalbot

Bot de Telegram do **Pedal Hidrográfico**. Um único frontend de chat para dois backends,
tratados como **clientes HTTP** (o bot não importa o código de nenhum):

- **sabiá** — compõe e publica posts no **Instagram** (`/api/publish`, `/api/posts`).
- **amora** — mapa + **censo**: passeios (`/upload-tour`), fotos/vídeos
  (`/upload-image`, `/upload-video`) e deletes.

## Comandos

| Comando | O que faz |
|---|---|
| `/anuncio` | compõe e publica um post no Instagram (wizard) → sabiá |
| `/passeio` | cadastra um passeio do censo (wizard) → amora; pode associar a um post do Instagram (colar URL ou publicar na hora) |
| `/subir_midia` | sobe **foto e/ou vídeo geolocalizado** pro mapa (um wizard, roteia por tipo) → amora |
| `/posts` | lista posts publicados (com métricas) |
| `/excluir_foto <phash>` · `/excluir_video <vhash>` · `/excluir_passeio <id>` · `/excluir_post <shortcode>` | exclusão **com confirmação** |
| `/ajuda` · `/cancelar` | ajuda / abortar wizard |

**Mídia pro mapa entra como ARQUIVO e com GPS.** Foto comprimida do Telegram perde o EXIF e é
recusada (o amora exige `schema:locationCreated`). Vídeo **acima de 20 MB** não sobe (limite do
`getFile` da Bot API) — corte/comprima ou use o formulário web.

## Arquitetura (Cloud Run, webhook, scale-to-zero)

```
Telegram ──webhook (secret-token)──► ph-bot-webhook (Cloud Run, min=0)
   dedup update_id (Firestore) · processa o passo do wizard · responde rápido
        └─ trabalho lento (publish/transcode) ─► Cloud Tasks ─► ph-bot-worker (Cloud Run, min=0)
                                                     getFile ≤20 MB · pHash/ffmpeg/TTL · publish (idempotente)
                                                     └─► HTTP p/ sabiá + amora · sendMessage(resultado)
Estado: Firestore (conversations + user_data + update_ids/marcadores de publicação)
```

- O passo do wizard roda **síncrono na requisição** do webhook (a CPU do Cloud Run fica
  alocada); só o trabalho pesado é offloaded p/ o worker via Cloud Tasks.
- **Idempotência obrigatória** (Telegram e Cloud Tasks reentregam): dedup de `update_id` +
  marcador de publicação antes de chamar o sabiá (que **não** é idempotente) — senão um retry
  dobraria o post no Instagram.

**Fallback local (dev):** long-polling, estado em memória, trabalho inline, sem GCP:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # + brew install ffmpeg exiftool
cp .env.example .env                      # preencha TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS
python -m bot
```

## Configuração

Veja `.env.example`. Essenciais: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` (CSV de IDs —
**obrigatório**, o bot não sobe sem). Backends: `SABIA_BASE_URL`, `SABIA_APP_PASSWORD`,
`AMORA_BASE_URL`. Cloud Run: `GCP_PROJECT`, `GCP_REGION`, `CLOUD_TASKS_QUEUE`, `WORKER_URL`,
`WORKER_SA_EMAIL`, `TELEGRAM_WEBHOOK_SECRET`, `FIRESTORE_PREFIX`.

Sem creds de IG/GCS no bot — elas vivem nos backends. **A allowlist é fail-closed**: vazia ⇒ o
bot aborta no boot.

## Deploy (Cloud Run)

```sh
bash deploy-cloudrun.sh        # cria a fila + 2 serviços; imprime o curl do setWebhook
```

O `worker` sobe **privado** (`--no-allow-unauthenticated`): só o Cloud Tasks o invoca, via OIDC.
O `webhook` é público (o Telegram precisa alcançá-lo) e valida o `secret_token`.

## Verificação

```sh
.venv/bin/python tests/verify.py
```

Cobre offline (sem creds/rede): `py_compile`; pHash/vHash determinístico; EXIF/GPS real
(exiftool); transcode real (ffmpeg, **pulado se o ffmpeg da máquina estiver quebrado**); e os
**builders TTL validados contra os shapes SHACL reais do amora** (tour/image/video sem
`sh:Violation`, incluindo a regra de intensidade derivada da energia).

Live (precisa de creds): publicar no sabiá em DRY_RUN, criar um passeio de teste no amora,
subir uma mídia geolocalizada, e o ciclo de deploy/`setWebhook`/idempotência — ver o plano.

## Notas

- **Intensidade do passeio é derivada da energia** (kJ), não escolha livre — regra SHACL do
  amora (`<150` De boa, `<300` Ok, `<500` Endorfinado, `<1000` Frito, `≥1000` Insano).
- **Paridade de hash não é bit-exata**: o resize/precisão divergem do browser; o amora
  deduplica por Hamming (limiar 5). A meta é minimizar e medir a divergência.
- **Edição no Instagram**: a API do IG não edita post publicado; edição de passeio (amora) é
  via `mode=patch`.
- Comentários/UI em PT, identificadores em EN (convenção do ecossistema).
