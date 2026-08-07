import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
FULL_COMMIT = r"[0-9a-f]{40}"


def test_security_workflow_scans_dependencies_and_images():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "scan-type: fs" in workflow
    assert "APPLICATION_IMAGE" in workflow
    assert "CADDY_IMAGE" in workflow
    assert workflow.count("format: sarif") == 3
    assert workflow.count("format: cyclonedx") == 3
    assert "severity: HIGH,CRITICAL" in workflow
    assert "ignore-unfixed: true" in workflow


def test_security_workflow_is_scheduled_and_retains_traceable_results():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "schedule:" in workflow
    assert "metadata.txt" in workflow
    assert "security-reports-${{ github.sha }}" in workflow
    assert "retention-days: 90" in workflow
    assert "upload-sarif" in workflow


def test_security_workflow_actions_are_pinned_to_full_commits():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action_references = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, re.MULTILINE)

    assert action_references
    assert all(re.fullmatch(rf"[^@]+@{FULL_COMMIT}", value) for value in action_references)
