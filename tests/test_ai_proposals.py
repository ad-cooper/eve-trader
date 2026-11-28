import json
from pathlib import Path
from services.retrospective import autopilot


def test_stage1_updates_target_desk_config_not_ledgers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "services"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    desk_cfg_file = cfg_dir / "desk_config.py"
    desk_cfg_file.write_text(
        'DESK_PROFILES = {\n'
        '    "tactical": DeskProfile(\n'
        '        name="Tactical",\n'
        '        min_margin=9.0,\n'
        '        live_min_margin=9.5,\n'
        '        min_dump_freq_pct=35.0,\n'
        '    ),\n'
        '}\n',
        encoding="utf-8",
    )

    ledgers_file = cfg_dir / "ledgers.py"
    ledgers_file.write_text("# untouched sentinel\n", encoding="utf-8")

    monkeypatch.setattr(autopilot, "DESK_CONFIG_PATH", "services/desk_config.py")
    monkeypatch.setattr(autopilot, "check_autopilot_allowed", lambda: (True, "ai-autopilot", "ok"))
    monkeypatch.setenv("GITHUB_TOKEN", "")

    review_result = {
        "model": "gemini-3.6-flash",
        "deep_code_review": {
            "is_significant_improvement": False,
            "apply_stage1_parameters": True,
            "approved_stage1_parameters": [
                {
                    "desk": "tactical",
                    "parameter": "min_dump_freq_pct",
                    "current_value": 35.0,
                    "proposed_value": 25.0,
                    "rationale": "Broader tactical candidate pool",
                }
            ],
            "confidence_score": 0.9,
            "estimated_monthly_pnl_uplift_pct": 15.0,
            "architectural_diagnosis": "Test diagnosis",
            "algorithmic_proposals": [],
            "recommended_unit_tests": [],
        },
    }

    # Without GITHUB_TOKEN, create_and_merge_github_pr returns "skipped" early;
    # test _apply_proposals_in_memory + _apply_approved_stage1_to_desk_config directly
    # as well as bundle path inclusion.
    patched_src, applied = autopilot._apply_approved_stage1_to_desk_config(
        desk_cfg_file.read_text(encoding="utf-8"),
        review_result["deep_code_review"]["approved_stage1_parameters"],
    )
    assert len(applied) == 1
    assert applied[0]["desk"] == "tactical"
    assert applied[0]["new_value"] == 25.0
    assert "min_dump_freq_pct=25.0," in patched_src
    assert ledgers_file.read_text(encoding="utf-8") == "# untouched sentinel\n"


def test_apply_proposals_in_memory_enforces_allowlist_and_ast(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "services").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "services" / "accounting.py"
    target.write_text("FEE_RATE = 0.01\n", encoding="utf-8")

    proposals = [
        {
            "title": "Valid accounting update",
            "file_path": "services/accounting.py",
            "target_function": "FEE_RATE",
            "expected_impact": "Lower fee",
            "rationale": "Test",
            "before_code": "FEE_RATE = 0.01",
            "after_code": "FEE_RATE = 0.005",
        },
        {
            "title": "Disallowed path",
            "file_path": "services/ledgers.py",
            "target_function": "foo",
            "expected_impact": "None",
            "rationale": "Test",
            "before_code": "a = 1",
            "after_code": "a = 2",
        },
        {
            "title": "Invalid syntax",
            "file_path": "services/accounting.py",
            "target_function": "FEE_RATE",
            "expected_impact": "None",
            "rationale": "Test",
            "before_code": "FEE_RATE = 0.01",
            "after_code": "FEE_RATE = ",
        },
    ]

    updated, skipped = autopilot._apply_proposals_in_memory(proposals, unit_tests=[])
    assert "services/accounting.py" in updated
    assert "FEE_RATE = 0.005" in updated["services/accounting.py"]
    assert len(skipped) == 2


def test_bundle_local_codebase_reads_desk_config_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "services").mkdir(parents=True, exist_ok=True)
    (tmp_path / "services" / "desk_config.py").write_text("DESK_PROFILES = {}\n", encoding="utf-8")
    monkeypatch.setattr(autopilot, "DESK_CONFIG_PATH", "services/desk_config.py")

    bundle = autopilot._bundle_local_codebase()
    assert "### File: services/desk_config.py" in bundle
    assert "DESK_PROFILES = {}" in bundle