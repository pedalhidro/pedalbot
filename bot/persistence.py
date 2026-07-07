"""Estado durável (Firestore) + marcadores de idempotência.

No Cloud Run o bot escala a zero e pode trocar de instância no meio de um wizard, então o
estado da conversa NÃO pode ficar em memória. Aqui:

- `Store` — dedup de `update_id` (retries do Telegram) e marcador de publicação
  (idempotência contra retry do Cloud Tasks que, de outro modo, dobraria post no Instagram).
  Usa Firestore quando há projeto/worker configurado; senão um dict em memória (polling/dev).
- `make_persistence()` — devolve uma `BasePersistence` do PTB p/ o ConversationHandler.
  Firestore no Cloud Run; `None` (memória do PTB) no modo polling.

OBS: `google-cloud-firestore` é síncrono — as chamadas rodam em `asyncio.to_thread`.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .config import Config

_fs_client = None


def _firestore():
    global _fs_client
    if _fs_client is None:
        from google.cloud import firestore  # import tardio: só no Cloud Run

        _fs_client = firestore.Client(project=Config.GCP_PROJECT)
    return _fs_client


def _col(name: str) -> str:
    return f"{Config.FIRESTORE_PREFIX}{name}"


# Uma reivindicação "in_progress" mais velha que isto é considerada órfã (a tentativa anterior
# morreu) e pode ser re-reivindicada — senão um crash entre claim e complete travaria pra sempre
# uma republicação legítima. Folga generosa sobre o publish (~80 s).
_CLAIM_STALE_S = 600
_FS_READ_TIMEOUT = 5
_FS_WRITE_TIMEOUT = 10


async def _to_thread(fn, *args, timeout: float):
    """to_thread com timeout — evita saturar o pool se o Firestore travar."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)


class Store:
    """Marcadores de idempotência. Firestore quando configurado, senão memória.

    Dedup de update_id é **marcado só após sucesso** (no `webhook`), então um crash no meio do
    processamento NÃO descarta o retry do Telegram. A publicação usa um marcador com staleness
    p/ não travar re-tentativas legítimas.
    """

    def __init__(self) -> None:
        self._use_fs = Config.using_cloud_run()
        self._mem: dict[str, dict[str, Any]] = {}

    # ── dedup de update_id (marcar DEPOIS do sucesso) ────────────────────────
    async def is_processed(self, update_id: int) -> bool:
        key = str(update_id)
        if not self._use_fs:
            return key in self._mem.get("updates", {})
        snap = await _to_thread(lambda: _firestore().collection(_col("updates")).document(key).get(),
                                timeout=_FS_READ_TIMEOUT)
        return snap.exists

    async def mark_processed(self, update_id: int) -> None:
        key = str(update_id)
        if not self._use_fs:
            self._mem.setdefault("updates", {})[key] = time.time()
            return
        await _to_thread(
            lambda: _firestore().collection(_col("updates")).document(key).set(
                {"ts": time.time(), "expire_at": time.time() + Config.UPDATE_DEDUP_TTL_S}),
            timeout=_FS_WRITE_TIMEOUT,
        )

    # ── marcador de publicação (idempotência do passo lento) ─────────────────
    async def claim_publish(self, request_id: str) -> bool:
        """True se for a 1ª vez OU a anterior morreu (stale); False se concluída/em-andamento-recente."""
        if not self._use_fs:
            prev = self._mem.get("publish", {}).get(request_id)
            if prev and (prev.get("status") == "done" or time.time() - prev.get("ts", 0) < _CLAIM_STALE_S):
                return False
            self._mem.setdefault("publish", {})[request_id] = {"ts": time.time(), "status": "in_progress"}
            return True
        return await _to_thread(self._fs_claim, "publish", request_id, timeout=_FS_WRITE_TIMEOUT)

    async def complete_publish(self, request_id: str, result: dict) -> None:
        if not self._use_fs:
            self._mem.setdefault("publish", {})[request_id] = {"ts": time.time(), "status": "done", "result": result}
            return
        await _to_thread(self._fs_complete, "publish", request_id, result, timeout=_FS_WRITE_TIMEOUT)

    async def publish_result(self, request_id: str) -> Optional[dict]:
        if not self._use_fs:
            return (self._mem.get("publish", {}).get(request_id) or {}).get("result")
        return await _to_thread(self._fs_get_result, "publish", request_id, timeout=_FS_READ_TIMEOUT)

    # ── acesso por senha (grant em runtime, persistido) ──────────────────────
    # Quem acerta a senha entra na allowlist "dinâmica". Precisa durar entre instâncias/reinícios
    # (Cloud Run troca de instância; polling reinicia), então vai no Firestore quando há. Sem
    # expire_at: o acesso é permanente até remoção manual.
    async def is_granted(self, user_id: "int | None") -> bool:
        if user_id is None:
            return False
        key = str(user_id)
        if not self._use_fs:
            return key in self._mem.get("access", {})
        snap = await _to_thread(lambda: _firestore().collection(_col("access")).document(key).get(),
                                timeout=_FS_READ_TIMEOUT)
        return snap.exists

    async def grant_access(self, user_id: int) -> None:
        key = str(user_id)
        if not self._use_fs:
            self._mem.setdefault("access", {})[key] = time.time()
            return
        await _to_thread(
            lambda: _firestore().collection(_col("access")).document(key).set({"ts": time.time()}),
            timeout=_FS_WRITE_TIMEOUT,
        )

    # ── implementações Firestore (síncronas) ─────────────────────────────────
    def _fs_claim(self, col: str, key: str) -> bool:
        from google.cloud import firestore

        doc = _firestore().collection(_col(col)).document(key)

        @firestore.transactional
        def _txn(txn):
            snap = doc.get(transaction=txn)
            if snap.exists:
                d = snap.to_dict() or {}
                if d.get("status") == "done" or time.time() - d.get("ts", 0) < _CLAIM_STALE_S:
                    return False  # concluída, ou em andamento recente
            txn.set(doc, {"ts": time.time(), "status": "in_progress",
                          "expire_at": time.time() + Config.UPDATE_DEDUP_TTL_S})
            return True

        return _txn(_firestore().transaction())

    def _fs_complete(self, col: str, key: str, result: dict) -> None:
        _firestore().collection(_col(col)).document(key).set(
            {"ts": time.time(), "status": "done", "result": result,
             "expire_at": time.time() + Config.UPDATE_DEDUP_TTL_S}, merge=True
        )

    def _fs_get_result(self, col: str, key: str) -> Optional[dict]:
        snap = _firestore().collection(_col(col)).document(key).get()
        return (snap.to_dict() or {}).get("result") if snap.exists else None


