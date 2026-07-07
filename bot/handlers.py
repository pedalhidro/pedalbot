"""Wizards do bot (ConversationHandler) + comandos.

Os passos do wizard são RÁPIDOS (perguntam/guardam estado e respondem). O trabalho LENTO
(publicar, transcodar, subir) vira um job: `dispatch()` enfileira no Cloud Tasks (Cloud Run)
ou roda inline (polling local). O resultado final volta por `sendMessage` (no `jobs.py`).

Estado em `context.user_data` (persistido no Firestore no Cloud Run). Auth por allowlist em
todo handler. Mídia do /subir-midia entra como **arquivo** (document/video) com GPS.
"""
from __future__ import annotations

import logging
import re
import uuid
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import jobs, tasks
from .clients import Amora, BackendError, Sabia
from .config import Config
from .persistence import Store, make_persistence

# Allowlist dinâmica (grant por senha) + marcadores — Firestore no Cloud Run, memória no polling.
_store = Store()

log = logging.getLogger("pedalbot.handlers")

# Persistência só existe no modo Cloud Run; sem ela, ConversationHandler(persistent=_PERSIST) é
# inválido. Casa com make_persistence().
_PERSIST = Config.using_cloud_run()

# estados
(A_PHOTOS, A_CAPTION, A_COLLAB, A_TAGGED, A_LOCATION, A_CONFIRM) = range(6)
(P_TITLE, P_DATE, P_SERIES, P_ROUTE, P_DESC, P_ATTEND, P_ENERGY, P_INSTA, P_INSTA_URL) = range(6, 15)
(M_COLLECT, M_TOUR) = range(15, 17)
(D_CONFIRM,) = range(17, 18)
(P_INSTA_IMG,) = range(18, 19)  # coleta da arte quando o IG é "publicar agora"
(D_PICK,) = range(19, 20)  # /excluir_post sem arg: escolher o post numa lista de botões

_PHASH_RE = re.compile(r"^[0-9a-f]{16}$")
_TOURID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IG_RE = re.compile(r"https?://(www\.)?instagram\.com/(p|reel)/[\w-]+", re.I)


# ── auth + utilidades ────────────────────────────────────────────────────────
async def _has_access(uid: "int | None") -> bool:
    """Allowlist estática (env) OU grant dinâmico por senha (persistido). Curto-circuita a
    allowlist antes de tocar o Firestore — quem já está no env não paga leitura."""
    return Config.is_allowed(uid) or await _store.is_granted(uid)


def restricted(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **k):
        uid = update.effective_user.id if update.effective_user else None
        if not await _has_access(uid):
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"⛔ Acesso negado. Seu ID do Telegram é {uid} — peça pra incluí-lo na "
                    "allowlist, ou entre com a senha: /senha <senha>."
                )
            return ConversationHandler.END
        return await func(update, context, *a, **k)

    return wrapper


