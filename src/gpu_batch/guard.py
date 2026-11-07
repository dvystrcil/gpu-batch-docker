"""Print-state guard: do not start GPU work while Neptune is printing.

Why: the GTX 970 is time-sliced with no VRAM isolation (homelab#1487). A
render that overruns can take obico-ml-api down with it, and obico-ml-api is
the print-failure detector. So renders must not run during a print
(homelab#1935 AC3).

Sources, in order:

1. Moonraker's print_stats (no auth, the same API the homepage widget reads).
   Authoritative whenever the printer answers.
2. Obico's app_printer.current_print_id. It is consulted ONLY when the printer
   cannot be reached, to tell "powered off" (NULL) from "network dropped
   mid-print" (still set). Obico's REST API takes only session/OAuth2 auth,
   and its one token is the printer's own identity, so this reads the
   database through a read-only role with column-level SELECT.

Every unknown fails CLOSED. A safety gate that fails open is not a gate.
The one escape hatch is the --printer-off override, and it applies only
while the printer is unreachable: a printer that answers disproves the claim.

CPU-only jobs (homelab#1974): a template may set GPU_BATCH_PRINT_GUARD=off
(exactly that value). It skips the guard only when this container cannot
reach a GPU, i.e. no /dev/nvidia<N> device and no NVIDIA_VISIBLE_DEVICES
naming one. A job with no GPU cannot touch the 970's VRAM. A visible GPU
keeps the guard on, so the switch set on a render (by anyone who can set a
Job's env) changes nothing.
"""
from __future__ import annotations

import base64
import errno
import json
import os
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

BUSY = frozenset({"printing", "paused"})
IDLE = frozenset({"standby", "complete", "cancelled", "error"})

# errnos that mean nothing answered: the printer is off or the path is down.
_NO_ANSWER = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN,
                        errno.ETIMEDOUT})

OVERRIDE_HINT = ("If Neptune is definitely powered off, resubmit with --printer-off "
                 "(accepted only while the printer is unreachable).")


@dataclass(frozen=True)
class PrinterProbe:
    kind: str            # "state" | "unreachable" | "unknown"
    state: str | None
    detail: str


@dataclass(frozen=True)
class ObicoState:
    kind: str            # "printing" | "idle" | "unknown"
    detail: str


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


# ----------------------------------------------------------------- decision

def decide(printer: PrinterProbe, obico: Callable[[], ObicoState], override: bool) -> Decision:
    if printer.kind == "state":
        if printer.state in BUSY:
            return Decision(False, f"Neptune is {printer.state} (moonraker)"
                            + ("; --printer-off override ignored, the printer is reachable" if override else ""))
        if printer.state in IDLE:
            return Decision(True, f"Neptune is {printer.state} (moonraker)")
        return Decision(False, f"moonraker reported unrecognised state {printer.state!r}; failing closed")

    if printer.kind != "unreachable":
        msg = f"Neptune is reachable but its print state is unknown ({printer.detail}); failing closed"
        if override:
            msg += "; --printer-off override ignored, the printer is reachable"
        return Decision(False, msg)

    if override:
        return Decision(True, f"Neptune unreachable ({printer.detail}); --printer-off override accepted")

    try:
        ob = obico()
    except Exception as e:  # a tie-breaker that crashes is an unknown, not a pass
        ob = ObicoState("unknown", f"{type(e).__name__}: {e}")
    if ob.kind == "idle":
        return Decision(True, f"Neptune unreachable ({printer.detail}) and Obico shows no active print")
    if ob.kind == "printing":
        return Decision(False, "Neptune unreachable but Obico still shows an active print "
                        "(network drop mid-print?). " + OVERRIDE_HINT)
    return Decision(False, f"Neptune unreachable and Obico print state unknown ({ob.detail}). " + OVERRIDE_HINT)


# ---------------------------------------------------------------- moonraker

def _no_answer(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, ConnectionRefusedError):
        return False  # a RST came back: the host is up
    if isinstance(exc, OSError) and exc.errno in _NO_ANSWER:
        return True
    return False


