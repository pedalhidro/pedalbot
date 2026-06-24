"""Clientes HTTP dos dois backends (sabiá + amora).

O bot **não importa** o código dos backends — fala HTTP com ambos, tratados como confiáveis
(amora sem auth; sabiá com HTTP Basic via APP_PASSWORD). Clientes síncronos (httpx.Client);
os chamadores (worker/polling) rodam isto em `asyncio.to_thread` para não travar o event loop.

Tipo de parte de arquivo em todo o módulo: `FilePart = (filename, bytes, content_type)`.
"""
from __future__ import annotations

from typing import Optional

import httpx

from .config import Config

FilePart = "tuple[str, bytes, str]"


class BackendError(RuntimeError):
    """Erro de um backend. `.details` carrega a lista de violações SHACL quando houver."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload or {}
        self.details: list[str] = list(self.payload.get("details") or [])
        super().__init__(f"HTTP {status}: {self.payload.get('error') or self.payload}")


def _body(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        # Resposta não-JSON: tipicamente uma página de erro de gateway (Cloudflare 5xx) que
        # esconde o corpo real do backend. Resume em vez de despejar o HTML inteiro pro usuário.
        ct = (resp.headers.get("content-type") or "").lower()
        text = resp.text or ""
        if "html" in ct or text.lstrip()[:1] == "<":
            return {"error": f"backend indisponível (HTTP {resp.status_code}; resposta de gateway, "
                             "não-JSON) — instabilidade ou credencial do backend"}
        return {"text": text[:600]}


def _check(resp: httpx.Response) -> dict:
    body = _body(resp)
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False):
        raise BackendError(resp.status_code, body if isinstance(body, dict) else {"text": str(body)})
    return body


# ── sabiá (Instagram) ────────────────────────────────────────────────────────
class Sabia:
    def __init__(self) -> None:
        self.base = Config.SABIA_BASE_URL
        self.auth = ("phidro", Config.SABIA_APP_PASSWORD) if Config.SABIA_APP_PASSWORD else None

    def publish(
        self,
        images: "list[FilePart]",
        caption: str,
        *,
        collaborators: str = "",
        tagged: str = "",
        location_name: str = "",
        location_id: str = "",
        location_url: str = "",
        is_posted: bool = True,
        confirm: bool = False,
    ) -> dict:
        files = [("images", (fn, data, ct)) for (fn, data, ct) in images]
        form = {
            "caption": caption,
            "collaborators": collaborators,
            "tagged": tagged,
            "location_name": location_name,
            "location_id": location_id,
            "location_url": location_url,
            "is_posted": "true" if is_posted else "false",
            "confirm": "true" if confirm else "false",
        }
        with httpx.Client(timeout=180.0) as c:
            r = c.post(f"{self.base}/api/publish", data=form, files=files, auth=self.auth)
        return _check(r)

    def list_posts(self) -> list:
        with httpx.Client(timeout=60.0) as c:
            r = c.get(f"{self.base}/api/posts", auth=self.auth)
        r.raise_for_status()
        return r.json()

    def delete_post(self, shortcode: str) -> dict:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{self.base}/api/posts/delete", data={"shortcode": shortcode}, auth=self.auth)
        return _check(r)


# ── amora (mapa + censo) ─────────────────────────────────────────────────────
class Amora:
    def __init__(self) -> None:
        self.base = Config.AMORA_BASE_URL

    def fetch_tours_ttl(self) -> str:
        """tours.ttl — usado pelo índice de pessoas/séries e p/ achar o próximo tour_id."""
        with httpx.Client(timeout=60.0) as c:
            r = c.get(f"{self.base}/data/tours.ttl")
        r.raise_for_status()
        return r.text

    def upload_tour(
        self,
        ttl: str,
        *,
        mode: str = "replace",
        remove: Optional[str] = None,
        announcement: "Optional[FilePart]" = None,
    ) -> dict:
        data = {"ttl": ttl, "mode": mode}
        if remove:
            data["remove"] = remove
        files = [("announcement", announcement)] if announcement else None
        with httpx.Client(timeout=120.0) as c:
            r = c.post(f"{self.base}/upload-tour", data=data, files=files)
        return _check(r)

    def delete_tour(self, tour_id: str) -> dict:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{self.base}/delete-tour/{tour_id}")
        return _check(r)

    def upload_image(
        self,
        ttl: str,
        *,
        original: "FilePart",
        large: "Optional[FilePart]" = None,
        thumb: "Optional[FilePart]" = None,
    ) -> dict:
        files = [("original", original)]
        if large:
            files.append(("large", large))
        if thumb:
            files.append(("thumb", thumb))
        with httpx.Client(timeout=120.0) as c:
            r = c.post(f"{self.base}/upload-image", data={"ttl": ttl}, files=files)
        return _check(r)

    def upload_video(
        self,
        ttl: str,
        vid_id: str,
        *,
        audio: "FilePart",
        video360: "Optional[FilePart]" = None,
        video720: "Optional[FilePart]" = None,
        thumb: "Optional[FilePart]" = None,
    ) -> dict:
        files = [("audio", audio)]
        if video360:
            files.append(("video360", video360))
        if video720:
            files.append(("video720", video720))
        if thumb:
            files.append(("thumb", thumb))
        with httpx.Client(timeout=120.0) as c:
            r = c.post(f"{self.base}/upload-video", data={"id": vid_id, "ttl": ttl}, files=files)
        return _check(r)

    def delete_image(self, phash: str) -> dict:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{self.base}/delete-image/{phash}")
        return _check(r)

    def delete_video(self, vhash: str) -> dict:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{self.base}/delete-video/{vhash}")
        return _check(r)