async def cmd_senha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Porta de acesso por senha: quem acertar entra na allowlist dinâmica (persistida).

    Casa com `/senha <senha>` OU com a senha pura (handler de texto exato). NÃO é `@restricted`
    — é o próprio portão. Para com `ApplicationHandlerStop` pra não vazar pro wizard/fallback.
    """
    msg = update.effective_message
    text = (msg.text or "") if msg else ""
    parts = text.split(maxsplit=1)
    candidate = parts[1].strip() if (parts and parts[0].lstrip("/").startswith("senha") and len(parts) == 2) \
        else text.strip()
    # case-insensitive: casa com o filtro de texto (re.I) e perdoa o auto-capitalize do celular.
    if not Config.ACCESS_PASSWORD or candidate.lower() != Config.ACCESS_PASSWORD.lower():
        if text.lstrip().startswith("/senha") and msg:
            await msg.reply_text("Senha incorreta. Use: /senha <senha>")
        raise ApplicationHandlerStop
    uid = update.effective_user.id if update.effective_user else None
    if uid is not None and not await _has_access(uid):
        await _store.grant_access(uid)
        log.info("acesso liberado por senha: user=%s", uid)
    if msg:
        await msg.reply_text("✅ Senha correta — acesso liberado! Use /start pra começar.")
    raise ApplicationHandlerStop


def _token(context) -> str:
    tok = context.user_data.get("_token")
    if not tok:
        tok = uuid.uuid4().hex[:12]
        context.user_data["_token"] = tok
    return tok


async def dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, payload: dict) -> None:
    """Manda o job pro worker (Cloud Run) ou roda inline (polling)."""
    chat_id = update.effective_chat.id
    job = {
        "action": action,
        "chat_id": chat_id,
        "request_id": f"{chat_id}:{action}:{_token(context)}",
        "payload": payload,
    }
    log.info("dispatch action=%s chat=%s (cloud_run=%s)", action, chat_id, Config.using_cloud_run())
    await context.bot.send_message(chat_id, "⏳ Processando…")
    if not tasks.enqueue(job):
        await jobs.execute_job(context.bot, job)


def _parse_dt(text: str) -> "str | None":
    """'2026-06-11 20:00' → ISO -03:00. Aceita já-ISO com TZ."""
    text = text.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text) and ("+" in text[10:] or "Z" in text):
        return text
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", text)
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:00-03:00"


# ── comandos simples ─────────────────────────────────────────────────────────
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    linhas = [
        "*digestor do censo hidrográfico*\n",
        "/anuncio — compor e publicar post no Instagram (sabiá)",
        "/posts — listar posts publicados",
        "/excluir\\_post — remover um post do Instagram (sem argumento, mostra a lista pra escolher)",
    ]
    if Config.AMORA_ENABLED:
        linhas += [
            "/passeio — cadastrar passeio no censo (amora)",
            "/subir\\_midia — subir foto/vídeo geolocalizado pro mapa",
            "/excluir\\_foto · /excluir\\_video · /excluir\\_passeio",
        ]
    linhas.append("/ajuda · /cancelar")
    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


@restricted
async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "Cada comando abre um passo-a-passo. Use /cancelar pra abortar."
    if Config.AMORA_ENABLED:
        txt += ("\nMídia pro mapa: envie como ARQUIVO (não foto comprimida) e com GPS — "
                "senão o amora rejeita.")
    await update.message.reply_text(txt)


@restricted
async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelado.")
    return ConversationHandler.END


# ── /anuncio (sabiá) ─────────────────────────────────────────────────────────
@restricted
async def anuncio_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["photos"] = []
    await update.message.reply_text("📷 Envie a(s) foto(s) do post; quando terminar, mande /pronto.")
    return A_PHOTOS


async def anuncio_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fid = None
    if msg.photo:
        fid = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        fid = msg.document.file_id
    if fid:
        context.user_data["photos"].append(fid)
        await msg.reply_text(f"ok ({len(context.user_data['photos'])} foto[s]). /pronto quando acabar.")
    return A_PHOTOS


async def anuncio_pronto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("photos"):
        await update.message.reply_text("Nenhuma foto ainda. Envie ao menos uma.")
        return A_PHOTOS
    await update.message.reply_text("✍️ Legenda do post?")
    return A_CAPTION


async def anuncio_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["caption"] = update.message.text or ""
    await update.message.reply_text("👥 Colaboradores (@handles) ou /pular")
    return A_COLLAB


async def anuncio_collab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["collaborators"] = "" if update.message.text == "/pular" else (update.message.text or "")
    await update.message.reply_text("🏷️ Marcar contas na imagem (@handles) ou /pular")
    return A_TAGGED


async def anuncio_tagged(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tagged"] = "" if update.message.text == "/pular" else (update.message.text or "")
    await update.message.reply_text("📍 Nome do local ou /pular")
    return A_LOCATION


async def anuncio_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["location_name"] = "" if update.message.text == "/pular" else (update.message.text or "")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Publicar AO VIVO", callback_data="anuncio:live")],
        [InlineKeyboardButton("📝 Salvar rascunho", callback_data="anuncio:draft")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="anuncio:cancel")],
    ])
    n = len(context.user_data["photos"])
    await update.message.reply_text(
        f"Revisar: {n} foto(s)\nLegenda: {context.user_data.get('caption','')[:120]}\n"
        f"Local: {context.user_data.get('location_name') or '—'}",
        reply_markup=kb,
    )
    return A_CONFIRM


async def anuncio_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":", 1)[1]
    if choice == "cancel":
        context.user_data.clear()
        await q.edit_message_text("Cancelado.")
        return ConversationHandler.END
    payload = {
        "image_file_ids": context.user_data["photos"],
        "caption": context.user_data.get("caption", ""),
        "collaborators": context.user_data.get("collaborators", ""),
        "tagged": context.user_data.get("tagged", ""),
        "location_name": context.user_data.get("location_name", ""),
        "is_posted": choice == "live",
        "confirm": choice == "live",
    }
    await q.edit_message_text("✅ Enviando…" if choice == "live" else "📝 Salvando rascunho…")
    await dispatch(update, context, "anuncio", payload)
    context.user_data.clear()
    return ConversationHandler.END


# ── /passeio (amora) ─────────────────────────────────────────────────────────
@restricted
async def passeio_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["tour"] = {}
    await update.message.reply_text("🚲 Novo passeio.\nTítulo?")
    return P_TITLE


async def passeio_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tour"]["title"] = update.message.text or ""
    await update.message.reply_text("📅 Data e hora? (ex.: 2026-06-11 20:00)")
    return P_DATE


async def passeio_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iso = _parse_dt(update.message.text or "")
    if not iso:
        await update.message.reply_text("Formato inválido. Use AAAA-MM-DD HH:MM.")
        return P_DATE
    context.user_data["tour"]["date_iso"] = iso
    await update.message.reply_text("🔢 Série e número? (ex.: PH 97) ou /pular")
    return P_SERIES


async def passeio_series(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text != "/pular":
        m = re.match(r"\s*([A-Za-z0-9]+)\s+(\d+)\s*$", update.message.text or "")
        if not m:
            await update.message.reply_text("Use: <SÉRIE> <número> (ex.: PH 97) ou /pular")
            return P_SERIES
        context.user_data["tour"]["series_code"] = m.group(1)
        context.user_data["tour"]["series_seq"] = int(m.group(2))
        context.user_data["tour"]["series_is_new"] = False
    await update.message.reply_text("🗺️ Link da rota (RideWithGPS) ou /pular")
    return P_ROUTE


async def passeio_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text != "/pular":
        context.user_data["tour"]["route_url"] = (update.message.text or "").strip()
    await update.message.reply_text("📝 Descrição/narrativa ou /pular")
    return P_DESC


async def passeio_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text != "/pular":
        context.user_data["tour"]["description"] = update.message.text or ""
    # (pergunta "Quantos participantes?" removida — count_attendee é opcional no TTL/SHACL)
    await update.message.reply_text("⚡ Energia estimada (kJ)? (a intensidade é derivada) ou /pular")
    return P_ENERGY


async def passeio_energy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text != "/pular":
        try:
            context.user_data["tour"]["energy_kj"] = float((update.message.text or "").replace(",", ".").strip())
        except ValueError:
            await update.message.reply_text("Número inválido. Tente de novo ou /pular")
            return P_ENERGY
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Pular Instagram", callback_data="insta:skip")],
        [InlineKeyboardButton("Colar URL existente", callback_data="insta:url")],
        [InlineKeyboardButton("Publicar agora (sabiá)", callback_data="insta:publish")],
    ])
    await update.message.reply_text("📸 Associar a um post do Instagram?", reply_markup=kb)
    return P_INSTA


async def passeio_insta_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = q.data.split(":", 1)[1]
    context.user_data["insta_mode"] = mode
    if mode == "url":
        await q.edit_message_text("Cole a URL do post/reel do Instagram:")
        return P_INSTA_URL  # estado dedicado a texto, sem ambiguidade com os callbacks
    if mode == "publish":
        context.user_data["insta"] = {"mode": "publish"}
        await q.edit_message_text(
            "📷 Envie a arte do anúncio (imagem) — vira a foto do post no Instagram e do passeio."
        )
        return P_INSTA_IMG
    await q.edit_message_text("Sem link do Instagram.")
    return await _passeio_finish(update, context, via_callback=True)


async def passeio_insta_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not _IG_RE.search(url):
        await update.message.reply_text("URL não parece do Instagram. Cole um link /p/ ou /reel/.")
        return P_INSTA_URL
    context.user_data["tour"]["instagram_url"] = url
    return await _passeio_finish(update, context)


async def passeio_insta_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fid = None
    if msg.photo:
        fid = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        fid = msg.document.file_id
    if not fid:
        await msg.reply_text("Mande uma imagem (foto ou arquivo de imagem).")
        return P_INSTA_IMG
    context.user_data["announcement_file_id"] = fid
    return await _passeio_finish(update, context)


async def _passeio_finish(update: Update, context: ContextTypes.DEFAULT_TYPE, *, via_callback=False):
    if "tour" not in context.user_data or not context.user_data["tour"].get("title"):
        reply = (update.callback_query.message.reply_text if via_callback else update.message.reply_text)
        await reply("Sessão expirada — recomece com /passeio.")
        context.user_data.clear()
        return ConversationHandler.END
    tour = dict(context.user_data["tour"])
    tour_id = _slug_tour_id(tour.get("title", "passeio"))
    tour["tour_id"] = tour_id
    payload = {
        "tour": tour,
        "instagram": context.user_data.get("insta"),
        "announcement_file_id": context.user_data.get("announcement_file_id"),
    }
    reply = (update.callback_query.message.reply_text if via_callback else update.message.reply_text)
    await reply(f"Criando passeio tour_{tour_id}…")
    await dispatch(update, context, "passeio", payload)
    context.user_data.clear()
    return ConversationHandler.END


def _slug_tour_id(title: str) -> str:
    import unicodedata

    base = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    s = "-".join(p for p in re.split(r"[^a-z0-9]+", base) if p)[:48] or "passeio"
    return s


# ── /subir-midia (amora foto+vídeo) ──────────────────────────────────────────
@restricted
async def midia_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["items"] = []
    await update.message.reply_text(
        "📎 Envie a(s) mídia(s) como ARQUIVO (com GPS). Foto comprimida perde o GPS e é recusada. "
        "Mande /pronto quando terminar."
    )
    return M_COLLECT


async def midia_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    vid = msg.video
    item = None
    if doc:
        mime = doc.mime_type or ""
        kind = "photo" if mime.startswith("image/") else "video" if mime.startswith("video/") else None
        if kind and doc.file_size and doc.file_size > Config.MAX_TELEGRAM_FILE_MB * 1024 * 1024 and kind == "video":
            await msg.reply_text(f"⚠️ Vídeo acima de {Config.MAX_TELEGRAM_FILE_MB} MB — corte/comprima e reenvie.")
            return M_COLLECT
        if kind:
            item = {"file_id": doc.file_id, "kind": kind, "filename": doc.file_name or "", "mime": mime}
    elif msg.photo:
        await msg.reply_text("⚠️ Isso veio como foto comprimida (sem GPS). Reenvie como ARQUIVO.")
        return M_COLLECT
    elif vid:
        await msg.reply_text("⚠️ Envie o vídeo como ARQUIVO (não como vídeo do Telegram) p/ preservar o GPS.")
        return M_COLLECT
    if item:
        context.user_data["items"].append(item)
        await msg.reply_text(f"ok ({len(context.user_data['items'])}). /pronto quando acabar.")
    return M_COLLECT


async def midia_pronto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("items"):
        await update.message.reply_text("Nada recebido ainda.")
        return M_COLLECT
    await update.message.reply_text("🚲 ID do passeio p/ associar (tour_<id>) ou /pular")
    return M_TOUR


async def midia_tour(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tour_id = None
    if update.message.text != "/pular":
        tid = (update.message.text or "").strip().replace("tour_", "")
        if not _TOURID_RE.match(tid):
            await update.message.reply_text("ID inválido. Use letras/números/-/_ ou /pular")
            return M_TOUR
        tour_id = tid
    await dispatch(update, context, "subir_midia",
                   {"items": context.user_data["items"], "tour_id": tour_id})
    context.user_data.clear()
    return ConversationHandler.END


# ── /posts + deletes (fora do worker — rápidos) ──────────────────────────────
@restricted
async def cmd_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    import asyncio

    try:
        posts = await asyncio.to_thread(Sabia().list_posts)
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Erro ao listar: {exc}")
        return
    if not posts:
        await update.message.reply_text("Nenhum post.")
        return
    lines = []
    for p in posts[:10]:
        flag = " 🗑️" if p.get("deletable") else ""
        lines.append(f"• {p.get('shortcode')} ❤{p.get('likes',0)} 💬{p.get('comments',0)}{flag}\n  {p.get('permalink','')}")
    await update.message.reply_text("\n".join(lines))


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Excluir", callback_data="del:yes"),
        InlineKeyboardButton("❌ Não", callback_data="del:no"),
    ]])


def _make_delete_cmd(kind: str, regex, label: str):
    @restricted
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        import asyncio

        parts = (update.message.text or "").split()
        has_arg = len(parts) >= 2 and bool(regex.match(parts[1]))

        if kind == "post":
            # Precisamos da lista da sabiá em ambos os casos: validar o arg OU montar o picker.
            # Só posts app-owned (`deletable`) podem ser removidos — a sabiá rejeita o resto (404).
            try:
                posts = await asyncio.to_thread(Sabia().list_posts)
            except Exception as exc:  # noqa: BLE001 - rede/backend fora do ar
                await update.message.reply_text(f"Não consegui consultar os posts da sabiá: {exc}")
                return ConversationHandler.END

            if not has_arg:
                # Sem shortcode: propõe a lista de posts excluíveis como botões (um por post).
                deletable = [p for p in posts if p.get("deletable")]
                if not deletable:
                    await update.message.reply_text(
                        "Nenhum post excluível por aqui — só dá pra remover o que a sabiá publicou "
                        "(app-owned). Veja /posts (os marcados com 🗑️)."
                    )
                    return ConversationHandler.END
                rows = [[InlineKeyboardButton(
                    f"{p.get('shortcode')} ❤{p.get('likes', 0)} 💬{p.get('comments', 0)}",
                    callback_data=f"del:pick:{p.get('shortcode')}")] for p in deletable[:10]]
                rows.append([InlineKeyboardButton("❌ Cancelar", callback_data="del:no")])
                await update.message.reply_text("Qual post excluir?", reply_markup=InlineKeyboardMarkup(rows))
                return D_PICK

            target = parts[1]
            if target not in {p.get("shortcode") for p in posts}:
                await update.message.reply_text(
                    f"❌ '{target}' não está entre os posts publicados pela sabiá — só dá pra "
                    "excluir o que foi criado por aqui. Use /posts pra ver os shortcodes."
                )
                return ConversationHandler.END
        else:
            # foto/vídeo/passeio (amora): sem endpoint de listagem aqui, então exige o id.
            if not has_arg:
                await update.message.reply_text(f"Uso: /{label} <id>")
                return ConversationHandler.END
            target = parts[1]

        context.user_data["del"] = {"kind": kind, "id": target}
        await update.message.reply_text(f"Confirmar exclusão de {kind} {target}?", reply_markup=_confirm_kb())
        return D_CONFIRM

    return _start


async def delete_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Post escolhido no picker (`del:pick:<shortcode>`) → pede confirmação."""
    q = update.callback_query
    await q.answer()
    shortcode = q.data.split(":", 2)[2]
    context.user_data["del"] = {"kind": "post", "id": shortcode}
    await q.edit_message_text(f"Confirmar exclusão de post {shortcode}?", reply_markup=_confirm_kb())
    return D_CONFIRM


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio

    q = update.callback_query
    await q.answer()
    if q.data == "del:no":
        await q.edit_message_text("Mantido.")
        return ConversationHandler.END
    d = context.user_data.get("del", {})
    kind, _id = d.get("kind"), d.get("id")
    amora, sabia = Amora(), Sabia()
    try:
        if kind == "foto":
            await asyncio.to_thread(amora.delete_image, _id)
        elif kind == "video":
            await asyncio.to_thread(amora.delete_video, _id)
        elif kind == "passeio":
            await asyncio.to_thread(amora.delete_tour, _id)
        elif kind == "post":
            await asyncio.to_thread(sabia.delete_post, _id)
        await q.edit_message_text(f"🗑️ {kind} {_id} excluído. (por {update.effective_user.id})")
    except BackendError as exc:
        await q.edit_message_text(f"❌ {exc.payload.get('error') or exc}")
    except Exception as exc:  # noqa: BLE001 - rede/timeout/backend fora do ar: NÃO deixe o botão "morto"
        log.warning("falha ao excluir %s %s", kind, _id, exc_info=True)
        await q.edit_message_text(f"❌ Falha ao excluir {kind} {_id}: {exc}")
    context.user_data.clear()
    return ConversationHandler.END


