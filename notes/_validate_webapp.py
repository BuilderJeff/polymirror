"""One-shot validation: data.json sanity, id cross-check, banned-palette scan."""
import json
import re
from pathlib import Path

W = Path(__file__).resolve().parent

d = json.loads((W / "data.json").read_text(encoding="utf-8"))
print("data.json OK  %.1f KB" % ((W / "data.json").stat().st_size / 1024))
print("top-level keys:", sorted(d.keys()))
m = d["meta"]
print("meta:", {k: m[k] for k in ("n_positions", "n_markets", "window_hours",
                                  "total_marks", "n_wallets_active", "n_buys",
                                  "n_sells", "pulls")})
print("edge_curve horizons:", [r["h"] for r in d["edge_curve"]])
print("markets rows:", len(d["markets"]), " per_wallet:", len(d["per_wallet"]),
      " timeline bins:", len(d["entries_timeline"]),
      " hist bins:", len(d["entry_hist"]["counts"]),
      " study1 pending:", d["study1"].get("pending"))

html = (W / "index.html").read_text(encoding="utf-8")
js = (W / "js" / "app.js").read_text(encoding="utf-8")
html_ids = set(re.findall(r'id="([\w-]+)"', html))
js_ids = set(re.findall(r'el\("([\w-]+)"\)', js))
js_ids |= set(re.findall(r'getElementById\("([\w-]+)"\)', js))
js_ids |= set(re.findall(r'Plotly\.(?:newPlot|react)\("([\w-]+)"', js))
missing = js_ids - html_ids
print("ids referenced in js:", len(js_ids),
      "| missing from html:", sorted(missing) if missing else "NONE")

banned = ["102A43", "1AA39A", "2C6FBB", "D64550", "E8A33D"]
hits = []
for f in list(W.rglob("*.html")) + list(W.rglob("*.css")) + list(W.rglob("*.js")):
    t = f.read_text(encoding="utf-8").upper()
    hits += [(f.name, b) for b in banned if b in t]
print("banned palette hits:", hits if hits else "NONE")
