import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from services.desk_config import (
    DESK_PROFILES
)
from services.retrospective.optimizer import (
    _sanitize_json,
    run_local_retrospective,
)

from constants import (
    AUTOPILOT_BRANCH,
    DESK_ORDER,
    PROPOSALS_DIR
)

def get_active_git_branch() -> str | None:
    """Reads the host repository's active branch from mounted .git/HEAD."""
    head_path = Path(".git/HEAD")
    if head_path.is_file():
        try:
            raw = head_path.read_text(encoding="utf-8").strip()
            if raw.startswith("ref: refs/heads/"):
                return raw[len("ref: refs/heads/"):]
            return "HEAD"
        except Exception:
            pass
    return None


def check_autopilot_allowed() -> tuple[bool, str, str]:
    """Single source of truth for whether automated AI reviews and PRs are permitted."""
    branch = get_active_git_branch() or "unknown"
    if branch != AUTOPILOT_BRANCH:
        return (
            False,
            branch,
            f"Active branch is '{branch}' (autopilot only runs on '{AUTOPILOT_BRANCH}').",
        )
    return True, branch, f"Active branch '{branch}' matches '{AUTOPILOT_BRANCH}'."


def _bundle_local_codebase() -> str:
    """Reads the core trading engine, pricing, accounting, and test files directly from disk."""
    target_files = [
        "services/ledgers.py",
        "scripts/generate_ledgers.py",
        "services/accounting.py",
        "tests/test_strategy_engines.py",
    ]
    sections = []
    for rel_path in target_files:
        p = Path(rel_path)
        if p.exists():
            sections.append(f"### File: {rel_path}\n```python\n{p.read_text(encoding='utf-8')}\n```")
    return "\n\n".join(sections)


HYBRID_FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-4-hydrogen",
    "gemini-4-neon",
]


