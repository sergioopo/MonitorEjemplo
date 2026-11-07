"""Obtiene el ID de un grupo o canal mediante un comando enviado al bot.

Se ejecuta manualmente desde GitHub Actions. No imprime tokens ni JSON.
"""
import html
import os
import sys
import time

import requests


def api_call(session, token, method, **params):
    response = session.get(
        f"https://api.telegram.org/bot{token}/{method}",
        params=params,
        timeout=40,
    )
    data = response.json()
    if not response.ok or not data.get("ok"):
        description = data.get("description", "Error desconocido de Telegram")
        if "webhook" in description.lower():
            raise RuntimeError("Hay un webhook activo: getUpdates no puede utilizarse hasta desactivarlo.")
        if "conflict" in description.lower():
            raise RuntimeError("Otro proceso está consultando las actualizaciones de este bot.")
        raise RuntimeError(f"Telegram: {description}")
    return data["result"]


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Falta el secreto TELEGRAM_BOT_TOKEN.", file=sys.stderr)
        return 1

    with requests.Session() as session:
        try:
            me = api_call(session, token, "getMe")
            username = me["username"]
            print(f"En un grupo escribe /id@{username}; en un canal privado publica cualquier mensaje NUEVO.")
            webhook = api_call(session, token, "getWebhookInfo")
            if webhook.get("url"):
                raise RuntimeError("Hay un webhook activo. Desactívalo antes de usar este workflow.")
            print("Webhook: no activo. Preparando escucha de nuevas publicaciones...")
            allowed = '["message","channel_post"]'
            # Ignorar actualizaciones anteriores al inicio sin vaciar el historial global.
            pending = api_call(session, token, "getUpdates", timeout=0, allowed_updates=allowed)
            offset = max((u["update_id"] for u in pending), default=-1) + 1 if pending else None
            started = int(time.time())
            print("Esperando el comando durante 90 segundos...")
            deadline = time.monotonic() + 90
            received = {"message": 0, "channel_post": 0, "other": 0}
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                params = {"timeout": min(20, max(1, int(remaining))), "allowed_updates": allowed}
                if offset is not None:
                    params["offset"] = offset
                updates = api_call(session, token, "getUpdates", **params)
                for update in updates:
                    offset = update["update_id"] + 1
                    kind = "channel_post" if "channel_post" in update else "message" if "message" in update else "other"
                    received[kind] += 1
                    msg = update.get(kind) or {}
                    chat = msg.get("chat") or {}
                    chat_type = chat.get("type")
                    if chat_type not in ("group", "supergroup", "channel"):
                        continue
                    if msg.get("date", 0) < started:
                        continue
                    if chat_type != "channel":
                        command = (msg.get("text") or "").split(maxsplit=1)[0].lower()
                        if command not in ("/id", f"/id@{username.lower()}"):
                            continue
                    chat_id = chat["id"]
                    title = chat.get("title", "Chat sin nombre")
                    api_call(
                        session, token, "sendMessage",
                        chat_id=chat_id,
                        text=f"📋 ID actual de «{html.escape(title)}»: \n<code>{chat_id}</code>\n\nCópialo en el secreto TELEGRAM_CHAT_ID de GitHub.",
                        parse_mode="HTML",
                    )
                    print(f"ID enviado a «{title}». Consulta Telegram.")
                    return 0
            print(f"Diagnóstico (sin contenido): mensajes={received['message']}, publicaciones_canal={received['channel_post']}, otros={received['other']}")
            print("No se detectó una publicación nueva del canal ni un comando de grupo. Publica un mensaje NUEVO durante la escucha, no un comentario ni un mensaje anterior.")
            return 1
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
            print(f"No se pudo obtener el ID: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
