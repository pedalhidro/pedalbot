"""pedalbot — bot de Telegram do Pedal Hidrográfico.

Frontend único de chat para dois backends (clientes HTTP, sem importar o código deles):
- sabiá  — compositor/publicador de posts no Instagram.
- amora  — mapa + censo (passeios, fotos, vídeos).

Núcleo compartilhado: config, clients, ttl, media, handlers, persistence, tasks.
Entrypoints: webhook.py + worker.py (Cloud Run), polling.py (fallback local).
"""

__version__ = "0.1.0"
