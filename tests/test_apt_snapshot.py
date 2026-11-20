"""apt reads a pinned snapshot, never the live mirror (homelab#1981, D-051).

The design and the tests behind it are in dvystrcil/homelab
architecture/apt-snapshot-image-builds.md. bin/apt-snapshot-test.sh proves it with
real builds (live mirror blocked: builds; snapshot source blocked: fails). These
checks keep the Dockerfile and build.yaml in the shape that proof relies on, so a
new stage or a new build line can't quietly skip the pin.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
BUILD = (ROOT / ".github/workflows/build.yaml").read_text()
CACHE = "http://apt-cacher-ng.arc-runner.svc.cluster.local:3142/HTTPS///snapshot.ubuntu.com/ubuntu"


def _stages() -> dict[str, tuple[str, str]]:
    """name -> (parent, body)."""
    out = {}
    for m in re.finditer(r"^FROM (\S+) AS (\S+)\n(.*?)(?=^FROM |\Z)", DOCKERFILE, re.M | re.S):
        out[m.group(2)] = (m.group(1), m.group(3))
    return out


def _job(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [\w-]+:\n|\Z)", BUILD, re.M | re.S)
    assert m, name
    return m.group(1)


def test_the_snapshot_id_is_written_once():
    # The weekly bump job (a later slice) edits exactly one line.
    ids = re.findall(r"^ARG APT_SNAPSHOT=(\S*)$", DOCKERFILE, re.M)
    assert len(ids) == 1, ids
    assert re.fullmatch(r"\d{8}T\d{6}Z", ids[0]), ids[0]
    assert DOCKERFILE.index("ARG APT_SNAPSHOT=") < DOCKERFILE.index("\nFROM "), "must be a global ARG"


def test_the_default_source_is_the_snapshot_direct():
    assert re.findall(r"^ARG APT_SNAPSHOT_BASE=(\S*)$", DOCKERFILE, re.M) == ["https://snapshot.ubuntu.com/ubuntu"]


def test_every_apt_update_errors_on_any_failure():
    # Without --error-on=any an unreachable source is a W: and apt carries on.
    updates = re.findall(r"apt-get update[^\n]*", DOCKERFILE)
    assert len(updates) >= 7, updates
    for u in updates:
        assert "--error-on=any" in u and "-o Acquire::Retries=3" in u, u


def test_the_pin_is_the_repository_not_the_snapshot_option():
    # --snapshot / Snapshot: still fetch the live mirror (the doc's trap 1).
    code = "\n".join(l for l in DOCKERFILE.splitlines() if not l.lstrip().startswith("#"))
    assert "--snapshot" not in code and "Snapshot:" not in code and "APT::Snapshot" not in code
    snap = _stages()["ubuntu-snapshot"][1]
    assert 'URIs: ${APT_SNAPSHOT_BASE}/${APT_SNAPSHOT}/' in snap
    assert "ARG APT_SNAPSHOT\n" in snap and "ARG APT_SNAPSHOT_BASE\n" in snap


def test_every_stage_that_runs_apt_descends_from_the_pinned_stage():
    stages = _stages()
    ubuntu = [n for n, (parent, _) in stages.items() if parent.startswith("ubuntu:")]
    assert ubuntu == ["ubuntu-snapshot"], f"only ubuntu-snapshot may start FROM ubuntu: {ubuntu}"
    for name, (_, body) in stages.items():
        if "apt-get" not in body or name == "ubuntu-snapshot":
            continue
        seen, cur = [], name
        while cur in stages and cur != "ubuntu-snapshot":
            seen.append(cur)
            cur = stages[cur][0]
        assert cur == "ubuntu-snapshot", f"{name} runs apt but descends {' -> '.join(seen)} -> {cur}"


def test_the_tls_bootstrap_bundle_is_digest_pinned():
    parent = _stages()["bootstrap-ca"][0]
    assert re.fullmatch(r"\S+:\S+@sha256:[0-9a-f]{64}", parent), parent


def test_in_cluster_builds_read_apt_through_the_cache():
    assert f"  APT_SNAPSHOT_BASE: {CACHE}\n" in BUILD
    compose = _job("compose-test")
    build = compose[compose.index("- name: Build the images"):]
    lines = re.findall(r"docker buildx build[^\n]*", build)
    assert len(lines) == 2, lines
    for line in lines:
        assert '--build-arg "APT_SNAPSHOT_BASE=${APT_SNAPSHOT_BASE}"' in line, line
    assert "APT_SNAPSHOT_BASE=${{ env.APT_SNAPSHOT_BASE }}" in _job("build-image")


def test_compose_test_runs_the_blocked_mirror_test_and_checks_it_finished():
    job = _job("compose-test")
    assert "bin/apt-snapshot-test.sh | tee apt-snapshot-test.out" in job
    assert 'test "$(tail -n 1 apt-snapshot-test.out)" = APT-SNAPSHOT-TEST-OK' in job
    assert job.index("apt-snapshot-test.sh") < job.index("- name: Build the images")


# The pin is for the build only (director decision, homelab#1981, 2026-11-19).
# In CI APT_SNAPSHOT_BASE is the apt-cacher-ng Service: left in a shipped image,
# a runtime apt-get would read the CI runners' cache, frozen at the build
# snapshot, and outside the cluster it can't resolve at all. So ubuntu-snapshot
# keeps the stock ubuntu.sources aside, and every final stage puts it back after
# its last apt step. bin/apt-snapshot-test.sh checks the built images.
ORIG = "/etc/apt/ubuntu.sources.orig"
SOURCES = "/etc/apt/sources.list.d/ubuntu.sources"


def _final_stages() -> list[str]:
    """Stages nothing else builds on (FROM, COPY --from, mount from=): the images."""
    stages = _stages()
    used = {parent for parent, _ in stages.values()}
    used |= set(re.findall(r"(?:--from=|from=)([\w.-]+)", DOCKERFILE))
    return [n for n in stages if n not in used]


def _published_targets() -> list[str]:
    return re.findall(r"^\s+- target: (\S+)$", _job("build-image"), re.M)


def test_ubuntu_snapshot_keeps_the_stock_sources_before_pinning():
    snap = _stages()["ubuntu-snapshot"][1]
    backup = f"cp {SOURCES} {ORIG}"
    assert backup in snap, "ubuntu-snapshot must keep the stock ubuntu.sources"
    assert snap.index(backup) < snap.index("sed -i"), "the backup must come before the rewrite"
    assert not ORIG.startswith("/etc/apt/sources.list.d/"), "apt would read the backup"


def test_every_published_target_is_a_final_stage():
    published = _published_targets()
    assert len(published) >= 4, published
    finals = _final_stages()
    for t in published:
        assert t in finals, f"{t} is published but another stage builds on it: {finals}"


def test_every_final_stage_restores_the_stock_sources_after_its_last_apt_step():
    stages = _stages()
    finals = _final_stages()
    assert len(finals) >= 5, finals   # runner + the four tools
    for name in finals:
        body = stages[name][1]
        restore = f"mv {ORIG} {SOURCES}"
        assert body.count(restore) == 1, f"{name}: no restore of the stock ubuntu.sources"
        at = body.index(restore)
        assert body.rfind("apt-get") < at, f"{name}: apt runs after the stock sources are back"
        step = body[at:body.index("\n\n", at) if "\n\n" in body[at:] else len(body)]
        assert "rm -rf /var/lib/apt/lists/*" in step, f"{name}: lists from the pinned source ship"
        assert "archive.ubuntu.com" in step and "apt-cacher-ng" in step, f"{name}: restore isn't checked"
        user = body.rfind("\nUSER 1000")
        assert user == -1 or at < user, f"{name}: restore must run as root, before USER 1000"