# ── rede de segurança p/ botões "órfãos" ─────────────────────────────────────
async def orphan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde QUALQUER callback que nenhuma conversa tratou.

    Os botões inline (`anuncio:`/`insta:`/`del:`) só são tratados DENTRO do ConversationHandler,
    com base no estado da conversa. Se esse estado sumiu — o bot reiniciou (no polling o estado é
    em memória e morre a cada restart), a conversa expirou (`conversation_timeout`), ou a instância
    do Cloud Run trocou sem restaurar a persistência — nenhum handler responde e o Telegram fica em
    "Loading…" pra sempre. Aqui a gente sempre responde: limpa o spinner e tira os botões pra não
    convidar a re-clicar. Vai por ÚLTIMO no grupo 0, então as conversas têm prioridade quando o
    estado existe (só cai aqui o que de fato ficou órfão).
    """
    q = update.callback_query
    if q is None:
        return
    # Tudo aqui é limpeza best-effort: se a query for velha demais (Telegram já dropou o spinner)
    # answer/edit estouram, mas não é erro de verdade — não vale sujar o on_error com isso.
    try:
        await q.answer("Essa sessão expirou — recomece o comando (ex.: /anuncio).", show_alert=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        await q.edit_message_reply_markup(reply_markup=None)  # tira os botões da mensagem antiga
    except Exception:  # noqa: BLE001 - msg antiga/inalterada: o que importa (answer) já saiu
        pass


async def fallback_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mensagem não reconhecida (fora de qualquer wizard) → orienta o usuário.

    Vai por ÚLTIMO no grupo 0 (depois de comandos e conversas): dentro do grupo só o 1º handler
    que casa roda, então só cai aqui o que nenhum outro tratou. Silencioso p/ quem não está na
    allowlist (não vira beacon p/ estranhos)."""
    uid = update.effective_user.id if update.effective_user else None
    if not await _has_access(uid):
        return
    msg = update.effective_message
    if msg:
        await msg.reply_text("Não entendi 🤔 Mande /start para ver as opções.")


