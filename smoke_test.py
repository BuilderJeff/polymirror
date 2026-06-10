"""smoke_test.py — fast end-to-end sanity check of the polymirror foundation.

Run:  ./.venv/Scripts/python.exe smoke_test.py
Exits non-zero if any check fails. Safe & read-only: only GETs public APIs.

Checks, in order:
  A. core deps import
  B. config.py imports, validates, exposes the spread presets
  C. polymirror package imports
  D. all three Polymarket APIs reachable + the fields the project depends on are present
  E. polyapi.py client (if the Phase-1 swarm has written it yet) imports & does a tiny call
"""
from __future__ import annotations
import sys, json, time, importlib, urllib.request

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []

def check(name: str, fn):
    try:
        detail = fn() or ""
        results.append((PASS, name, str(detail)[:160]))
    except Exception as e:
        results.append((FAIL, name, f"{type(e).__name__}: {e}"))

def http_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": "polymirror-smoke/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))

# --- A. deps -----------------------------------------------------------------
def a_deps():
    for m in ("requests", "pandas", "numpy", "scipy", "statsmodels", "matplotlib", "pyarrow"):
        importlib.import_module(m)
    return "requests/pandas/numpy/scipy/statsmodels/matplotlib/pyarrow OK"
check("A. core deps import", a_deps)

# --- B. config ---------------------------------------------------------------
def b_config():
    import config
    config.DEFAULT.validate()
    presets = config.SPREAD_PRESETS
    assert set(presets) == {"optimistic", "base", "conservative"}, "missing a spread preset"
    # R1 guard must actually fire on leakage:
    bad = config.DEFAULT.with_(train_end="2026-05-01T00:00:00Z", test_start="2026-04-01T00:00:00Z")
    try:
        bad.validate()
        raise AssertionError("validate() did NOT reject test_start < train_end (R1 guard broken!)")
    except ValueError:
        pass
    return f"validate() OK; R1 leakage guard fires; presets={list(presets)}"
check("B. config validates + R1 guard fires", b_config)

# --- C. package --------------------------------------------------------------
def c_pkg():
    import polymirror
    return f"polymirror v{polymirror.__version__}"
check("C. polymirror package imports", c_pkg)

# --- D. live APIs + required fields ------------------------------------------
def d_gamma():
    s, data = http_json("https://gamma-api.polymarket.com/markets?limit=1")
    assert s == 200 and isinstance(data, list) and data, f"bad gamma response (HTTP {s})"
    m = data[0]
    for f in ("conditionId", "outcomes", "outcomePrices", "enableOrderBook"):
        assert f in m, f"Gamma market missing field: {f}"
    return f"HTTP 200; conditionId+outcomes+outcomePrices+enableOrderBook present"
check("D1. Gamma /markets (resolution fields)", d_gamma)

def d_data():
    s, data = http_json("https://data-api.polymarket.com/trades?limit=1&type=TRADE")
    assert s == 200 and isinstance(data, list) and data, f"bad data-api response (HTTP {s})"
    t = data[0]
    required = ("proxyWallet", "conditionId", "price", "timestamp", "side", "outcomeIndex")
    missing = [f for f in required if f not in t]
    assert not missing, f"Data /trades missing fields: {missing}"
    return "HTTP 200; proxyWallet+conditionId+price+timestamp+side+outcomeIndex present"
check("D2. Data /trades (attribution fields)", d_data)

def d_clob():
    s, data = http_json("https://clob.polymarket.com/markets?limit=1")
    assert s == 200 and isinstance(data, dict) and data.get("data"), f"bad clob response (HTTP {s})"
    return "HTTP 200; /markets returns data[]"
check("D3. CLOB /markets", d_clob)

# --- E. polyapi client (only once the swarm has written it) ------------------
def e_polyapi():
    try:
        import polymirror.polyapi as papi  # noqa
    except ModuleNotFoundError:
        return "SKIP — polyapi.py not written yet (swarm in progress)"
    fns = [n for n in ("get_trades", "get_market_gamma") if hasattr(papi, n)]
    return f"polyapi imports; helpers found: {fns or 'none (check names)'}"
check("E. polyapi client import", e_polyapi)

# --- report ------------------------------------------------------------------
print("\n" + "=" * 64)
print("  polymirror smoke test")
print("=" * 64)
nfail = 0
for status, name, detail in results:
    mark = "[PASS]" if status == PASS else "[FAIL]"
    if status == FAIL:
        nfail += 1
    print(f"{mark} {name}\n        {detail}")
print("=" * 64)
print(f"  {len(results)-nfail}/{len(results)} passed"
      + ("" if nfail == 0 else f"  ({nfail} FAILED)"))
print("=" * 64)
sys.exit(1 if nfail else 0)
