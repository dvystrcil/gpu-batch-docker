"""Completion-webhook sink for the compose harness (gpu-batch-docker#10).

Stands in for n8n's receiver. POST /webhook/gpu-batch-complete with the
runner's status.json body: writes it to <out>/<id>.json (atomically), bumps
<out>/<id>.count, and returns 200. So the assert step can prove the webhook
fired, with the right body, exactly once per job.

SINK_FAIL=<code> makes every POST return that code and write nothing (to
prove the harness goes red when nobody was told).
GET /_mock/health is readiness.

  python3 notify_sink.py      (SINK_DIR, default /work/_notify; port 8080)
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def make_server(host: str, port: int, out_dir, fail_status: int | None = None) -> ThreadingHTTPServer:
    out = Path(out_dir)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/_mock/health":
                return self._send(200, b'{"ok": true}')
            return self._send(404, b'{"error": "not found"}')

        def do_POST(self):
            if self.path != "/webhook/gpu-batch-complete":
                return self._send(404, b'{"error": "not found"}')
            if fail_status:
                return self._send(fail_status, b'{"error": "SINK_FAIL"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                jid = body["id"]
                if not isinstance(jid, str) or not _ID.match(jid):
                    raise ValueError(f"bad id {jid!r}")
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
            with lock:
                out.mkdir(parents=True, exist_ok=True)
                tmp = out / f".{jid}.json.tmp"
                tmp.write_text(json.dumps(body))
                os.replace(tmp, out / f"{jid}.json")
                cnt = out / f"{jid}.count"
                cnt.write_text(str(int(cnt.read_text()) + 1 if cnt.exists() else 1))
            return self._send(200, b'{"ok": true}')

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    fail = os.environ.get("SINK_FAIL")
    srv = make_server("0.0.0.0", int(os.environ.get("SINK_PORT", "8080")),
                      os.environ.get("SINK_DIR", "/work/_notify"), int(fail) if fail else None)
    print(f"notify-sink: fail={fail or 'no'}", flush=True)
    srv.serve_forever()
