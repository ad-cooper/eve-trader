import os
import re
import glob
import importlib
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_all_services_import_cleanly():
    """Imports every module in services/ to catch NameError, ImportError, or circular imports at load time."""
    service_modules = [
        "services.accounting",
        "services.blacklist",
        "services.esi",
        "services.ledgers",
        "services.market",
        "services.sde",
        "services.staging_diff",
        "services.storage",
        "workflows.daily_pipeline",
        "workflows.scheduler",
        "workflows.dashboard",
        "workflows.order_actions",
        "workflows.buy_orders",
        "workflows.sell_advisor",
        "workflows.imports",
        "routers.auth",
        "routers.api",
    ]
    for mod_name in service_modules:
        mod = importlib.import_module(mod_name)
        assert mod is not None


def test_all_scripts_compile_cleanly():
    """Parses, compiles, and runs --help on active CLI scripts to catch SyntaxError and sys.path issues."""
    import sys
    import subprocess

    for path in sorted(glob.glob("scripts/*.py")):
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        compile(source, path, "exec")
        if "argparse.ArgumentParser" in source:
            res = subprocess.run([sys.executable, path, "--help"], capture_output=True, text=True, timeout=10)
            assert res.returncode == 0, f"CLI --help failed for {path}: {res.stderr}"


def _find_unterminated_js_strings(js_code: str) -> list[tuple[int, str]]:
    in_line_comment = False
    in_block_comment = False
    stack = []
    line_no = 1
    i = 0
    n = len(js_code)
    errors = []

    while i < n:
        ch = js_code[i]
        nxt = js_code[i + 1] if i + 1 < n else ""
        top = stack[-1] if stack else None

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                line_no += 1
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            if ch == "\n":
                line_no += 1
            i += 1
            continue

        if top and top[0] == "quote":
            q = top[1]
            if ch == "\\":
                if nxt == "\n":
                    line_no += 1
                i += 2
                continue
            if ch == "\n":
                errors.append((top[2], f"Unterminated {q} string"))
                stack.pop()
                line_no += 1
                i += 1
                continue
            if ch == q:
                stack.pop()
            i += 1
            continue

        if top and top[0] == "template" and top[1] == 0:
            if ch == "\\":
                if nxt == "\n":
                    line_no += 1
                i += 2
                continue
            if ch == "$" and nxt == "{":
                stack[-1] = ("template", 1, top[2])
                i += 2
                continue
            if ch == "`":
                stack.pop()
                i += 1
                continue
            if ch == "\n":
                line_no += 1
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"'):
            stack.append(("quote", ch, line_no))
            i += 1
            continue
        if ch == "`":
            stack.append(("template", 0, line_no))
            i += 1
            continue
        if top and top[0] == "template" and top[1] > 0:
            if ch == "{":
                stack[-1] = ("template", top[1] + 1, top[2])
            elif ch == "}":
                stack[-1] = ("template", top[1] - 1, top[2])
        if ch == "\n":
            line_no += 1
        i += 1

    for item in stack:
        errors.append((item[2], f"Unterminated {item[1] if item[0] == 'quote' else '`'} at EOF"))
    return errors


def test_ui_html_and_es_modules_integrity():
    """
    Verifies the root HTML mounts the Preact app and every ES module imported across
    static/js/ exists, serves HTTP 200, and has no unescaped literal newlines in string literals.
    """
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    assert 'id="app"' in html
    assert '/static/js/app.js' in html
    assert '"preact"' in html and '"htm"' in html

    js_files = sorted(glob.glob("static/js/**/*.js", recursive=True))
    assert len(js_files) >= 8, f"Expected modular Preact JS files, found {js_files}"

    import_re = re.compile(r"""(?:import|from)\s+['"](\.[^'"]+)['"]""")

    for js_path in js_files:
        with open(js_path, "r", encoding="utf-8") as f:
            content = f.read()

        js_errors = _find_unterminated_js_strings(content)
        assert not js_errors, f"Unterminated JS string literal in {js_path}: {js_errors}"

        rel_url = "/" + js_path.replace(os.sep, "/")
        static_res = client.get(rel_url)
        assert static_res.status_code == 200, f"Static JS asset failed to serve: {rel_url}"

        base_dir = os.path.dirname(js_path)
        for rel_import in import_re.findall(content):
            resolved = os.path.normpath(os.path.join(base_dir, rel_import))
            assert os.path.exists(resolved), f"Broken ES module import '{rel_import}' in {js_path}"


def test_api_state_contract():
    """Verifies /api/state returns the exact top-level schema required by Preact components."""
    res = client.get("/api/state")
    assert res.status_code == 200
    data = res.json()
    for key in ("status", "character", "metrics", "desks", "desk_metrics", "sniper", "performance"):
        assert key in data, f"Missing '{key}' in /api/state response"
    for desk in ("evergreen", "tactical", "hold"):
        assert desk in data["desks"]
        assert desk in data["desk_metrics"]


def test_api_inventory_advisor_contract():
    """Verifies GET & POST /api/inventory/advisor execute end-to-end without 500 errors."""
    res_get = client.get("/api/inventory/advisor")
    assert res_get.status_code == 200
    data = res_get.json()
    for key in ("items", "total_items", "total_value", "total_cost", "total_profit", "ready_value", "multisell_text"):
        assert key in data

    res_post = client.post("/api/inventory/advisor", json={"raw_text": "Tritanium 100"})
    assert res_post.status_code == 200
    assert "items" in res_post.json()


def test_api_missing_buys_contract():
    """Verifies /api/orders/missing-buys returns valid MultiBuy payload across desk filters."""
    for desk in ("evergreen", "tactical", "hold", "all"):
        res = client.get(f"/api/orders/missing-buys?desk={desk}")
        assert res.status_code == 200
        data = res.json()
        for key in ("total_items", "total_escrow", "total_isk_needed", "total_expected_profit", "total_fees", "total_buy_fees", "total_exit_fees", "blended_roi_pct", "items", "multibuy_text"):
            assert key in data


def test_api_import_opportunities_contract():
    """Verifies /api/import/opportunities returns both C-J6MT and Amarr sections."""
    res = client.get("/api/import/opportunities")
    assert res.status_code == 200
    data = res.json()
    assert "cj6mt" in data and "amarr" in data
    for dest_key in ("cj6mt", "amarr"):
        sec = data[dest_key]
        for key in ("items", "multibuy_text", "multibuy_m3", "multibuy_collateral", "multibuy_freight", "multibuy_profit"):
            assert key in sec


def test_api_order_actions_contract():
    """Verifies /api/orders/actions returns valid bumps & cancels schema across desk filters."""
    for desk in ("evergreen", "tactical", "hold", "all"):
        res = client.get(f"/api/orders/actions?desk={desk}")
        assert res.status_code == 200
        data = res.json()
        for key in (
            "total_actions",
            "bump_count",
            "cancel_count",
            "total_escrow_freed",
            "total_bump_escrow_delta",
            "net_isk_impact",
            "bumps",
            "cancels",
        ):
            assert key in data
