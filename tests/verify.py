"""Verificação offline do núcleo do pedalbot (sem creds, sem rede aos backends).

Roda com o venv:  .venv/bin/python tests/verify.py
Cobre: py_compile · pHash/vHash · EXIF real (exiftool) · transcode real (ffmpeg) ·
TTL → rdflib → pyshacl contra os shapes REAIS do amora (sem sh:Violation).
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AMORA_DATA = Path("/Users/danlessa/repos/pedalhidro/amora/web/data")
SHAPES = AMORA_DATA / "shapes.ttl"
ONTOLOGY = AMORA_DATA / "ontology.ttl"

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
_failures = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _failures
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        _failures += 1


# ── 1. py_compile ────────────────────────────────────────────────────────────
def test_compile() -> None:
    print("py_compile:")
    mods = sorted((ROOT / "bot").glob("*.py"))
    cp = subprocess.run([sys.executable, "-m", "py_compile", *map(str, mods)], capture_output=True)
    check("all bot/*.py compile", cp.returncode == 0, cp.stderr.decode()[:300])


# ── 2. pHash / vHash ─────────────────────────────────────────────────────────
def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def test_hashing() -> None:
    from PIL import Image
    from bot import media

    print("pHash / vHash:")
    grad = Image.new("RGB", (200, 160))
    px = grad.load()
    for y in range(160):
        for x in range(200):
            px[x, y] = (x % 256, y % 256, (x + y) % 256)

    h1 = media.phash_image(grad)
    h2 = media.phash_image(grad.copy())
    check("pHash determinístico", h1 == h2)
    check("pHash é 16 hex", len(h1) == 16 and all(c in "0123456789abcdef" for c in h1), h1)

    # leve perturbação (re-encode JPEG q90) → Hamming pequeno (dedup do amora usa limiar 5)
    buf = io.BytesIO()
    grad.convert("RGB").save(buf, format="JPEG", quality=90)
    h3 = media.phash_bytes(buf.getvalue())
    check("pHash estável sob re-encode (Hamming ≤5)", _hamming(h1, h3) <= 5, f"Hamming={_hamming(h1, h3)}")

    frames = [grad.convert("L"), grad.transpose(Image.Transpose.FLIP_LEFT_RIGHT).convert("L")] * 4
    vh = media.vhash_frames(frames)
    check("vHash é 16 hex", len(vh) == 16 and all(c in "0123456789abcdef" for c in vh), vh)


# ── 3. EXIF real (exiftool injeta GPS num JPEG) ──────────────────────────────
def test_exif() -> None:
    from PIL import Image
    from bot import media

    print("EXIF / GPS (foto real via exiftool):")
    check("_dms_to_deg S/W negativa", abs(media._dms_to_deg((23, 33, 0), "S") - (-23.55)) < 1e-6)
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.jpg")
        Image.new("RGB", (64, 48), (120, 90, 60)).save(p, "JPEG")
        cp = subprocess.run(
            ["exiftool", "-overwrite_original",
             "-GPSLatitude=23.55", "-GPSLatitudeRef=S",
             "-GPSLongitude=46.63", "-GPSLongitudeRef=W",
             "-DateTimeOriginal=2026:06:11 20:00:00",
             "-FocalLengthIn35mmFilm=28", p],
            capture_output=True,
        )
        if cp.returncode != 0:
            check("exiftool injetou EXIF", False, cp.stderr.decode()[:200])
            return
        meta = media.image_meta(Path(p).read_bytes())
        check("lat ≈ -23.55", abs(meta.get("lat", 0) + 23.55) < 1e-3, str(meta.get("lat")))
        check("lon ≈ -46.63", abs(meta.get("lon", 0) + 46.63) < 1e-3, str(meta.get("lon")))
        check("date_iso com TZ", meta.get("date_iso") == "2026-06-11T20:00:00-03:00", str(meta.get("date_iso")))
        # focal é opcional (Warning) e o exiftool não persiste FocalLengthIn35mmFilm neste JPEG
        # sintético; em fotos reais o sub-IFD Exif traz o campo. Informativo, não falha.
        print(f"  [info] focal (opcional, best-effort): {meta.get('focal')}")


# ── 4. Transcode real (ffmpeg) + vHash + parsing de video_meta ───────────────
def _ffmpeg_ok() -> bool:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def test_video() -> None:
    from bot import media

    print("vídeo (transcode ffmpeg + vHash + parsing):")
    if not _ffmpeg_ok():
        print("  [SKIP] ffmpeg não roda nesta máquina (ex.: dylib do x265 quebrada) — "
              "transcode/vHash não verificáveis aqui. Conserte com: brew reinstall ffmpeg")
    else:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.mp4")
            cp = subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", src],
                capture_output=True,
            )
            if cp.returncode != 0:
                check("ffmpeg gerou clipe de teste", False, cp.stderr.decode()[:200])
            else:
                frames = media.video_sample_frames(src, n=8)
                vh = media.vhash_frames(frames)
                check("vHash do vídeo é 16 hex", len(vh) == 16, vh)
                out = media.video_transcode(src, td, vh, want_hd=False)
                check("transcode gerou áudio", "audio" in out["files"] and os.path.getsize(out["files"]["audio"]) > 0)
                check("transcode gerou 360p", "video360p" in out["files"], str(out["resolutions"]))
                check("resolutions inclui 'audio'+'360p'", set(out["resolutions"]) >= {"audio", "360p"})

    # parsing de video_meta (monkeypatch p/ não depender de GPS no container de teste)
    media._exiftool_json = lambda path: {"Duration": 12.3, "GPSLatitude": -23.55,
                                         "GPSLongitude": -46.63, "CreationDate": "2026:06:11 20:00:00-03:00"}
    media._ffprobe_duration = lambda path: 12.3
    vm = media.video_meta("x.mp4")
    check("video_meta duração", abs(vm["duration_s"] - 12.3) < 1e-6)
    check("video_meta GPS", abs(vm["lat"] + 23.55) < 1e-6 and abs(vm["lon"] + 46.63) < 1e-6)
    check("video_meta data c/ TZ", vm["date_iso"].startswith("2026-06-11T20:00:00"), vm.get("date_iso"))


# ── 5. TTL → rdflib → pyshacl contra os shapes REAIS do amora ────────────────
def _shacl_violations(ttl_text: str) -> "list[str]":
    import rdflib
    from pyshacl import validate as shacl_validate

    data = rdflib.Graph()
    data.parse(data=ttl_text, format="turtle")
    data.parse(str(ONTOLOGY), format="turtle")  # backend mescla ontologia nos dados
    shapes = rdflib.Graph()
    shapes.parse(str(SHAPES), format="turtle")
    shapes.parse(str(ONTOLOGY), format="turtle")
    _, res, _ = shacl_validate(data, shacl_graph=shapes, advanced=True)
    SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
    return [
        str(res.value(r, SH.resultMessage))
        for r in res.subjects(rdflib.RDF.type, SH.ValidationResult)
        if res.value(r, SH.resultSeverity) == SH.Violation
    ]


def test_ttl() -> None:
    import rdflib
    from bot import ttl

    print("TTL builders → rdflib → SHACL (shapes reais do amora):")
    if not SHAPES.exists() or not ONTOLOGY.exists():
        check("shapes/ontology do amora encontrados", False, f"{SHAPES} ausente")
        return

    tour = ttl.build_tour_ttl(ttl.TourInput(
        tour_id="bot-teste-1", title="Pedal de teste do bot",
        date_iso="2026-06-11T20:00:00-03:00",
        series_code="PH", series_seq=999, series_is_new=True,
        route_url="https://ridewithgps.com/routes/55785987",
        instagram_url="https://www.instagram.com/p/ABC123/",
        description="Narrativa de teste.\nDuas linhas.",
        count_attendee=14, count_newcomer=3, energy_kj=220.5, intensity="De boa",
        departed_iso="2026-06-11T20:05:00-03:00", arrived_iso="2026-06-11T23:40:00-03:00",
        moving_duration="PT2H35M", measured_kj=195,
        author_slugs=["pessoaTesteBot"], attendee_slugs=["pessoaTesteBot"],
        new_people={"pessoaTesteBot": "Teste Bot"},
    ))
    img = ttl.build_image_ttl(phash="a1b2c3d4e5f6a7b8", date_iso="2026-06-11T20:10:00-03:00",
                              lat=-23.55, lon=-46.63, bearing=180.0, focal=28.0,
                              tour_id="bot-teste-1")
    vid = ttl.build_video_ttl(vhash="0c1c8a5190f8b219", date_iso="2026-06-11T20:12:00-03:00",
                              lat=-23.55, lon=-46.63, duration_s=12.34,
                              resolutions=["audio", "360p", "720p"],
                              audio_path="0c1c8a5190f8b219.audio.webm",
                              thumb_path="0c1c8a5190f8b219.thumb.jpg",
                              video360p="0c1c8a5190f8b219.360p.webm",
                              video720p="0c1c8a5190f8b219.720p.webm",
                              tour_id="bot-teste-1")
    for name, doc in (("tour", tour), ("image", img), ("video", vid)):
        try:
            rdflib.Graph().parse(data=doc, format="turtle")
            parses = True
        except Exception as exc:  # noqa: BLE001
            parses, err = False, str(exc)[:200]
        check(f"{name} TTL faz parse (rdflib)", parses, "" if parses else err)
        if parses:
            v = _shacl_violations(doc)
            check(f"{name} TTL sem sh:Violation", not v, "; ".join(v)[:300])


# ── 6. Rede de segurança p/ botões órfãos (sem rede; bot falso) ──────────────
def test_orphan_callback() -> None:
    """Regressão do 'Loading…' infinito: callback sem conversa ativa DEVE ser respondido.

    Os botões inline só são tratados dentro do ConversationHandler (com estado). Se o estado some
    (restart no polling, timeout, troca de instância no Cloud Run), o catch-all no fim do grupo 0
    é a única coisa que responde — senão o Telegram fica em "Loading…" pra sempre.
    """
    import asyncio
    import datetime
    import json
    import warnings

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:DUMMYTESTTOKEN")
    os.environ.setdefault("TELEGRAM_ALLOWED_USERS", "1")
    warnings.simplefilter("ignore")  # silencia o PTBUserWarning de per_message
    from telegram import CallbackQuery, Chat, Message, Update, User
    from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, ConversationHandler,
                              MessageHandler)
    from telegram.request import BaseRequest

    from bot import handlers as H
    from bot.config import Config

    print("callback órfão (rede de segurança p/ 'Loading…' infinito):")

    class FakeRequest(BaseRequest):
        def __init__(self) -> None:
            super().__init__()
            self.api: list[str] = []

        async def initialize(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

        async def do_request(self, url, method, request_data=None, **kw):
            m = url.rsplit("/", 1)[-1]
            p = request_data.parameters if request_data else {}
            self.api.append(m)
            if m == "getMe":
                res: object = {"id": 1, "is_bot": True, "first_name": "b", "username": "phbot"}
            elif m in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
                res = {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}, "text": p.get("text", "")}
            else:
                res = True
            return 200, ('{"ok":true,"result":' + json.dumps(res) + "}").encode()

    async def run() -> None:
        Config.ALLOWED_USERS = frozenset({1})  # o user de teste (id=1) precisa passar no gate
        fr = FakeRequest()
        app = (ApplicationBuilder().token(Config.TELEGRAM_BOT_TOKEN or "123456:DUMMY")
               .request(fr).get_updates_request(FakeRequest()).build())
        H.register(app)
        grp0 = app.handlers[0]
        check("fallback de mensagem é o ÚLTIMO handler do grupo 0",
              isinstance(grp0[-1], MessageHandler) and not isinstance(grp0[-1], ConversationHandler))
        check("catch-all de callback órfão vem logo antes (sem pattern)",
              isinstance(grp0[-2], CallbackQueryHandler) and not isinstance(grp0[-2], ConversationHandler)
              and grp0[-2].pattern is None)
        await app.initialize()
        u, c = User(id=1, first_name="t", is_bot=False), Chat(id=1, type="private")
        msg = Message(message_id=9, date=datetime.datetime.now(datetime.timezone.utc),
                      chat=c, from_user=u, text="x")
        msg.set_bot(app.bot)
        for data in ("anuncio:draft", "anuncio:cancel", "insta:skip", "del:yes"):
            q = CallbackQuery(id="cb", from_user=u, chat_instance="ci", data=data, message=msg)
            q.set_bot(app.bot)
            upd = Update(update_id=1, callback_query=q)
            upd.set_bot(app.bot)
            n = len(fr.api)
            await app.process_update(upd)
            check(f"callback órfão '{data}' é respondido (answerCallbackQuery)",
                  "answerCallbackQuery" in fr.api[n:])

        # mensagem desconhecida (texto sem comando, fora de conversa) → fallback orienta /start
        m2 = Message(message_id=10, date=datetime.datetime.now(datetime.timezone.utc),
                     chat=c, from_user=u, text="qualquer coisa aleatória")
        m2.set_bot(app.bot)
        upd2 = Update(update_id=2, message=m2)
        upd2.set_bot(app.bot)
        n = len(fr.api)
        await app.process_update(upd2)
        check("mensagem desconhecida recebe resposta do fallback (sendMessage)",
              "sendMessage" in fr.api[n:])
        await app.shutdown()

    asyncio.run(run())


# ── 7. Polling inscrito em callback_query (senão botão = "Loading…" pra sempre) ─
def test_polling_subscription() -> None:
    """O Telegram só entrega cliques de botão se o bot estiver inscrito em `callback_query`.

    Em polling, `run_polling` precisa passar `allowed_updates` EXPLÍCITO — omitir mantém a
    assinatura anterior (uma `["message"]` antiga descarta todos os callbacks). Aqui garantimos
    que `bot.polling.main` passa `Update.ALL_TYPES` e que ele cobre `callback_query`.
    """
    import inspect

    from telegram import Update

    from bot import polling

    print("inscrição de updates (polling):")
    check("Update.ALL_TYPES inclui 'callback_query'", "callback_query" in Update.ALL_TYPES)
    src = inspect.getsource(polling.main)
    check("polling.main passa allowed_updates=Update.ALL_TYPES no run_polling",
          "allowed_updates=Update.ALL_TYPES" in src)


# ── helpers p/ os testes de webhook/acesso (offline, sem rede) ────────────────
from telegram.request import BaseRequest as _BaseRequest


class _FakeRequest(_BaseRequest):  # responde getMe/sendMessage/etc. localmente (sem rede)
    def __init__(self) -> None:
        super().__init__()
        self.api: list[str] = []
        self.calls: list[tuple] = []  # (método, parâmetros) p/ inspecionar texto/markup

    async def initialize(self) -> None: ...
    async def shutdown(self) -> None: ...

    async def do_request(self, url, method, request_data=None, **kw):
        import json as _json
        m = url.rsplit("/", 1)[-1]
        p = request_data.parameters if request_data else {}
        self.api.append(m)
        self.calls.append((m, p))
        if m == "getMe":
            res: object = {"id": 1, "is_bot": True, "first_name": "b", "username": "phbot"}
        elif m in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            res = {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}, "text": p.get("text", "")}
        else:
            res = True
        return 200, ('{"ok":true,"result":' + _json.dumps(res) + "}").encode()


def _fake_firestore():
    """Um Firestore em memória (dict de coleções) p/ simular o store COMPARTILHADO entre
    instâncias do Cloud Run."""
    store: dict = {}

    class Snap:
        def __init__(self, cid, d): self.id, self._d, self.exists = cid, d, d is not None
        def to_dict(self): return dict(self._d) if self._d else {}

    class Doc:
        def __init__(self, col, cid): self.col, self.cid = col, cid
        def get(self, *a, **k): return Snap(self.cid, store.get(self.col, {}).get(self.cid))
        def set(self, data, merge=False): store.setdefault(self.col, {})[self.cid] = dict(data)
        def delete(self): store.setdefault(self.col, {}).pop(self.cid, None)

    class Col:
        def __init__(self, col): self.col = col
        def document(self, cid): return Doc(self.col, str(cid))
        def stream(self): return [Snap(c, d) for c, d in store.get(self.col, {}).items()]

    class FS:
        def collection(self, name): return Col(name)

    return FS(), store


def _cmd_msg(bot, mid, text, chat_id=1, user_id=1):
    import datetime
    from telegram import Chat, Message, MessageEntity, User
    ents = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text.split()[0]))] \
        if text.startswith("/") else None
    m = Message(message_id=mid, date=datetime.datetime.now(datetime.timezone.utc),
                chat=Chat(id=chat_id, type="private"),
                from_user=User(id=user_id, first_name="t", is_bot=False), text=text, entities=ents)
    m.set_bot(bot)
    return m


def _build_app(persistence=None):
    from telegram.ext import ApplicationBuilder
    from bot import handlers as H
    req = _FakeRequest()
    b = ApplicationBuilder().token("123456:DUMMY").request(req).get_updates_request(_FakeRequest())
    if persistence is not None:
        b = b.persistence(persistence)
    app = b.build()
    H.register(app)
    return app, req


# ── 8. Webhook: reidratação da conversa entre instâncias (Cloud Run) ──────────
def test_webhook_conversation_reload() -> None:
    """Regressão do 'botão morto' no Cloud Run: o passo do wizard roda numa instância e o clique
    do botão pode cair em OUTRA. O PTB só lê `conversations` da persistência no initialize(),
    então sem `handlers.refresh_conversations()` o callback vira órfão ('sessão expirou') mesmo
    com o estado salvo no Firestore — /excluir_post (e todo botão inline) falhando de forma
    intermitente. Aqui: instância A abre /excluir_post; instância B (que nunca viu o comando)
    conclui a exclusão.
    """
    import asyncio
    import warnings

    warnings.simplefilter("ignore")
    os.environ["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "123456:DUMMY")
    os.environ["AMORA_ENABLED"] = ""

    from telegram import CallbackQuery, Update, User

    from bot import clients as C, handlers as H, persistence as P
    from bot.config import Config

    print("webhook: reidratação de conversa entre instâncias (Cloud Run):")
    # Simula o Cloud Run direto: força a persistência (FirestorePersistence com um Firestore
    # em memória compartilhado) em vez de depender de env — o modo webhook usa exatamente isto.
    Config.ALLOWED_USERS = frozenset({1})
    deleted: list[str] = []
    C.Sabia.list_posts = lambda self: [{"shortcode": "ABC123", "deletable": True}]
    C.Sabia.delete_post = lambda self, sc: (deleted.append(sc), {"ok": True})[1]
    fs, store = _fake_firestore()
    P._fs_client = fs
    _persist_saved = H._PERSIST
    H._PERSIST = True

    async def run() -> None:
        appA, _ = _build_app(P.FirestorePersistence())
        appB, frB = _build_app(P.FirestorePersistence())
        await appA.initialize()
        await appB.initialize()

        updA = Update(update_id=1, message=_cmd_msg(appA.bot, 1, "/excluir_post ABC123"))
        updA.set_bot(appA.bot)
        await H.refresh_conversations(appA, updA)
        await appA.process_update(updA)
        await appA.update_persistence()
        check("A persistiu o estado da conversa no Firestore", bool(store.get("phbot_conv_del_post")))

        q = CallbackQuery(id="cb", from_user=User(id=1, first_name="t", is_bot=False),
                          chat_instance="ci", data="del:yes", message=_cmd_msg(appB.bot, 9, "Confirmar"))
        q.set_bot(appB.bot)
        updB = Update(update_id=2, callback_query=q)
        updB.set_bot(appB.bot)
        n = len(frB.api)
        await H.refresh_conversations(appB, updB)
        await appB.process_update(updB)
        await appB.update_persistence()
        check("B (outra instância) executa a exclusão de fato", deleted == ["ABC123"], f"deleted={deleted}")
        check("B responde editando a msg (não cai no 'sessão expirou')",
              "editMessageText" in frB.api[n:] and "answerCallbackQuery" in frB.api[n:])
        await appA.shutdown()
        await appB.shutdown()

    try:
        asyncio.run(run())
    finally:
        H._PERSIST = _persist_saved
        P._fs_client = None


# ── 9. Acesso por senha (allowlist dinâmica) ──────────────────────────────────
def test_password_access() -> None:
    """Regressão do acesso por senha: quem envia a senha (via `/senha <senha>` OU a senha pura,
    case-insensitive) entra na allowlist dinâmica; senha errada não concede nada."""
    import asyncio
    import warnings

    warnings.simplefilter("ignore")
    os.environ.pop("WORKER_URL", None)
    os.environ.pop("K_SERVICE", None)

    from telegram import Update

    from bot import handlers as H, persistence as P
    from bot.config import Config

    print("acesso por senha (allowlist dinâmica):")
    Config.ALLOWED_USERS = frozenset({1})
    Config.ACCESS_PASSWORD = "biciagua"
    Config.WORKER_URL = ""  # garante modo polling (Store em memória) independente da ordem dos testes
    H._PERSIST = False
    H._store = P.Store()  # store novo em memória (modo polling)

    async def run() -> None:
        app, _ = _build_app()
        await app.initialize()
        check("estranho (fora da allowlist) começa SEM acesso", not await H._has_access(999))

        async def send(mid, text, uid):
            m = _cmd_msg(app.bot, mid, text, chat_id=uid, user_id=uid)
            upd = Update(update_id=mid, message=m)
            upd.set_bot(app.bot)
            await app.process_update(upd)

        await send(1, "/senha errada", 999)
        check("senha errada NÃO concede acesso", not await H._has_access(999))
        await send(2, "/senha biciagua", 999)
        check("/senha correta concede acesso", await H._has_access(999))
        await send(3, "  BICIAGUA ", 888)  # senha pura, com espaços e maiúscula
        check("senha pura (case-insensitive) concede acesso", await H._has_access(888))
        await app.shutdown()

    asyncio.run(run())


# ── 10. /excluir_post sem arg propõe a lista de posts excluíveis ──────────────
def test_delete_post_picker() -> None:
    """/excluir_post SEM shortcode deve propor os posts excluíveis (app-owned) como botões;
    tocar num → confirmar → excluir. Posts não-app-owned (sem `deletable`) não aparecem."""
    import asyncio
    import warnings

    warnings.simplefilter("ignore")
    os.environ.pop("WORKER_URL", None)
    os.environ.pop("K_SERVICE", None)
    os.environ["AMORA_ENABLED"] = ""

    from telegram import CallbackQuery, Update, User

    from bot import clients as C, handlers as H
    from bot.config import Config

    print("/excluir_post sem arg → picker de posts:")
    Config.ALLOWED_USERS = frozenset({1})
    Config.WORKER_URL = ""
    H._PERSIST = False
    deleted: list[str] = []
    C.Sabia.list_posts = lambda self: [
        {"shortcode": "ABC123", "deletable": True, "likes": 5, "comments": 1},
        {"shortcode": "XYZ999", "deletable": True},
        {"shortcode": "OLDPST", "deletable": False},  # não app-owned
    ]
    C.Sabia.delete_post = lambda self, sc: (deleted.append(sc), {"ok": True})[1]

    async def run() -> None:
        app, fr = _build_app()
        await app.initialize()

        upd = Update(update_id=1, message=_cmd_msg(app.bot, 1, "/excluir_post"))
        upd.set_bot(app.bot)
        await app.process_update(upd)
        picker = next((p for (m, p) in fr.calls if m == "sendMessage" and "Qual post" in str(p.get("text", ""))), None)
        markup = str(picker.get("reply_markup")) if picker else ""
        check("propõe o post excluível ABC123 como botão", "del:pick:ABC123" in markup)
        check("não oferece post não-app-owned (OLDPST)", "del:pick:OLDPST" not in markup)

        # toca no XYZ999 → confirma
        cq = CallbackQuery(id="c1", from_user=User(id=1, first_name="t", is_bot=False),
                           chat_instance="ci", data="del:pick:XYZ999", message=_cmd_msg(app.bot, 9, "Qual post excluir?"))
        cq.set_bot(app.bot)
        u2 = Update(update_id=2, callback_query=cq)
        u2.set_bot(app.bot)
        await app.process_update(u2)

        # confirma (del:yes) → exclui
        cq2 = CallbackQuery(id="c2", from_user=User(id=1, first_name="t", is_bot=False),
                            chat_instance="ci", data="del:yes", message=_cmd_msg(app.bot, 9, "Confirmar exclusão de post XYZ999?"))
        cq2.set_bot(app.bot)
        u3 = Update(update_id=3, callback_query=cq2)
        u3.set_bot(app.bot)
        await app.process_update(u3)
        check("escolher no picker + confirmar exclui o post certo", deleted == ["XYZ999"], f"deleted={deleted}")
        await app.shutdown()

    asyncio.run(run())


if __name__ == "__main__":
    test_compile()
    test_hashing()
    test_exif()
    test_video()
    test_ttl()
    test_orphan_callback()
    test_polling_subscription()
    test_webhook_conversation_reload()
    test_password_access()
    test_delete_post_picker()
    print()
    if _failures:
        print(f"\033[31m{_failures} verificação(ões) falharam\033[0m")
        sys.exit(1)
    print("\033[32mTudo verde ✔\033[0m")
