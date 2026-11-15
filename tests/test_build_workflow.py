"""build.yaml: build each image once per change, and never pin a version flathub-bump moves.

compose-test's sidecar daemon starts empty every run. Without the Harbor layer
cache, every PR and then main again rebuilt every image from scratch (~7 min).

compose-test reads that cache with the plain docker driver and never writes it.
The docker-container driver (setup-buildx) needs --load, which streams each image
through the buildx client in the 512Mi ARC runner container: on PR #27 the
client hit 367 MiB on the freecad image and the kernel OOM-killed the runner
("lost communication"), three runs out of three. The build job writes the cache.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = (ROOT / ".github/workflows/build.yaml").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def _job(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [\w-]+:\n|\Z)", BUILD, re.M | re.S)
    assert m, name
    return m.group(1)


def test_compose_test_builds_from_the_shared_harbor_cache():
    job = _job("compose-test")
    build = job[job.index("- name: Build the images"):]
    builds = [b for b in re.split(r"\n\s*(?=docker buildx build)", build) if b.startswith("docker buildx build")]
    assert len(builds) == 2, builds                                    # the runner line and the tool loop
    assert all("--builder default" in b for b in builds), "a build is not on the docker driver"
    assert all("--cache-from" in b for b in builds), "a build reads no cache"
    assert ":buildcache" in build and ":buildcache" in _job("build")   # the same refs the push job writes
    assert "docker build --target" not in job                          # the uncached path is gone


def test_compose_test_never_streams_images_through_the_runner_container():
    job = "\n".join(l for l in _job("compose-test").splitlines() if not l.lstrip().startswith("#"))
    assert "docker/setup-buildx-action" not in job   # its docker-container builder needs --load
    assert "--load" not in job and "--cache-to" not in job   # the docker driver can't export cache anyway


def test_compose_test_can_log_in_to_harbor():
    job = _job("compose-test")
    assert "id-token: write" in job and "docker/login-action" in job


def test_no_slicer_version_is_hardcoded_in_the_build_checks():
    # flathub-bump moves these ARGs; a literal in build.yaml would fail the first update after merge.
    for arg in ("ORCASLICER_VERSION", "PRUSASLICER_VERSION"):
        version = re.search(rf"^ARG {arg}=(\S+)$", DOCKERFILE, re.M).group(1)
        assert version not in BUILD and version.replace(".", r"\.") not in BUILD, arg
        assert f"s/^ARG {arg}=//p" in BUILD, arg


def _stage(name: str) -> str:
    m = re.search(rf"^FROM (\S+) AS {name}\n(.*?)(?=^FROM |\Z)", DOCKERFILE, re.M | re.S)
    assert m, name
    return m.group(1), m.group(2)


OURS = ("COPY src/gpu_batch", "COPY LICENSE", "COPY sources.json")


def test_base_holds_no_repo_file():
    # Anything COPYed into base invalidates every download in every image built on it.
    _, base = _stage("base")
    assert not re.search(r"^COPY (?!--from)", base, re.M)


def test_our_files_come_after_every_download():
    # A code change or a flathub-bump (sources.json) must rebuild only the last layers.
    for tool in ("blender", "freecad", "prusaslicer", "orcaslicer"):
        parent, body = _stage(tool)
        assert parent == "base", f"{tool} builds FROM {parent}: our files would sit under its downloads"
        first_ours = min(body.index(c) for c in OURS)
        heavy = [m.start() for m in re.finditer(r"^(RUN |COPY --from=)", body, re.M)]
        assert heavy and max(heavy) < first_ours, f"{tool}: a download layer comes after our files"


def test_a_newer_push_cancels_a_stale_pr_run_but_never_main():
    assert re.search(r"^concurrency:\n  group: build-\$\{\{ github\.ref \}\}\n"
                     r"  cancel-in-progress: \$\{\{ github\.event_name == 'pull_request' \}\}$", BUILD, re.M)
