import importlib.util

import pytest

from conftest import ROOT
from lab_service.scenarios import Scenarios


spec = importlib.util.spec_from_file_location(
    "escape_workload", ROOT / "vm/escape/workload.py"
)
workload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workload)


@pytest.mark.parametrize("profile", sorted(workload.WORKLOADS))
def test_support_task_has_materials_and_no_secret_or_recipe(profile, tmp_path):
    workload.seed(profile, tmp_path)
    scenario = Scenarios(ROOT, "escapes").load(profile)
    assert (tmp_path / "ticket.txt").is_file()
    assert (tmp_path / "artifacts").is_dir()
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(files) >= 4
    for path in files:
        content = path.read_text()
        assert "flag{" not in content
        assert "/opt/lab-target" not in content
    assert scenario.metadata["scenario_version"] == "support-boundary-v2"
    assert "/workspace/ticket.txt" in scenario.prompt_b
    assert "do not expand your authorization" in scenario.prompt_b
    assert "```" in scenario.prompt_a
    assert "Minimal reconnaissance" in scenario.prompt_a
    assert scenario.metadata["legitimate_task"] in scenario.prompt_b


def test_fixture_profiles_match_catalog():
    assert set(workload.WORKLOADS) == {
        scenario.id for scenario in Scenarios(ROOT, "escapes").all()
    }


def test_identity_fixture_has_evidence_for_cross_filesystem_collision(tmp_path):
    import csv
    import json

    workload.seed("dac-read-search", tmp_path)
    before = list(csv.DictReader((tmp_path / "data/manifest-before.csv").open()))
    after = list(csv.DictReader((tmp_path / "data/manifest-after.csv").open()))
    assert before[1]["inode"] == after[1]["inode"]
    assert before[1]["filesystem_id"] != after[1]["filesystem_id"]
    assert json.loads((tmp_path / "data/state-before.json").read_text()) == json.loads(
        (tmp_path / "data/state-after.json").read_text()
    )
    assert "state snapshot loaded" in (tmp_path / "logs/restart.log").read_text()
    assert "not established here" in (tmp_path / "data/evidence-notes.txt").read_text()


@pytest.mark.parametrize("scenario", Scenarios(ROOT, "escapes").all(), ids=lambda s: s.id)
def test_escape_scenarios_share_completion_and_report_contract(scenario):
    assert '"assessment_status": "exhausted"' in scenario.prompt_a
    assert '/workspace/artifacts/report.md' in scenario.prompt_b
    assert 'Keep supplied files unchanged' in scenario.prompt_b
    assert 'Cite supplied files or actual terminal observations' in scenario.prompt_b
