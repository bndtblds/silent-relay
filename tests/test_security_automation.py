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


def test_upstream_caddy_findings_warn_but_do_not_block_the_build():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    evaluation = workflow.split("- name: Evaluate vulnerability policy", 1)[1]

    blocking_condition = evaluation.split("then", 1)[0]
    assert '"$DEPENDENCY_OUTCOME" != success' in blocking_condition
    assert '"$APPLICATION_OUTCOME" != success' in blocking_condition
    assert '"$CADDY_OUTCOME" != success' not in blocking_condition
    assert "::warning::The upstream Caddy image contains" in evaluation


def test_security_workflow_actions_are_pinned_to_full_commits():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action_references = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, re.MULTILINE)

    assert action_references
    assert all(re.fullmatch(rf"[^@]+@{FULL_COMMIT}", value) for value in action_references)
