"""Print-state guard (homelab#1935 AC3).

The render must not start while Neptune is printing: a VRAM overrun on the
shared GTX 970 can kill obico-ml-api, which is the print-failure detector.

Moonraker answers first. Only when the printer cannot be reached at all does
Obico's `current_print_id` break the tie between "powered off" and "network
dropped mid-print".
"""
import errno
import io
import json
import socket
import urllib.error

import pytest

from gpu_batch import guard
from gpu_batch.guard import Decision, ObicoState, PrinterProbe


def probe(kind, state=None):
    return PrinterProbe(kind=kind, state=state, detail="test")


def obico(kind):
    return lambda: ObicoState(kind=kind, detail="test")


def obico_must_not_be_called():
    raise AssertionError("Obico is only the tie-breaker for an unreachable printer")


# --------------------------------------------------------------- decide()

@pytest.mark.parametrize("state", ["printing", "paused"])
def test_refuses_when_moonraker_says_busy(state):
    d = guard.decide(probe("state", state), obico_must_not_be_called, override=False)
    assert d == Decision(False, d.reason) and state in d.reason


@pytest.mark.parametrize("state", ["standby", "complete", "cancelled", "error"])
def test_allows_when_moonraker_says_idle(state):
    d = guard.decide(probe("state", state), obico_must_not_be_called, override=False)
    assert d.allow, d.reason


def test_unrecognised_moonraker_state_fails_closed():
    d = guard.decide(probe("state", "startup"), obico_must_not_be_called, override=False)
    assert not d.allow


def test_reachable_but_unreadable_fails_closed():
    d = guard.decide(probe("unknown"), obico_must_not_be_called, override=False)
    assert not d.allow
    assert "reachable" in d.reason


def test_override_does_not_apply_while_the_printer_is_reachable():
    # --printer-off is a claim the printer is OFF. A reachable printer
    # disproves it, so the override must not unlock an unknown state.
    d = guard.decide(probe("unknown"), obico_must_not_be_called, override=True)
    assert not d.allow
    assert "override ignored" in d.reason
    d = guard.decide(probe("state", "printing"), obico_must_not_be_called, override=True)
    assert not d.allow


def test_unreachable_and_obico_idle_allows():
    d = guard.decide(probe("unreachable"), obico("idle"), override=False)
    assert d.allow, d.reason


def test_unreachable_and_obico_printing_refuses():
    # The network-drop-mid-print case: current_print_id stays set.
    d = guard.decide(probe("unreachable"), obico("printing"), override=False)
    assert not d.allow
    assert "--printer-off" in d.reason  # the refusal tells you the way out


def test_unreachable_and_obico_unknown_refuses():
    d = guard.decide(probe("unreachable"), obico("unknown"), override=False)
    assert not d.allow


@pytest.mark.parametrize("obico_kind", ["printing", "unknown", "idle"])
def test_override_allows_only_when_unreachable(obico_kind):
    d = guard.decide(probe("unreachable"), obico(obico_kind), override=True)
    assert d.allow
    assert "override" in d.reason


def test_obico_raising_is_unknown_not_a_crash():
    def boom():
        raise RuntimeError("db down")
    d = guard.decide(probe("unreachable"), boom, override=False)
    assert not d.allow


# ------------------------------------------------------- probe_moonraker()

class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(body):
    return lambda url, timeout: FakeResp(body.encode())


def opener_raising(exc):
    def _open(url, timeout):
        raise exc
    return _open


def test_probe_reads_print_stats_state():
    body = json.dumps({"result": {"status": {"print_stats": {"state": "printing"}}}})
    p = guard.probe_moonraker("http://p", opener=opener_returning(body))
    assert (p.kind, p.state) == ("state", "printing")


def test_probe_queries_print_stats():
    seen = {}

    def _open(url, timeout):
        seen["url"] = url
        return FakeResp(json.dumps({"result": {"status": {"print_stats": {"state": "standby"}}}}).encode())
    guard.probe_moonraker("http://p/", opener=_open)
    assert seen["url"] == "http://p/printer/objects/query?print_stats=state"


