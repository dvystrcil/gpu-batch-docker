"""bin/watch-blender.py: keep the Blender pin on the newest 5.2.x, report new series.

Renovate has no Blender datasource and blender.org publishes no GitHub releases,
so the scheduled watch-blender workflow runs this script against
https://download.blender.org/release/ (homelab#1939). A newer patch in the pinned
series moves BLENDER_VERSION *and* BLENDER_SHA256 together, the sha taken from
blender.org's own blender-<ver>.sha256 file. A newer major/minor (5.3, 6.0) is
only reported: a Cycles change can alter renders, so a human moves the series.

Everything here runs offline. --offline-mirror points the script at a directory
laid out like download.blender.org; the fixtures in tests/fixtures/blender are
real responses captured 2026-11-18. The edits land on a frozen copy of the four
files (fixtures/blender/repo-5.2.2), never the live ones.
"""
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures/blender"
SCRIPT = ROOT / "bin/watch-blender.py"
SNAPSHOT = FIX / "repo-5.2.2"
FILES = ["Dockerfile", "sources.json", "README.md", "docs/using-the-batch-tools.md"]

OLD_SHA = "84098912789dc450e95697c4184fb8a90acbe5111c2ba4aede3fecb57806a168"
NEW_SHA = "2fd14c50987426897ba60237ee996edf2b4151f4b1a72a460ca20be58c1d2d86"  # 5.2.4 linux-x64, from the .sha256
MAC_SHA = "e73d564f97061b2496ccc5a0bd738937d2d3d0acae23621a00bbc7534dfd135f"  # same file, first line


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for f in FILES:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SNAPSHOT / f, root / f)
    return root


def _drop(html: str, *patterns: str) -> str:
    """Remove every listing line that mentions one of the patterns."""
    return "".join(l for l in html.splitlines(keepends=True) if not any(p in l for p in patterns))


def mirror(tmp_path, release=None, series=None, sha=None):
    """Lay out download.blender.org under tmp_path/mirror.

    release: text of /release/; series: {"5.2": text}; sha: {"5.2.4": text}.
    Defaults are the real 2026-11-18 captures (5.2.4 and 5.3.0 are out).
    """
    m = tmp_path / "mirror"
    rel = m / "release"
    rel.mkdir(parents=True)
    (rel / "index.html").write_text((FIX / "release-index.html").read_text() if release is None else release)
    if series is None:
        series = {s: (FIX / f"Blender{s}-index.html").read_text() for s in ("5.2", "5.3")}
    for s, text in series.items():
        (rel / f"Blender{s}").mkdir()
        (rel / f"Blender{s}" / "index.html").write_text(text)
    if sha is None:
        sha = {"5.2.4": (FIX / "blender-5.2.4.sha256").read_text()}
    for v, text in sha.items():
        (rel / f"Blender{v.rsplit('.', 1)[0]}" / f"blender-{v}.sha256").write_text(text)
    return m


def up_to_date_mirror(tmp_path, **kw):
    """Only 5.2.0..5.2.2 published and no newer series: the pin is current."""
    kw.setdefault("release", _drop((FIX / "release-index.html").read_text(), "Blender5.3/"))
    kw.setdefault("series", {"5.2": _drop((FIX / "Blender5.2-index.html").read_text(), "5.2.3", "5.2.4")})
    kw.setdefault("sha", {})
    return mirror(tmp_path, **kw)


def run(repo, m):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo), "--offline-mirror", str(m)],
                          capture_output=True, text=True)


def _snapshot(repo):
    return {f: (repo / f).read_text() for f in FILES}


