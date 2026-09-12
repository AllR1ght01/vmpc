"""Local HTTP application powering vmpc's HTML desktop interface."""

from __future__ import annotations

import json
import mimetypes
import queue
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from vmpc.api.client import stream_agent_chat
from vmpc.api.events import ApiError, EventKind
from vmpc.chats import ChatRecord, fallback_title, list_chats, load_chat, new_id, save_chat
from vmpc.commands import CommandRegistry
from vmpc.commands.model import _list_models, known_models
from vmpc.config import (
    AUTH_BEARER,
    DEFAULT_AUTH_FOR_WIRE,
    KEY_TYPE_STATIC,
    WIRE_OPENAI,
    Config,
    ConfigError,
    Provider,
    load_config,
)
from vmpc.context import apply as apply_context
from vmpc.session import Session


class UiError(Exception):
    """An expected problem that can be shown without a traceback."""


class WebController:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.registry = CommandRegistry(config)
        self.session = Session()
        self.session.set_mode(self.registry.active_mode())
        self.chat_id = ""
        self.chat_created = 0.0
        self.chat_model = ""
        self.cancel: Optional[threading.Event] = None
        self.worker: Optional[threading.Thread] = None
        self.lock = threading.RLock()
        self.subscribers: list[queue.Queue[dict[str, Any]]] = []
        self.closed = threading.Event()

    # -- event stream -------------------------------------------------

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        channel: queue.Queue[dict[str, Any]] = queue.Queue()
        with self.lock:
            self.subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            if channel in self.subscribers:
                self.subscribers.remove(channel)

    def publish(self, event: dict[str, Any]) -> None:
        with self.lock:
            channels = list(self.subscribers)
        for channel in channels:
            channel.put(event)

    # -- state --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            provider = self.config.active_provider()
            modes = [
                {
                    "name": "",
                    "label": "Обычный",
                    "description": "Универсальный помощник без дополнительных инструкций",
                }
            ] + [
                {
                    "name": mode.name,
                    "label": {
                        "dev": "Разработка",
                        "short": "Кратко",
                        "explain": "Объяснение",
                    }.get(mode.name, mode.name.capitalize()),
                    "description": mode.description,
                }
                for mode in self.registry.modes()
            ]
            return {
                "busy": bool(self.worker and self.worker.is_alive()),
                "activeProvider": self.config.active,
                "activeModel": provider.model if provider else "",
                "activeMode": self.session.mode,
                "providers": [self._provider_json(item) for item in self.config.providers],
                "modes": modes,
                "chat": self._chat_json(),
                "chats": self._chat_list_json(),
            }

    def _provider_json(self, provider: Provider) -> dict[str, Any]:
        return {
            "name": provider.name,
            "wire": provider.wire,
            "baseUrl": provider.base_url,
            "model": provider.model,
            "models": known_models(provider),
            "keyType": provider.key_type,
            "keyHint": provider.masked_key(),
            "authScheme": provider.auth_scheme,
            "reasoning": provider.reasoning,
        }

    def _chat_list_json(self) -> list[dict[str, Any]]:
        return [
            {
                "id": record.id,
                "title": record.label,
                "model": record.model,
                "updated": record.updated,
                "turns": record.turns,
            }
            for record in list_chats(limit=100)
        ]

    def _chat_json(self) -> dict[str, Any]:
        provider = self.config.active_provider()
        fallback_model = self.chat_model or (provider.model if provider else "")
        messages = []
        for message in self.session.messages:
            row = {
                "role": message.get("role", ""),
                "content": message.get("content", ""),
            }
            if row["role"] == "assistant":
                row["model"] = message.get("model", "") or fallback_model
            messages.append(row)
        return {
            "id": self.chat_id,
            "title": self.session.title or "Новый чат",
            "messages": messages,
            "context": list(self.session.context_paths),
            "tokens": self.session.total_tokens,
        }

    # -- conversations -----------------------------------------------

    def new_chat(self) -> dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            self.session.reset()
            self.session.set_mode(self.registry.active_mode())
            self.chat_id = ""
            self.chat_created = 0.0
            self.chat_model = ""
            return self._chat_json()

    def open_chat(self, chat_id: str) -> dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            record = load_chat(chat_id)
            record.into_session(self.session)
            self.chat_id = record.id
            self.chat_created = record.created
            self.chat_model = record.model
            if self.session.context_paths:
                apply_context(self.session)
            return self._chat_json()

    def send(self, text: str) -> dict[str, Any]:
        message = text.strip()
        if not message:
            raise UiError("Введите сообщение")
        with self.lock:
            self._ensure_idle()
            provider = self.config.active_provider()
            if provider is None:
                raise UiError("Сначала настройте API endpoint")
            problems = provider.validate()
            if problems:
                raise UiError(f"Endpoint настроен не полностью: {problems[0]}")
            provider = provider.copy()
            self.session.add_user(message)
            messages = [dict(item) for item in self.session.messages]
            system = self.session.full_system()
            self.cancel = threading.Event()
            cancel = self.cancel
            self.worker = threading.Thread(
                target=self._turn,
                args=(provider, messages, system, cancel),
                name="vmpc-web-turn",
                daemon=True,
            )
            self.worker.start()
        self.publish({"type": "turn_start", "message": message, "model": provider.model})
        return {"ok": True, "model": provider.model}

    def _turn(self, provider: Provider, messages: list[dict[str, str]], system: str, cancel: threading.Event) -> None:
        answer: list[str] = []
        usage = 0
        try:
            for event in stream_agent_chat(provider, messages, system=system, cancel=cancel):
                if event.kind == EventKind.ANSWER and event.text:
                    answer.append(event.text)
                    self.publish({"type": "answer", "text": event.text})
                elif event.kind == EventKind.REASONING and event.text:
                    self.publish({"type": "reasoning", "text": event.text})
                elif event.kind == EventKind.USAGE and event.usage:
                    usage = event.usage.total

            text = "".join(answer)
            with self.lock:
                if text:
                    self.session.add_assistant(text, model=provider.model)
                    self.session.total_tokens += usage
                    if not self.session.title:
                        self.session.title = fallback_title(self.session.messages)
                    self._persist(provider)
                else:
                    self.session.drop_last_user()
                stopped = cancel.is_set()
                payload = self._chat_json()
                chats = self._chat_list_json()
            self.publish({"type": "turn_done", "stopped": stopped, "chat": payload, "chats": chats})
        except ApiError as exc:
            with self.lock:
                self.session.drop_last_user()
            self.publish({"type": "turn_error", "message": str(exc), "hint": exc.hint})
        except Exception as exc:  # noqa: BLE001 - errors belong in the interface
            with self.lock:
                self.session.drop_last_user()
            self.publish({"type": "turn_error", "message": str(exc), "hint": ""})
        finally:
            with self.lock:
                self.worker = None
                self.cancel = None

    def _persist(self, provider: Provider) -> None:
        if not self.chat_id:
            self.chat_id = new_id()
        record = ChatRecord.from_session(
            self.session,
            self.chat_id,
            provider_name=provider.name,
            model=provider.model,
            created=self.chat_created,
        )
        self.chat_created = record.created
        self.chat_model = record.model
        save_chat(record)

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if self.cancel is not None:
                self.cancel.set()
        return {"ok": True}

    # -- selections and endpoint settings ----------------------------

    def select(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            provider_name = str(data.get("provider", "")).strip()
            if provider_name:
                if self.config.get(provider_name) is None:
                    raise UiError("Endpoint не найден")
                self.config.active = provider_name
            provider = self.config.active_provider()
            model = str(data.get("model", "")).strip()
            if provider is not None and model:
                provider.model = model
                if model not in provider.models:
                    provider.models.append(model)
            mode_name = str(data.get("mode", self.config.mode)).strip()
            mode = next((item for item in self.registry.modes() if item.name == mode_name), None)
            self.config.mode = mode.name if mode else ""
            self.session.set_mode(mode)
            self.config.save()
            return self.snapshot()

    def save_provider(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            old_name = str(data.get("oldName", "")).strip()
            old = self.config.get(old_name) if old_name else None
            wire = str(data.get("wire", WIRE_OPENAI))
            key_value = str(data.get("keyValue", "")).strip()
            if old is not None and not key_value:
                key_value = old.key_value
            changes = {
                "name": str(data.get("name", "")).strip(),
                "wire": wire,
                "base_url": str(data.get("baseUrl", "")).strip(),
                "model": str(data.get("model", "")).strip(),
                "key_type": str(data.get("keyType", KEY_TYPE_STATIC)),
                "key_value": key_value,
                "auth_scheme": str(data.get("authScheme", "")) or DEFAULT_AUTH_FOR_WIRE.get(wire, AUTH_BEARER),
                "reasoning": bool(data.get("reasoning", False)),
            }
            provider = old.copy(**changes) if old else Provider(**changes)
            problems = provider.validate()
            if problems:
                raise UiError(problems[0])
            if old is not None and old.name != provider.name:
                self.config.remove(old.name)
            self.config.upsert(provider)
            self.config.active = provider.name
            self.config.save()
            return self.snapshot()

    def models(self, provider_name: str) -> dict[str, Any]:
        provider = self.config.get(provider_name) if provider_name else self.config.active_provider()
        if provider is None:
            raise UiError("Endpoint не настроен")
        live = _list_models(provider) or []
        return {
            "models": known_models(provider, live),
            "advertised": live,
            "source": "endpoint" if live else "saved",
        }

    def probe_models(self, provider_name: str) -> dict[str, Any]:
        from vmpc.probe import candidates, probe

        provider = self.config.get(provider_name) if provider_name else self.config.active_provider()
        if provider is None:
            raise UiError("Endpoint не настроен")
        results = list(probe(provider, candidates(provider)))
        available = [result.model for result in results if result.available]
        with self.lock:
            for model in available:
                if model not in provider.models:
                    provider.models.append(model)
            self.config.save()
        return {
            "models": known_models(provider),
            "available": available,
            "checked": len(results),
        }

    # -- attached files ----------------------------------------------

    def pick_context(self, kind: str) -> dict[str, Any]:
        # The browser sandbox intentionally does not reveal absolute paths.
        # A native file picker gives the existing context loader a real path.
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = (
                filedialog.askdirectory(title="Выберите папку с контекстом", parent=root)
                if kind == "folder"
                else filedialog.askopenfilename(title="Выберите файл", parent=root)
            )
        finally:
            root.destroy()
        if not selected:
            return {"context": list(self.session.context_paths)}
        resolved = str(Path(selected).resolve())
        with self.lock:
            if resolved not in self.session.context_paths:
                self.session.context_paths.append(resolved)
            _bundles, errors = apply_context(self.session)
        return {"context": list(self.session.context_paths), "errors": errors}

    def clear_context(self) -> dict[str, Any]:
        with self.lock:
            self.session.context_paths.clear()
            self.session.context_text = ""
        return {"context": []}

    def _ensure_idle(self) -> None:
        if self.worker and self.worker.is_alive():
            raise UiError("Сначала остановите текущий ответ")


class VmpcHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], controller: WebController) -> None:
        super().__init__(address, VmpcHandler)
        self.controller = controller


