# gpu-batch-docker

Build repo for the **GPU batch lane** on the GTX 970 (`k8s-node-03`): fire-and-forget batch work whose completion is pushed, not polled ([homelab#1935](https://github.com/dvystrcil/homelab/issues/1935); design in [`homelab/architecture/gpu-batch-lane.md`](https://github.com/dvystrcil/homelab/blob/main/architecture/gpu-batch-lane.md)).

| Image | Contents |
|---|---|
| `harbor.sirddail.net/ai/gpu-batch-blender` | Ubuntu 24.04, Blender 5.2.2 LTS (official tarball, sha256-pinned), the `gpu_batch` runner, psql |

Deployed by [`dvystrcil/gpu-batch`](https://github.com/dvystrcil/gpu-batch) (Job templates), submitted with `homelab/bin/gpu-batch-submit.py`.

## The runner contract

A job is a directory on the NFS work share. The runner (`python3 -m gpu_batch.runner <job>/spec.json`, the image entrypoint):

1. **Guard:** refuses to start while Neptune is printing (`src/gpu_batch/guard.py`). Moonraker first; Obico's `current_print_id` breaks the tie when the printer is unreachable. Every unknown fails closed. It re-checks every 60 s during a step and stops the step if a print starts.
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
python3 -m pytest -q                                   # unit + composition tests (python 3.12 = the image)
docker build --target blender -t gpu-batch-blender:local .
bin/smoke-test.sh gpu-batch-blender:local              # CPU render through the real entrypoint, plus a refusal
```

Bumping Blender: change `BLENDER_VERSION` and `BLENDER_SHA256` together, both taken from `https://download.blender.org/release/Blender<major.minor>/blender-<ver>.sha256`.

Licensed [MIT](LICENSE).