def test_a_newer_patch_moves_version_and_sha_together(repo, tmp_path):
    r = run(repo, mirror(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "BUMPED blender 5.2.2 -> 5.2.4"
    docker = (repo / "Dockerfile").read_text()
    assert re.findall(r"^ARG BLENDER_VERSION=(\S+)$", docker, re.M) == ["5.2.4"]
    assert re.findall(r"^ARG BLENDER_SHA256=(\S+)$", docker, re.M) == [NEW_SHA]
    assert OLD_SHA not in docker
    blender = json.loads((repo / "sources.json").read_text())["images"]["blender"][0]
    assert blender["version"] == "5.2.4"
    assert blender["pins"] == ["5.2.4", NEW_SHA]
    assert blender["source"] == "https://download.blender.org/source/blender-5.2.4.tar.xz"
    assert "Blender 5.2.4 LTS" in (repo / "README.md").read_text()
    doc = (repo / "docs/using-the-batch-tools.md").read_text()
    assert "Blender 5.2.4 LTS" in doc and "| Blender | 5.2.4 LTS |" in doc
    assert "5.2.2" not in doc


def test_the_sha_is_the_linux_tarball_line_of_blender_orgs_file(repo, tmp_path):
    # The .sha256 lists eight artifacts; the macOS line comes first. Only the
    # linux-x64 tarball's hash is the one the Dockerfile checks.
    sha = (FIX / "blender-5.2.4.sha256").read_text().replace(NEW_SHA, "f" * 64)
    r = run(repo, mirror(tmp_path, sha={"5.2.4": sha}))
    assert r.returncode == 0, r.stderr
    docker = (repo / "Dockerfile").read_text()
    assert f"ARG BLENDER_SHA256={'f' * 64}" in docker
    assert MAC_SHA not in docker


def test_up_to_date_changes_nothing(repo, tmp_path):
    before = _snapshot(repo)
    r = run(repo, up_to_date_mirror(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["UP-TO-DATE blender 5.2.2"]
    assert _snapshot(repo) == before


def test_a_new_minor_is_reported_not_bumped(repo, tmp_path):
    before = _snapshot(repo)
    r = run(repo, up_to_date_mirror(
        tmp_path, release=(FIX / "release-index.html").read_text(),
        series={"5.2": _drop((FIX / "Blender5.2-index.html").read_text(), "5.2.3", "5.2.4"),
                "5.3": (FIX / "Blender5.3-index.html").read_text()}))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["NEWER-SERIES 5.3 5.3.0", "UP-TO-DATE blender 5.2.2"]
    assert _snapshot(repo) == before


def test_a_new_major_is_reported_alongside_a_patch_bump(repo, tmp_path):
    release = (FIX / "release-index.html").read_text().replace(
        '<a href="Blender5.3/">Blender5.3/</a>',
        '<a href="Blender5.3/">Blender5.3/</a>\n<a href="Blender6.0/">Blender6.0/</a>')
    series = {s: (FIX / f"Blender{s}-index.html").read_text() for s in ("5.2", "5.3")}
    series["6.0"] = series["5.3"].replace("5.3.0", "6.0.1")
    r = run(repo, mirror(tmp_path, release=release, series=series))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == [
        "NEWER-SERIES 5.3 5.3.0", "NEWER-SERIES 6.0 6.0.1", "BUMPED blender 5.2.2 -> 5.2.4"]
    # The series never moves on its own: 5.2 stays 5.2.
    assert "ARG BLENDER_VERSION=5.2.4" in (repo / "Dockerfile").read_text()


def test_a_series_directory_without_a_final_release_is_not_reported(repo, tmp_path):
    # blender.org can create BlenderX.Y/ before X.Y.0 ships; an empty one is not news.
    empty = _drop((FIX / "Blender5.3-index.html").read_text(), "5.3.0")
    r = run(repo, up_to_date_mirror(
        tmp_path, release=(FIX / "release-index.html").read_text(),
        series={"5.2": _drop((FIX / "Blender5.2-index.html").read_text(), "5.2.3", "5.2.4"), "5.3": empty}))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["UP-TO-DATE blender 5.2.2"]


@pytest.mark.parametrize("sha", [
    None,  # no .sha256 published (yet)
    "e73d564f97061b2496ccc5a0bd738937d2d3d0acae23621a00bbc7534dfd135f  blender-5.2.4-macos-arm64.dmg\n",
    "not-a-hash  blender-5.2.4-linux-x64.tar.xz\n",
    f"{NEW_SHA}  blender-5.2.4-linux-x64.tar.xz\n{'a' * 64}  blender-5.2.4-linux-x64.tar.xz\n",
], ids=["missing", "no-linux-line", "malformed", "two-linux-lines"])
def test_no_trustworthy_sha_refuses_and_changes_nothing(repo, tmp_path, sha):
    before = _snapshot(repo)
    r = run(repo, mirror(tmp_path, sha={} if sha is None else {"5.2.4": sha}))
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "refused" in r.stderr
    assert "BUMPED" not in r.stdout
    assert _snapshot(repo) == before


def test_a_listed_patch_without_a_linux_tarball_is_skipped(repo, tmp_path):
    # 5.2.4 listed for macOS/Windows only (upload in progress): stay on what
    # Linux can use, the highest patch with a linux-x64 tarball (5.2.3).
    listing = _drop((FIX / "Blender5.2-index.html").read_text(), "blender-5.2.4-linux-x64.tar.xz")
    sha53 = (FIX / "blender-5.2.4.sha256").read_text().replace("5.2.4", "5.2.3")
    r = run(repo, mirror(tmp_path, series={"5.2": listing, "5.3": (FIX / "Blender5.3-index.html").read_text()},
                         sha={"5.2.3": sha53}))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "BUMPED blender 5.2.2 -> 5.2.3"


def test_the_live_files_parse():
    # The scenarios use a frozen copy; this keeps the live files in the format
    # the script edits, so a reworded Dockerfile fails here, not at 6am.
    spec = importlib.util.spec_from_file_location("watch_blender", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    version, sha = mod.current_pin((ROOT / "Dockerfile").read_text())
    blender = json.loads((ROOT / "sources.json").read_text())["images"]["blender"][0]
    assert blender["pins"] == [version, sha]
    assert blender["version"] == version
