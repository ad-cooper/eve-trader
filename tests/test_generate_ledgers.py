import importlib.util
from pathlib import Path

def test_resolve_dynamic_budget_explicit_auto_and_fallback(monkeypatch):
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "generate_ledgers.py"
    spec = importlib.util.spec_from_file_location("generate_ledgers", script_path)
    gl_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gl_mod)

    # 1. Explicit number (< 1000 -> billions, >= 1000 -> raw ISK)
    assert gl_mod.resolve_dynamic_budget("75.0") == 75.0e9
    assert gl_mod.resolve_dynamic_budget("25000000000") == 25_000_000_000.0

    # 2. 'auto' uses resolved equity when above 1B ISK
    monkeypatch.setattr(gl_mod, "resolve_character_equity", lambda **kw: (30e9, 15e9, 45e9))
    assert gl_mod.resolve_dynamic_budget("auto") == 45e9

    # 3. Invalid capital falls back to 'auto' resolution
    assert gl_mod.resolve_dynamic_budget("not-a-number") == 45e9

    # 4. Low/zero equity falls back to DEFAULT_FALLBACK_BUDGET
    monkeypatch.setattr(gl_mod, "resolve_character_equity", lambda **kw: (0.0, 0.0, 500e6))
    assert gl_mod.resolve_dynamic_budget("auto") == gl_mod.DEFAULT_FALLBACK_BUDGET