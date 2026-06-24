"""Processamento de mídia que o browser do amora faz client-side e que o bot precisa
reproduzir (o backend do amora não computa nada).

- **pHash (DCT-64)**: 32×32 cinza → DCT 2-D → bloco 8×8 → mediana (sem o DC) → 64 bits → 16 hex.
- **vHash**: 8 quadros uniformes → pHash de cada → voto majoritário por bit.
- **Variantes** de imagem: original / large (≤500 KB) / thumb (≤256 px).
- **EXIF/GPS** de foto; **moov/ISO-6709** de vídeo (via exiftool/ffprobe).
- **Transcode** de vídeo via ffmpeg (vp9+opus 360p/720p, áudio opus, thumbnail).

PARIDADE: os hashes NÃO são bit-idênticos aos do browser (resize/precisão divergem). O amora
deduplica por distância de Hamming (limiar 5), então a meta é MINIMIZAR a divergência (LANCZOS,
float32) e MEDIR — ver o passo de verificação no plano. Aqui implementamos o mesmo algoritmo.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
from typing import Optional

import numpy as np
from PIL import Image, ExifTags

try:  # HEIC do iPhone
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - dependência de sistema opcional
    pass

from .config import Config

_IMG_SIZE = 32
_HASH_SIZE = 8


class MediaError(RuntimeError):
    pass


class NoGPSError(MediaError):
    """Mídia sem GPS — rejeitada antes de qualquer trabalho pesado (política files-only)."""


# ── pHash / vHash ────────────────────────────────────────────────────────────
def _dct_basis(n: int) -> np.ndarray:
    """B[k, i] = cos((2i+1)·k·π / 2N) — DCT-II separável, em float32 (igual ao Float32Array do JS)."""
    i = np.arange(n, dtype=np.float32)
    k = np.arange(n, dtype=np.float32).reshape(-1, 1)
    return np.cos((2.0 * i + 1.0) * k * np.pi / (2.0 * n)).astype(np.float32)


_B = _dct_basis(_IMG_SIZE)


def phash_image(img: Image.Image) -> str:
    """pHash perceptual de 64 bits → 16 hex (row-major, MSB primeiro)."""
    gray = img.convert("L").resize((_IMG_SIZE, _IMG_SIZE), Image.Resampling.LANCZOS)
    a = np.asarray(gray, dtype=np.float32)
    dct = _B @ a @ _B.T  # DCT 2-D separável
    block = dct[:_HASH_SIZE, :_HASH_SIZE]
    flat = block.flatten()
    median = float(np.median(flat[1:]))  # exclui o termo DC (índice 0)
    bits = (flat > median)
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return f"{val:016x}"


def phash_bytes(data: bytes) -> str:
    return phash_image(open_image(data))


def vhash_frames(frames: "list[Image.Image]") -> str:
    """Voto majoritário por bit sobre o pHash de N quadros."""
    if not frames:
        raise MediaError("vHash precisa de ao menos 1 quadro")
    counts = [0] * 64
    n = len(frames)
    for f in frames:
        v = int(phash_image(f), 16)
        for i in range(64):
            if (v >> (63 - i)) & 1:
                counts[i] += 1
    val = 0
    for i in range(64):
        val = (val << 1) | (1 if counts[i] * 2 > n else 0)
    return f"{val:016x}"


# ── Imagem: abrir, variantes, EXIF/GPS ───────────────────────────────────────
def open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception as exc:  # noqa: BLE001
        raise MediaError(f"não consegui abrir a imagem: {exc}") from exc


def _encode_jpeg(img: Image.Image, max_side: int, quality: int, target_kb: Optional[int] = None) -> bytes:
    im = img.convert("RGB")
    w, h = im.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    q = quality
    while True:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, optimize=True)
        out = buf.getvalue()
        if target_kb is None or len(out) <= target_kb * 1024 or q <= 30:
            return out
        q -= 10


def image_variants(data: bytes, img: Optional[Image.Image] = None) -> "dict[str, bytes]":
    """`original` (como veio), `large` (≤500 KB), `thumb` (≤256 px). Espelha o amora."""
    img = img or open_image(data)
    return {
        "original": data,
        "large": _encode_jpeg(img, max_side=2400, quality=85, target_kb=500),
        "thumb": _encode_jpeg(img, max_side=256, quality=75),
    }


def _dms_to_deg(dms, ref: str) -> float:
    d, m, s = (float(x) for x in dms)
    val = d + m / 60.0 + s / 3600.0
    return -val if (ref or "").upper() in ("S", "W") else val


def image_meta(data: bytes, img: Optional[Image.Image] = None) -> dict:
    """Extrai data + GPS (+ rumo/focal) do EXIF. Lança NoGPSError se não houver GPS."""
    img = img or open_image(data)
    exif = img.getexif()
    meta: dict = {}

    # data
    tag = {v: k for k, v in ExifTags.TAGS.items()}
    dto = exif.get(tag.get("DateTimeOriginal")) or exif.get(tag.get("DateTime"))
    # DateTimeOriginal costuma viver no sub-IFD Exif
    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
    except Exception:  # noqa: BLE001
        sub = {}
    dto = sub.get(36867) or sub.get(36868) or dto  # DateTimeOriginal / DateTimeDigitized
    offset = sub.get(36881) or sub.get(36880)       # OffsetTimeOriginal / OffsetTime
    if dto:
        d = str(dto).strip().replace(":", "-", 2).replace(" ", "T")
        meta["date_iso"] = d + (offset if offset else "-03:00")
    focal = sub.get(41989)  # FocalLengthIn35mmFilm
    if focal:
        try:
            meta["focal"] = float(focal)
        except (TypeError, ValueError):
            pass

    # GPS
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:  # noqa: BLE001
        gps = {}
    if gps and gps.get(2) and gps.get(4):
        meta["lat"] = _dms_to_deg(gps[2], gps.get(1, "N"))
        meta["lon"] = _dms_to_deg(gps[4], gps.get(3, "E"))
        if gps.get(17) is not None:  # GPSImgDirection
            try:
                meta["bearing"] = float(gps[17])
            except (TypeError, ValueError):
                pass
    if "lat" not in meta or "lon" not in meta:
        raise NoGPSError("imagem sem GPS no EXIF")
    return meta


# ── Vídeo: probe (exiftool/ffprobe), quadros, transcode (ffmpeg) ─────────────
def _run(cmd: "list[str]", timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def _exiftool_json(path: str) -> dict:
    cp = _run(["exiftool", "-json", "-n", "-api", "QuickTimeUTC=0", path], timeout=60)
    if cp.returncode != 0:
        return {}
    try:
        return (json.loads(cp.stdout.decode("utf-8", "replace")) or [{}])[0]
    except Exception:  # noqa: BLE001
        return {}


def _ffprobe_duration(path: str) -> Optional[float]:
    cp = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", path],
        timeout=60,
    )
    try:
        return float(cp.stdout.decode().strip())
    except (ValueError, AttributeError):
        return None


def video_meta(path: str) -> dict:
    """duration_s + GPS + data de gravação. Lança NoGPSError se faltar GPS."""
    et = _exiftool_json(path)
    meta: dict = {}

    dur = et.get("Duration")
    if isinstance(dur, (int, float)):
        meta["duration_s"] = float(dur)
    else:
        d = _ffprobe_duration(path)
        if d is not None:
            meta["duration_s"] = d
    if not meta.get("duration_s"):
        raise MediaError("não consegui ler a duração do vídeo")

    lat, lon = et.get("GPSLatitude"), et.get("GPSLongitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        meta["lat"], meta["lon"] = float(lat), float(lon)
    else:
        raise NoGPSError("vídeo sem GPS (moov/ISO-6709)")

    # data: prefere a CreationDate da Apple (hora real), com TZ
    raw = et.get("CreationDate") or et.get("CreateDate")
    if raw:
        s = str(raw).strip().replace(":", "-", 2).replace(" ", "T")
        meta["date_iso"] = s if ("+" in s[10:] or "-" in s[10:] or s.endswith("Z")) else s + "-03:00"
    return meta


def video_sample_frames(path: str, n: int = 8) -> "list[Image.Image]":
    """N quadros uniformes (pula início/fim pretos) como imagens 32×32 p/ o vHash."""
    dur = _ffprobe_duration(path) or 0.0
    frames: list[Image.Image] = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(n):
            t = max(0.05, ((i + 0.5) / n) * dur) if dur > 0 else 0.0
            out = os.path.join(td, f"f{i}.png")
            cp = _run(
                ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                 "-vf", f"scale={_IMG_SIZE}:{_IMG_SIZE}", out],
                timeout=60,
            )
            if cp.returncode == 0 and os.path.exists(out):
                with Image.open(out) as im:
                    frames.append(im.convert("L").copy())
    if not frames:
        raise MediaError("não consegui amostrar quadros do vídeo")
    return frames


def video_transcode(path: str, out_dir: str, vhash: str, *, want_hd: bool = True) -> dict:
    """Gera áudio opus + 360p/720p (vp9+opus) + thumbnail. Devolve caminhos + resolutions.

    Nomes seguem o contrato do amora: `<vhash>.audio.webm`, `<vhash>.360p.webm`,
    `<vhash>.720p.webm`, `<vhash>.thumb.jpg` (paths relativos a `clips/`).
    """
    os.makedirs(out_dir, exist_ok=True)
    t = Config.FFMPEG_TIMEOUT_S
    audio = os.path.join(out_dir, f"{vhash}.audio.webm")
    v360 = os.path.join(out_dir, f"{vhash}.360p.webm")
    v720 = os.path.join(out_dir, f"{vhash}.720p.webm")
    thumb = os.path.join(out_dir, f"{vhash}.thumb.jpg")
    dur = _ffprobe_duration(path) or 0.0

    def ff(args: "list[str]", out: str) -> bool:
        cp = _run(["ffmpeg", "-y", *args], timeout=t)
        return cp.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0

    ok_audio = ff(["-i", path, "-vn", "-c:a", "libopus", "-b:a", "128k", audio], audio)
    if not ok_audio:
        raise MediaError("falha ao extrair áudio (ffmpeg)")

    ok_360 = ff(
        ["-i", path, "-vf", "scale=-2:360", "-c:v", "libvpx-vp9", "-b:v", "700k",
         "-c:a", "libopus", "-b:a", "128k", v360],
        v360,
    )
    ok_720 = False
    if want_hd:
        ok_720 = ff(
            ["-i", path, "-vf", "scale=-2:720", "-c:v", "libvpx-vp9", "-b:v", "1600k",
             "-c:a", "libopus", "-b:a", "128k", v720],
            v720,
        )

    ss = max(0.2, 0.05 * dur) if dur > 0 else 0.2
    ff(["-ss", f"{ss:.3f}", "-i", path, "-frames:v", "1", "-vf", "scale=320:-2", thumb], thumb)

    resolutions = ["audio"]
    files = {"audio": audio}
    if ok_360:
        resolutions.append("360p")
        files["video360p"] = v360
    if ok_720:
        resolutions.append("720p")
        files["video720p"] = v720
    if os.path.exists(thumb):
        files["thumb"] = thumb
    return {"resolutions": resolutions, "files": files}
