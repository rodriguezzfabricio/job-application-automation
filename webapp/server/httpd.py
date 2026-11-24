"""HTTP surface: /healthz + /api/sources (+ write routes, Phase 1).

GET  /healthz                       health summary (R3)
GET  /api/sources                   registered boards + politeness state
POST /api/sources                   board registration (live 200 + envelope)
POST /api/sources/<id>/check-now    immediate poll; refused while backing off
POST /api/sources/<id>/resume       manual resume; never overrides host Retry-After

Write endpoints require `Authorization: Bearer <SETUP_TOKEN>` (interim auth
until a11 sessions land, L-0010). Frontend comes after R2.
"""

import hmac
import json
import math
import sqlite3
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, exporter, health, liveness_transport, politeness, poller, sources


def make_server(
    address,
    store,
    heartbeat,
    interval=config.POLL_INTERVAL,
    fetcher=None,
):
    fetcher = fetcher or politeness.PoliteFetcher(store)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib API)
            if self.path == "/healthz":
                status, payload = health.health_summary(store, heartbeat, interval)
                payload["version"] = config.__version__  # rollback-verification stamp (R1)
                self._json(status, payload)
            elif self.path == "/api/sources":
                self._json(200, {"sources": store.list_sources()})
            elif self.path == "/api/jobs":
                self._json(200, {"jobs": store.list_jobs()})
            elif self.path == "/api/applications":
                self._json(200, {"applications": store.list_applications()})
            elif self.path == "/api/export":
                # Sensitive read (P11): authenticated even though it is a GET.
                if not self._write_guard():
                    return
                self._json(200, exporter.build_export(
                    store.list_applications(), store.list_status_history()))
            elif self.path.startswith("/api/applications/"):
                job_id = urllib.parse.unquote(self.path.split("/", 3)[3])
                app = store.get_application(job_id)
                if app is None:
                    self._json(404, {"error": "unknown application"})
                else:
                    self._json(200, {"application": app})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802 (stdlib API)
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/sources":
                return self._register_source()
            parts = path.rstrip("/").split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "sources"]:
                source_id, action = parts[3], parts[4]
                if action == "check-now":
                    return self._check_now(source_id)
                if action == "resume":
                    return self._resume(source_id)
            self._json(404, {"error": "not found"})

        # --- interim write auth (a11 replaces this) ---

        def _write_guard(self):
            if not config.SETUP_TOKEN:
                self._json(503, {"error": "SETUP_TOKEN not configured"})
                return False
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {config.SETUP_TOKEN}"):
                self._json(401, {"error": "unauthorized"})
                return False
            return True

        def _body_json(self):
            raw = self.headers.get("Content-Length")
            try:
                length = int(raw) if raw is not None else 0
            except ValueError:
                self._json(400, {"error": "invalid Content-Length"})
                return None
            if length < 0 or length > 64 * 1024:
                self._json(400, {"error": "invalid Content-Length"})
                return None
            if length == 0:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None

        # --- registration (a1: live 200 + typed envelope; cannot widen transport) ---

        def _register_source(self):
            if not self._write_guard():
                return
            payload = self._body_json()
            if not isinstance(payload, dict):
                return self._json(400, {"error": "JSON body required"})
            name = str(payload.get("name") or "").strip()
            kind = str(payload.get("kind") or "").strip()
            board_id = str(payload.get("board_id") or "").strip()
            if not name:
                return self._json(400, {"error": "name is required"})
            if kind not in sources.BOARD_KINDS:
                return self._json(
                    400,
                    {
                        "error": f"unknown kind {kind!r} — registration cannot"
                        " widen the feed transport"
                    },
                )
            careers_url = payload.get("careers_url")
            if careers_url is not None:
                # The employer transport's construction gate (HTTPS-only,
                # usable host) must pass BEFORE any socket opens (P1).
                try:
                    liveness_transport.build_employer_transport(careers_url)
                except liveness_transport.EmployerTransportError as exc:
                    return self._json(400, {"error": str(exc)})
            try:
                url = sources.build_feed_url(kind, board_id)
            except sources.InvalidBoardToken as exc:
                return self._json(400, {"error": str(exc)})
            source_id = f"{kind}:{board_id}"
            if store.get_source(source_id):
                return self._json(409, {"error": "board already registered"})
            host = urllib.parse.urlparse(url).hostname
            try:
                status, body, _ = fetcher.fetch(url, host, config.FETCH_TIMEOUT_S)
            except politeness.HostCoolingDown as exc:
                retry_s = max(1, int(math.ceil(exc.until - time.time())))
                return self._json(
                    429,
                    {"error": f"host cooling down — retry after {retry_s}s"},
                    headers={"Retry-After": str(retry_s)},
                )
            except Exception as exc:  # noqa: BLE001 — report upstream failure
                return self._json(
                    502, {"error": f"live check failed: {type(exc).__name__}"}
                )
            if not 200 <= status < 300:
                return self._json(502, {"error": f"upstream returned {status}"})
            if not sources.validate_envelope(kind, body):
                return self._json(400, {"error": f"not a valid {kind} feed envelope"})
            try:
                store.add_source(
                    {
                        "id": source_id,
                        "name": name,
                        "kind": kind,
                        "host": host,
                        "feed_url": url,
                        "careers_url": careers_url,
                    }
                )
            except sqlite3.IntegrityError:
                return self._json(409, {"error": "board already registered"})
            self._json(201, {"source": store.get_source(source_id)})

        # --- check-now: server-side backoff refusal (a9) ---

        def _check_now(self, source_id):
            if not self._write_guard():
                return
            row = store.get_source(source_id)
            if row is None:
                return self._json(404, {"error": "unknown board"})
            now = time.time()
            if row["deferred_until"] and row["deferred_until"] > now:
                retry_s = int(math.ceil(row["deferred_until"] - now))
                return self._json(
                    429,
                    {
                        "error": f"backing off — next check allowed in {retry_s}s",
                        "retry_after_s": retry_s,
                    },
                    headers={"Retry-After": str(retry_s)},
                )
            try:
                status, _state, _summary = poller.poll_board(
                    store, fetcher, row, now)
            except politeness.HostCoolingDown as exc:
                retry_s = max(1, int(math.ceil(exc.until - now)))
                return self._json(
                    429,
                    {
                        "error": f"backing off — next check allowed in {retry_s}s"
                        " (Retry-After)",
                        "retry_after_s": retry_s,
                    },
                    headers={"Retry-After": str(retry_s)},
                )
            except Exception as exc:  # noqa: BLE001
                return self._json(502, {"error": f"check failed: {type(exc).__name__}"})
            if status in (429, 403):
                # The check itself was throttled: surface the fresh cooldown so
                # the a9 countdown can render it.
                fresh = store.get_source(source_id)
                retry_s = max(1, int(math.ceil((fresh["deferred_until"] or now) - now)))
                return self._json(
                    429,
                    {
                        "error": f"backing off — next check allowed in {retry_s}s",
                        "retry_after_s": retry_s,
                    },
                    headers={"Retry-After": str(retry_s)},
                )
            self._json(200, {"id": source_id, "status": status})

        # --- manual resume: never overrides an outstanding server deadline (a1) ---

        def _resume(self, source_id):
            if not self._write_guard():
                return
            row = store.get_source(source_id)
            if row is None:
                return self._json(404, {"error": "unknown board"})
            until = store.host_cooldown_until(row["host"])
            now = time.time()
            if until and until > now:
                return self._json(
                    409,
                    {
                        "error": "resume deferred — host Retry-After active until"
                        f" {time.ctime(until)}"
                    },
                )
            store.resume_board(source_id)
            self._json(200, {"resumed": True, "id": source_id})

        # --- plumbing ---

        def _json(self, status, payload, headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the container log quiet
            pass

    return ThreadingHTTPServer(address, Handler)
