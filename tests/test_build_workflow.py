"""build.yaml: build each image once per change, and never pin a version flathub-bump moves.

compose-test's sidecar daemon starts empty every run. Without the Harbor layer
cache, every PR and then main again rebuilt every image from scratch (~7 min).
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
    assert "docker buildx build --load" in build
    builds = [b for b in re.split(r"\n\s*(?=docker buildx build)", build) if b.startswith("docker buildx build")]
    assert len(builds) == 2, builds                                    # the runner line and the tool loop
    assert all("--cache-from" in b for b in builds), "a build reads no cache"
    assert "--cache-to" in builds[1] and "ignore-error=true" in builds[1]
    assert ":buildcache" in build and ":buildcache" in _job("build")   # the same refs the push job reads
    assert "docker build --target" not in job                          # the uncached path is gone


def test_compose_test_can_log_in_to_harbor():
    job = _job("compose-test")
    assert "id-token: write" in job and "docker/login-action" in job and "docker/setup-buildx-action" in job


def test_no_slicer_version_is_hardcoded_in_the_build_checks():
    # flathub-bump moves these ARGs; a literal in build.yaml would fail the first update after merge.
    for arg in ("ORCASLICER_VERSION", "PRUSASLICER_VERSION"):
        version = re.search(rf"^ARG {arg}=(\S+)$", DOCKERFILE, re.M).group(1)
        assert version not in BUILD and version.replace(".", r"\.") not in BUILD, arg
        assert f"s/^ARG {arg}=//p" in BUILD, arg
