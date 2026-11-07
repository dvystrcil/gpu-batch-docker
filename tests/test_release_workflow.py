"""The bump PR this repo opens on dvystrcil/gpu-batch must pass homelab's
auto-merge gate (homelab bin/pr-auto-merge.py, class "image-bump").

The gate needs EVERY commit on the PR to be authored by `homelab-bump-bot`.
A commit with user.email noreply@github.com is attributed by GitHub to the
`web-flow` account, so the gate held every bump (gpu-batch#14: "commit author
'web-flow' is not 'homelab-bump-bot'"), and each one waited for a human.
homelab's own bump-image-tag.py uses this identity:
homelab-bump-bot <noreply@sirddail.net>.
"""
import pathlib
import re

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/docker-release.yaml"


def _bump_step() -> str:
    text = WORKFLOW.read_text()
    start = text.index("- name: Open the tag bump PR")
    return text[start:]


def test_bump_commit_is_authored_by_the_bump_bot():
    step = _bump_step()
    names = re.findall(r'git config user\.name "([^"]+)"', step)
    assert names == ["homelab-bump-bot"]


def test_bump_commit_email_is_not_a_github_account():
    step = _bump_step()
    emails = re.findall(r'git config user\.email "([^"]+)"', step)
    assert emails == ["noreply@sirddail.net"]


def test_bump_branch_matches_the_gate_class():
    # pr-auto-merge.py IMAGE_BUMP_BRANCH = ^bump/.+-\d+\.\d+\.\d+$
    step = _bump_step()
    m = re.search(r'BR="(bump/[^"]+)"', step)
    assert m
    branch = m.group(1).replace("${VERSION}", "0.0.11")
    assert re.match(r"^bump/.+-\d+\.\d+\.\d+$", branch)


# --- the receipt-target marker (homelab#1992) -------------------------------
# homelab's merge-receipt sweep would read this bump's namespace from the
# diff as `overlays`, and a suspended CronJob never runs a Job to verify.
# The PR body names the namespace and the templates the bump changed:
#   receipt-target: image-bump ns=gpu-batch workloads=blender-render,freecad-sweep
import subprocess

SCRIPT = WORKFLOW.parents[1] / "scripts/receipt-workloads.sh"

CRONJOB = """\
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {name}
  namespace: gpu-batch
spec:
  suspend: true
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: x
              image: harbor.sirddail.net/ai/{image}
"""


def _deploy_repo(tmp_path, templates):
    base = tmp_path / "base"
    base.mkdir()
    for name, image in templates.items():
        (base / f"{name}.yaml").write_text(CRONJOB.format(name=name, image=image))
    (base / "serviceaccount.yaml").write_text(
        "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: gpu-batch-runner\n")
    return tmp_path


def _run(root, *images):
    return subprocess.run(["bash", str(SCRIPT), str(root), *images],
                          capture_output=True, text=True)


def test_workloads_are_the_cronjobs_using_the_bumped_images(tmp_path):
    root = _deploy_repo(tmp_path, {"blender-render": "gpu-batch-blender",
                                   "freecad-sweep": "gpu-batch-freecad"})
    p = _run(root, "gpu-batch-blender", "gpu-batch-freecad")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "blender-render,freecad-sweep"


def test_only_the_bumped_images_count(tmp_path):
    root = _deploy_repo(tmp_path, {"blender-render": "gpu-batch-blender",
                                   "freecad-sweep": "gpu-batch-freecad"})
    p = _run(root, "gpu-batch-freecad")
    assert p.stdout.strip() == "freecad-sweep"


def test_an_image_prefix_is_not_a_match(tmp_path):
    # gpu-batch-blender must not match gpu-batch-blender-nightly.
    root = _deploy_repo(tmp_path, {"blender-render": "gpu-batch-blender-nightly"})
    p = _run(root, "gpu-batch-blender")
    assert p.returncode != 0


def test_a_bumped_image_no_template_uses_fails(tmp_path):
    # Half a target is worse than none: refuse rather than emit a partial list.
    root = _deploy_repo(tmp_path, {"blender-render": "gpu-batch-blender"})
    p = _run(root, "gpu-batch-blender", "gpu-batch-freecad")
    assert p.returncode != 0
    assert "gpu-batch-freecad" in p.stderr


def test_the_real_deploy_repo_shape(tmp_path):
    # Against gpu-batch's own base/, when a checkout is next to this repo.
    real = WORKFLOW.parents[3] / "gpu-batch"
    if not (real / "base").is_dir():
        import pytest
        pytest.skip("no gpu-batch checkout alongside")
    p = _run(real, "gpu-batch-blender", "gpu-batch-freecad")
    assert p.stdout.strip() == "blender-render,freecad-sweep", p.stderr


def test_the_pr_body_carries_the_marker():
    step = _bump_step()
    assert "receipt-workloads.sh" in step
    assert re.search(r"receipt-target: image-bump ns=gpu-batch workloads=\$\{?WORKLOADS", step)
