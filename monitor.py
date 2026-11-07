"""Monitor configurable de documentos HTML y notificaciones a Telegram."""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_EXTENSIONS = [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods", ".csv", ".zip", ".7z", ".rtf"]
MAX_BYTES = 45 * 1024 * 1024
HEADERS = {"User-Agent": "DocumentosMonitor/1.1", "Accept-Language": "es-ES,es;q=0.9"}


def load_sources(path):
    sources = json.loads(Path(path).read_text(encoding="utf-8"))["sources"]
    if not isinstance(sources, list):
        raise ValueError("'sources' debe ser una lista")
    ids = set()
    for source in sources:
        if not all(source.get(k) for k in ("id", "name", "url")):
            raise ValueError("Cada fuente necesita id, name y url")
        sid = source["id"]
        if not re.fullmatch("[a-z0-9][a-z0-9_-]{0,63}", sid) or sid in ids:
            raise ValueError("ID inválido o duplicado: " + sid)
        parsed = urlparse(source["url"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("URL inválida: " + source["url"])
        if source.get("mode", "documents") not in ("documents", "links", "both"):
            raise ValueError("Modo inválido: " + sid)
        ids.add(sid)
    return sources


RETRY_DELAYS = (5, 15)
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def wait_seconds(response, attempt):
    if response is not None and response.status_code == 429:
        try:
            return min(120, max(0, int(response.headers.get("Retry-After", ""))))
        except (TypeError, ValueError):
            pass
    return RETRY_DELAYS[attempt]


def get(url, timeout=30):
    for attempt in range(3):
        try:
            result = requests.get(url, timeout=timeout, headers=HEADERS)
            result.raise_for_status()
            return result
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            if attempt == 2 or (response is not None and response.status_code not in TRANSIENT_HTTP):
                raise
            delay = wait_seconds(response, attempt)
            print(f"Web: intento {attempt + 1}/3 fallido; reintento en {delay} s")
            time.sleep(delay)


def telegram_post(token, data, files, timeout, method="sendDocument"):
    for attempt in range(3):
        try:
            result = requests.post(
                f"https://api.telegram.org/bot{token}/{method}",
                data=data, files=files, timeout=timeout,
            )
            result.raise_for_status()
            if not result.json().get("ok"):
                raise RuntimeError("Telegram no confirmó el envío")
            return
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            if attempt == 2 or (response is not None and response.status_code not in TRANSIENT_HTTP):
                raise
            delay = wait_seconds(response, attempt)
            if response is not None and response.status_code == 429:
                try:
                    delay = min(120, max(0, int(response.json()["parameters"]["retry_after"])))
                except (KeyError, TypeError, ValueError):
                    pass
            print(f"Telegram: intento {attempt + 1}/3 fallido; reintento en {delay} s")
            time.sleep(delay)


def discover(source, html=None):
    if html is None:
        html = get(source["url"]).text
    soup = BeautifulSoup(html, "html.parser")
    extensions = tuple(x.lower() for x in source.get("extensions", DEFAULT_EXTENSIONS))
    markers = tuple(x.lower() for x in source.get("download_markers", []))
    found = {}
    mode = source.get("mode", "documents")
    for link in soup.select(source.get("selector", "a[href]")):
        href = link.get("href")
        if not href:
            continue
        url = urljoin(source["url"], href).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        path = unquote(parsed.path).lower()
        query = unquote(parsed.query).lower()
        link_type = link.get("type", "").lower()
        is_document = (
            path.endswith(extensions)
            or any(x in query for x in extensions)
            or any(x in path for x in markers)
            or link.has_attr("download")
            or (link_type.startswith("application/") and "html" not in link_type)
        )
        kind = "document" if is_document else "link"
        if (kind == "document" and mode == "links") or (kind == "link" and mode == "documents"):
            continue
        if kind == "link":
            haystack = (link.get_text(" ", strip=True) + " " + url).lower()
            if any(word.lower() in haystack for word in source.get("link_exclude", [])):
                continue
            includes = source.get("link_include", [])
            if includes and not any(word.lower() in haystack for word in includes):
                continue
        title = (link.get_text(" ", strip=True)
                 or link.get("aria-label", "").strip()
                 or link.get("title", "").strip()
                 or unquote(Path(parsed.path).name)
                 or "Documento")
        key = hashlib.sha256((source["id"] + "|" + url).encode()).hexdigest()[:24]
        found[key] = {"title": title, "url": url, "kind": kind}
    return found


def save_state(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def send_document(source, item, token, chat):
    response = get(item["url"], timeout=90)
    content_type = response.headers.get("Content-Type", "application/octet-stream").lower()
    if "text/html" in content_type:
        raise ValueError("El enlace devuelve HTML: " + item["url"])
    if len(response.content) > MAX_BYTES:
        raise ValueError("Documento demasiado grande")
    filename = unquote(Path(urlparse(item["url"]).path).name) or "documento"
    filename = re.sub(r'[\\/:*?"<>|]+', "-", filename).strip(". ")[:130] or "documento"
    if "." not in filename:
        filename += ".pdf" if "pdf" in content_type else ".bin"
    caption = ("Nuevo documento · " + source["name"] + "\n" + item["title"] + "\n" + item["url"])[:1000]
    telegram_post(token, {"chat_id": chat, "caption": caption},
                  {"document": (filename, response.content, content_type)}, 120)

def send_link(source, item, token, chat):
    message = ("Nuevo enlace · " + source["name"] + "\n" + item["title"] + "\n" + item["url"])[:4096]
    telegram_post(token, {"chat_id": chat, "text": message, "disable_web_page_preview": True},
                  None, 30, method="sendMessage")


def send_test(token, chat):
    telegram_post(token, {"chat_id": chat, "caption": "Prueba de monitor genérico"},
                  {"document": ("prueba.txt", b"Monitor conectado a Telegram.", "text/plain")}, 30)

def run(sources, state_path, dry_run=False, token="", default_chat=""):
    state_path = Path(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    errors = []
    current_week = datetime.now(timezone.utc).strftime("%G-W%V")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for source in sources:
        if not source.get("enabled", False):
            continue
        sid = source["id"]
        try:
            found = discover(source)
            print(f"{sid}: {len(found)} elementos encontrados")
            if dry_run:
                print(f"{sid}: simulación; sin envíos ni estado")
                continue
            previous = state.get(sid)
            # Formato antiguo: lista sin URL de origen. Se migra con un
            # inventario seguro para evitar envíos masivos por cambio de web.
            if (previous is None or isinstance(previous, list)
                    or not isinstance(previous, dict)
                    or previous.get("url") != source["url"]):
                reason = "URL modificada" if isinstance(previous, dict) and previous.get("url") != source["url"] else "inventario inicial o migración"
                state[sid] = {"url": source["url"], "processed": sorted(found), "mode": source.get("mode", "documents")}
                save_state(state_path, state)
                print(f"{sid}: {reason}; {len(found)} documentos registrados sin enviar históricos")
                state[sid].update({"heartbeat_week": current_week, "last_success": timestamp, "last_documents_found": len(found)})
                save_state(state_path, state)
                continue
            mode = source.get("mode", "documents")
            previous_mode = previous.get("mode", "documents")
            if mode != previous_mode:
                kinds = {"documents": {"document"}, "links": {"link"}, "both": {"document", "link"}}
                added = kinds[mode] - kinds[previous_mode]
                previous["processed"] = sorted(set(previous["processed"]) | {
                    key for key, item in found.items() if item.get("kind", "document") in added
                })
                previous["mode"] = mode
                save_state(state_path, state)
            seen = set(previous["processed"])
            for key, item in found.items():
                if key in seen:
                    continue
                chat = source.get("chat_id") or default_chat
                if not token or not chat:
                    raise ValueError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
                if item.get("kind", "document") == "link":
                    send_link(source, item, token, str(chat))
                else:
                    send_document(source, item, token, str(chat))
                seen.add(key)
                state[sid]["processed"] = sorted(seen)
                save_state(state_path, state)
                print(f"{sid}: enviado {item['title']}")
            state[sid].pop("last_error", None)
            state[sid].update({"heartbeat_week": current_week, "last_success": timestamp, "last_documents_found": len(found)})
            save_state(state_path, state)
        except Exception as error:
            errors.append(sid)
            if not dry_run and isinstance(state.get(sid), dict) and state[sid].get("url") == source["url"]:
                state[sid]["last_error"] = {"at": timestamp, "message": f"{type(error).__name__}: {error}"[:400]}
                save_state(state_path, state)
            print(f"ERROR {sid}: {type(error).__name__}: {error}", file=sys.stderr)
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="sources.json")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send-test", action="store_true")
    args = parser.parse_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if args.send_test:
        if not token or not chat:
            parser.error("Configura primero los secretos de Telegram")
        send_test(token, chat)
        print("Mensaje de prueba enviado")
    errors = run(load_sources(args.config), args.state, args.dry_run, token, chat)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
