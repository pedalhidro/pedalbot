"""Executor dos jobs lentos (rodado pelo worker no Cloud Run, ou inline no polling).

Um job é um dict JSON-serializável: `{action, chat_id, request_id, payload}`. Os arquivos
trafegam como `file_id` do Telegram (≤20 MB, rebaixáveis via getFile) — nada de bytes no
payload, então um retry do Cloud Tasks só rebaixa de novo, sem perder nada.

Idempotência: antes de publicar no Instagram (passo NÃO idempotente do sabiá) reivindicamos um
marcador; um retry encontra o marcador e não republica.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

import httpx

from . import media, ttl
from .clients import Amora, BackendError, Sabia
from .persistence import Store

log = logging.getLogger("pedalbot.jobs")
_store = Store()


# ── helpers de download ──────────────────────────────────────────────────────
async def _download_bytes(bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)  # estoura se >20 MB na cloud Bot API
    return bytes(await f.download_as_bytearray())


async def _download_to(bot, file_id: str, path: str) -> None:
    f = await bot.get_file(file_id)
    await f.download_to_drive(path)


async def _reply(bot, chat_id: int, text: str) -> None:
    await bot.send_message(chat_id=chat_id, text=text[:3900])


# ── dispatch ─────────────────────────────────────────────────────────────────
async def execute_job(bot, job: dict) -> None:
    # Política: erros são reportados ao usuário e o job termina OK (sem reentrega do Cloud
    # Tasks). É deliberado — o publish do sabiá NÃO é idempotente, então reentrega automática
    # arriscaria post duplicado. Para falha transitória, o usuário re-roda o comando (o marcador
    # de publicação evita duplicar se o post já tiver saído).
    action = job.get("action")
    chat_id = job["chat_id"]
    log.info("executando job action=%s chat=%s", action, chat_id)
    try:
        if action == "anuncio":
            await _do_anuncio(bot, job)
        elif action == "passeio":
            await _do_passeio(bot, job)
        elif action == "subir_midia":
            await _do_subir_midia(bot, job)
        else:
            await _reply(bot, chat_id, f"Ação desconhecida: {action}")
        log.info("job action=%s concluído", action)
    except BackendError as exc:
        log.warning("backend error em %s: %s | details=%s", action, exc, exc.details)
        if exc.details:
            await _reply(bot, chat_id, "⚠️ Erros de validação:\n" + "\n".join("• " + d for d in exc.details))
        else:
            await _reply(bot, chat_id, f"❌ Erro do backend ({exc.status}): {exc.payload.get('error') or exc}")
    except media.NoGPSError as exc:
        log.info("mídia sem GPS em %s: %s", action, exc)
        await _reply(bot, chat_id, f"❌ {exc}")
    except httpx.RequestError as exc:
        # Backend inacessível (recusou conexão, DNS, timeout de conexão…) — diferente de
        # BackendError, que é o backend RESPONDENDO com erro. Diz qual URL falhou pra não
        # confundir com bug do bot (ex.: sabiá/amora fora do ar, ou *_BASE_URL apontando p/ um
        # localhost morto — config de teste deliberada).
        url = ""
        try:
            url = str(exc.request.url)
        except Exception:  # noqa: BLE001 - request pode não estar setado
            pass
        log.warning("backend inacessível em %s (action=%s): %s", url or "?", action, exc)
        await _reply(bot, chat_id,
                     f"❌ Backend inacessível{f' em {url}' if url else ''} — o serviço (sabiá/amora) "
                     "está no ar? Confira SABIA_BASE_URL / AMORA_BASE_URL no .env.")
    except Exception as exc:  # noqa: BLE001
        log.exception("job action=%s FALHOU", action)
        await _reply(bot, chat_id, f"❌ Falhou: {type(exc).__name__}: {exc}")


# ── /anuncio → sabiá ─────────────────────────────────────────────────────────
async def _do_anuncio(bot, job: dict) -> dict:
    chat_id = job["chat_id"]
    p = job["payload"]
    request_id = job["request_id"]

    if not await _store.claim_publish(request_id):
        prev = await _store.publish_result(request_id)
        await _reply(bot, chat_id, "Esse anúncio já foi processado (ignorando duplicata)."
                     + (f"\n{prev.get('permalink')}" if prev else ""))
        return prev or {}

    images = []
    for i, fid in enumerate(p["image_file_ids"], start=1):
        data = await _download_bytes(bot, fid)
        images.append((f"img-{i}.jpg", data, "image/jpeg"))

    sabia = Sabia()
    result = await asyncio.to_thread(
        sabia.publish, images, p.get("caption", ""),
        collaborators=p.get("collaborators", ""), tagged=p.get("tagged", ""),
        location_name=p.get("location_name", ""), location_id=p.get("location_id", ""),
        location_url=p.get("location_url", ""),
        is_posted=p.get("is_posted", True), confirm=p.get("confirm", False),
    )
    ig = result.get("instagram", {})
    await _store.complete_publish(request_id, {"permalink": ig.get("permalink")})
    if ig.get("dry_run"):
        await _reply(bot, chat_id, "📝 (DRY-RUN) anúncio registrado — sem publicação real.")
    elif p.get("is_posted", True):
        await _reply(bot, chat_id, f"✅ Publicado no Instagram:\n{ig.get('permalink')}")
    else:
        await _reply(bot, chat_id, "📝 Rascunho salvo (não publicado).")
    await _maybe_warn(bot, chat_id, result.get("validation"))
    return ig


async def _maybe_warn(bot, chat_id, validation) -> None:
    viols = (validation or {}).get("violations") or []
    if viols:
        await _reply(bot, chat_id, "⚠️ Avisos de validação:\n" + "\n".join("• " + v for v in viols))


# ── /passeio → amora (+ Instagram opcional) ──────────────────────────────────
async def _do_passeio(bot, job: dict) -> None:
    chat_id = job["chat_id"]
    p = job["payload"]
    amora = Amora()

    tour_input = ttl.TourInput(**p["tour"])
    doc = ttl.build_tour_ttl(tour_input)

    announcement = None
    if p.get("announcement_file_id"):
        data = await _download_bytes(bot, p["announcement_file_id"])
        announcement = ("announcement.jpg", data, "image/jpeg")

    result = await asyncio.to_thread(amora.upload_tour, doc, mode="replace", announcement=announcement)
    tid = result.get("tour_id")
    route = result.get("route", {})
    msg = f"✅ Passeio criado: tour_{tid}"
    if route.get("status") == "ok":
        msg += f" · rota sincronizada ({route.get('points')} pts)"
    elif route.get("status") in ("fetch_failed", "error", "stale"):
        msg += " · rota privada/indisponível — passeio criado sem mapa"
    await _reply(bot, chat_id, msg)

    # Instagram: publicar agora e linkar (patch) — opcional
    ig = p.get("instagram") or {}
    if ig.get("mode") == "publish" and announcement is not None:
        request_id = job["request_id"] + ":ig"
        if await _store.claim_publish(request_id):
            sabia = Sabia()
            pub = await asyncio.to_thread(
                sabia.publish, [announcement], ig.get("caption") or tour_input.description or tour_input.title,
                is_posted=True, confirm=True,
            )
            permalink = pub.get("instagram", {}).get("permalink")
            await _store.complete_publish(request_id, {"permalink": permalink})
            if permalink and not pub.get("instagram", {}).get("dry_run"):
                patch = ttl.build_tour_ttl(ttl.TourInput(
                    tour_id=tid, title=tour_input.title, date_iso=tour_input.date_iso,
                    instagram_url=permalink,
                ))
                await asyncio.to_thread(amora.upload_tour, patch, mode="patch")
                await _reply(bot, chat_id, f"✅ Instagram publicado e vinculado:\n{permalink}")
            else:
                await _reply(bot, chat_id, "📝 Anúncio em rascunho/dry-run — passeio fica sem link do Instagram.")


# ── /subir-midia → amora (foto e/ou vídeo, roteado por tipo) ─────────────────
async def _do_subir_midia(bot, job: dict) -> None:
    chat_id = job["chat_id"]
    p = job["payload"]
    amora = Amora()
    tour_id = p.get("tour_id")
    ok, skipped = 0, []

    for item in p["items"]:
        fid, kind, name = item["file_id"], item["kind"], item.get("filename", "")
        try:
            if kind == "photo":
                await _upload_one_photo(bot, amora, fid, tour_id)
            elif kind == "video":
                await _upload_one_video(bot, amora, fid, tour_id)
            else:
                skipped.append(f"{name}: tipo não suportado")
                continue
            ok += 1
        except media.NoGPSError:
            skipped.append(f"{name or kind}: sem GPS")
        except BackendError as exc:
            skipped.append(f"{name or kind}: {('; '.join(exc.details) or exc.payload.get('error') or exc)}")

    msg = f"✅ {ok} mídia(s) enviada(s)."
    if skipped:
        msg += "\n⏭️ Puladas:\n" + "\n".join("• " + s for s in skipped)
    await _reply(bot, chat_id, msg)


async def _upload_one_photo(bot, amora: Amora, file_id: str, tour_id) -> None:
    data = await _download_bytes(bot, file_id)
    img = await asyncio.to_thread(media.open_image, data)
    meta = await asyncio.to_thread(media.image_meta, data, img)  # NoGPSError se faltar GPS
    phash = await asyncio.to_thread(media.phash_image, img)
    variants = await asyncio.to_thread(media.image_variants, data, img)
    doc = ttl.build_image_ttl(
        phash=phash, date_iso=meta.get("date_iso", "2026-01-01T00:00:00-03:00"),
        lat=meta["lat"], lon=meta["lon"], bearing=meta.get("bearing"), focal=meta.get("focal"),
        tour_id=tour_id,
    )
    await asyncio.to_thread(
        amora.upload_image, doc,
        original=(f"{phash}.jpg", variants["original"], "image/jpeg"),
        large=(f"{phash}.large.jpg", variants["large"], "image/jpeg"),
        thumb=(f"{phash}.thumb.jpg", variants["thumb"], "image/jpeg"),
    )


async def _upload_one_video(bot, amora: Amora, file_id: str, tour_id) -> None:
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src")
        await _download_to(bot, file_id, src)
        meta = await asyncio.to_thread(media.video_meta, src)  # NoGPSError se faltar GPS
        frames = await asyncio.to_thread(media.video_sample_frames, src, 8)
        vhash = await asyncio.to_thread(media.vhash_frames, frames)
        out = await asyncio.to_thread(media.video_transcode, src, td, vhash, True)
        files = out["files"]

        doc = ttl.build_video_ttl(
            vhash=vhash, date_iso=meta.get("date_iso", "2026-01-01T00:00:00-03:00"),
            lat=meta["lat"], lon=meta["lon"], duration_s=meta["duration_s"],
            resolutions=out["resolutions"], audio_path=f"{vhash}.audio.webm",
            thumb_path=f"{vhash}.thumb.jpg" if "thumb" in files else None,
            video360p=f"{vhash}.360p.webm" if "video360p" in files else None,
            video720p=f"{vhash}.720p.webm" if "video720p" in files else None,
            tour_id=tour_id,
        )

        # leitura dos arquivos + upload TODOS na thread (não bloquear o event loop com I/O)
        def _read_and_upload() -> None:
            def part(path, ct):
                with open(path, "rb") as fh:
                    return (os.path.basename(path), fh.read(), ct)

            amora.upload_video(
                doc, vhash,
                audio=part(files["audio"], "audio/webm"),
                video360=part(files["video360p"], "video/webm") if "video360p" in files else None,
                video720=part(files["video720p"], "video/webm") if "video720p" in files else None,
                thumb=part(files["thumb"], "image/jpeg") if "thumb" in files else None,
            )

        await asyncio.to_thread(_read_and_upload)
