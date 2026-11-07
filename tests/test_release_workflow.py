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
