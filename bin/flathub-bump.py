#!/usr/bin/env python3
"""Move a Flathub-built slicer image to Flathub's current build.

    flatpak remote-info --system flathub app/com.orcaslicer.OrcaSlicer/x86_64/stable > app.txt
    flatpak remote-info --system flathub runtime/org.gnome.Platform/x86_64/50 > runtime.txt
    bin/flathub-bump.py orcaslicer --app-info app.txt --runtime-info runtime.txt

Renovate can't see Flathub, so the flathub-bump workflow runs this daily. It
edits only the pin lines in the Dockerfile and sources.json (and the version
named in README.md and the user doc), never a parse-and-rewrite of a whole
file, and writes nothing unless every edit applies. The last stdout line is
`BUMPED <tool> <old> -> <new>` or `UP-TO-DATE <tool> <version>`. A move to
another GNOME runtime branch (50 -> 51) is refused: it changes the libraries
both slicers run on, so a human does that one.
"""
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

TOOLS = {
    "orcaslicer": {"prefix": "ORCASLICER", "name": "OrcaSlicer", "app": "com.orcaslicer.OrcaSlicer",
                   "upstream": "OrcaSlicer/OrcaSlicer", "tag": "v{}"},
    "prusaslicer": {"prefix": "PRUSASLICER", "name": "PrusaSlicer", "app": "com.prusa3d.PrusaSlicer",
                    "upstream": "prusa3d/PrusaSlicer", "tag": "version_{}"},
}
DOCS = ["README.md", "docs/using-the-batch-tools.md"]


class Refuse(Exception):
    pass


def parse_info(text: str) -> dict:
    """`flatpak remote-info` -> {field: value} for the 'Key: value' lines."""
    return {m[1]: m[2].strip() for m in re.finditer(r"^\s*([A-Z][\w ]*?):\s(.*)$", text, re.M)}


def flathub_state(app_text: str, runtime_text: str) -> dict:
    app, rt = parse_info(app_text), parse_info(runtime_text)
    try:
        version, commit, subject, runtime = app["Version"], app["Commit"], app["Subject"], app["Runtime"]
        rt_commit, rt_subject, rt_branch = rt["Commit"], rt["Subject"], rt["Branch"]
    except KeyError as e:
        hint = " (no appstream data: run `flatpak update --appstream flathub` first)" if e.args[0] == "Version" else ""
        raise Refuse(f"remote-info has no {e} line{hint}") from e
    manifest = re.search(r"\(([0-9a-f]{7,40})\)\s*$", subject)
    built_from = re.search(r"from: (\S+-g([0-9a-f]{7,40}))$", rt_subject)
    if not re.fullmatch(r"[0-9a-f]{64}", commit) or not re.fullmatch(r"[0-9a-f]{64}", rt_commit):
        raise Refuse("an ostree commit is not 64 hex digits")
    if not re.fullmatch(r"\d+(\.\d+)+", version):
        raise Refuse(f"unexpected version {version!r}")
    if not manifest or not built_from:
        raise Refuse(f"can't find the manifest / gnome-build-meta commit in {subject!r} / {rt_subject!r}")
    return {"version": version, "commit": commit, "manifest": manifest[1], "runtime_branch": runtime.rsplit("/", 1)[-1],
            "rt_branch": rt_branch, "rt_commit": rt_commit, "rt_build": built_from[1], "rt_meta": built_from[2]}


def _only(values: list, what: str) -> str:
    if not values or len(set(values)) != 1:
        raise Refuse(f"expected one value for {what} in the Dockerfile, found {sorted(set(values))}")
    return values[0]


def _sub(text: str, old: str, new: str, what: str, count_at_least: int = 1) -> str:
    if text.count(old) < count_at_least:
        raise Refuse(f"{what}: {old!r} not found")
    return text.replace(old, new)


def _block(sources: str, tool: str) -> tuple[int, int]:
    start = sources.index(f'"{tool}": [')
    return start, sources.index("\n    ]", start)