def make_persistence():
    """BasePersistence p/ o ConversationHandler — Firestore no Cloud Run, None no polling."""
    if not Config.using_cloud_run():
        return None
    return FirestorePersistence()


# ── BasePersistence do PTB sobre Firestore (partes usadas: user_data + conversations) ──
try:
    from telegram.ext import BasePersistence, PersistenceInput
except Exception:  # pragma: no cover - PTB ausente em alguns contextos de teste
    BasePersistence = object  # type: ignore
    PersistenceInput = None  # type: ignore


class FirestorePersistence(BasePersistence):  # type: ignore[misc]
    """Persistência mínima e suficiente p/ os wizards: `user_data` + `conversations`.

    `bot_data`/`chat_data`/`callback_data` ficam em memória (não usados pelos fluxos). Cada
    leitura/escrita do Firestore roda em thread (cliente síncrono). TTL dos docs é tratado
    por uma regra de TTL no Firestore (campo `expire_at`).
    """

    def __init__(self) -> None:
        if PersistenceInput is not None:
            super().__init__(store_data=PersistenceInput(bot_data=False, chat_data=False,
                                                          user_data=True, callback_data=False),
                             update_interval=30)
        self._bot_data: dict = {}
        self._chat_data: dict = {}

    def _doc(self, col: str, key: str):
        return _firestore().collection(_col(col)).document(str(key))

    # user_data
    async def get_user_data(self) -> dict:
        def _load():
            out = {}
            for snap in _firestore().collection(_col("user_data")).stream():
                out[int(snap.id)] = (snap.to_dict() or {}).get("data", {})
            return out
        return await asyncio.to_thread(_load)

    async def update_user_data(self, user_id: int, data: dict) -> None:
        await asyncio.to_thread(
            lambda: self._doc("user_data", user_id).set(
                {"data": data, "expire_at": time.time() + Config.CONVERSATION_TIMEOUT_S * 4}
            )
        )

    async def drop_user_data(self, user_id: int) -> None:
        await asyncio.to_thread(lambda: self._doc("user_data", user_id).delete())

    async def refresh_user_data(self, user_id: int, user_data: dict) -> None:
        snap = await asyncio.to_thread(lambda: self._doc("user_data", user_id).get())
        if snap.exists:
            user_data.update((snap.to_dict() or {}).get("data", {}))

    # conversations
    async def get_conversations(self, name: str) -> dict:
        def _load():
            out = {}
            for snap in _firestore().collection(_col(f"conv_{name}")).stream():
                d = snap.to_dict() or {}
                out[tuple(d.get("key", []))] = d.get("state")
            return out
        return await asyncio.to_thread(_load)

    async def update_conversation(self, name: str, key: tuple, new_state: Optional[object]) -> None:
        docid = "_".join(map(str, key))

        def _save():
            ref = self._doc(f"conv_{name}", docid)
            if new_state is None:
                ref.delete()
            else:
                ref.set({"key": list(key), "state": new_state,
                         "expire_at": time.time() + Config.CONVERSATION_TIMEOUT_S * 4})
        await asyncio.to_thread(_save)

    # não usados (memória / no-op)
    async def get_bot_data(self) -> dict:
        return self._bot_data

    async def update_bot_data(self, data: dict) -> None:
        self._bot_data = data

    async def refresh_bot_data(self, bot_data: dict) -> None:
        return None

    async def get_chat_data(self) -> dict:
        return self._chat_data

    async def update_chat_data(self, chat_id: int, data: dict) -> None:
        self._chat_data[chat_id] = data

    async def drop_chat_data(self, chat_id: int) -> None:
        self._chat_data.pop(chat_id, None)

    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None:
        return None

    async def get_callback_data(self):
        return None

    async def update_callback_data(self, data) -> None:
        return None

    async def flush(self) -> None:
        return None
