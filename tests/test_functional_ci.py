import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
FULL_COMMIT = r"[0-9a-f]{40}"


def test_functional_workflow_runs_the_complete_locked_suite():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "branches:\n      - main" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "uv sync --locked --extra test" in workflow
    assert "uv run pytest" in workflow
    assert "tests/" not in workflow
    assert "scripts/check_version.py" in workflow


def test_functional_workflow_publishes_branch_coverage_without_arbitrary_gate():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "--cov-branch" in workflow
    assert "--cov-report=xml:coverage/coverage.xml" in workflow
    assert "--cov-report=html:coverage/html" in workflow
    assert "name: coverage-${{ github.sha }}" in workflow
    assert "fail-under" not in workflow


def test_functional_workflow_actions_are_pinned_to_full_commits():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action_references = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, re.MULTILINE)

    assert action_references
    assert all(re.fullmatch(rf"[^@]+@{FULL_COMMIT}", value) for value in action_references)