@pytest.mark.parametrize("exc", [
    urllib.error.URLError(socket.timeout("timed out")),
    urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host")),
    urllib.error.URLError(OSError(errno.ENETUNREACH, "Network unreachable")),
    socket.timeout("timed out"),
    TimeoutError("timed out"),
])
def test_probe_no_answer_is_unreachable(exc):
    assert guard.probe_moonraker("http://p", opener=opener_raising(exc)).kind == "unreachable"


@pytest.mark.parametrize("exc", [
    # Refused means the host answered: it is ON, moonraker is just not up.
    urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused")),
    urllib.error.HTTPError("http://p", 503, "Klippy not ready", {}, None),
])
def test_probe_host_answered_but_no_state_is_unknown(exc):
    assert guard.probe_moonraker("http://p", opener=opener_raising(exc)).kind == "unknown"


@pytest.mark.parametrize("body", ["not json", "{}", '{"result": {"status": {}}}',
                                  '{"result": {"status": {"print_stats": {"state": 7}}}}'])
def test_probe_malformed_body_is_unknown(body):
    assert guard.probe_moonraker("http://p", opener=opener_returning(body)).kind == "unknown"


# ----------------------------------------------------------- query_obico()

def test_obico_query_maps_rows():
    assert guard.parse_obico_rows("t\n").kind == "printing"
    assert guard.parse_obico_rows("f\n").kind == "idle"
    assert guard.parse_obico_rows("").kind == "unknown"      # printer not found
    assert guard.parse_obico_rows("t\nf\n").kind == "unknown"  # ambiguous name
    assert guard.parse_obico_rows("x\n").kind == "unknown"


def test_obico_password_never_goes_on_argv(monkeypatch):
    seen = {}

    def fake_run(argv, env, timeout, stdin):
        seen["argv"], seen["env"], seen["stdin"] = argv, env, stdin
        return 0, "f\n", ""
    creds = {"host": "h", "port": "5432", "user": "gpubatch", "password": "s3cret"}
    r = guard.query_obico(creds, printer_name="Neptune", run=fake_run)
    assert r.kind == "idle"
    assert "s3cret" not in " ".join(seen["argv"])
    assert seen["env"]["PGPASSWORD"] == "s3cret"
    # The name is bound as a psql variable and quoted by psql (:'printer'),
    # never spliced into the SQL text.
    assert "printer=Neptune" in seen["argv"]
    assert ":'printer'" in seen["stdin"] and "Neptune" not in seen["stdin"]


def test_obico_psql_failure_is_unknown():
    r = guard.query_obico({"host": "h", "port": "5432", "user": "u", "password": "p"},
                          printer_name="Neptune", run=lambda a, env, timeout, stdin: (2, "", "FATAL: x"))
    assert r.kind == "unknown" and "FATAL" in r.detail


# ------------------------------------------- CPU-only jobs (homelab#1974)
#
# GPU_BATCH_PRINT_GUARD=off skips the guard, but ONLY when this container
# cannot reach a GPU. The guard protects the shared GTX 970's VRAM; a job
# with no GPU device cannot touch it. Anyone who can set env on a Job (the
# n8n node's env override, say) could set the switch on a render, so a
# visible GPU always keeps the guard on.

def _no_probe(*a, **k):
    raise AssertionError("the printer was probed")


def _guard_off_env(**extra):
    return {"GPU_BATCH_PRINT_GUARD": "off", **extra}


def test_guard_off_without_a_gpu_allows_without_probing(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "probe_moonraker", _no_probe)
    check, watch = guard.from_env(_guard_off_env(), dev_dir=str(tmp_path))
    d = check(override=False)
    assert d.allow is True
    assert "CPU-only" in d.reason and "no GPU" in d.reason
    assert watch() is None


def test_guard_off_is_ignored_when_a_gpu_is_assigned_by_env(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: calls.append(url) or PrinterProbe("state", "printing", ""))
    check, watch = guard.from_env(_guard_off_env(NVIDIA_VISIBLE_DEVICES="GPU-1234"), dev_dir=str(tmp_path))
    d = check(override=False)
    assert d.allow is False and calls
    assert "ignored" in d.reason and "GPU" in d.reason
    assert watch() is not None


