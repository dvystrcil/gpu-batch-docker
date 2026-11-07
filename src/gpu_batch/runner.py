"""GPU batch step runner: guard -> steps -> prove outputs -> status.json -> notify.

Contract (homelab#1935). A job is a directory on the shared work volume:

    <job>/spec.json     written by the submitter (bin/gpu-batch-submit)
    <job>/job.log       full stdout of every step (written here)
    <job>/job.err       full stderr of every step, kept apart so a buffered
                        stdout cannot push the actual error out of the tail
    <job>/status.json   the result (written here, atomically, at every transition)

    The runner's LAST stdout line (the pod log) is "GPU-BATCH-STATUS " plus the
    final status.json on one line, for readers that cannot see the work share
    (an n8n chain, homelab#1961).

spec.json:
    {
      "id": "...",
      "steps": [{"name": "render", "argv": ["blender", "-b", ...], "timeout_s": 3000}],
      "expect_outputs": ["out/*.png"],     # globs relative to the job dir
      "min_outputs": 2,
      "notify": {"github": "owner/repo#N"} | null,
      "printer_off": false
    }

`{job}` in any argv element expands to the job directory.

The success signal means the images exist: a step exiting 0 with no outputs
is a failure (async-ack-is-not-success). Every exit path writes status.json
and pushes it to the notifier exactly once, so a failure reports through the
same channel as a success. A job that the guard refuses runs no step.

Steps are multi-entry on purpose. Work that runs inside one image (render,
then encode, then a contact sheet) is more steps, not a new engine. Work
that needs different images per step is the point at which Argo Workflows
takes over, running this same runner per step. See
homelab/architecture/gpu-batch-lane.md.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Exit code = DELIVERY, not outcome. Once the result has reached the notifier
# (the comment is posted), the runner exits 0 whatever the state: status.json
# and the comment carry the outcome. A non-zero exit, and so a k8s Failed Job
# and a KubeJobFailed alert, means exactly one thing: nobody was told. The
# code then says what nobody was told about. (homelab#1941: a refusal that
# was already reported on the issue also paged Alertmanager.)
EXIT_OK, EXIT_FAILED, EXIT_UNREPORTED_SUCCESS, EXIT_REFUSED = 0, 1, 2, 3
_UNREPORTED_EXIT = {"succeeded": EXIT_UNREPORTED_SUCCESS, "refused": EXIT_REFUSED}
LOG_TAIL_CHARS = 4000
# The runner's last stdout line: "GPU-BATCH-STATUS " + status.json as one line.
STATUS_LINE_PREFIX = "GPU-BATCH-STATUS "
# The Job's activeDeadlineSeconds kills the pod with no chance to report, so
# the runner gives up this long before it and reports the timeout itself.
DEADLINE_MARGIN_S = 120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Job:
    def __init__(self, spec_path: Path):
        self.dir = spec_path.parent
        self.status = {
            "id": self.dir.name, "state": "starting", "reason": "", "job_dir": str(self.dir),
            "started": _now(), "finished": None, "steps": [], "outputs": [],
            "log_tail": "", "notify": None, "notified": None, "notify_error": None,
            "image": os.environ.get("GPU_BATCH_IMAGE"), "node": os.environ.get("NODE_NAME") or socket.gethostname(),
            "guard": None,
        }
        self.log_path = self.dir / "job.log"
        self.err_path = self.dir / "job.err"

    def write(self) -> None:
        tmp = self.dir / f"status.json.{os.getpid()}"
        tmp.write_text(json.dumps(self.status, indent=2) + "\n")
        os.replace(tmp, self.dir / "status.json")

    @staticmethod
    def _tail_of(path: Path, n: int) -> str:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - n * 4))
                text = f.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return ""
        return text[-n:]

    def tail(self) -> str:
        # stderr first and guaranteed room: that is where the error is.
        err = self._tail_of(self.err_path, LOG_TAIL_CHARS // 2).strip("\n")
        head = f"--- stderr (tail) ---\n{err}\n--- stdout (tail) ---\n" if err else ""
        out = self._tail_of(self.log_path, LOG_TAIL_CHARS - len(head))
        return (head + out)[-LOG_TAIL_CHARS:]


def load_spec(path: Path) -> dict:
    spec = json.loads(path.read_text())
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("spec.steps must be a non-empty list")
    for s in steps:
        if not (isinstance(s, dict) and isinstance(s.get("name"), str)
                and isinstance(s.get("argv"), list) and s["argv"]
                and all(isinstance(a, str) for a in s["argv"])):
            raise ValueError(f"spec step malformed: {s!r}")
    if not isinstance(spec.get("expect_outputs"), list) or not spec["expect_outputs"]:
        raise ValueError("spec.expect_outputs must be a non-empty list of globs")
    return spec


# ------------------------------------------------------------------ outputs

def _png_dims(data: bytes):
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return (w, h) if w and h else None


def check_outputs(job_dir: Path, globs: list[str], minimum: int):
    good, bad = [], []
    for pattern in globs:
        for p in sorted(glob.glob(str(job_dir / pattern))):
            path = Path(p)
            if not path.is_file():
                continue
            data = path.read_bytes()
            rel = str(path.relative_to(job_dir))
            if not data:
                bad.append(f"{rel} (empty)")
                continue
            entry = {"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            if path.suffix.lower() == ".png":
                dims = _png_dims(data)
                if not dims:
                    bad.append(f"{rel} (not a valid PNG)")
                    continue
                entry["width"], entry["height"] = dims
            good.append(entry)
    problems = []
    if len(good) < minimum:
        problems.append(f"expected outputs missing: found {len(good)} of {minimum} matching {globs}")
    if bad:
        problems.append("invalid outputs: " + ", ".join(bad))
    return good, "; ".join(problems)


# -------------------------------------------------------------------- steps

def _raise_oom_score():
    # Make the render, not this wrapper, the OOM killer's first choice, so
    # the wrapper survives to report the kill.
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass
    os.setsid()


def run_step(step: dict, job: Job, timeout_s: float, watch, watch_interval_s: float):
    argv = [a.replace("{job}", str(job.dir)) for a in step["argv"]]
    record = {"name": step["name"], "argv": argv, "started": _now(), "returncode": None}
    job.status["steps"].append(record)
    with open(job.log_path, "ab") as log, open(job.err_path, "ab") as err:
        for f in (log, err):
            f.write(f"\n=== step {step['name']}: {' '.join(argv)}\n".encode())
            f.flush()
        try:
            proc = subprocess.Popen(argv, stdout=log, stderr=err,
                                    cwd=job.dir, preexec_fn=_raise_oom_score)
        except OSError as e:
            record["returncode"] = 127
            return "failed", f"step {step['name']}: cannot start {argv[0]}: {e}"

        abort = {}
        stop = threading.Event()

        def watcher():
            while not stop.wait(watch_interval_s):
                why = watch()
                if why:
                    abort["why"] = why
                    _killpg(proc)
                    return

        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _killpg(proc)
            proc.wait()
            record["returncode"] = proc.returncode
            return "failed", f"step {step['name']} timed out after {int(timeout_s)}s"
        finally:
            stop.set()
            t.join(timeout=5)
        _killpg(proc)  # reap anything the step left behind in its group
    record["returncode"] = rc
    record["finished"] = _now()
    if "why" in abort:
        return "aborted", f"step {step['name']} stopped: {abort['why']}"
    if rc == 0:
        return "ok", ""
    if rc == -signal.SIGKILL or rc == 137:
        return "failed", f"step {step['name']} killed by SIGKILL (probable OOM: check the memory limit)"
    if rc < 0:
        return "failed", f"step {step['name']} killed by signal {signal.Signals(-rc).name}"
    return "failed", f"step {step['name']} failed with exit {rc}"


def _killpg(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# --------------------------------------------------------------------- main

def run(spec_path: Path, guard, notifier, watch=lambda: None, deadline_s: float | None = None,
        watch_interval_s: float = 60.0) -> int:
    spec_path = Path(spec_path)
    job = Job(spec_path)
    t0 = time.monotonic()
    state, reason = "failed", ""
    try:
        spec = load_spec(spec_path)
        job.status["id"] = spec.get("id", job.status["id"])
        job.status["notify"] = spec.get("notify")
        job.status["state"] = "guard"
        job.write()

        decision = guard(override=bool(spec.get("printer_off")))
        job.status["guard"] = decision.reason
        if not decision.allow:
            state, reason = "refused", decision.reason
        else:
            job.status["state"] = "running"
            job.write()
            state = "ok"
            for step in spec["steps"]:
                limit = float(step.get("timeout_s", 3600))
                if deadline_s is not None:
                    limit = max(1.0, min(limit, deadline_s - (time.monotonic() - t0)))
                state, reason = run_step(step, job, limit, watch, watch_interval_s)
                job.write()
                if state != "ok":
                    break
            if state == "ok":
                outputs, problem = check_outputs(job.dir, spec["expect_outputs"], int(spec.get("min_outputs", 1)))
                job.status["outputs"] = outputs
                if problem:
                    state, reason = "failed", f"steps exited 0 but {problem}"
                else:
                    state = "succeeded"
                    reason = f"{len(outputs)} output(s) verified"
    except (ValueError, KeyError, TypeError) as e:
        state, reason = "failed", f"bad spec: {type(e).__name__}: {e}"
    except Exception as e:  # report, never die silently
        state, reason = "failed", f"runner error: {type(e).__name__}: {e}"

    job.status.update(state=state, reason=reason, finished=_now(), log_tail=job.tail())
    try:
        notifier(job.status)
        job.status["notified"] = True
    except Exception as e:
        job.status["notified"], job.status["notify_error"] = False, f"{type(e).__name__}: {e}"
    job.write()
    print(f"GPU-BATCH-RESULT {job.status['id']} {state}: {reason}"
          + ("" if job.status["notified"] else f" (NOT REPORTED: {job.status['notify_error']})"), flush=True)
    # LAST line, always: status.json itself on one line (json.dumps escapes
    # every newline in the log tail). An n8n chain cannot see the work share,
    # so it reads the step's result here, from the pod log (homelab#1961).
    print(STATUS_LINE_PREFIX + json.dumps(job.status, separators=(",", ":")), flush=True)
    if job.status["notified"]:
        return EXIT_OK
    return _UNREPORTED_EXIT.get(state, EXIT_FAILED)


def http_notifier(url: str, attempts: int = 3):
    def send(status: dict) -> None:
        body = json.dumps(status).encode()
        last = None
        for i in range(attempts):
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    if 200 <= r.status < 300:
                        return
                    last = RuntimeError(f"HTTP {r.status}")
            except Exception as e:
                last = e
            time.sleep(2 ** i)
        raise last
    return send


def _no_channel(status: dict) -> None:
    raise RuntimeError("GPU_BATCH_NOTIFY_URL is not set: the result reached no channel")


def main(argv=None) -> int:
    from gpu_batch import guard as guard_mod

    ap = argparse.ArgumentParser(prog="gpu-batch-run", description=__doc__.split("\n\n")[0])
    ap.add_argument("spec", type=Path, help="path to <job>/spec.json on the work volume")
    args = ap.parse_args(argv)

    check, watch = guard_mod.from_env()
    url = os.environ.get("GPU_BATCH_NOTIFY_URL")
    notifier = http_notifier(url) if url else _no_channel
    deadline = os.environ.get("GPU_BATCH_DEADLINE_S")
    deadline_s = float(deadline) - DEADLINE_MARGIN_S if deadline else None
    return run(args.spec, guard=check, notifier=notifier, watch=watch, deadline_s=deadline_s)


if __name__ == "__main__":
    sys.exit(main())
