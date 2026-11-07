"""Mock Moonraker for the compose harness (gpu-batch-docker#10).

GET  /printer/objects/query   what the guard probes. Answer depends on the
                              current state:
       <any print state>      200 {"result": {"status": {"print_stats": {"state": <state>}}}}
       unreachable            accept, then never answer (the probe times out:
                              an unplugged printer, so the guard falls back to Obico)
       reset                  close the connection without a reply ("reachable,
                              state unknown": NOT the Obico path)
       garbage                200 with a body that is not JSON
POST /_mock/state {"state": s}   change the state at runtime (a print starts mid-step)
GET  /_mock/requests          JSON list of every probe and the state it was served
GET  /_mock/health            readiness; not logged

  python3 moonraker_mock.py      (MOCK_STATE, default standby; port 7125)
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("unreachable", "reset", "garbage")


def make_server(host: str, port: int, state: str, hang_s: float = 60.0) -> ThreadingHTTPServer:
    lock = threading.Lock()
    box = {"state": state, "log": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/_mock/health":
                return self._send(200, b'{"ok": true}')
            if self.path == "/_mock/requests":
                with lock:
                    return self._send(200, json.dumps(box["log"]).encode())
            if not self.path.startswith("/printer/objects/query"):
                return self._send(404, b'{"error": "not found"}')
            with lock:
                served = box["state"]
                box["log"].append({"t": time.time(), "method": "GET", "path": self.path, "served": served})
            if served == "unreachable":
                time.sleep(hang_s)  # longer than the guard's 5 s probe timeout
                self.close_connection = True
                return
            if served == "reset":
                self.close_connection = True
                self.connection.close()
                return
            if served == "garbage":
                return self._send(200, b"<html>not moonraker{", "text/html")
            body = {"result": {"status": {"print_stats": {"state": served}}}}
            return self._send(200, json.dumps(body).encode())

        def do_POST(self):
            if self.path != "/_mock/state":
                return self._send(404, b'{"error": "not found"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                new = json.loads(self.rfile.read(n))["state"]
                if not isinstance(new, str) or not new:
                    raise ValueError("state must be a non-empty string")
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
            with lock:
                box["state"] = new
            return self._send(200, json.dumps({"state": new}).encode())

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    srv = make_server("0.0.0.0", int(os.environ.get("MOCK_PORT", "7125")),
                      os.environ.get("MOCK_STATE", "standby"))
    print(f"moonraker-mock: state={os.environ.get('MOCK_STATE', 'standby')}", flush=True)
    srv.serve_forever()