async def refresh_conversations(app: Application, update: Update) -> None:
    """Relê da persistência o estado da conversa DESTE update, antes de despachar.

    No webhook (Cloud Run) a instância é efêmera e o PTB só carrega `conversations` da
    persistência UMA vez, no `initialize()`. Então o passo do wizard pode rodar numa instância e
    o clique do botão cair em OUTRA que nunca viu essa conversa: sem isto, o callback vai pro
    catch-all órfão ("sessão expirou") mesmo com o estado salvo no Firestore — /excluir_post e
    todo botão inline "não funcionam" de forma intermitente. Aqui a gente relê, por
    ConversationHandler persistente, só a chave deste update. No polling (persistence=None) é
    no-op — o estado em memória do processo único já basta.

    Acopla a internals do PTB (`_get_key`/`_conversations`) de propósito: é o único jeito de
    reidratar o roteamento da conversa por update sem reconstruir a Application inteira.
    """
    persistence = app.persistence
    if persistence is None:
        return
    for group in app.handlers.values():
        for h in group:
            if not (isinstance(h, ConversationHandler) and h.persistent and h.name):
                continue
            try:
                key = h._get_key(update)  # noqa: SLF001
            except Exception:  # noqa: BLE001 - update sem chat/user: nada a reidratar
                continue
            try:
                stored = await persistence.get_conversations(h.name)
            except Exception:  # noqa: BLE001 - Firestore fora do ar: segue com o que há em memória
                log.warning("não consegui reler conversas de %s", h.name, exc_info=True)
                continue
            conv = h._conversations  # noqa: SLF001
            if key in stored:
                setter = getattr(conv, "update_no_track", conv.update)
                setter({key: stored[key]})
            elif key in conv:
                conv.pop(key, None)  # encerrada noutra instância: não aja sobre estado morto


