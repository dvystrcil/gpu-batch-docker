"""Step runner (homelab#1935 AC2/AC4).

Exit code = DELIVERY, not outcome. A result that reached the notifier exits
0 whatever its state (status.json and the comment carry the outcome), so a
k8s Failed Job means exactly one thing: the result did NOT reach the
channel. That is what KubeJobFailed should page on; a refused render that
was already reported is not (homelab#1941).

The contract: a job directory on the shared volume holds spec.json. The
runner checks the guard, runs each step, then proves the expected outputs
exist before calling the job a success. Whatever happens it writes
status.json and pushes that status to the notifier once.

A step exiting 0 is NOT success. Success means the images are there.
"""
import json
import os
import signal
import struct
import sys
import zlib
from pathlib import Path

import pytest

from gpu_batch import runner
from gpu_batch.guard import Decision

PY = sys.executable


def png_bytes(w=4, h=3):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


WRITE_PNGS = (
    "import sys,pathlib; out=pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)\n"
    f"data={png_bytes()!r}\n"
    "[(out/f'v{i}.png').write_bytes(data) for i in range(2)]; print('rendered 2')\n"
)


class Recorder:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, status):
        self.sent.append(json.loads(json.dumps(status)))
        if self.fail:
            raise OSError("n8n down")


def allow(**_):
    return Decision(True, "test: printer idle")


def refuse(**_):
    return Decision(False, "test: Neptune is printing")


def make_job(tmp_path, steps, expect=("out/*.png",), min_outputs=2, notify="dvystrcil/vt350#38"):
    job = tmp_path / "job-1"
    job.mkdir()
    spec = {
        "id": "job-1",
        "steps": steps,
        "expect_outputs": list(expect),
        "min_outputs": min_outputs,
        "notify": {"github": notify} if notify else None,
        "printer_off": False,
    }
    (job / "spec.json").write_text(json.dumps(spec))
    return job


def run(job, guard=allow, notifier=None, **kw):
    notifier = notifier or Recorder()
    kw.setdefault("watch", lambda: None)
    rc = runner.run(job / "spec.json", guard=guard, notifier=notifier, **kw)
    status = json.loads((job / "status.json").read_text())
    return rc, status, notifier


def py_step(code, *args, name="render", timeout_s=60):
    return {"name": name, "argv": [PY, "-c", code, *args], "timeout_s": timeout_s}


# ------------------------------------------------------------------ success

def test_success_reports_each_output_with_size_and_hash(tmp_path):
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")])
    rc, st, n = run(job)
    assert rc == 0
    assert st["state"] == "succeeded"
    assert sorted(o["path"] for o in st["outputs"]) == ["out/v0.png", "out/v1.png"]
    assert all(o["bytes"] > 0 and len(o["sha256"]) == 64 for o in st["outputs"])
    assert all(o["width"] == 4 and o["height"] == 3 for o in st["outputs"])
    assert n.sent[-1]["state"] == "succeeded"
    assert n.sent[-1]["notify"] == {"github": "dvystrcil/vt350#38"}
    assert n.sent[-1]["job_dir"] == str(job)


def test_job_placeholder_expands_in_argv(tmp_path):
    job = make_job(tmp_path, [py_step("import sys; open(sys.argv[1],'w').write('x')", "{job}/marker")],
                   expect=("marker",), min_outputs=1)
    rc, st, _ = run(job)
    assert rc == 0, st
    assert (job / "marker").read_text() == "x"


def test_status_says_running_while_the_step_runs(tmp_path):
    code = ("import json,sys; s=json.load(open(sys.argv[1])); assert s['state']=='running', s;"
            "import pathlib; pathlib.Path(sys.argv[2]).write_text('ok')")
    job = make_job(tmp_path, [py_step(code, "{job}/status.json", "{job}/seen")],
                   expect=("seen",), min_outputs=1)
    rc, st, _ = run(job)
    assert rc == 0, st["log_tail"]


def test_multiple_steps_run_in_order(tmp_path):
    job = make_job(tmp_path, [
        py_step(WRITE_PNGS, "{job}/out", name="render"),
        py_step("import sys,os; n=len(os.listdir(sys.argv[1])); open(sys.argv[2],'w').write(str(n))",
                "{job}/out", "{job}/count.txt", name="count"),
    ], expect=("out/*.png", "count.txt"), min_outputs=3)
    rc, st, _ = run(job)
    assert rc == 0, st
    assert (job / "count.txt").read_text() == "2"
    assert [s["name"] for s in st["steps"]] == ["render", "count"]
    assert all(s["returncode"] == 0 for s in st["steps"])


# ------------------------------------------------------------------ failure

def test_exit_zero_without_outputs_is_failure(tmp_path):
    job = make_job(tmp_path, [py_step("print('pretended to render')")])
    rc, st, n = run(job)
    assert rc == runner.EXIT_OK
    assert st["state"] == "failed"
    assert "expected outputs" in st["reason"]
    assert "pretended to render" in st["log_tail"]
    assert n.sent[-1]["state"] == "failed"