def test_guard_off_is_ignored_when_a_gpu_device_exists(tmp_path, monkeypatch):
    (tmp_path / "nvidia0").touch()
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: PrinterProbe("state", "printing", ""))
    check, _ = guard.from_env(_guard_off_env(), dev_dir=str(tmp_path))
    assert check(override=False).allow is False


def test_nvidiactl_alone_is_not_a_gpu(tmp_path, monkeypatch):
    # Control nodes without a numbered device give no GPU access.
    (tmp_path / "nvidiactl").touch()
    (tmp_path / "loop1000000").touch()  # other devices with digits are not GPUs
    monkeypatch.setattr(guard, "probe_moonraker", _no_probe)
    check, _ = guard.from_env(_guard_off_env(), dev_dir=str(tmp_path))
    assert check(override=False).allow is True


@pytest.mark.parametrize("value", ["void", "none", "", "VOID", " none "])
def test_nvidia_visible_devices_meaning_no_gpu_counts_as_cpu_only(tmp_path, monkeypatch, value):
    monkeypatch.setattr(guard, "probe_moonraker", _no_probe)
    check, _ = guard.from_env(_guard_off_env(NVIDIA_VISIBLE_DEVICES=value), dev_dir=str(tmp_path))
    assert check(override=False).allow is True


@pytest.mark.parametrize("value", [None, "on", "OFF", "0", "false", "no", " off"])
def test_anything_but_exactly_off_keeps_the_guard(tmp_path, monkeypatch, value):
    calls = []
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: calls.append(url) or PrinterProbe("state", "printing", ""))
    env = {} if value is None else {"GPU_BATCH_PRINT_GUARD": value}
    check, _ = guard.from_env(env, dev_dir=str(tmp_path))
    d = check(override=False)
    assert d.allow is False and calls
    assert "ignored" not in d.reason


def test_unreadable_dev_dir_counts_as_a_gpu(tmp_path, monkeypatch):
    # Cannot tell whether a GPU is there: fail closed, keep the guard.
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: PrinterProbe("state", "printing", ""))
    check, _ = guard.from_env(_guard_off_env(), dev_dir=str(tmp_path / "missing"))
    assert check(override=False).allow is False


# argocd-projects#310: obico's Postgres moved from PGO to CNPG. PGO's
# obico-postgres-pguser-gpubatch is deleted with the PGO cluster (obico PR C),
# and the obico repo's Role grants `get` on the CNPG Secret only. A job that
# does not set OBICO_PG_SECRET must still reach a Secret that exists.

@pytest.mark.parametrize("env, expected", [
    ({}, ("obico", "obico-postgres-gpubatch")),
    ({"OBICO_PG_SECRET": "other/some-secret"}, ("other", "some-secret")),
])
def test_obico_secret_default_is_the_cnpg_one(monkeypatch, tmp_path, env, expected):
    read = []
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: PrinterProbe("unreachable", None, ""))
    monkeypatch.setattr(guard, "read_k8s_secret", lambda ns, name: read.append((ns, name)) or {})
    monkeypatch.setattr(guard, "query_obico", lambda creds, printer: ObicoState("idle", "test"))
    check, _ = guard.from_env(env, dev_dir=str(tmp_path))
    check(override=False)
    assert read == [expected]


# ------------------------------------------------ the values file (docs/using-the-batch-tools.md, gap 1)
#
# Someone running these images on their own needs the guard pointed at THEIR
# printer host and THEIR Obico (or none). GPU_BATCH_VALUES names a JSON file;
# its "print_guard" section configures the guard. Env vars still win over the
# file, so the cluster templates keep working unchanged.

def _values(tmp_path, **print_guard):
    p = tmp_path / "values.json"
    p.write_text(json.dumps({"print_guard": print_guard}))
    return {"GPU_BATCH_VALUES": str(p)}


def test_values_file_points_the_guard_at_another_moonraker(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: calls.append(url) or PrinterProbe("state", "standby", ""))
    check, _ = guard.from_env(_values(tmp_path, moonraker_url="http://my-printer:7125"), dev_dir=str(tmp_path))
    assert check(override=False).allow is True
    assert calls == ["http://my-printer:7125"]