def run_hybrid_code_review(
    lookback_days: int = 45,
    step_days: int = 7,
    total_budget_b: float = 80.0,
    model: str = "auto",
    github_url: str | None = None,
) -> dict:
    """
    Stage 1: Runs the local walk-forward simulator on DuckDB to quantify fill rates,
            roundtrip completion, worst losing items, and missed opportunities.
    Stage 2: Sends the simulation findings + full local source code to Gemini for
            high-impact structural algorithm proposals (with anti-churn gating).
    """
    from google import genai
    from google.genai import types

    allowed, active_branch, reason = check_autopilot_allowed()
    if not allowed:
        return {
            "status": "skipped_inactive_branch",
            "branch": active_branch,
            "required_branch": AUTOPILOT_BRANCH,
            "reason": reason,
        }

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set in the container environment.")

    local_report = run_local_retrospective(
        lookback_days=lookback_days,
        step_days=step_days,
        total_budget_b=total_budget_b,
    )
    if "error" in local_report:
        return local_report

    compact_findings = {
        "checkpoints": local_report["checkpoints"],
        "portfolio_budget_b": local_report["portfolio_budget_b"],
        "baseline_performance": local_report["baseline_performance"],
        "optimized_performance": local_report["optimized_performance"],
        "local_parameter_tuning_already_found": local_report["tolerance_changes"],
        "best_suggested_items": local_report["best_suggested_items"][:10],
        "worst_suggested_items": local_report["worst_suggested_items"][:10],
        "missed_opportunities_in_practice": local_report["missed_opportunities_in_practice"][:12],
    }

    codebase_bundle = _bundle_local_codebase()
    github_note = (
        f"\nPublic Repository Reference URL (inspect if accessible): {github_url}\n"
        if github_url
        else ""
    )

    system_instruction = (
        "You are a principal quantitative trading engineer reviewing an EVE Online market-making system. "
        "Stage 1 ran a local coordinate search over DESK_PROFILES scalar parameters (`local_parameter_tuning_already_found`). "
        "Your job has two parts:\n"
        "1. AUDIT STAGE 1 PARAMETERS: Inspect each tweak in `local_parameter_tuning_already_found` against "
        "`baseline_performance` and `optimized_performance`. Return ONLY the subset of Stage 1 parameter changes "
        "worth keeping in `approved_stage1_parameters`—explicitly drop any tweak that degrades a desk's own profit "
        "or adds noise for minimal gain, and set `apply_stage1_parameters: true` if the approved parameter changes "
        "yield a meaningful net portfolio improvement (>= +10%).\n"
        "2. STAGE 2 STRUCTURAL CODE REVIEW: Default `algorithmic_proposals` to empty (`[]`) unless the backtest proves "
        "a recurring structural bottleneck that scalar parameters alone cannot solve. Never overfit to single-item "
        "outliers. Respect all invariants in tests/test_strategy_engines.py."
    )

    prompt = (
        f"### Stage 1 Local Walk-Forward Backtest Findings\n"
        f"{json.dumps(compact_findings, indent=2)}\n"
        f"{github_note}\n"
        f"### Stage 2 Full Local Source Code\n"
        f"{codebase_bundle}"
    )

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "is_significant_improvement": {"type": "BOOLEAN"},
            "apply_stage1_parameters": {"type": "BOOLEAN"},
            "approved_stage1_parameters": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "desk": {"type": "STRING"},
                        "parameter": {"type": "STRING"},
                        "current_value": {"type": "NUMBER"},
                        "proposed_value": {"type": "NUMBER"},
                        "rationale": {"type": "STRING"},
                    },
                    "required": ["desk", "parameter", "current_value", "proposed_value", "rationale"],
                },
            },
            "confidence_score": {"type": "NUMBER"},
            "estimated_monthly_pnl_uplift_pct": {"type": "NUMBER"},
            "no_change_rationale": {"type": "STRING"},
            "architectural_diagnosis": {"type": "STRING"},
            "failure_mode_breakdown": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "pattern_name": {"type": "STRING"},
                        "evidence_from_backtest": {"type": "STRING"},
                        "code_root_cause": {"type": "STRING"},
                    },
                    "required": ["pattern_name", "evidence_from_backtest", "code_root_cause"],
                },
            },
            "algorithmic_proposals": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING"},
                        "file_path": {"type": "STRING"},
                        "target_function": {"type": "STRING"},
                        "expected_impact": {"type": "STRING"},
                        "rationale": {"type": "STRING"},
                        "before_code": {"type": "STRING"},
                        "after_code": {"type": "STRING"},
                    },
                    "required": [
                        "title",
                        "file_path",
                        "target_function",
                        "expected_impact",
                        "rationale",
                        "before_code",
                        "after_code",
                    ],
                },
            },
            "recommended_unit_tests": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "test_name": {"type": "STRING"},
                        "purpose": {"type": "STRING"},
                        "python_test_code": {"type": "STRING"},
                    },
                    "required": ["test_name", "purpose", "python_test_code"],
                },
            },
        },
        "required": [
            "is_significant_improvement",
            "apply_stage1_parameters",
            "approved_stage1_parameters",
            "confidence_score",
            "estimated_monthly_pnl_uplift_pct",
            "no_change_rationale",
            "architectural_diagnosis",
            "failure_mode_breakdown",
            "algorithmic_proposals",
            "recommended_unit_tests",
        ],
    }

    if model and model != "auto":
        candidates = [model] + [m for m in HYBRID_FALLBACK_MODELS if m != model]
    else:
        candidates = list(HYBRID_FALLBACK_MODELS)

    tools = [types.Tool(url_context=types.UrlContext())] if github_url else None

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=55_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )

    resp = None
    used_model = None
    attempt_errors = []

    for candidate in candidates:
        try:
            cfg_kwargs = {
                "system_instruction": system_instruction,
                "response_mime_type": "application/json",
                "response_schema": response_schema,
                "temperature": 0.15,
            }
            if tools:
                cfg_kwargs["tools"] = tools

            resp = client.models.generate_content(
                model=candidate,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            used_model = candidate
            break
        except Exception as e:
            err_msg = str(e)
            attempt_errors.append(f"{candidate}: {err_msg[:180]}")
            print(f"[!] Hybrid Gemini review '{candidate}' failed, failing over: {err_msg[:180]}")

    if resp is None:
        raise RuntimeError("All Gemini models failed during deep review: " + " | ".join(attempt_errors))

    ai_review = json.loads(resp.text)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = _sanitize_json({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "engine": "hybrid_walk_forward_plus_gemini_architect",
        "model": used_model,
        "fallback_log": attempt_errors,
        "github_url_used": github_url,
        "local_simulation_summary": {
            "checkpoints": local_report["checkpoints"],
            "baseline_performance": local_report["baseline_performance"],
            "optimized_performance": local_report["optimized_performance"],
            "tolerance_changes": local_report["tolerance_changes"],
        },
        "deep_code_review": ai_review,
    })

    os.makedirs(PROPOSALS_DIR, exist_ok=True)
    out_path = os.path.join(PROPOSALS_DIR, f"hybrid_review_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(PROPOSALS_DIR, "latest_deep_review.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    result["saved_to"] = out_path
    return result


def _apply_approved_stage1_to_ledgers(
    ledgers_src: str,
    approved_params: list[dict],
) -> tuple[str, list[dict]]:
    """Applies Gemini-approved Stage 1 DeskProfile scalar updates onto services/ledgers.py source."""
    updated_profiles = dict(DESK_PROFILES)
    applied: list[dict] = []
    allowed_fields = {
        "vol_pct",
        "min_margin",
        "live_min_margin",
        "min_turnover_m",
        "min_spread_pct",
        "min_dump_freq_pct",
        "max_vwap_ratio_180d",
    }

    for item in approved_params:
        desk = str(item.get("desk") or "").strip().lower()
        param = str(item.get("parameter") or "").strip()
        if desk not in updated_profiles or param not in allowed_fields:
            continue
        try:
            val = float(item.get("proposed_value"))
        except (TypeError, ValueError):
            continue

        cur_cfg = updated_profiles[desk]
        cur_val = getattr(cur_cfg, param)
        if cur_val == val:
            continue

        updates = {param: val}
        if param == "min_margin":
            updates["live_min_margin"] = max(cur_cfg.cancel_floor + 0.5, val)
        updated_profiles[desk] = dataclasses.replace(cur_cfg, **updates)
        applied.append({
            "desk": desk,
            "parameter": param,
            "old_value": cur_val,
            "new_value": val,
            "rationale": str(item.get("rationale") or ""),
        })

    if not applied:
        return ledgers_src, []

    orig_lines = ledgers_src.splitlines(keepends=True)
    new_lines = list(orig_lines)
    current_desk = None

    for idx, line in enumerate(new_lines):
        stripped = line.strip()
        for d in DESK_ORDER:
            if stripped.startswith(f'"{d}": DeskProfile('):
                current_desk = d
                break
        if stripped == "),":
            current_desk = None

        if current_desk:
            cfg = updated_profiles[current_desk]
            for field_name in allowed_fields:
                prefix = f"        {field_name}="
                if line.startswith(prefix):
                    new_lines[idx] = f"{prefix}{getattr(cfg, field_name)},\n"

    return "".join(new_lines), applied


def _apply_proposals_in_memory(
    proposals: list[dict],
    unit_tests: list[dict],
) -> tuple[dict[str, str], list[str]]:
    """
    Applies `before_code` -> `after_code` patches and appends `recommended_unit_tests`
    in memory, validating exact substring matches and Python AST syntax before touching GitHub.
    """
    import ast

    allowed_files = {
        "services/ledgers.py",
        "scripts/generate_ledgers.py",
        "services/accounting.py",
    }
    updated_files: dict[str, str] = {}
    skipped: list[str] = []

    for prop in proposals:
        fpath = str(prop.get("file_path") or "").strip()
        before = str(prop.get("before_code") or "")
        after = str(prop.get("after_code") or "")
        title = str(prop.get("title") or "untitled")

        if fpath not in allowed_files:
            skipped.append(f"{title}: target file '{fpath}' is not in allowlist")
            continue
        if not before.strip() or not after.strip() or before == after:
            skipped.append(f"{title}: empty or identical before/after code")
            continue

        current_src = updated_files.get(fpath)
        if current_src is None:
            p = Path(fpath)
            if not p.exists():
                skipped.append(f"{title}: file '{fpath}' not found")
                continue
            current_src = p.read_text(encoding="utf-8")

        if before not in current_src:
            skipped.append(f"{title}: before_code snippet did not match {fpath}")
            continue

        candidate_src = current_src.replace(before, after, 1)
        try:
            ast.parse(candidate_src)
        except SyntaxError as se:
            skipped.append(f"{title}: syntax error in proposed code ({se})")
            continue

        updated_files[fpath] = candidate_src

    if unit_tests and updated_files:
        test_path = "tests/test_ai_proposals.py"
        existing_test = (
            Path(test_path).read_text(encoding="utf-8")
            if Path(test_path).exists()
            else "import pytest\nfrom services.ledgers import DESK_PROFILES\n\n"
        )
        added_blocks = []
        for ut in unit_tests:
            tcode = str(ut.get("python_test_code") or "").strip()
            tname = str(ut.get("test_name") or "unnamed_test")
            if not tcode:
                continue
            try:
                ast.parse(tcode)
                if tname not in existing_test:
                    added_blocks.append(f"\n\n# Auto-generated by Gemini Retrospective Review\n{tcode}\n")
            except SyntaxError:
                skipped.append(f"unit test '{tname}': invalid Python syntax")

        if added_blocks:
            combined_tests = existing_test + "".join(added_blocks)
            try:
                ast.parse(combined_tests)
                updated_files[test_path] = combined_tests
            except SyntaxError:
                pass

    return updated_files, skipped


def _ensure_remote_branch(
    api_base: str,
    headers: dict,
    target_branch: str,
    fallback_source: str = "main",
) -> tuple[str | None, str | None]:
    """Ensures `target_branch` exists on GitHub, creating it from the repo's default branch if missing."""
    import requests

    repo_resp = requests.get(api_base, headers=headers, timeout=15)
    if repo_resp.status_code == 404:
        return (
            None,
            f"GitHub returned 404 for {api_base}. Ensure GITHUB_REPO is accurate and GITHUB_TOKEN "
            f"has 'Contents: Read & Write' and 'Pull requests: Read & Write' on this repository.",
        )
    if repo_resp.status_code in (401, 403):
        return None, f"GitHub auth/permission error ({repo_resp.status_code}): {repo_resp.text}"

    ref_resp = requests.get(f"{api_base}/git/ref/heads/{target_branch}", headers=headers, timeout=15)
    if ref_resp.status_code == 200:
        return ref_resp.json()["object"]["sha"], None

    default_branch = repo_resp.json().get("default_branch") or fallback_source
    src_resp = requests.get(f"{api_base}/git/ref/heads/{default_branch}", headers=headers, timeout=15)
    if src_resp.status_code != 200:
        return None, f"Failed to read {default_branch} ref: {src_resp.text}"
    src_sha = src_resp.json()["object"]["sha"]

    create_resp = requests.post(
        f"{api_base}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{target_branch}", "sha": src_sha},
        timeout=15,
    )
    if create_resp.status_code not in (200, 201):
        return None, f"Failed to create branch {target_branch} from {default_branch}: {create_resp.text}"
    return src_sha, None


def create_and_merge_github_pr(
    review_result: dict,
    auto_merge: bool = True,
    base_branch: str = AUTOPILOT_BRANCH,
    min_confidence: float = 0.75,
    min_uplift_pct: float = 10.0,
) -> dict:
    """
    Enforces an anti-churn significance gate, then creates a feature branch off `base_branch`
    (never `main`), opens a Pull Request with the backtest diagnosis, and merges into `base_branch`.
    """
    import base64
    import requests

    allowed, _, branch_reason = check_autopilot_allowed()
    if not allowed or base_branch == "main":
        return {"status": "blocked", "reason": "Direct PR merges into 'main' are disabled; use 'ai-autopilot'."}

    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPO", "ad-cooper/eve-trader").strip().rstrip("/")
    for _pfx in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if repo.startswith(_pfx):
            repo = repo[len(_pfx):]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not token:
        return {"status": "skipped", "reason": "GITHUB_TOKEN is not set in container environment"}

    deep = review_result.get("deep_code_review") or {}
    is_sig = bool(deep.get("is_significant_improvement", False))
    apply_s1 = bool(deep.get("apply_stage1_parameters", False))
    approved_s1 = deep.get("approved_stage1_parameters") or []
    confidence = float(deep.get("confidence_score") or 0.0)
    uplift_pct = float(deep.get("estimated_monthly_pnl_uplift_pct") or 0.0)
    no_change_reason = str(deep.get("no_change_rationale") or "")

    has_actionable = is_sig or (apply_s1 and len(approved_s1) > 0)
    if not has_actionable or confidence < min_confidence or uplift_pct < min_uplift_pct:
        return {
            "status": "skipped_no_significant_gain",
            "is_significant_improvement": is_sig,
            "apply_stage1_parameters": apply_s1,
            "confidence_score": confidence,
            "estimated_monthly_pnl_uplift_pct": uplift_pct,
            "thresholds": {"min_confidence": min_confidence, "min_uplift_pct": min_uplift_pct},
            "reason": no_change_reason or "Proposed changes did not clear significance/confidence thresholds.",
        }

    proposals = deep.get("algorithmic_proposals") or []
    unit_tests = deep.get("recommended_unit_tests") or []

    updated_files, skipped = _apply_proposals_in_memory(proposals, unit_tests)

    applied_s1_records = []
    if apply_s1 and approved_s1:
        base_ledgers = updated_files.get("services/ledgers.py") or Path("services/ledgers.py").read_text(encoding="utf-8")
        patched_ledgers, applied_s1_records = _apply_approved_stage1_to_ledgers(base_ledgers, approved_s1)
        if applied_s1_records:
            updated_files["services/ledgers.py"] = patched_ledgers
    if not updated_files:
        return {
            "status": "no_applicable_changes",
            "reason": "No proposals matched exact source blocks cleanly",
            "skipped": skipped,
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api_base = f"https://api.github.com/repos/{repo}"

    base_sha, branch_err = _ensure_remote_branch(api_base, headers, target_branch=base_branch, fallback_source="main")
    if branch_err or not base_sha:
        return {"status": "error", "reason": branch_err or f"Could not resolve {base_branch}"}

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch_name = f"ai-proposal/{ts}"
    branch_resp = requests.post(
        f"{api_base}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        timeout=15,
    )
    if branch_resp.status_code not in (200, 201):
        return {"status": "error", "reason": f"Failed to create branch {branch_name}: {branch_resp.text}"}

    committed_files = []
    for fpath, content in updated_files.items():
        get_file = requests.get(
            f"{api_base}/contents/{fpath}",
            headers=headers,
            params={"ref": branch_name},
            timeout=15,
        )
        file_sha = get_file.json().get("sha") if get_file.status_code == 200 else None
        put_payload = {
            "message": f"chore(ai-retro): update {fpath} from walk-forward review",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if file_sha:
            put_payload["sha"] = file_sha

        put_resp = requests.put(f"{api_base}/contents/{fpath}", headers=headers, json=put_payload, timeout=20)
        if put_resp.status_code not in (200, 201):
            return {"status": "error", "reason": f"Failed to commit {fpath}: {put_resp.text}"}
        committed_files.append(fpath)

    diagnosis = deep.get("architectural_diagnosis", "")
    s1_bullets = [
        f"- **[Stage 1 Parameter] {r['desk']}.{r['parameter']}**: `{r['old_value']}` -> `{r['new_value']}` ({r['rationale']})"
        for r in applied_s1_records
    ]
    s2_bullets = [
        f"- **[Stage 2 Code] {p.get('title')}** (`{p.get('file_path')} :: {p.get('target_function')}`): {p.get('rationale')}"
        for p in proposals
    ]
    prop_bullets = "\n".join(s1_bullets + s2_bullets)
    pr_body = (
        f"## Automated Retrospective Algorithm Review\n\n"
        f"**Model:** `{review_result.get('model')}` | **Target Branch:** `{base_branch}` | "
        f"**Confidence:** `{confidence:.2f}` | **Est. Monthly Uplift:** `+{uplift_pct:.1f}%`\n\n"
        f"### Architectural Diagnosis\n{diagnosis}\n\n"
        f"### Proposed Changes\n{prop_bullets}\n\n"
        f"### Files Modified\n" + "\n".join(f"- `{f}`" for f in committed_files) + "\n\n"
        f"> *Merged into `{base_branch}` only (`main` remains your manually reviewed baseline). "
        f"Local `run_daily_ledger.sh` runs `pytest` after pulling and automatically reverts if any test fails.*"
    )

    pr_resp = requests.post(
        f"{api_base}/pulls",
        headers=headers,
        json={
            "title": f"AI Retrospective Optimization ({ts})",
            "head": branch_name,
            "base": base_branch,
            "body": pr_body,
        },
        timeout=20,
    )
    if pr_resp.status_code not in (200, 201):
        return {"status": "error", "reason": f"Failed to open PR: {pr_resp.text}"}

    pr_data = pr_resp.json()
    pr_number = pr_data["number"]
    pr_url = pr_data["html_url"]

    merged = False
    merge_note = f"PR opened against {base_branch} (auto_merge=False)"
    if auto_merge:
        merge_resp = requests.put(
            f"{api_base}/pulls/{pr_number}/merge",
            headers=headers,
            json={
                "commit_title": f"Merge AI Retrospective Optimization (#{pr_number})",
                "merge_method": "squash",
            },
            timeout=20,
        )
        if merge_resp.status_code == 200:
            merged = True
            merge_note = f"PR squash-merged into {base_branch} (main untouched)"
        else:
            merge_note = f"PR opened, but auto-merge returned HTTP {merge_resp.status_code}: {merge_resp.text}"

    return {
        "status": "merged" if merged else "opened",
        "base_branch": base_branch,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "branch": branch_name,
        "confidence_score": confidence,
        "estimated_monthly_pnl_uplift_pct": uplift_pct,
        "committed_files": committed_files,
        "skipped": skipped,
        "note": merge_note,
    }


def run_autopilot_pipeline(
    lookback_days: int = 45,
    step_days: int = 7,
    total_budget_b: float = 80.0,
    model: str = "auto",
    github_url: str | None = None,
    auto_pr: bool = True,
    auto_merge: bool = True,
    min_confidence: float = 0.75,
    min_uplift_pct: float = 10.0,
) -> dict:
    """Single entry point used by both FastAPI routes and background scheduler jobs."""
    result = run_hybrid_code_review(
        lookback_days=lookback_days,
        step_days=step_days,
        total_budget_b=total_budget_b,
        model=model,
        github_url=github_url,
    )
    if auto_pr and "error" not in result and not str(result.get("status", "")).startswith("skipped"):
        result["github_pr"] = create_and_merge_github_pr(
            result,
            auto_merge=auto_merge,
            base_branch=AUTOPILOT_BRANCH,
            min_confidence=min_confidence,
            min_uplift_pct=min_uplift_pct,
        )
    return result