def test_fewer_outputs_than_min_is_failure(tmp_path):
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")], min_outputs=3)
    rc, st, _ = run(job)
    assert rc == runner.EXIT_OK and "2 of 3" in st["reason"]


def test_empty_or_corrupt_png_is_not_an_output(tmp_path):
    code = ("import pathlib,sys; o=pathlib.Path(sys.argv[1]); o.mkdir();"
            "(o/'a.png').write_bytes(b''); (o/'b.png').write_bytes(b'not a png at all')")
    job = make_job(tmp_path, [py_step(code, "{job}/out")], min_outputs=1)
    rc, st, _ = run(job)
    assert rc == runner.EXIT_OK
    assert "a.png" in st["reason"] and "b.png" in st["reason"]


def test_failing_step_reports_exit_code_and_log_tail(tmp_path):
    code = "import sys\nfor i in range(500): print('line', i)\nsys.stderr.write('Error: CUDA out of memory\\n'); sys.exit(3)"
    job = make_job(tmp_path, [py_step(code), py_step(WRITE_PNGS, "{job}/out", name="never")])
    rc, st, n = run(job)
    assert rc == runner.EXIT_OK
    assert st["state"] == "failed"
    assert "render" in st["reason"] and "exit 3" in st["reason"]
    assert "CUDA out of memory" in st["log_tail"]
    assert "line 499" in st["log_tail"] and "line 0\n" not in st["log_tail"]
    assert [s["name"] for s in st["steps"]] == ["render"]  # stopped at the failure
    assert n.sent[-1]["log_tail"] == st["log_tail"]
    # the full logs are kept next to the status
    assert "line 0" in (job / "job.log").read_text()
    assert "CUDA out of memory" in (job / "job.err").read_text()


def test_sigkill_is_reported_as_probable_oom(tmp_path):
    code = "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"
    job = make_job(tmp_path, [py_step(code)])
    rc, st, _ = run(job)
    assert rc == runner.EXIT_OK
    assert "SIGKILL" in st["reason"] and "OOM" in st["reason"]


