#!/usr/bin/env python3
"""Keep the Blender pin on the newest patch of its series; report newer series.

    bin/watch-blender.py                       # asks download.blender.org
    bin/watch-blender.py --offline-mirror DIR  # reads DIR/release/... instead (tests)

Renovate has no Blender datasource and blender.org publishes no GitHub
releases, so the watch-blender workflow runs this daily (homelab#1939).

- A newer patch in the pinned series (5.2.2 -> 5.2.4) moves BLENDER_VERSION and
  BLENDER_SHA256 together in the Dockerfile, plus the Blender pins in
  sources.json and the version named in README.md and the user doc. The sha256
  is the linux-x64 line of blender.org's own blender-<ver>.sha256; nothing is
  downloaded and hashed here. The Docker build checks the tarball against it.
- A newer major/minor (5.3, 6.0) is only reported, never bumped: a Cycles change
  can alter renders, so a human moves the series.

Only the pin lines are edited (never a parse-and-rewrite of a whole file), and
nothing is written unless every edit applies. stdout: one
`NEWER-SERIES <major.minor> <latest>` line per newer series with a final release,
then a last line `BUMPED blender <old> -> <new>` or `UP-TO-DATE blender <old>`.
Exit 2 (nothing changed) when blender.org can't be read or gives no single
well-formed sha256 for the new tarball.
"""
import argparse
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "https://download.blender.org/"
DOCS = ["README.md", "docs/using-the-batch-tools.md"]
TARBALL = re.compile(r'href="blender-(\d+)\.(\d+)\.(\d+)-linux-x64\.tar\.xz"')
SERIES = re.compile(r'href="Blender(\d+)\.(\d+)/"')


class Refuse(Exception):
    pass


def fetch(path: str, mirror: pathlib.Path | None) -> str | None:
    """Text at download.blender.org/<path>, or None if it doesn't exist."""
    if mirror is not None:
        f = mirror / (path + "index.html" if path.endswith("/") else path)
        return f.read_text() if f.is_file() else None
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "gpu-batch-docker watch-blender"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise Refuse(f"{BASE}{path}: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise Refuse(f"{BASE}{path}: {e}") from e


def _only(values: list, what: str) -> str:
    if len(values) != 1:
        raise Refuse(f"expected one {what}, found {values}")
    return values[0]


def current_pin(docker: str) -> tuple[str, str]:
    version = _only(re.findall(r"^ARG BLENDER_VERSION=(\d+\.\d+\.\d+)$", docker, re.M), "ARG BLENDER_VERSION")
    sha = _only(re.findall(r"^ARG BLENDER_SHA256=([0-9a-f]{64})$", docker, re.M), "ARG BLENDER_SHA256")
    return version, sha


def _vt(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def releases(listing: str) -> list[str]:
    """Final versions with a linux-x64 tarball in a BlenderX.Y/ listing, oldest first."""
    return sorted({".".join(m) for m in TARBALL.findall(listing)}, key=_vt)


def published_sha(text: str | None, version: str) -> str:
    name = f"blender-{version}-linux-x64.tar.xz"
    if text is None:
        raise Refuse(f"blender-{version}.sha256 is not published (yet)")
    lines = [l.split() for l in text.splitlines() if l.split()[-1:] == [name]]
    sha = _only([l[0] for l in lines], f"line for {name} in blender-{version}.sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise Refuse(f"blender-{version}.sha256 gives {sha!r} for {name}, not 64 hex digits")
    return sha


def _sub(text: str, old: str, new: str, what: str) -> str:
    if old not in text:
        raise Refuse(f"{what}: {old!r} not found")
    return text.replace(old, new)


def watch(root: pathlib.Path, mirror: pathlib.Path | None = None) -> list[str]:
    files = {f: (root / f).read_text() for f in ["Dockerfile", "sources.json", *DOCS] if (root / f).exists()}
    old_v, old_sha = current_pin(files["Dockerfile"])
    series = tuple(_vt(old_v)[:2])
    out = []

    index = fetch("release/", mirror)
    if index is None:
        raise Refuse(f"{BASE}release/ not found")
    for s in sorted({(int(a), int(b)) for a, b in SERIES.findall(index)}):
        if s <= series:
            continue
        found = releases(fetch(f"release/Blender{s[0]}.{s[1]}/", mirror) or "")
        if found:  # a directory can exist before X.Y.0 ships
            out.append(f"NEWER-SERIES {s[0]}.{s[1]} {found[-1]}")

    listing = fetch(f"release/Blender{series[0]}.{series[1]}/", mirror)
    if listing is None:
        raise Refuse(f"{BASE}release/Blender{series[0]}.{series[1]}/ not found")
    newer = [v for v in releases(listing) if _vt(v) > _vt(old_v)]
    if not newer:
        return out + [f"UP-TO-DATE blender {old_v}"]

    new_v = newer[-1]
    new_sha = published_sha(fetch(f"release/Blender{series[0]}.{series[1]}/blender-{new_v}.sha256", mirror), new_v)

    docker = _sub(files["Dockerfile"], f"ARG BLENDER_VERSION={old_v}\n", f"ARG BLENDER_VERSION={new_v}\n",
                  "Dockerfile version")
    files["Dockerfile"] = _sub(docker, f"ARG BLENDER_SHA256={old_sha}\n", f"ARG BLENDER_SHA256={new_sha}\n",
                               "Dockerfile sha256")
    src = files["sources.json"]
    a = src.index('"blender": [')
    b = src.index("\n    ]", a)
    block = src[a:b]
    block = _sub(block, f'"version": "{old_v}"', f'"version": "{new_v}"', "sources.json version")
    block = _sub(block, f'"pins": ["{old_v}", "{old_sha}"]', f'"pins": ["{new_v}", "{new_sha}"]', "sources.json pins")
    block = _sub(block, f"/source/blender-{old_v}.tar.xz", f"/source/blender-{new_v}.tar.xz", "sources.json source")
    files["sources.json"] = src[:a] + block + src[b:]
    for doc in DOCS:
        if doc in files:  # the version as the docs name it, nothing else
            files[doc] = files[doc].replace(f"Blender {old_v} ", f"Blender {new_v} ") \
                                   .replace(f"| Blender | {old_v} ", f"| Blender | {new_v} ")

    for f, text in files.items():
        (root / f).write_text(text)
    return out + [f"BUMPED blender {old_v} -> {new_v}"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=pathlib.Path, default=ROOT)
    ap.add_argument("--offline-mirror", type=pathlib.Path,
                    help="read DIR/release/... (laid out like download.blender.org) instead of the network")
    args = ap.parse_args(argv)
    try:
        print("\n".join(watch(args.root, args.offline_mirror)))
    except Refuse as e:
        print(f"watch-blender: refused, nothing changed: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