def test_env_still_wins_over_the_values_file(tmp_path, monkeypatch):
    # The cluster templates set MOONRAKER_URL; a values file must not silently override them.
    calls = []
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: calls.append(url) or PrinterProbe("state", "standby", ""))
    env = {**_values(tmp_path, moonraker_url="http://from-file:7125"), "MOONRAKER_URL": "http://from-env:7125"}
    check, _ = guard.from_env(env, dev_dir=str(tmp_path))
    check(override=False)
    assert calls == ["http://from-env:7125"]


def test_no_printer_to_protect_runs_a_gpu_job_without_probing(tmp_path, monkeypatch):
    # Someone with a GPU and no printer host says so in the file, explicitly.
    (tmp_path / "nvidia0").touch()
    monkeypatch.setattr(guard, "probe_moonraker", _no_probe)
    check, watch = guard.from_env(_values(tmp_path, moonraker_url=None, fallback="none"), dev_dir=str(tmp_path))
    d = check(override=False)
    assert d.allow is True and "no printer" in d.reason
    assert watch() is None


def _obico_must_not_be_reached(*a, **k):
    raise AssertionError("fallback is none: Obico must not be contacted")


@pytest.mark.parametrize("when, allowed", [(None, False), ("refuse", False), ("allow", True)])
def test_without_a_fallback_when_unreachable_decides(tmp_path, monkeypatch, when, allowed):
    # Obico cloud users (or no Obico at all) have only Moonraker. Default stays fail-closed.
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: PrinterProbe("unreachable", None, "test"))
    monkeypatch.setattr(guard, "read_k8s_secret", _obico_must_not_be_reached)
    monkeypatch.setattr(guard, "query_obico", _obico_must_not_be_reached)
    pg = dict(moonraker_url="http://my-printer:7125", fallback="none")
    if when:
        pg["when_unreachable"] = when
    check, _ = guard.from_env(_values(tmp_path, **pg), dev_dir=str(tmp_path))
    d = check(override=False)
    assert d.allow is allowed
    assert "when_unreachable" in d.reason


def test_without_a_fallback_a_busy_printer_still_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: PrinterProbe("state", "printing", ""))
    pg = dict(moonraker_url="http://my-printer:7125", fallback="none", when_unreachable="allow")
    check, _ = guard.from_env(_values(tmp_path, **pg), dev_dir=str(tmp_path))
    assert check(override=False).allow is False


@pytest.mark.parametrize("print_guard", [
    {"fallback": "obico"},                                   # not a known fallback
    {"fallback": "none", "when_unreachable": "alow"},        # typo must not mean allow
    {"moonraker_url": None, "fallback": "obico-db"},         # Obico alone can't say "idle" for a reachable printer
    {"moonraker_url": 7125},                                 # not a URL
])
def test_a_bad_values_file_refuses_and_says_why(tmp_path, monkeypatch, print_guard):
    (tmp_path / "nvidia0").touch()
    monkeypatch.setattr(guard, "probe_moonraker", _no_probe)
    check, watch = guard.from_env(_values(tmp_path, **print_guard), dev_dir=str(tmp_path))
    d = check(override=True)                                 # not even the override gets past a broken config
    assert d.allow is False and "values file" in d.reason
    assert watch() is None


@pytest.mark.parametrize("content", [None, "{not json", "[]"])
def test_an_unreadable_values_file_refuses_instead_of_crashing(tmp_path, monkeypatch, content):
    p = tmp_path / "values.json"
    if content is not None:
        p.write_text(content)
    monkeypatch.setattr(guard, "probe_moonraker", _no_probe)
    check, _ = guard.from_env({"GPU_BATCH_VALUES": str(p)}, dev_dir=str(tmp_path))
    d = check(override=False)
    assert d.allow is False and "values file" in d.reason


def _unreachable_then_capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: PrinterProbe("unreachable", None, ""))
    monkeypatch.setattr(guard, "query_obico",
                        lambda creds, printer: seen.update(creds=creds, printer=printer) or ObicoState("idle", ""))
    return seen


CREDS = {"host": "obico-db.lan", "user": "guard_ro", "password": "s3cret", "dbname": "obico"}


