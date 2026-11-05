"""Keep a free Render web service awake.

Render's free web services spin down after ~15 minutes without inbound HTTP
traffic, and background workers have no free plan. So we run as a web
service: a tiny HTTP server answers health checks and shows live stats, and a
self-ping thread hits our own public URL every few minutes. The ping goes out
through Render's proxy and back in, so it counts as inbound traffic.
"""
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

STARTED_AT = datetime.now(timezone.utc)


def start_http_server(replayer, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/healthz"):
                body, ctype = b"ok", "text/plain"
            else:
                ok, bad = replayer.success_count, replayer.failure_count
                total = ok + bad
                body = json.dumps({
                    "status": "running",
                    "started_at": STARTED_AT.isoformat(),
                    "this_process": {
                        "total": total,
                        "successes": ok,
                        "failures": bad,
                        "success_pct": round(100 * ok / total, 2) if total else None,
                        "last_status_code": replayer.last_status_code,
                    },
                }, indent=2).encode()
                ctype = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_HEAD = do_GET

        def log_message(self, *args):
            pass  # keep pings/health checks out of the logs

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"🌐 Status server listening on port {port}")
    return server


def start_self_ping(url, interval_seconds=600):
    """Ping url every interval_seconds (default 10 min, under Render's 15 min idle limit)."""
    url = url.rstrip("/") + "/healthz"

    def loop():
        while True:
            time.sleep(interval_seconds)
            try:
                r = requests.get(url, timeout=15)
                if r.status_code != 200:
                    print(f"   ⚠️ Self-ping got HTTP {r.status_code}")
            except Exception as e:
                print(f"   ⚠️ Self-ping failed: {e}")

    threading.Thread(target=loop, daemon=True).start()
    print(f"🔁 Self-pinging {url} every {interval_seconds // 60} min")