def test_step_timeout_kills_the_whole_process_group(tmp_path):
    # The step forks a child that would outlive a plain kill of the parent.
    code = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)")
    job = make_job(tmp_path, [py_step(code, "{job}/child.pid", timeout_s=1)])
    rc, st, _ = run(job)
    assert rc == runner.EXIT_OK
    assert "timed out after 1s" in st["reason"]
    child = int((job / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        for _ in range(50):
            os.kill(child, 0)
            import time; time.sleep(0.05)
        os.kill(child, 0)


def test_deadline_caps_step_timeouts(tmp_path):
    # The Job's activeDeadlineSeconds kills the pod without a report, so the
    # runner must give up first and report the timeout itself.
    job = make_job(tmp_path, [py_step("import time; time.sleep(60)", timeout_s=3600)])
    rc, st, _ = run(job, deadline_s=1)
    assert rc == runner.EXIT_OK and "timed out after 1s" in st["reason"]


def test_missing_binary_is_a_failure_not_a_crash(tmp_path):
    job = make_job(tmp_path, [{"name": "render", "argv": ["/nonexistent/blender"], "timeout_s": 5}])
    rc, st, n = run(job)
    assert rc == runner.EXIT_OK and "nonexistent" in st["reason"]
    assert n.sent[-1]["state"] == "failed"


def test_bad_spec_still_writes_status_and_notifies(tmp_path):
    job = tmp_path / "job-1"
    job.mkdir()
    (job / "spec.json").write_text('{"id": "job-1", "steps": "nope"}')
    rc, st, n = run(job)
    assert rc == runner.EXIT_OK and st["state"] == "failed" and "spec" in st["reason"]
    assert n.sent[-1]["state"] == "failed"


# ------------------------------------------------------------------- guard

def test_refused_job_runs_no_steps(tmp_path):
    job = make_job(tmp_path, [py_step("open('/dev/null')", name="render")])
    rc, st, n = run(job, guard=refuse)
    assert rc == runner.EXIT_OK
    assert st["state"] == "refused"
    assert "printing" in st["reason"]
    assert st["steps"] == []
    assert n.sent[-1]["state"] == "refused"


def test_guard_receives_the_printer_off_flag(tmp_path):
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")])
    spec = json.loads((job / "spec.json").read_text())
    spec["printer_off"] = True
    (job / "spec.json").write_text(json.dumps(spec))
    seen = {}

    def g(override):
        seen["override"] = override
        return Decision(True, "ok")
    run(job, guard=g)
    assert seen["override"] is True


def test_print_starting_mid_run_aborts_the_step(tmp_path):
    job = make_job(tmp_path, [py_step("import time; time.sleep(30)", timeout_s=60)])
    calls = []

    def watch():
        calls.append(1)
        return "Neptune started printing" if len(calls) >= 2 else None
    rc, st, _ = run(job, watch=watch, watch_interval_s=0.1)
    assert rc == runner.EXIT_OK
    assert st["state"] == "aborted"
    assert "started printing" in st["reason"]


# ------------------------------------------------------------------ notify

def test_notify_failure_is_recorded_but_does_not_change_the_result(tmp_path):
    # The STATE stays succeeded; only the exit code says nobody was told.
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")])
    rc, st, _ = run(job, notifier=Recorder(fail=True))
    assert rc == runner.EXIT_UNREPORTED_SUCCESS
    assert st["state"] == "succeeded"
    assert st["notified"] is False and "n8n down" in st["notify_error"]


def test_notified_true_on_success(tmp_path):
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")])
    _, st, _ = run(job)
    assert st["notified"] is True


def test_no_notify_target_still_notifies_with_null(tmp_path):
    # n8n records every completion; the GitHub comment is optional.
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")], notify=None)
    _, st, n = run(job)
    assert n.sent[-1]["notify"] is None


def test_log_tail_is_bounded(tmp_path):
    code = "print('x' * 200000)"
    job = make_job(tmp_path, [py_step(code)])
    _, st, _ = run(job)
    assert len(st["log_tail"]) <= runner.LOG_TAIL_CHARS


def test_status_json_is_written_atomically(tmp_path):
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")])
    run(job)
    assert not list(job.glob("status.json.*"))


# ------------------------------------------------------------ exit codes

@pytest.mark.parametrize("make,expect", [
    (lambda tmp: (make_job(tmp, [py_step(WRITE_PNGS, "{job}/out")]), allow), runner.EXIT_UNREPORTED_SUCCESS),
    (lambda tmp: (make_job(tmp, [py_step("import sys; sys.exit(3)")]), allow), runner.EXIT_FAILED),
    (lambda tmp: (make_job(tmp, [py_step(WRITE_PNGS, "{job}/out")]), refuse), runner.EXIT_REFUSED),
])
def test_an_unreported_result_exits_nonzero_with_its_state(tmp_path, make, expect):
    job, g = make(tmp_path)
    rc, st, _ = run(job, guard=g, notifier=Recorder(fail=True))
    assert st["notified"] is False
    assert rc == expect != runner.EXIT_OK


def test_unreported_exit_codes_are_distinct():
    codes = {runner.EXIT_OK, runner.EXIT_FAILED, runner.EXIT_UNREPORTED_SUCCESS, runner.EXIT_REFUSED}
    assert len(codes) == 4


def test_no_notify_url_is_not_reported(tmp_path, monkeypatch):
    # Without a channel nothing was delivered; claiming notified=true would
    # make the Job look Complete when nobody was told.
    monkeypatch.delenv("GPU_BATCH_NOTIFY_URL", raising=False)
    monkeypatch.setenv("MOONRAKER_URL", "http://127.0.0.1:9")
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")])
    rc = runner.main([str(job / "spec.json")])
    st = json.loads((job / "status.json").read_text())
    assert st["notified"] is False and "GPU_BATCH_NOTIFY_URL" in st["notify_error"]
    assert rc != runner.EXIT_OK


# ------------------------------------------- status on stdout (homelab#1961)
#
# An n8n chain cannot see the NFS work share. It reads the step's result from
# the pod log (the node's Get Job Log Tail), so the runner's LAST stdout line
# is status.json itself: one line, the same object, written after the final
# status.json. A Complete Job is not a successful step (exit code = delivery).

def status_line(out: str) -> dict:
    last = out.rstrip("\n").split("\n")[-1]
    prefix = "GPU-BATCH-STATUS "
    assert last.startswith(prefix), f"last stdout line is not the status: {last[:200]!r}"
    return json.loads(last[len(prefix):])


@pytest.mark.parametrize("make,guard", [
    (lambda tmp: make_job(tmp, [py_step(WRITE_PNGS, "{job}/out")]), allow),
    (lambda tmp: make_job(tmp, [py_step("import sys; print('boom', file=sys.stderr); sys.exit(3)")]), allow),
    (lambda tmp: make_job(tmp, [py_step(WRITE_PNGS, "{job}/out")]), refuse),
])
def test_last_stdout_line_is_the_final_status_json(tmp_path, capsys, make, guard):
    job = make(tmp_path)
    _, st, _ = run(job, guard=guard)
    printed = status_line(capsys.readouterr().out)
    assert printed == st  # byte-for-byte the object in status.json, notified included


def test_status_line_is_one_line_whatever_the_log_tail_holds(tmp_path, capsys):
    code = "import sys; print('a\\nb\\r\\nGPU-BATCH-STATUS {}', file=sys.stderr); sys.exit(1)"
    job = make_job(tmp_path, [py_step(code)])
    _, st, _ = run(job)
    out = capsys.readouterr().out
    assert out.count("\nGPU-BATCH-STATUS ") + out.startswith("GPU-BATCH-STATUS ") == 1
    assert status_line(out)["log_tail"] == st["log_tail"]


def test_status_line_reports_an_unreported_result_too(tmp_path, capsys):
    job = make_job(tmp_path, [py_step(WRITE_PNGS, "{job}/out")])
    run(job, notifier=Recorder(fail=True))
    printed = status_line(capsys.readouterr().out)
    assert printed["state"] == "succeeded" and printed["notified"] is False