# ── registro ─────────────────────────────────────────────────────────────────
def register(app: Application) -> None:
    fallbacks = [CommandHandler("cancelar", cmd_cancelar)]
    timeout = Config.CONVERSATION_TIMEOUT_S

    # Porta de acesso por senha — PRIMEIRO no grupo 0, antes de tudo (não é @restricted; é o
    # próprio portão). Casa só com `/senha ...` ou com a senha PURA (regex exato, case-insensitive)
    # p/ não sequestrar texto de wizard. Para com ApplicationHandlerStop, então não vaza adiante.
    if Config.ACCESS_PASSWORD:
        app.add_handler(CommandHandler("senha", cmd_senha))
        app.add_handler(MessageHandler(
            filters.Regex(re.compile(rf"^\s*{re.escape(Config.ACCESS_PASSWORD)}\s*$", re.I)),
            cmd_senha,
        ))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ajuda", cmd_ajuda))
    app.add_handler(CommandHandler("posts", cmd_posts))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("anuncio", anuncio_start)],
        states={
            A_PHOTOS: [CommandHandler("pronto", anuncio_pronto),
                      MessageHandler(filters.PHOTO | filters.Document.IMAGE, anuncio_photo)],
            A_CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, anuncio_caption)],
            A_COLLAB: [CommandHandler("pular", anuncio_collab),
                      MessageHandler(filters.TEXT & ~filters.COMMAND, anuncio_collab)],
            A_TAGGED: [CommandHandler("pular", anuncio_tagged),
                      MessageHandler(filters.TEXT & ~filters.COMMAND, anuncio_tagged)],
            A_LOCATION: [CommandHandler("pular", anuncio_location),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, anuncio_location)],
            A_CONFIRM: [CallbackQueryHandler(anuncio_confirm, pattern=r"^anuncio:")],
        },
        # allow_reentry: re-digitar /anuncio reinicia o wizard mesmo no meio de uma conversa —
        # senão, preso num passo (ex.: botão que não responde), o comando seria ignorado.
        fallbacks=fallbacks, conversation_timeout=timeout, name="anuncio", persistent=_PERSIST,
        allow_reentry=True,
    ))

    # Features do amora (passeio, subir_midia, exclusões do mapa/censo) — atrás da flag
    # AMORA_ENABLED. Default OFF: o bot expõe só o fluxo do Instagram (sabiá).
    if Config.AMORA_ENABLED:
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler("passeio", passeio_start)],
            states={
                P_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, passeio_title)],
                P_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, passeio_date)],
                P_SERIES: [CommandHandler("pular", passeio_series),
                          MessageHandler(filters.TEXT & ~filters.COMMAND, passeio_series)],
                P_ROUTE: [CommandHandler("pular", passeio_route),
                         MessageHandler(filters.TEXT & ~filters.COMMAND, passeio_route)],
                P_DESC: [CommandHandler("pular", passeio_desc),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, passeio_desc)],
                P_ENERGY: [CommandHandler("pular", passeio_energy),
                          MessageHandler(filters.TEXT & ~filters.COMMAND, passeio_energy)],
                P_INSTA: [CallbackQueryHandler(passeio_insta_choice, pattern=r"^insta:")],
                P_INSTA_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, passeio_insta_url)],
                P_INSTA_IMG: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, passeio_insta_img)],
            },
            fallbacks=fallbacks, conversation_timeout=timeout, name="passeio", persistent=_PERSIST,
            allow_reentry=True,
        ))

        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler("subir_midia", midia_start)],
            states={
                M_COLLECT: [CommandHandler("pronto", midia_pronto),
                           MessageHandler(filters.Document.ALL | filters.VIDEO | filters.PHOTO, midia_collect)],
                M_TOUR: [CommandHandler("pular", midia_tour),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, midia_tour)],
            },
            fallbacks=fallbacks, conversation_timeout=timeout, name="subir_midia", persistent=_PERSIST,
            allow_reentry=True,
        ))

    # Exclusões: /excluir_post (sabiá) sempre; foto/vídeo/passeio (amora) só com a flag.
    delete_kinds = [("post", "excluir_post", re.compile(r"^.+$"))]
    if Config.AMORA_ENABLED:
        delete_kinds = [("foto", "excluir_foto", _PHASH_RE),
                        ("video", "excluir_video", _PHASH_RE),
                        ("passeio", "excluir_passeio", _TOURID_RE)] + delete_kinds
    for kind, label, rx in delete_kinds:
        states = {D_CONFIRM: [CallbackQueryHandler(delete_confirm, pattern=r"^del:")]}
        if kind == "post":
            # Estado do picker: escolher um post (del:pick:) ou cancelar (del:no).
            states[D_PICK] = [CallbackQueryHandler(delete_pick, pattern=r"^del:pick:"),
                              CallbackQueryHandler(delete_confirm, pattern=r"^del:no$")]
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler(label, _make_delete_cmd(kind, rx, label))],
            states=states,
            fallbacks=fallbacks, conversation_timeout=300, name=f"del_{kind}", persistent=_PERSIST,
            allow_reentry=True,
        ))

    # Por ÚLTIMO no grupo 0: redes de segurança. Dentro do grupo só o 1º handler que casa roda,
    # então as conversas acima têm prioridade; aqui só cai o que ninguém tratou:
    #  - órfão de callback (botão sem conversa) → responde e limpa o "Loading…";
    #  - mensagem desconhecida → orienta o usuário a mandar /start.
    # Não registre nada depois destes dois.
    app.add_handler(CallbackQueryHandler(orphan_callback))
    app.add_handler(MessageHandler(filters.ALL, fallback_unknown))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("erro não tratado processando update", exc_info=context.error)


def build_application() -> Application:
    """Monta a Application do PTB com handlers + persistência (Firestore no Cloud Run)."""
    builder = ApplicationBuilder().token(Config.TELEGRAM_BOT_TOKEN)
    persistence = make_persistence()
    if persistence is not None:
        builder = builder.persistence(persistence)
    app = builder.build()
    register(app)
    app.add_error_handler(on_error)
    return app