def probe_moonraker(base_url: str, timeout: float = 5.0, opener=None) -> PrinterProbe:
    opener = opener or (lambda url, timeout: urllib.request.urlopen(url, timeout=timeout))
    url = base_url.rstrip("/") + "/printer/objects/query?print_stats=state"
    try:
        with opener(url, timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        return PrinterProbe("unknown", None, f"HTTP {e.code} from moonraker")
    except urllib.error.URLError as e:
        reason = e.reason if isinstance(e.reason, BaseException) else OSError(str(e.reason))
        kind = "unreachable" if _no_answer(reason) else "unknown"
        return PrinterProbe(kind, None, f"{type(reason).__name__}: {reason}")
    except OSError as e:
        kind = "unreachable" if _no_answer(e) else "unknown"
        return PrinterProbe(kind, None, f"{type(e).__name__}: {e}")
    try:
        state = json.loads(body)["result"]["status"]["print_stats"]["state"]
    except (ValueError, KeyError, TypeError) as e:
        return PrinterProbe("unknown", None, f"unparseable moonraker reply ({type(e).__name__})")
    if not isinstance(state, str):
        return PrinterProbe("unknown", None, f"non-string print_stats.state {state!r}")
    return PrinterProbe("state", state, "moonraker print_stats")


# -------------------------------------------------------------------- obico

# Column-level grant on the obico side: id, name, current_print_id,
# archived_at. app_printer also holds auth_token, the printer's identity,
# which this role must never be able to read.
OBICO_SQL = ("SELECT current_print_id IS NOT NULL FROM app_printer "
             "WHERE name = :'printer' AND archived_at IS NULL;\n")


def parse_obico_rows(out: str) -> ObicoState:
    rows = [r.strip() for r in out.splitlines() if r.strip()]
    if len(rows) != 1:
        return ObicoState("unknown", f"expected 1 active printer row, got {len(rows)}")
    return {"t": ObicoState("printing", "current_print_id set"),
            "f": ObicoState("idle", "current_print_id NULL")}.get(
        rows[0], ObicoState("unknown", f"unexpected value {rows[0]!r}"))


def _run_psql(argv, env, timeout, stdin):
    p = subprocess.run(argv, env=env, input=stdin, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def query_obico(creds: dict, printer_name: str, run=_run_psql, timeout: float = 10.0) -> ObicoState:
    # Password via the environment only: argv is world-readable in /proc.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PGPASSWORD": creds["password"],
           "PGCONNECT_TIMEOUT": "5", "PGSSLMODE": "require"}
    argv = ["psql", "-h", creds["host"], "-p", str(creds.get("port", "5432")), "-U", creds["user"],
            "-d", creds.get("dbname", "obico"), "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
            "-v", f"printer={printer_name}", "-f", "-"]
    try:
        rc, out, err = run(argv, env, timeout, OBICO_SQL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return ObicoState("unknown", f"psql: {type(e).__name__}: {e}")
    if rc != 0:
        return ObicoState("unknown", f"psql exit {rc}: {err.strip()[-300:]}")
    return parse_obico_rows(out)


SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def read_k8s_secret(namespace: str, name: str, sa_dir: str = SA_DIR) -> dict:
    """Read a Secret through the API with the pod's own SA token.

    The obico repo grants this SA `get` on exactly one Secret, PGO's
    obico-postgres-pguser-gpubatch. Reading it live, not copying it, means a
    PGO password rotation needs nothing from this side.
    """
    token = open(os.path.join(sa_dir, "token")).read().strip()
    ctx = ssl.create_default_context(cafile=os.path.join(sa_dir, "ca.crt"))
    req = urllib.request.Request(
        f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/secrets/{name}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        data = json.load(r)["data"]
    return {k: base64.b64decode(v).decode() for k, v in data.items()}


# ------------------------------------------------------------------ wiring

_NO_GPU_VALUES = frozenset({"", "void", "none"})


def gpu_visible(env, dev_dir: str = "/dev") -> bool:
    """True if this container can reach an NVIDIA GPU."""
    visible = env.get("NVIDIA_VISIBLE_DEVICES")
    if visible is not None and visible.strip().lower() not in _NO_GPU_VALUES:
        return True
    try:
        names = os.listdir(dev_dir)
    except OSError:
        return True  # cannot tell: fail closed
    return any(n.startswith("nvidia") and n[len("nvidia"):].isdigit() for n in names)


def from_env(env=os.environ, dev_dir: str = "/dev"):
    """Build the guard the runner calls, configured from the pod env."""
    guard_off = env.get("GPU_BATCH_PRINT_GUARD") == "off"
    if guard_off and not gpu_visible(env, dev_dir):
        def skipped(override: bool) -> Decision:
            return Decision(True, "print guard off: CPU-only job, no GPU in this container")
        return skipped, lambda: None
    moonraker = env.get("MOONRAKER_URL", "http://192.168.86.243")
    printer = env.get("OBICO_PRINTER_NAME", "Neptune")
    secret = env.get("OBICO_PG_SECRET", "obico/obico-postgres-pguser-gpubatch")

    def obico() -> ObicoState:
        ns, name = secret.split("/", 1)
        return query_obico(read_k8s_secret(ns, name), printer)

    def check(override: bool) -> Decision:
        d = decide(probe_moonraker(moonraker), obico, override)
        if guard_off:
            return Decision(d.allow, f"GPU_BATCH_PRINT_GUARD=off ignored: a GPU is visible; {d.reason}")
        return d

    def watch() -> str | None:
        # Mid-run re-check: moonraker only, and only a definite busy state
        # aborts. An unreachable printer mid-render does not stop the render.
        p = probe_moonraker(moonraker)
        return f"Neptune started {p.state} during the render" if p.kind == "state" and p.state in BUSY else None

    return check, watch