def bump(tool: str, root: pathlib.Path, app_text: str, runtime_text: str) -> str:
    t = TOOLS[tool]
    new = flathub_state(app_text, runtime_text)
    files = {f: (root / f).read_text() for f in ["Dockerfile", "sources.json", *DOCS] if (root / f).exists()}
    docker, sources = files["Dockerfile"], files["sources.json"]

    old_v = _only(re.findall(rf"^ARG {t['prefix']}_VERSION=(\S+)$", docker, re.M), f"{t['prefix']}_VERSION")
    old_c = _only(re.findall(rf"^ARG {t['prefix']}_COMMIT=(\S+)$", docker, re.M), f"{t['prefix']}_COMMIT")
    branch = _only(re.findall(r"^ARG GNOME_RUNTIME=(\S+)$", docker, re.M), "GNOME_RUNTIME")
    old_rt = _only(re.findall(r"^ARG GNOME_RUNTIME_COMMIT=(\S+)$", docker, re.M), "GNOME_RUNTIME_COMMIT")
    if new["runtime_branch"] != branch or new["rt_branch"] != branch:
        raise Refuse(f"{t['name']} {new['version']} needs GNOME runtime {new['runtime_branch']}, the image pins "
                     f"{branch}: a runtime branch move changes both slicers, so it's a human's PR")

    if (old_c, old_rt) == (new["commit"], new["rt_commit"]):
        return f"UP-TO-DATE {tool} {old_v}"

    if old_c != new["commit"]:
        docker = re.sub(rf"^ARG {t['prefix']}_VERSION={re.escape(old_v)}$",
                        f"ARG {t['prefix']}_VERSION={new['version']}", docker, flags=re.M)
        docker = _sub(docker, f"ARG {t['prefix']}_COMMIT={old_c}", f"ARG {t['prefix']}_COMMIT={new['commit']}",
                      "Dockerfile commit")
        a, b = _block(sources, tool)
        block = sources[a:b]
        block = _sub(block, f'"{old_v}"', f'"{new["version"]}"', "sources.json version", 2)
        block = _sub(block, old_c, new["commit"], "sources.json commit")
        tag = f"https://github.com/{t['upstream']}/archive/refs/tags/"
        block = _sub(block, tag + t["tag"].format(old_v), tag + t["tag"].format(new["version"]), "sources.json source")
        recipe = re.search(rf"github\.com/flathub/{re.escape(t['app'])}/archive/([0-9a-f]+)\.tar\.gz", block)
        if not recipe:
            raise Refuse("sources.json recipe URL not found")
        block = block.replace(recipe[0], recipe[0].replace(recipe[1], new["manifest"]))
        sources = sources[:a] + block + sources[b:]
        for doc in DOCS:
            if doc in files:  # the version as the docs name it; "v2.5.0's src/..." is history and stays
                files[doc] = files[doc].replace(f"{t['name']} {old_v}", f"{t['name']} {new['version']}") \
                                       .replace(f"| {t['name']} | {old_v} |", f"| {t['name']} | {new['version']} |")

    if old_rt != new["rt_commit"]:  # shared by both slicers: every occurrence moves together
        old_build = _only(re.findall(r'"version": "(\d+\.\d+-\d+-g[0-9a-f]+)"', sources), "runtime build")
        old_meta = old_build.rsplit("-g", 1)[1]
        docker = _sub(docker, f"ARG GNOME_RUNTIME_COMMIT={old_rt}", f"ARG GNOME_RUNTIME_COMMIT={new['rt_commit']}",
                      "Dockerfile runtime commit", 2)
        sources = _sub(sources, old_rt, new["rt_commit"], "sources.json runtime commit", 2)
        sources = _sub(sources, old_build, new["rt_build"], "sources.json runtime build", 2)
        sources = _sub(sources, f"/archive/{old_meta}/gnome-build-meta-{old_meta}.",
                       f"/archive/{new['rt_meta']}/gnome-build-meta-{new['rt_meta']}.", "sources.json runtime source", 2)

    files["Dockerfile"], files["sources.json"] = docker, sources
    for f, text in files.items():
        (root / f).write_text(text)
    rt_note = f" (GNOME runtime {new['rt_build']})" if old_rt != new["rt_commit"] else ""
    return f"BUMPED {tool} {old_v} -> {new['version']}{rt_note}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tool", choices=sorted(TOOLS))
    ap.add_argument("--app-info", type=pathlib.Path, required=True, help="flatpak remote-info of the app")
    ap.add_argument("--runtime-info", type=pathlib.Path, required=True, help="flatpak remote-info of the runtime")
    ap.add_argument("--root", type=pathlib.Path, default=ROOT)
    args = ap.parse_args(argv)
    try:
        print(bump(args.tool, args.root, args.app_info.read_text(), args.runtime_info.read_text()))
    except Refuse as e:
        print(f"flathub-bump: refused, nothing changed: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