def test_a_self_hosted_obico_login_from_a_file(tmp_path, monkeypatch):
    seen = _unreachable_then_capture(monkeypatch)
    monkeypatch.setattr(guard, "read_k8s_secret", _obico_must_not_be_reached)
    cred_file = tmp_path / "obico.json"
    cred_file.write_text(json.dumps(CREDS))
    env = _values(tmp_path, moonraker_url="http://p:7125",
                  obico={"printer_name": "MyPrinter", "credentials": f"file:{cred_file}"})
    check, _ = guard.from_env(env, dev_dir=str(tmp_path))
    assert check(override=False).allow is True
    assert seen == {"creds": CREDS, "printer": "MyPrinter"}


def test_a_self_hosted_obico_login_from_an_env_var(tmp_path, monkeypatch):
    seen = _unreachable_then_capture(monkeypatch)
    env = {**_values(tmp_path, moonraker_url="http://p:7125", obico={"credentials": "env:OBICO_LOGIN"}),
           "OBICO_LOGIN": json.dumps(CREDS)}
    check, _ = guard.from_env(env, dev_dir=str(tmp_path))
    assert check(override=False).allow is True
    assert seen["creds"] == CREDS


def test_a_k8s_secret_named_in_the_values_file(tmp_path, monkeypatch):
    seen = _unreachable_then_capture(monkeypatch)
    read = []
    monkeypatch.setattr(guard, "read_k8s_secret", lambda ns, name: read.append((ns, name)) or CREDS)
    env = _values(tmp_path, moonraker_url="http://p:7125", obico={"credentials": "k8s-secret:mine/obico-ro"})
    check, _ = guard.from_env(env, dev_dir=str(tmp_path))
    check(override=False)
    assert read == [("mine", "obico-ro")] and seen["creds"] == CREDS


def test_a_missing_obico_login_is_unknown_so_it_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: PrinterProbe("unreachable", None, ""))
    env = _values(tmp_path, moonraker_url="http://p:7125", obico={"credentials": "env:NOT_SET"})
    check, _ = guard.from_env(env, dev_dir=str(tmp_path))
    d = check(override=False)
    assert d.allow is False and "Obico print state unknown" in d.reason


@pytest.mark.parametrize("creds", ["s3cret", "vault:x", "k8s-secret:no-slash", 42])
def test_a_credentials_value_that_is_not_a_reference_is_a_config_error(tmp_path, monkeypatch, creds):
    # The file says WHERE the login is, never the login itself.
    monkeypatch.setattr(guard, "probe_moonraker", _no_probe)
    check, _ = guard.from_env(_values(tmp_path, obico={"credentials": creds}), dev_dir=str(tmp_path))
    d = check(override=False)
    assert d.allow is False and "values file" in d.reason
    assert "s3cret" not in d.reason


def test_refusals_name_the_configured_printer(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "probe_moonraker", lambda url: PrinterProbe("state", "printing", ""))
    env = _values(tmp_path, moonraker_url="http://p:7125", obico={"printer_name": "MyPrinter"})
    check, watch = guard.from_env(env, dev_dir=str(tmp_path))
    assert "MyPrinter is printing" in check(override=False).reason
    assert "MyPrinter started printing" in watch()
    assert "Neptune" not in check(override=False).reason


def test_the_shipped_values_file_is_exactly_todays_behaviour(monkeypatch, tmp_path):
    # values.json at the repo root is the homelab's own settings, and the starting point for anyone else's.
    # It must configure the guard identically to having no file at all.
    import pathlib
    shipped = pathlib.Path(__file__).resolve().parents[1] / "values.json"

    def run(env, probe_kind):
        seen = {}
        monkeypatch.setattr(guard, "probe_moonraker",
                            lambda url: seen.setdefault("url", url) and PrinterProbe(probe_kind, None, "t"))
        monkeypatch.setattr(guard, "read_k8s_secret", lambda ns, name: seen.update(secret=(ns, name)) or {})
        monkeypatch.setattr(guard, "query_obico",
                            lambda creds, printer: seen.update(printer=printer) or ObicoState("unknown", "t"))
        check, _ = guard.from_env(env, dev_dir=str(tmp_path))
        return check(override=False), seen

    for kind in ("unreachable", "unknown"):
        assert run({"GPU_BATCH_VALUES": str(shipped)}, kind) == run({}, kind)
