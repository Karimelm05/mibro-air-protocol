"""
Local dashboard: a stdlib HTTP server bound to 127.0.0.1, serving one page
and a small JSON API. Nothing is loaded from the network.

Health data, so: loopback only, Host header checked (blocks DNS rebinding),
and the one state-changing call (POST /api/sync) requires a custom header
that a cross-site form or fetch cannot send without a preflight we refuse.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .store import Store

_sync_state = {"running": False, "last": None, "log": []}
_sync_lock = threading.Lock()


def _run_sync_thread(cfg):
    from .sync import BleLink, run_sync

    def log(m):
        _sync_state["log"] = (_sync_state["log"] + [f"{dt.datetime.now():%H:%M:%S} {m}"])[-30:]

    st = Store(cfg.db_path)          # own connection for this thread
    try:
        link = BleLink(cfg.address, cfg.adapter, cfg.use_path)
        _sync_state["last"] = asyncio.run(
            run_sync(st, link, cfg.lock_path, backfill_days=cfg.backfill_days, log=log))
    except Exception as exc:
        _sync_state["last"] = {"ok": False, "error": str(exc)}
        log(f"error: {exc}")
    finally:
        st.close()
        _sync_state["running"] = False


def make_handler(cfg, store: Store, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    db_lock = threading.Lock()
    page = resources.files("mibro").joinpath("static/index.html").read_bytes()

    class H(BaseHTTPRequestHandler):
        server_version = "mibro"

        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode())

        def _host_ok(self):
            if self.headers.get("Host") not in allowed_hosts:
                self._send(403, b"forbidden", "text/plain")
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            today = dt.date.today()
            try:
                if u.path == "/":
                    return self._send(200, page, "text/html; charset=utf-8")
                with db_lock:
                    if u.path == "/api/version":
                        # polled every few seconds by the page; see index.html
                        return self._json({"v": store.fingerprint(),
                                           "today": today.isoformat()})
                    if u.path == "/api/day":
                        day = q.get("date") or today.isoformat()
                        d = dt.date.fromisoformat(day)
                        prev = (d - dt.timedelta(days=1)).isoformat()
                        return self._json({
                            "day": day,
                            "totals": store.day_totals(day),
                            "hours": store.day_hours(day),
                            "night": store.night(prev),
                        })
                    if u.path == "/api/overview":
                        n = min(int(q.get("days", 30)), 365)
                        return self._json(store.overview(n))
                    if u.path == "/api/status":
                        return self._json({
                            "address": cfg.address,
                            "device": store.device(),
                            "battery": store.battery_series(60),
                            "syncs": store.syncs(15),
                            "days": store.known_days()[:400],
                            "sync": {k: _sync_state[k] for k in ("running", "last", "log")},
                            "today": today.isoformat(),
                        })
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._host_ok():
                return
            if self.headers.get("X-Mibro") != "1":
                return self._send(403, b"missing header", "text/plain")
            if urlparse(self.path).path != "/api/sync":
                return self._send(404, b"not found", "text/plain")
            if not cfg.address:
                return self._json({"error": "No watch address. Run: mibro setup ADDR"}, 400)
            with _sync_lock:
                if _sync_state["running"]:
                    return self._json({"error": "A sync is already running"}, 409)
                _sync_state.update(running=True, log=[])
            threading.Thread(target=_run_sync_thread, args=(cfg,), daemon=True).start()
            self._json({"started": True}, 202)

    return H


def serve(cfg, port: int = 8765, open_browser: bool = False):
    store = Store(cfg.db_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, store, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"dashboard on {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
