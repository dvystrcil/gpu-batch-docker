"""A release deploys itself (homelab D-049, gpu-batch-docker#33).

docker-release.yaml's `deploy` job hands the released tag to
dvystrcil/release-action@v1, which edits the `newTag:` lines in
dvystrcil/gpu-batch, renders every kustomize root, and commits to main (or,
with the repo variable AUTO_DEPLOY=false, opens a PR). It replaces the
hand-rolled bump-PR job this repo used to run.

Read as text, not through a YAML parser: the CI job installs only pytest.
"""
import pathlib
import re

import pytest

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/docker-release.yaml"
GPU_BATCH = WORKFLOW.parents[3] / "gpu-batch"

# The images dvystrcil/gpu-batch deploys (overlays/kustomization.yaml). The
# slicers are built and promoted, but no CronJob runs them yet: release-action
# fails a release for a listed image the deploy repo has no newTag for.
DEPLOYED = {"harbor.sirddail.net/ai/gpu-batch-blender",
            "harbor.sirddail.net/ai/gpu-batch-freecad"}


def _job(name: str) -> str:
    """The text of one top-level job, from its key to the next job's."""
    text = WORKFLOW.read_text()
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text, re.M | re.S)
    assert m, f"no job {name!r} in {WORKFLOW.name}"
    return m.group(1)


def _input(job: str, key: str) -> str:
    m = re.search(rf"^\s+{re.escape(key)}:[ \t]*(.*)$", job, re.M)
    assert m, f"no {key}: in the job"
    return m.group(1).strip()


def _deploy_images(job: str) -> list[str]:
    """`deploy-images:` inline or as a block scalar (| or >-)."""
    m = re.search(r"^(\s+)deploy-images:[ \t]*(.*)\n((?:\1\s+\S.*\n)*)", job, re.M)
    assert m, "no deploy-images: in the deploy job"
    inline, block = m.group(2).strip(), m.group(3)
    return block.split() if inline in ("|", ">-", ">", "|-") else inline.split()


def test_the_bump_pr_job_is_gone():
    text = WORKFLOW.read_text()
    assert "bump-deploy-repo" not in text
    assert "gh pr create" not in text


def test_deploy_runs_after_promote_one_at_a_time():
    job = _job("deploy")
    assert _input(job, "needs") == "promote"
    assert re.search(r"concurrency:\s*\n\s+group: deploy-dvystrcil/gpu-batch\s*\n"
                     r"\s+cancel-in-progress: false", job)


def test_deploy_uses_release_action_without_cutting_a_release():
    job = _job("deploy")
    assert re.search(r"^\s+uses: dvystrcil/release-action@v1\s*$", job, re.M)
    assert _input(job, "create-release") == "false"
    assert _input(job, "version") == "${{ github.event.release.tag_name }}"
    assert _input(job, "deploy-repo") == "dvystrcil/gpu-batch"
    assert _input(job, "auto-deploy") == "${{ vars.AUTO_DEPLOY }}"


def test_deploy_images_are_exactly_the_deployed_ones():
    assert sorted(_deploy_images(_job("deploy"))) == sorted(DEPLOYED)


def test_every_deployed_image_is_promoted_first():
    # The tag must exist in Harbor before the deploy repo points at it.
    m = re.search(r"image: \[([^\]]+)\]", _job("promote"))
    promoted = {f"harbor.sirddail.net/ai/{i.strip()}" for i in m.group(1).split(",")}
    assert DEPLOYED <= promoted


def test_the_token_is_the_cicd_app_scoped_to_gpu_batch():
    job = _job("deploy")
    assert "actions/create-github-app-token@" in job
    assert _input(job, "repositories") == "gpu-batch"
    assert _input(job, "token") == "${{ steps.app-token.outputs.token }}"
    assert "secrets.GITHUB_TOKEN" not in job


def test_infisical_fetches_one_named_secret_per_step():
    job = _job("deploy")
    steps = job.count("uses: Infisical/secrets-action@")
    names = re.findall(r"^\s+secret-name: (\S+)\s*$", job, re.M)
    assert steps == 2
    assert names == ["CICD_APP_CLIENT_ID", "CICD_APP_PRIVATE_KEY"]


def test_deploy_images_match_the_deploy_repo():
    # Against gpu-batch's own overlay, when a checkout is next to this repo.
    k = GPU_BATCH / "overlays/kustomization.yaml"
    if not k.is_file():
        pytest.skip("no gpu-batch checkout alongside")
    listed = set(re.findall(r"^-\s*name:\s*(harbor\.sirddail\.net/ai/gpu-batch-\S+)\s*$",
                            k.read_text(), re.M))
    assert set(_deploy_images(_job("deploy"))) == listed
