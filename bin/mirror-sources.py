#!/usr/bin/env python3
"""Download the exact source of every pinned binary in the images, for a release.

    bin/mirror-sources.py --list            # print each URL once (no network)
    bin/mirror-sources.py --out DIR         # download them to DIR + SHA256SUMS

The release workflow attaches DIR's files to the GitHub release, next to the
images it publishes. A GPL licence lets us point at upstream's copy only while
upstream keeps it, so we keep our own copy. sources.json is the record
(docs/using-the-batch-tools.md, gap 2).
"""
import argparse
import hashlib
import json
import pathlib
import sys
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def urls(sources: dict) -> list[str]:
    seen = []
    for comps in sources["images"].values():
        for c in comps:
            for key in ("source", "recipe"):
                if c.get(key) and c[key] not in seen:
                    seen.append(c[key])
    return seen


def filename(url: str) -> str:
    """A name that says what the archive is: github.com/o/r/archive/<ref>.tar.gz -> r-<ref>.tar.gz."""
    parts = urllib.parse.urlparse(url).path.strip("/").split("/")
    if "archive" in parts and parts[0] != "GNOME":
        repo, last = parts[parts.index("archive") - 1], parts[-1]
        return last if last.startswith(repo) else f"{repo}-{last}"
    return parts[-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sources", type=pathlib.Path, default=ROOT / "sources.json")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="print each URL once")
    g.add_argument("--out", type=pathlib.Path, help="download every archive here, with SHA256SUMS")
    args = ap.parse_args(argv)
    todo = urls(json.loads(args.sources.read_text()))
    if args.list:
        print("\n".join(todo))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    sums = []
    for url in todo:
        dest = args.out / filename(url)
        print(f"mirror-sources: {url} -> {dest.name}", file=sys.stderr)
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        if dest.stat().st_size == 0:
            raise SystemExit(f"mirror-sources: {url} downloaded empty")
        sums.append(f"{h.hexdigest()}  {dest.name}")
    (args.out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    print(f"mirror-sources: {len(todo)} archives in {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
