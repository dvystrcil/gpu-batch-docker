"""Tests for the compose harness mocks (gpu-batch-docker#10 AC2).

The mocks are what the harness's verdict rests on, so they are tested on
their own: a mock that always says "standby" would make every guard case
pass vacuously.
"""
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "mocks"))
import moonraker_mock  # noqa: E402
import notify_sink  # noqa: E402

from gpu_batch import guard  # noqa: E402


def _serve(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def moonraker():
    srv = moonraker_mock.make_server("127.0.0.1", 0, "standby", hang_s=3)
    url = _serve(srv)
    yield url
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def sink(tmp_path):
    srv = notify_sink.make_server("127.0.0.1", 0, tmp_path / "_notify")
    url = _serve(srv)
    yield url, tmp_path / "_notify", srv
    srv.shutdown()
    srv.server_close()


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def _post(url, body, timeout=5):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _set_state(url, state):
    return _post(url + "/_mock/state", {"state": state})


# ------------------------------------------------------------ moonraker mock

def test_serves_the_initial_state_in_moonrakers_shape(moonraker):
    p = guard.probe_moonraker(moonraker)
    assert (p.kind, p.state) == ("state", "standby")


def test_state_changes_at_runtime(moonraker):
    assert guard.probe_moonraker(moonraker).state == "standby"
    assert _set_state(moonraker, "printing")[0] == 200
    assert guard.probe_moonraker(moonraker).state == "printing"
    # and the guard really refuses on it
    d = guard.decide(guard.probe_moonraker(moonraker), lambda: None, override=False)
    assert d.allow is False and "printing" in d.reason


def test_request_log_records_probes_with_the_state_served(moonraker):
    guard.probe_moonraker(moonraker)
    _set_state(moonraker, "printing")
    guard.probe_moonraker(moonraker)
    log = json.loads(_get(moonraker + "/_mock/requests")[1])
    assert [e["served"] for e in log] == ["standby", "printing"]
    assert all(e["path"].startswith("/printer/objects/query") for e in log)


def test_control_and_health_calls_are_not_logged(moonraker):
    _get(moonraker + "/_mock/health")
    _set_state(moonraker, "standby")
    assert json.loads(_get(moonraker + "/_mock/requests")[1]) == []


def test_unreachable_never_answers_so_the_guard_sees_no_answer(moonraker):
    # Closing the socket would be a RST/EOF: the guard calls that "reachable,
    # state unknown" and never reaches the Obico fallback. No answer at all
    # is what an unplugged printer looks like.
    _set_state(moonraker, "unreachable")
    t0 = time.monotonic()
    p = guard.probe_moonraker(moonraker, timeout=1.0)
    assert p.kind == "unreachable", p
    assert time.monotonic() - t0 >= 0.9


def test_reset_closes_without_a_reply_which_is_unknown_not_unreachable(moonraker):
    _set_state(moonraker, "reset")
    p = guard.probe_moonraker(moonraker, timeout=2.0)
    assert p.kind == "unknown", p


def test_garbage_is_an_unparseable_reply(moonraker):
    _set_state(moonraker, "garbage")
    p = guard.probe_moonraker(moonraker)
    assert p.kind == "unknown" and "unparseable" in p.detail


def test_modes_are_logged_too(moonraker):
    _set_state(moonraker, "garbage")
    guard.probe_moonraker(moonraker)
    assert [e["served"] for e in json.loads(_get(moonraker + "/_mock/requests")[1])] == ["garbage"]


@pytest.mark.parametrize("body", [b"not json", b'{"nostate": 1}', b'{"state": 3}', b'{"state": ""}'])
def test_bad_state_requests_are_rejected_and_change_nothing(moonraker, body):
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(moonraker + "/_mock/state", body)
    assert e.value.code == 400
    assert guard.probe_moonraker(moonraker).state == "standby"


def test_unknown_path_is_404(moonraker):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(moonraker + "/nope")
    assert e.value.code == 404


# --------------------------------------------------------------- notify sink

def test_sink_writes_each_body_by_id_and_returns_200(sink):
    url, out, _ = sink
    status = {"id": "freecad", "state": "succeeded", "outputs": [{"path": "out/part.stl"}]}
    assert _post(url + "/webhook/gpu-batch-complete", status)[0] == 200
    assert json.loads((out / "freecad.json").read_text()) == status


def test_sink_is_what_the_runner_notifier_talks_to(sink):
    from gpu_batch import runner
    url, out, _ = sink
    runner.http_notifier(url + "/webhook/gpu-batch-complete", attempts=1)({"id": "blender", "state": "refused"})
    assert json.loads((out / "blender.json").read_text())["state"] == "refused"


def test_sink_refuses_a_path_in_the_id(sink):
    url, out, _ = sink
    for bad in ["../x", "a/b", "", None, 5]:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(url + "/webhook/gpu-batch-complete", {"id": bad})
        assert e.value.code == 400
    assert not out.exists() or list(out.iterdir()) == []


def test_sink_refuses_non_json(sink):
    url, _, _ = sink
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(url + "/webhook/gpu-batch-complete", b"nope")
    assert e.value.code == 400


def test_sink_wrong_path_is_404(sink):
    url, out, _ = sink
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(url + "/webhook/other", {"id": "x"})
    assert e.value.code == 404
    assert not (out / "x.json").exists()


def test_sink_fail_mode_returns_the_code_and_writes_nothing(tmp_path):
    srv = notify_sink.make_server("127.0.0.1", 0, tmp_path / "_notify", fail_status=500)
    url = _serve(srv)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(url + "/webhook/gpu-batch-complete", {"id": "x"})
        assert e.value.code == 500
        assert not (tmp_path / "_notify" / "x.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_sink_counts_repeat_deliveries(sink):
    # The runner POSTs once per job; a second body for the same id means a
    # double report, which the assert step must be able to see.
    url, out, _ = sink
    for _ in range(2):
        _post(url + "/webhook/gpu-batch-complete", {"id": "dup", "state": "succeeded"})
    assert json.loads((out / "dup.json").read_text())["state"] == "succeeded"
    assert (out / "dup.count").read_text() == "2"
