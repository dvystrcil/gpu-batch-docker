# Using the batch tools yourself

> **Status: draft (2026-11-14).** This page says what works for someone outside this homelab today and what doesn't
> yet. The gaps are listed in [What isn't ready yet](#what-isnt-ready-yet). Each gap is a concrete fix, not a caveat
> to live with.

## What this is

A set of pinned, containerised CAD/CAM tools you can hand a long job to and leave. Each one runs a list of
commands and **proves** the job produced what it promised before it reports success:

| Image (Dockerfile target) | Tool | What it's for |
|---|---|---|
| `freecad` | FreeCAD 26.3rc2 + Fasteners workbench 0.5.64, `xvfb-run` | headless model edits, clash checks, mesh/STL export, also GUI-only scripts under a virtual display |
| `blender` | Blender 5.3.0 | Cycles renders of exported meshes, on an NVIDIA GPU or the CPU |
| `orcaslicer` | OrcaSlicer 2.5.1 | slice STLs with your own presets; print time and filament statistics |
| `prusaslicer` | PrusaSlicer 2.9.7 | the same, with PrusaSlicer |

All four share one runner (`src/gpu_batch/runner.py`), so they all behave the same way. You give a job a
`spec.json` and get back a `status.json`.

## Why it is built this way

These are the reasons behind the design. If you're adapting it, they tell you what you can drop and what you
shouldn't.

- **Pinned tools, so a result can be reproduced.** A FreeCAD or slicer update changes the geometry or the G-code
  without telling you. Every tool here is pinned by sha256 or commit, so the same job gives the same answer next
  month. Running natively, you have to pin the versions yourself (see the table below).
- **Exit 0 isn't success.** FreeCAD and Blender both exit 0 after failing halfway through. The runner checks that
  `expect_outputs` exist, aren't empty, and are valid PNGs where they claim to be. Only then is the job a success.
- **Fire and forget.** Renders and sweeps take minutes to hours. The runner writes `status.json` at every step and can
  POST the result to a webhook, so nobody has to sit and poll a terminal.
- **A print guard on the GPU.** The original setup shares a small GPU with a printer's host. A render that runs out of
  GPU memory mid-print can stall the printer, so GPU jobs refuse to start while a print is running. If any check
  returns an unknown, the guard refuses. CPU-only jobs can switch the guard off.
- **One job folder.** Inputs, the spec, logs and outputs all live in one directory. That makes a job easy to move
  between a laptop, a container and a cluster, and easy to inspect afterwards.

## Three ways to run it

| Route | You need | Good for |
|---|---|---|
| **1. Native install** | the tools below on your own machine | running scripts by hand, one at a time |
| **2. Docker, one image at a time** | Docker (+ NVIDIA Container Toolkit for GPU renders) | reproducible runs without installing FreeCAD/Blender |
| **3. Docker Compose, chained** | Docker Compose v2 | FreeCAD → slicer/Blender as one pipeline |

The homelab itself runs a fourth route: Kubernetes Jobs, an NFS work share and n8n chains that post results to GitHub.
That one is documented in `dvystrcil/gpu-batch` and `homelab/architecture/gpu-batch-lane.md`. It assumes this
homelab and isn't meant to be copied as-is.

### 1. Native install

Match the image versions, or your results will differ from anyone else's:

| Tool | Version | Where to get it |
|---|---|---|
| FreeCAD | 26.3rc2 (AppImage) | freecad.org downloads; set `FREECAD_APPIMAGE` to the AppImage path |
| Fasteners workbench | V0.5.64-beta (`85a21e22`) | FreeCAD Addon Manager, or clone at that commit into FreeCAD's `Mod/` |
| Blender | 5.3.0 | blender.org (the official tarball; the snap tracks newer releases) |
| OrcaSlicer | 2.5.1 | Flathub or GitHub releases |
| PrusaSlicer | 2.9.7 | Flathub or GitHub releases |
| Python | 3.14 for the runner; numpy for some scripts | your distro |
| `xvfb-run` | any | `apt install xvfb`, for FreeCAD scripts that need the GUI |

<!-- TODO: a check-tools.sh that prints each installed version against this table. -->

### 2. Docker, one image at a time

Build the images. Nothing is published to a public registry yet (see the gaps below):

```bash
git clone https://github.com/dvystrcil/gpu-batch-docker && cd gpu-batch-docker
for t in runner freecad blender orcaslicer prusaslicer; do
  docker build --target $t -t gpu-batch-$t:local .
done
```

Make a job folder with a `spec.json`. A job is a list of steps plus the outputs that prove it worked. For example,
this slices one STL:

```json
{"id": "slice-test", "steps": [
  {"name": "export-gcode", "timeout_s": 300,
   "argv": ["sh", "-c", "xvfb-run -a flatpak run --filesystem=/workdir:rw com.prusa3d.PrusaSlicer --export-gcode {job}/part.stl -o {job}/part.gcode"]}],
 "expect_outputs": ["part.gcode"], "min_outputs": 1, "notify": null}
```

Run it. The entrypoint is the runner, and its argument is the path of the spec inside the container:

```bash
docker run --rm -v "$PWD/myjob:/work/myjob" \
  -e GPU_BATCH_PRINT_GUARD=off \
  gpu-batch-prusaslicer:local /work/myjob/spec.json
cat myjob/status.json        # "state" is the outcome; the exit code only says whether it was reported
```

<!-- TODO: check that the {job} expansion and flatpak argv above match the prusaslicer image as built; it is adapted
     from the repo's spec.json smoke test, not run yet. -->

For a GPU render, add `--gpus all` and give the guard a values file (next section). Without one, the guard looks for
this homelab's printer, can't find it, and refuses the job:

```bash
docker run --rm --gpus all \
  -v "$PWD/myjob:/work/myjob" -v "$PWD/my-values.json:/etc/gpu-batch/values.json:ro" \
  -e GPU_BATCH_VALUES=/etc/gpu-batch/values.json \
  gpu-batch-blender:local /work/myjob/spec.json
```

### 3. Docker Compose, chained

`tests/compose/compose.yaml` already chains all four images on a shared volume:
seed → freecad → (prusaslicer, orcaslicer, blender) → assert. It runs against a mock printer host and a stand-in
webhook. Right now it is a **test harness** (`bin/compose-test.sh`), not a user pipeline. The user version will
use the same structure with your own job folders in place of the seeded fixtures.

<!-- TODO: a user-facing compose file + a run-chain.sh that wires job folders through it (see gaps). -->

## Configuring the print guard

The guard exists so a GPU job can't starve a printer that's mid-print (see [Why](#why-it-is-built-this-way)). It
reads the `print_guard` section of the JSON file named by `GPU_BATCH_VALUES`. [`values.json`](../values.json) at the
repo root is this homelab's own settings. Copy it and change what applies to you:

```json
{
  "print_guard": {
    "moonraker_url": "http://192.168.86.243",
    "fallback": "obico-db",
    "obico": {
      "printer_name": "Neptune",
      "credentials": "k8s-secret:obico/obico-postgres-gpubatch"
    },
    "when_unreachable": "refuse"
  }
}
```

| Key | Values | Meaning |
|---|---|---|
| `moonraker_url` | URL, or `null` | Your printer's Moonraker. Its answer always wins when the printer responds. |
| `fallback` | `"obico-db"` or `"none"` | What to ask when Moonraker can't be reached, to tell "printer switched off" from "network dropped mid-print". |
| `obico.printer_name` | string | The printer's name in Obico. It's also the name used in refusal messages. |
| `obico.credentials` | `k8s-secret:<ns>/<name>`, `file:<path>`, `env:<VAR>` | **Where** the Obico database login is. The file and the variable hold a JSON object with `host`, `port`, `user`, `password` and `dbname`. Never put the login itself in the values file. |
| `when_unreachable` | `"refuse"` or `"allow"` | Only with `fallback: "none"`: what to do when Moonraker can't be reached. |

Pick the setup that matches yours:

| Your setup | Settings |
|---|---|
| **No printer on this machine** (a render box) | `"moonraker_url": null, "fallback": "none"`. Both are required: one alone is refused as a mistake. |
| **Moonraker, no Obico, or Obico cloud** | your `moonraker_url`, `"fallback": "none"`, and `when_unreachable`. `"refuse"` (the default) is safe. `"allow"` means a printer you can't reach is assumed to be off. |
| **Moonraker + self-hosted Obico** | your `moonraker_url`, `"fallback": "obico-db"`, your `printer_name`, and `credentials`. |

**Why the self-hosted Obico route reads the database.** Obico's REST API only accepts a session or OAuth2 login,
and the only token it issues is the printer's own identity. So the guard reads one column through a **read-only**
Postgres role. Give that role `SELECT` on `app_printer`'s `id`, `name`, `current_print_id` and `archived_at` only,
never on `auth_token`. Obico cloud users have no database to read, so they use `fallback: "none"`.

Rules the guard keeps, whatever the file says:

- **A bad file refuses every job.** A file that's missing, isn't JSON, has an unknown value (a typo like `"alow"`)
  or holds a login instead of a reference blocks every job, and the reason is written to `status.json`. It is
  never treated as "no file".
- **The environment variables win.** `MOONRAKER_URL`, `OBICO_PRINTER_NAME` and `OBICO_PG_SECRET` override the file,
  so existing cluster templates behave exactly as before.
- **`GPU_BATCH_PRINT_GUARD=off` is unchanged.** It skips the guard only when the container can't reach a GPU.

## What isn't ready yet

Found by reading the code on 2026-11-14. These keep an outsider from following the steps above unchanged:

1. ~~**The print guard is hard-wired to this homelab.**~~ **Done (branch `docs/using-the-batch-tools`):** the guard
   now reads its settings from the values file. See [Configuring the print guard](#configuring-the-print-guard).
2. **No public images.** CI pushes only to the internal Harbor. **Built, off until you switch it on:** see
   [Publishing the images](#publishing-the-images-licences-and-sources). What's left is a human step: set the
   repository variable `PUBLISH_GHCR` to `true`, cut a release, then make each new GHCR package public.
3. **No user-facing compose file or chain script.** Only the test harness exists (route 3 above).
4. **The scripts the images run have hardcoded values.** The FreeCAD/Blender scripts (mesh export, `render.py`, clash
   library) are in `dvystrcil/vt350`'s `platform/`, and they still default to paths and constants from this laptop
   (`/home/dan/...` paths, the FreeCAD AppImage location, model folder, worker counts). **Decision (2026-11-14):**
   we won't rewrite them to be generic. Instead, each one slowly gets **configurable**: we ship one values file
   with today's defaults, and a script reads its settings from there instead of from a constant. One script at a
   time, keeping the current behaviour identical. Someone else then edits the values file, not the code. This is
   the same idea as the "one printer description file" in vt350 #122 phase 3. The order, the format and whether the
   guard settings (gap 1) go in the same file are still open.
5. **The slicer presets are one person's printer.** The OrcaSlicer fixtures are copies of a specific printer's presets.
   Users need to drop in their own, and this page should say where.
6. **The cluster route depends on homelab-only pieces**: Harbor, NFS, n8n, the submit script in `homelab/bin`,
   and a policy that admits only `harbor.sirddail.net/ai/gpu-batch-*` images. That's fine to keep private. It should be
   described as optional, not required.

## Publishing the images: licences and sources

The tools inside the images are free software (GPL, LGPL, AGPL), and their licences allow redistribution. But
putting a binary on a public registry **is** distribution, and it comes with two duties. We have to ship the licence
texts, and say where the exact source of each binary is. This is what the images and the release do about it:

| What | Where |
|---|---|
| Our own licence (MIT) | `/licenses/gpu-batch-docker/LICENSE` in every image |
| Each tool's licence texts, including the libraries it bundles | `/licenses/blender`, `/licenses/freecad`, `/licenses/fasteners-workbench`, `/licenses/prusaslicer`, `/licenses/orcaslicer`, `/licenses/gnome-runtime`. The build fails if any of them is missing upstream |
| Ubuntu's own packages | `/usr/share/doc/<package>/copyright`, as in any Ubuntu image |
| Where the exact source of each pinned binary is | [`sources.json`](../sources.json), also at `/licenses/sources.json` in every image |
| A copy of that source | attached to each GitHub release by `bin/mirror-sources.py` before any image reaches GHCR |
| SPDX labels | `org.opencontainers.image.licenses` names our licence and the main tool's. The bundled libraries are too many for one label, so they're listed in `/licenses` |

**Why we keep our own copy of the source.** A GPL licence lets you point at upstream's copy, but only as long as
upstream keeps it available. Blender, FreeCAD and the slicers keep old releases, but a Flathub manifest commit or
a release candidate tag could disappear. Attaching the archives to our release means the source stays available as
long as the image does.

**Why `sources.json` can't go stale.** `tests/test_licensing.py` fails if a Dockerfile pin (a version, sha256 or
commit) has no entry in `sources.json`, or if `sources.json` names a pin the Dockerfile no longer builds. A Renovate
or manual version bump therefore has to update the source record in the same PR.

**What `sources.json` points at and doesn't mirror.** The FreeCAD AppImage bundles about 30 conda-forge libraries
(listed in `/opt/freecad/packages.txt`). The GNOME runtime holds hundreds of components. For those, the record
points at the build recipe (the conda-forge feedstocks, and `gnome-build-meta` at the pinned commit) instead of
mirroring every library. Upstream FreeCAD, Flathub and GNOME do the same when they distribute these binaries.

**Turning it on** (the release workflow's `mirror-sources` and `publish-ghcr` jobs):

1. Set the repository variable `PUBLISH_GHCR` to `true` (Settings → Secrets and variables → Actions → Variables).
2. Publish a release as usual. `promote` tags the image in Harbor, `mirror-sources` attaches the source archives
   to the release, and then `publish-ghcr` copies the **same digest** to `ghcr.io/dvystrcil/gpu-batch-<tool>`.
3. A new GHCR package starts private. Make each one public once (the package's settings page on GitHub).

## Keeping the slicers current

OrcaSlicer and PrusaSlicer come from Flathub. Renovate can't see Flathub, so `.github/workflows/flathub-bump.yaml`
asks it every day (06:17 UTC, or run it by hand from the Actions tab):

1. A throwaway `ubuntu:26.04` container runs `flatpak remote-info` for the slicer and the GNOME runtime it uses.
   It fetches Flathub's appstream data first, because without it a fresh container reports no version.
2. `bin/flathub-bump.py` moves only the pin lines: the Dockerfile `ARG`s, the `sources.json` record (version,
   ostree commit, upstream source tag, Flathub manifest commit), and the version the README and this doc name. It
   writes nothing unless every edit applies. It refuses a move to a new runtime **branch** (GNOME 50 → 51), because
   that changes the libraries both slicers run on.
3. If anything moved, it opens a PR on `update/<slicer>-<version>-…` using the CICD App token. That way CI runs on
   the PR: the licensing tests and the compose harness, which slices with real presets in the new build.
4. **The PR waits for you.** Nothing merges it automatically, because a new slicer can change the G-code and the
   time and filament estimates even when the harness passes. The PR body has the checklist.

Why it's built this way: the images pin the same Flathub build the laptop runs, so a slice in the cluster can be
compared byte for byte with one on the laptop. Flathub updates the laptop on its own, so the images have to follow,
or the two drift apart. On 2026-11-14 the laptop was already on OrcaSlicer 2.5.1 while the image still pinned 2.5.0.

## Where the pieces live

| Repo | Public | Holds |
|---|---|---|
| `dvystrcil/gpu-batch-docker` (this one) | yes | the images, the runner, the compose harness |
| `dvystrcil/gpu-batch` | yes | Kubernetes Job templates (the cluster route) |
| `dvystrcil/vt350` → `platform/` | yes | the generic FreeCAD/Blender/slicer scripts the jobs run (see gap 4) |
| `dvystrcil/homelab` → `bin/gpu-batch-submit.py`, `architecture/gpu-batch-lane.md` | yes | the submit CLI and the design doc for the cluster route |
