"""main() end to end over real HTTP: env -> guard -> steps -> notify.

The unit tests inject the guard and notifier. This wires the real ones
from the environment the Job sets, against a fake moonraker and a fake n8n
receiver, so a wiring mistake (wrong env name, notifier never called,
refusal not posted) fails here rather than on node-03.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gpu_batch import runner


class Fake:
    def __init__(self):
        self.printer_state = "standby"
        self.posts = []

        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                assert self.path == "/printer/objects/query?print_stats=state"
                body = json.dumps({"result": {"status": {"print_stats": {"state": fake.printer_state}}}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                fake.posts.append(json.loads(self.rfile.read(n)))
                self.send_response(200)
                self.end_headers()

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setenv("MOONRAKER_URL", f.url)
    monkeypatch.setenv("GPU_BATCH_NOTIFY_URL", f.url + "/webhook/gpu-batch-complete")
    monkeypatch.setenv("GPU_BATCH_DEADLINE_S", "600")
    yield f
    f.srv.shutdown()


def job(tmp_path, code):
    d = tmp_path / "j"
    d.mkdir()
    (d / "spec.json").write_text(json.dumps({
        "id": "j", "steps": [{"name": "s", "argv": [sys.executable, "-c", code, "{job}/out.txt"]}],
        "expect_outputs": ["out.txt"], "min_outputs": 1, "notify": {"github": "dvystrcil/homelab#1935"},
    }))
    return d


def test_idle_printer_runs_and_posts_success(fake, tmp_path):
    d = job(tmp_path, "import sys; open(sys.argv[1],'w').write('ok')")
    assert runner.main([str(d / "spec.json")]) == 0
    assert [p["state"] for p in fake.posts] == ["succeeded"]
    assert fake.posts[0]["notify"] == {"github": "dvystrcil/homelab#1935"}
    assert "standby" in fake.posts[0]["guard"]


def test_printing_refuses_and_posts_refusal(fake, tmp_path):
    fake.printer_state = "printing"
    d = job(tmp_path, "import sys; open(sys.argv[1],'w').write('ok')")
    assert runner.main([str(d / "spec.json")]) == runner.EXIT_OK  # reported: the Job is Complete
    assert not (d / "out.txt").exists()
    assert [p["state"] for p in fake.posts] == ["refused"]
    assert "printing" in fake.posts[0]["reason"]


def test_failing_step_posts_failure_with_log_tail(fake, tmp_path):
    d = job(tmp_path, "import sys; sys.stderr.write('Traceback: boom\\n'); sys.exit(1)")
    assert runner.main([str(d / "spec.json")]) == runner.EXIT_OK  # reported
    assert fake.posts[0]["state"] == "failed"
    assert "boom" in fake.posts[0]["log_tail"]


def test_unreachable_notifier_fails_the_job(fake, tmp_path, monkeypatch):
    # The one case k8s must see as Failed: the result reached nobody.
    monkeypatch.setenv("GPU_BATCH_NOTIFY_URL", "http://127.0.0.1:9/webhook/gpu-batch-complete")
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)  # skip retry backoff
    d = job(tmp_path, "import sys; open(sys.argv[1],'w').write('ok')")
    assert runner.main([str(d / "spec.json")]) == runner.EXIT_UNREPORTED_SUCCESS
    st = json.loads((d / "status.json").read_text())
    assert st["state"] == "succeeded" and st["notified"] is False
