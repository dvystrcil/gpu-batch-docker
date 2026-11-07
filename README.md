# gpu-batch-docker

Build repo for the **GPU batch lane** on the GTX 970 (`k8s-node-03`): fire-and-forget batch work whose completion is pushed, not polled ([homelab#1935](https://github.com/dvystrcil/homelab/issues/1935); design in [`homelab/architecture/gpu-batch-lane.md`](https://github.com/dvystrcil/homelab/blob/main/architecture/gpu-batch-lane.md)).

| Image | Contents |
|---|---|
| `harbor.sirddail.net/ai/gpu-batch-blender` | Ubuntu 26.04, Blender 5.2.2 LTS (official tarball, sha256-pinned), the `gpu_batch` runner, psql |
| `harbor.sirddail.net/ai/gpu-batch-freecad` | Ubuntu 26.04, FreeCAD 26.3rc2 (official AppImage, sha256-pinned, extracted), Fasteners workbench 0.5.64 (commit-pinned), `xvfb-run`, the `gpu_batch` runner, psql. CPU-only (max-01) |
| `harbor.sirddail.net/ai/gpu-batch-prusaslicer` | Ubuntu 26.04, PrusaSlicer 2.9.7 (Flathub build, app + GNOME 50 runtime pinned by ostree commit, run without flatpak), `rsvg-convert`, the `gpu_batch` runner, psql. CPU-only (max-01) |

Deployed by [`dvystrcil/gpu-batch`](https://github.com/dvystrcil/gpu-batch) (Job templates), submitted with `homelab/bin/gpu-batch-submit.py`.

## The runner contract

A job is a directory on the NFS work share. The runner (`python3 -m gpu_batch.runner <job>/spec.json`, the image entrypoint):

1. **Guard:** refuses to start while Neptune is printing (`src/gpu_batch/guard.py`). Moonraker first; Obico's `current_print_id` breaks the tie when the printer is unreachable. Every unknown fails closed. It re-checks every 60 s during a step and stops the step if a print starts.
   - **CPU-only templates** (FreeCAD, PrusaSlicer on max-01) set `GPU_BATCH_PRINT_GUARD=off` (exactly `off`). That skips the guard **only if the container cannot reach a GPU**: no `/dev/nvidia<N>` and no `NVIDIA_VISIBLE_DEVICES` naming one. If a GPU is visible, the switch is ignored and the reason says so, so setting it on a render does nothing. The guard protects the GTX 970's VRAM, and a job without a GPU can't touch it.
2. **Steps:** runs `spec.steps` in order, `{job}` expanded, each in its own process group with a timeout capped by the Job deadline minus 120 s. stdout goes to `job.log` and stderr to `job.err`.
3. **Proof:** success means `expect_outputs` exist, are non-empty, and are valid PNGs where `.png`, with at least `min_outputs` of them. Exit 0 alone is not success.
4. **Report:** writes `status.json` atomically at every transition, then POSTs it once to `GPU_BATCH_NOTIFY_URL` (the n8n receiver, which comments on GitHub).

| Exit | Meaning |
|---|---|
| 0 | succeeded, outputs verified |
| 1 | failed or aborted (step error, timeout, SIGKILL/OOM, missing outputs, bad spec) |
| 3 | refused by the print guard; no step ran |

Adding a workload: add a `FROM runner AS <name>` stage and a build step, plus a template in `gpu-batch`.

## Development

```bash
python3 -m pytest -q                                   # unit + composition tests (python 3.14 = the image)
docker build --target blender -t gpu-batch-blender:local .
bin/smoke-test.sh gpu-batch-blender:local              # CPU render through the real entrypoint, plus a refusal
docker build --target freecad -t gpu-batch-freecad:local .
bin/smoke-test-freecad.sh gpu-batch-freecad:local      # headless Fasteners screw + GUI mode under xvfb-run, plus a refusal
docker build --target prusaslicer -t gpu-batch-prusaslicer:local .
bin/smoke-test-prusaslicer.sh gpu-batch-prusaslicer:local  # slice a cube, check the G-code; refusal; guard-off CPU run while printing
```

Bumping Blender: change `BLENDER_VERSION` and `BLENDER_SHA256` together, both taken from `https://download.blender.org/release/Blender<major.minor>/blender-<ver>.sha256`.

Bumping FreeCAD: change `FREECAD_VERSION` and `FREECAD_SHA256` together. The checksum is the `digest` GitHub publishes for the `FreeCAD_<ver>-Linux-x86_64.AppImage` asset (`gh api repos/FreeCAD/FreeCAD/releases/tags/<ver> --jq '.assets[]|.name+" "+.digest'`); FreeCAD ships no separate checksum file. Fasteners: change `FASTENERS_REF` and `FASTENERS_COMMIT` together (the build fails if the fetched commit is not the pinned one). In GUI mode FreeCAD *imports* a `.py` argument as a module, so a script whose name shadows a loaded module (e.g. `gui.py`) never runs.

Bumping PrusaSlicer: Linux builds ship only through Flathub, so the `prusaslicer-fetch` stage installs `com.prusa3d.PrusaSlicer` with flatpak, pins it and its runtime to exact ostree commits (content hashes of the whole tree, so they are the integrity check), and the final stage copies just the files. Change `PRUSASLICER_VERSION`, `PRUSASLICER_COMMIT`, `GNOME_RUNTIME` and `GNOME_RUNTIME_COMMIT` together, from `flatpak info com.prusa3d.PrusaSlicer` and `flatpak info org.gnome.Platform//<branch>` on a machine running the version you want; the build fails if a commit or the version doesn't match. `prusa-slicer` runs on the runtime's own `ld-linux` and libraries, not through `flatpak run`: bwrap needs user namespaces, which a pod's seccomp profile refuses. Pass `--datadir` under the job dir; a pod's HOME isn't writable.

Licensed [MIT](LICENSE).
