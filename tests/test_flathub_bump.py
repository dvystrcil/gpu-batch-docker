"""bin/flathub-bump.py: move a Flathub-built slicer to Flathub's current build.

Renovate can't see Flathub, so the scheduled flathub-bump workflow runs
`flatpak remote-info` for the app and its runtime and hands the text to this
script. It edits only the pin lines (never a parse-and-rewrite of a whole
file), and the result opens a PR that waits for a human: a new slicer can
change G-code even when the harness passes.

Fixtures are real `flatpak remote-info` output (2026-11-14: OrcaSlicer 2.5.1).
The scenarios run on a frozen copy of the four files (fixtures/flathub/repo-orca-2.5.0),
never the live ones: a bump changes the live pins, and when these tests read them the
first real bump (PR #28, 2.5.0 -> 2.5.1) failed three of them.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures/flathub"
SCRIPT = ROOT / "bin/flathub-bump.py"
SNAPSHOT = FIX / "repo-orca-2.5.0"
FILES = ["Dockerfile", "sources.json", "README.md", "docs/using-the-batch-tools.md"]

OLD_ORCA = "e6c923c16c51c60d015167c442e4b1f387ce75e07f6e6dce19c8c3b124dd167c"
NEW_ORCA = "edc269f43f06c1a0a63abb1f61d2d28e69316f895722121288153296430f6c01"
RUNTIME = "4337a389125f814785bd83f4ace01da5b6baa80fdcf5a9c81e60dcd03896c067"


@pytest.fixture
def repo(tmp_path):
    for f in FILES:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SNAPSHOT / f, tmp_path / f)
    return tmp_path


@pytest.fixture
def live_repo(tmp_path):
    for f in FILES:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / f, tmp_path / f)
    return tmp_path


def bump(repo, tool, app_fixture, runtime_text):
    rt = repo / "runtime.txt"
    rt.write_text(runtime_text)
    return subprocess.run([sys.executable, str(SCRIPT), tool, "--root", str(repo),
                           "--app-info", str(app_fixture), "--runtime-info", str(rt)],
                          capture_output=True, text=True)


def gnome(**over):
    text = (FIX / "gnome-50.txt").read_text()
    for old, new in over.items():
        text = text.replace(old, new)
    return text


def test_orca_moves_to_flathubs_current_build(repo):
    r = bump(repo, "orcaslicer", FIX / "orca-2.5.1.txt", gnome())
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "BUMPED orcaslicer 2.5.0 -> 2.5.1"
    docker = (repo / "Dockerfile").read_text()
    assert re.findall(r"^ARG ORCASLICER_VERSION=(\S+)$", docker, re.M) == ["2.5.1", "2.5.1"]
    assert re.findall(r"^ARG ORCASLICER_COMMIT=(\S+)$", docker, re.M) == [NEW_ORCA]
    orca = json.loads((repo / "sources.json").read_text())["images"]["orcaslicer"][0]
    assert orca["version"] == "2.5.1"
    assert orca["pins"] == ["2.5.1", NEW_ORCA]
    assert orca["source"] == "https://github.com/OrcaSlicer/OrcaSlicer/archive/refs/tags/v2.5.1.tar.gz"
    assert orca["recipe"] == "https://github.com/flathub/com.orcaslicer.OrcaSlicer/archive/34e915f92bf7.tar.gz"


def _snapshot(repo):
    return {f: (repo / f).read_text() for f in FILES}


def test_the_docs_name_the_new_version_but_history_stays(repo):
    bump(repo, "orcaslicer", FIX / "orca-2.5.1.txt", gnome())
    readme, doc = (repo / "README.md").read_text(), (repo / "docs/using-the-batch-tools.md").read_text()
    assert "OrcaSlicer 2.5.1 (Flathub build" in readme and "OrcaSlicer 2.5.0" not in readme
    assert "read from v2.5.0's `src/OrcaSlicer.cpp`" in readme
    assert "| OrcaSlicer | 2.5.1 |" in doc and "OrcaSlicer 2.5.1 |" in doc


def test_a_bump_keeps_every_dockerfile_pin_backed_by_a_source_record(repo):
    # The same invariant tests/test_licensing.py holds the real files to.
    bump(repo, "orcaslicer", FIX / "orca-2.5.1.txt", gnome(**{RUNTIME: "a" * 64, "50.6-18-g0ed65ce92": "50.7-2-gabc1234"}))
    pins = {p for comps in json.loads((repo / "sources.json").read_text())["images"].values()
            for c in comps for p in c.get("pins", [])}
    dockerfile_pins = re.findall(r"^ARG \w+_(?:VERSION|SHA256|COMMIT|REF)=(\S+)$", (repo / "Dockerfile").read_text(), re.M)
    assert set(dockerfile_pins) == pins


def test_nothing_new_on_flathub_changes_nothing(repo):
    app = (FIX / "orca-2.5.1.txt").read_text().replace(NEW_ORCA, OLD_ORCA).replace("2.5.1", "2.5.0")
    (repo / "app.txt").write_text(app)
    before = _snapshot(repo)
    r = bump(repo, "orcaslicer", repo / "app.txt", gnome())
    assert r.returncode == 0 and r.stdout.strip() == "UP-TO-DATE orcaslicer 2.5.0"
    assert _snapshot(repo) == before


def test_a_new_runtime_build_moves_both_slicers(repo):
    r = bump(repo, "orcaslicer", FIX / "orca-2.5.1.txt",
             gnome(**{RUNTIME: "b" * 64, "50.6-18-g0ed65ce92": "50.7-2-gabc1234"}))
    assert r.returncode == 0, r.stderr
    assert "GNOME runtime 50.7-2-gabc1234" in r.stdout
    docker, sources = (repo / "Dockerfile").read_text(), (repo / "sources.json").read_text()
    assert re.findall(r"^ARG GNOME_RUNTIME_COMMIT=(\S+)$", docker, re.M) == ["b" * 64, "b" * 64]
    assert RUNTIME not in docker and RUNTIME not in sources and "0ed65ce92" not in sources
    assert sources.count("gnome-build-meta/-/archive/abc1234/gnome-build-meta-abc1234.tar.gz") == 2


@pytest.mark.parametrize("app_edit, runtime_edit, why", [
    (("org.gnome.Platform/x86_64/50", "org.gnome.Platform/x86_64/51"), None, "runtime 51"),
    (("Version: 2.5.1", "Version: 2.5.1-beta"), None, "unexpected version"),
    (("(34e915f92bf7)", ""), None, "manifest"),
    (("Commit: " + NEW_ORCA, "Commit: nope"), None, "64 hex"),
    (None, ("Subject: Build of the GNOME Runtime from: 50.6-18-g0ed65ce92", "Subject: something else"), "gnome-build-meta"),
])
def test_anything_unexpected_refuses_and_changes_nothing(repo, app_edit, runtime_edit, why):
    app = (FIX / "orca-2.5.1.txt").read_text()
    if app_edit:
        app = app.replace(*app_edit)
    (repo / "app.txt").write_text(app)
    before = _snapshot(repo)
    r = bump(repo, "orcaslicer", repo / "app.txt", gnome() if not runtime_edit else gnome(**dict([runtime_edit])))
    assert r.returncode == 2 and why in r.stderr and "nothing changed" in r.stderr
    assert _snapshot(repo) == before


def test_prusaslicer_uses_its_own_tag_format(repo):
    # Synthetic: the Orca fixture's shape with PrusaSlicer's ids (PrusaSlicer had no newer build on 2026-11-14).
    app = ((FIX / "orca-2.5.1.txt").read_text().replace("com.orcaslicer.OrcaSlicer", "com.prusa3d.PrusaSlicer")
           .replace("2.5.1", "2.9.8").replace(NEW_ORCA, "c" * 64).replace("(34e915f92bf7)", "(0123abcd4567)"))
    (repo / "app.txt").write_text(app)
    r = bump(repo, "prusaslicer", repo / "app.txt", gnome())
    assert r.stdout.strip() == "BUMPED prusaslicer 2.9.7 -> 2.9.8", r.stderr
    prusa = json.loads((repo / "sources.json").read_text())["images"]["prusaslicer"][0]
    assert prusa["source"].endswith("/prusa3d/PrusaSlicer/archive/refs/tags/version_2.9.8.tar.gz")
    assert prusa["recipe"].endswith("/com.prusa3d.PrusaSlicer/archive/0123abcd4567.tar.gz")
    orca = json.loads((repo / "sources.json").read_text())["images"]["orcaslicer"][0]
    assert orca["version"] == "2.5.0"  # the other slicer is untouched


@pytest.mark.parametrize("tool, app_id, prefix, manifest", [
    ("orcaslicer", "com.orcaslicer.OrcaSlicer", "ORCASLICER", None),
    ("prusaslicer", "com.prusa3d.PrusaSlicer", "PRUSASLICER", "(0123abcd4567)"),
])
def test_the_live_files_are_up_to_date_with_their_own_pins(live_repo, tool, app_id, prefix, manifest):
    # Whatever the live pins are after any number of bumps, Flathub reporting exactly
    # them must read as UP-TO-DATE and change nothing. This keeps the script honest
    # about the live files' format, which the frozen snapshot can't.
    docker = (ROOT / "Dockerfile").read_text()
    version = re.search(rf"^ARG {prefix}_VERSION=(\S+)$", docker, re.M).group(1)
    commit = re.search(rf"^ARG {prefix}_COMMIT=(\S+)$", docker, re.M).group(1)
    runtime = re.search(r"^ARG GNOME_RUNTIME_COMMIT=(\S+)$", docker, re.M).group(1)
    app = (FIX / "orca-2.5.1.txt").read_text().replace("com.orcaslicer.OrcaSlicer", app_id)
    app = app.replace(NEW_ORCA, commit).replace("2.5.1", version)
    if manifest:
        app = app.replace("(34e915f92bf7)", manifest)
    (live_repo / "app.txt").write_text(app)
    before = _snapshot(live_repo)
    r = bump(live_repo, tool, live_repo / "app.txt", gnome(**{RUNTIME: runtime}))
    assert r.returncode == 0 and r.stdout.strip() == f"UP-TO-DATE {tool} {version}", r.stderr
    assert _snapshot(live_repo) == before


WORKFLOW = (ROOT / ".github/workflows/flathub-bump.yaml").read_text()


def test_the_update_pr_waits_for_a_human():
    # Owner's decision, 2026-11-14: a new slicer can change G-code even when the harness passes.
    assert "gh pr create" in WORKFLOW
    for merge in ("gh pr merge", "--auto", "enable-auto-merge", "merge_pull_request"):
        assert merge not in WORKFLOW, merge


def test_the_branch_never_matches_homelabs_auto_merge_gate():
    # homelab bin/pr-auto-merge.py IMAGE_BUMP_BRANCH = ^bump/.+-\d+\.\d+\.\d+$
    m = re.search(r'BR="([^"]+)"', WORKFLOW)
    assert m and m.group(1).startswith("update/") and "bump/" not in m.group(1)


def test_the_pr_is_opened_with_the_app_token_so_ci_runs_on_it():
    step = WORKFLOW[WORKFLOW.index("- name: Open the PR"):]
    assert "GH_TOKEN: ${{ steps.app-token.outputs.token }}" in step
    assert "github.token" not in step and "secrets.GITHUB_TOKEN" not in step


def test_it_runs_daily_and_by_hand():
    assert re.search(r"schedule:\n\s+- cron: '\d+ \d+ \* \* \*'", WORKFLOW)
    assert "workflow_dispatch:" in WORKFLOW


def test_remote_info_without_appstream_refuses_and_says_how_to_fix_it(repo):
    # Real output from a fresh container (2026-11-14): no Version line until appstream is fetched.
    before = _snapshot(repo)
    r = bump(repo, "orcaslicer", FIX / "orca-2.5.1-no-appstream.txt", gnome())
    assert r.returncode == 2 and "flatpak update --appstream" in r.stderr
    assert _snapshot(repo) == before


def test_the_workflow_fetches_appstream_before_asking():
    ask = WORKFLOW[WORKFLOW.index("- name: Ask Flathub"):WORKFLOW.index("- name: Move the pins")]
    assert ask.index("flatpak update --system --appstream flathub") < ask.index("flatpak remote-info")