class VmpcHandler(BaseHTTPRequestHandler):
    server: VmpcHttpServer
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/state":
                self._json(self.server.controller.snapshot())
                return
            if parsed.path == "/api/models":
                query = parse_qs(parsed.query)
                self._json(self.server.controller.models(query.get("provider", [""])[0]))
                return
            if parsed.path == "/api/events":
                self._events()
                return
            self._static(parsed.path)
        except UiError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        try:
            data = self._body()
            path = urlparse(self.path).path
            controller = self.server.controller
            if path == "/api/chat/new":
                result = controller.new_chat()
            elif path == "/api/chat/open":
                result = controller.open_chat(str(data.get("id", "")))
            elif path == "/api/send":
                result = controller.send(str(data.get("message", "")))
            elif path == "/api/stop":
                result = controller.stop()
            elif path == "/api/select":
                result = controller.select(data)
            elif path == "/api/provider/save":
                result = controller.save_provider(data)
            elif path == "/api/models/probe":
                result = controller.probe_models(str(data.get("provider", "")))
            elif path == "/api/context/pick":
                result = controller.pick_context(str(data.get("kind", "file")))
            elif path == "/api/context/clear":
                result = controller.clear_context()
            elif path == "/api/close":
                controller.closed.set()
                result = {"ok": True}
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(result)
        except (UiError, ConfigError, OSError, ValueError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0") or 0)
        if length > 2_000_000:
            raise UiError("Слишком большой запрос")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _events(self) -> None:
        channel = self.server.controller.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while not self.server.controller.closed.is_set():
                try:
                    event = channel.get(timeout=15)
                    data = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.controller.unsubscribe(channel)

    def _static(self, request_path: str) -> None:
        names = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
        name = names.get(request_path)
        if name is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        resource = files("vmpc.web").joinpath(name)
        data = resource.read_bytes()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def run_html_gui(config: Config) -> int:
    import webview

    controller = WebController(config)
    server = VmpcHttpServer(("127.0.0.1", 0), controller)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    server_thread = threading.Thread(target=server.serve_forever, name="vmpc-web-server", daemon=True)
    server_thread.start()

    window = webview.create_window(
        "vmpc",
        url=url,
        min_size=(820, 560),
        maximized=True,
        background_color="#0e151d",
        text_select=True,
    )
    window.events.closed += controller.closed.set
    try:
        webview.start(gui="edgechromium", debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        controller.closed.set()
        server.shutdown()
        server.server_close()
    return 0


def main() -> int:
    try:
        return run_html_gui(load_config())
    except ConfigError as exc:
        # The executable has no console, so use the browser for startup errors.
        raise SystemExit(f"vmpc: {exc}") from exc


__all__ = ["WebController", "run_html_gui", "main"]
