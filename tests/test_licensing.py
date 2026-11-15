"""Licences and sources travel with every image (docs/using-the-batch-tools.md, gap 2).

Publishing an image distributes GPL/LGPL/AGPL binaries. That obliges us to ship
their licence texts and say where the exact source is. sources.json is that
record. These tests keep it in step with the Dockerfile's pins, so a version
bump can't ship a binary whose source pointer is stale.
"""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
SOURCES = json.loads((ROOT / "sources.json").read_text())
PUBLISHED = ["blender", "freecad", "prusaslicer", "orcaslicer"]

# Pins are the ARGs that fix what a binary is. GNOME_RUNTIME is a branch name; its commit is the pin.
PIN_ARG = re.compile(r"^ARG (\w+_(?:VERSION|SHA256|COMMIT|REF))=(\S+)$", re.M)


def _stage(name: str) -> str:
    m = re.search(rf"^FROM \S+ AS {name}\n(.*?)(?=^FROM |\Z)", DOCKERFILE, re.M | re.S)
    assert m, f"no stage {name}"
    return m.group(1)


def _pins_in_sources() -> set:
    return {p for comps in SOURCES["images"].values() for c in comps for p in c.get("pins", [])}


def test_every_dockerfile_pin_has_a_source_record():
    missing = {f"{k}={v}" for k, v in PIN_ARG.findall(DOCKERFILE) if v not in _pins_in_sources()}
    assert not missing, f"bumped in the Dockerfile but not in sources.json: {sorted(missing)}"


def test_every_source_record_is_still_pinned_in_the_dockerfile():
    pinned = {v for _, v in PIN_ARG.findall(DOCKERFILE)}
    stale = _pins_in_sources() - pinned
    assert not stale, f"sources.json describes binaries the Dockerfile no longer builds: {sorted(stale)}"


def test_sources_cover_every_published_image():
    assert set(PUBLISHED) <= set(SOURCES["images"])
    for image in PUBLISHED:
        assert all(c.get("source") and c.get("license") for c in SOURCES["images"][image]), image


def test_the_runner_base_carries_our_licence_and_the_source_record():
    runner = _stage("runner")
    assert "COPY LICENSE /licenses/gpu-batch-docker/LICENSE" in runner
    assert "COPY sources.json /licenses/sources.json" in runner
    assert 'org.opencontainers.image.source="https://github.com/dvystrcil/gpu-batch-docker"' in runner


def test_every_published_image_puts_its_tool_licences_in_slash_licenses():
    for image in PUBLISHED:
        stage = _stage(image) + (_stage(f"{image}-fetch") if f"AS {image}-fetch" in DOCKERFILE else "")
        assert re.search(r"/licenses/\w", stage), f"{image}: nothing lands in /licenses"
        assert "org.opencontainers.image.licenses=" in stage, f"{image}: no licence label"


def test_the_slicer_images_keep_the_gnome_runtime_licences():
    # The runtime's lib/ was copied without its share/licenses until gap 2.
    for image in ("prusaslicer", "orcaslicer"):
        assert "files/share/licenses" in _stage(f"{image}-fetch")
        assert "/licenses/gnome-runtime" in _stage(image)


def test_mirror_lists_every_source_and_recipe_once():
    out = subprocess.run([sys.executable, str(ROOT / "bin/mirror-sources.py"), "--list"],
                         capture_output=True, text=True, check=True).stdout.split()
    want = {c[k] for comps in SOURCES["images"].values() for c in comps for k in ("source", "recipe") if c.get(k)}
    assert sorted(out) == sorted(want)


def _job(name: str) -> str:
    text = (ROOT / ".github/workflows/docker-release.yaml").read_text()
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.M | re.S)
    assert m, f"no job {name}"
    return m.group(1)


def test_ghcr_publishing_is_off_until_the_repo_variable_turns_it_on():
    for job in ("mirror-sources", "publish-ghcr"):
        assert "if: vars.PUBLISH_GHCR == 'true'" in _job(job), job


def test_images_reach_ghcr_only_after_their_sources_are_on_the_release():
    assert "needs: mirror-sources" in _job("publish-ghcr")
    assert "bin/mirror-sources.py --out" in _job("mirror-sources")
    assert "gh release upload" in _job("mirror-sources")


def test_no_image_keeps_the_ubuntu_base_description():
    # Seen in a real build: every image inherited "The Ubuntu container image maintained by Canonical".
    assert "org.opencontainers.image.description=" in _stage("runner")
    for image in PUBLISHED:
        assert "org.opencontainers.image.description=" in _stage(image), image
