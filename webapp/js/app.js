/* app.js: Copy the Winners? data explorer.
   Vanilla JS + Plotly (CDN). All numbers come from data.json at load time. */
(function () {
  "use strict";

  // ---- palette (classroom-clean) -------------------------------------------
  var GREEN = "#1E7A52";   // mirror strategy / money
  var GRAY  = "#6B7280";   // benchmark
  var GOLD  = "#D98E04";   // edge / highlight
  var BRICK = "#B23A2F";   // losses / warnings
  var INK   = "#1F1F1F";
  var GRID  = "#ECE7DC";
  var GREEN_BAND_LT = "rgba(30,122,82,0.10)";
  var GREEN_BAND_MD = "rgba(30,122,82,0.22)";
  var GRAY_BAND_LT  = "rgba(107,114,128,0.10)";
  var GRAY_BAND_MD  = "rgba(107,114,128,0.22)";
  var GOLD_BAND     = "rgba(217,142,4,0.16)";
  // per-wallet line colors: tints/shades of the four palette hues only
  var WALLET_COLORS = [GREEN, GOLD, GRAY, BRICK,
                       "#4C9B74", "#E3AC3F", "#9AA1AB", "#C9665B",
                       "#145C3D", "#A06803"];

  var PLOTCFG = { displayModeBar: false, responsive: true };

  // ---- tiny d3-format-style helpers (hand-rolled, no deps) ------------------
  function fmtInt(n) {
    if (n === null || n === undefined || isNaN(n)) return "-";
    return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function pct(x, d) {
    if (x === null || x === undefined || isNaN(x)) return "-";
    return (100 * x).toFixed(d === undefined ? 1 : d) + "%";
  }
  function spct(x, d) {
    if (x === null || x === undefined || isNaN(x)) return "-";
    return (x >= 0 ? "+" : "") + pct(x, d);
  }
  function fmtNum(x, d) {
    if (x === null || x === undefined || isNaN(x)) return "-";
    return x.toFixed(d === undefined ? 2 : d);
  }
  function fmtP(p) {
    if (p === null || p === undefined || isNaN(p)) return "-";
    return p < 0.001 ? "<0.001" : p.toFixed(3);
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function el(id) { return document.getElementById(id); }

  // ---- shared Plotly layout --------------------------------------------------
  function baseLayout(extra) {
    var lay = {
      paper_bgcolor: "#FFFFFF",
      plot_bgcolor: "#FFFFFF",
      font: { family: "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
              size: 13, color: INK },
      margin: { l: 60, r: 20, t: 10, b: 48 },
      xaxis: { gridcolor: GRID, zeroline: false, linecolor: GRAY },
      yaxis: { gridcolor: GRID, zeroline: false, linecolor: GRAY },
      legend: { orientation: "h", y: 1.12, x: 0, font: { size: 12 } },
      hovermode: "x unified",
      hoverlabel: { bgcolor: "#FFFFFF", bordercolor: GRAY, font: { color: INK } }
    };
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        if (lay[k] && typeof lay[k] === "object" && !Array.isArray(extra[k])) {
          Object.keys(extra[k]).forEach(function (k2) { lay[k][k2] = extra[k][k2]; });
        } else { lay[k] = extra[k]; }
      });
    }
    return lay;
  }
  function zeroLine(color, dash) {
    return { type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0,
             line: { color: color || INK, width: 1, dash: dash || "dot" } };
  }

  // ---- counters ---------------------------------------------------------------
  function animateCounter(node, target, suffix) {
    var t0 = null, dur = 1300;
    function step(ts) {
      if (!t0) t0 = ts;
      var u = Math.min(1, (ts - t0) / dur);
      u = 1 - Math.pow(1 - u, 3); // ease-out cubic
      node.textContent = fmtInt(target * u) + (suffix || "");
      if (u < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  // ---- HERO --------------------------------------------------------------------
  function renderHero(D) {
    var m = D.meta;
    animateCounter(el("stat-positions"), m.n_positions);
    animateCounter(el("stat-markets"), m.n_markets);
    animateCounter(el("stat-hours"), m.window_hours);
    animateCounter(el("stat-marks"), m.total_marks);

    el("hero-finding").textContent = findingText(D.edge_curve);
    el("meta-line").textContent =
      "Live window: " + m.experiment_start.slice(0, 16).replace("T", " ") + " to " +
      m.experiment_end.slice(0, 16).replace("T", " ") + " UTC | " +
      fmtInt(m.pulls) + " data pulls | " + fmtInt(m.n_buys) + " buys / " +
      fmtInt(m.n_sells) + " sells copied | " + fmtInt(m.n_wallets_active) +
      " of the Top-10 wallets traded | built " + (D.meta.built || "").slice(0, 16).replace("T", " ") + " UTC";
  }

  function findingText(ec) {
    if (!ec || !ec.length) return "No horizon data yet. Check back after the first hourly marks land.";
    var f = ec[0], l = ec[ec.length - 1];
    var inclZero = ec.filter(function (r) { return r.lo_m <= 0 && r.hi_m >= 0; }).length;
    var neg = null;
    for (var i = 0; i < ec.length; i++) { if (ec[i].edge_m < 0) { neg = ec[i]; break; } }
    var s = "The verdict so far: copying showed " +
      (f.edge_m > 0
        ? "a small early edge (" + spct(f.edge_m) + " at " + f.h + " h)"
        : (f.hi_m < 0
            ? "an immediately negative edge (" + spct(f.edge_m) + " at " + f.h + " h)"
            : "no early edge (" + spct(f.edge_m) + " at " + f.h + " h)"));
    s += neg && neg.h !== f.h
      ? ", and it flipped negative by " + neg.h + " h."
      : (neg ? "." : ", and the point estimate stayed positive through " + l.h + " h.");
    if (l.strat < 0 && l.bench < 0) {
      s += " Both strategies lose money gross at " + l.h + " h (mirror " + spct(l.strat) +
           ", favorite " + spct(l.bench) + ").";
    } else {
      s += " At " + l.h + " h the mirror sits at " + spct(l.strat) + " vs " + spct(l.bench) +
           " for the favorite.";
    }
    var nPosEx = ec.filter(function (r) { return r.lo_m > 0; }).length;
    var nNegEx = ec.filter(function (r) { return r.hi_m < 0; }).length;
    if (nPosEx === 0 && nNegEx > 0) {
      s += " " + inclZero + " of " + ec.length + " horizons have a 95% interval that includes " +
        "zero, and the only intervals that exclude it are negative: no evidence of skill, " +
        "before trading costs.";
    } else if (nPosEx === 0) {
      s += " " + inclZero + " of " + ec.length + " horizons have a 95% interval that includes " +
        "zero: statistically indistinguishable from luck, before trading costs.";
    } else {
      s += " " + nPosEx + " of " + ec.length + " horizons exclude zero on the positive side " +
        "(by market); see whether that survives wallet clustering below.";
    }
    return s;
  }

  // ---- EDGE CURVE -----------------------------------------------------------------
  function renderEdge(D) {
    var ec = D.edge_curve;
    var H = ec.map(function (r) { return r.h; });

    function draw(cluster) {
      var byM = cluster === "m";
      var edge = ec.map(function (r) { return byM ? r.edge_m : r.edge_w; });
      var lo = ec.map(function (r) { return byM ? r.lo_m : r.lo_w; });
      var hi = ec.map(function (r) { return byM ? r.hi_m : r.hi_w; });

      var band = {
        x: H.concat(H.slice().reverse()),
        y: hi.concat(lo.slice().reverse()),
        fill: "toself", fillcolor: GOLD_BAND,
        line: { width: 0 }, hoverinfo: "skip", showlegend: true,
        name: "edge 95% CI (" + (byM ? "by market" : "by wallet") + ")",
        type: "scatter"
      };
      // Use the SAME clustering for all three series so the gold line is exactly
      // the difference of the green and gray lines under either toggle.
      var mirror = {
        x: H, y: ec.map(function (r) { return byM ? r.strat_cell : r.strat; }),
        name: "Mirror (copy the leaderboard)", type: "scatter", mode: "lines+markers",
        line: { color: GREEN, width: 2.5 }, marker: { size: 6, color: GREEN },
        hovertemplate: "mirror %{y:+.1%}<extra></extra>"
      };
      var fav = {
        x: H, y: ec.map(function (r) { return byM ? r.bench_cell : r.bench; }),
        name: "Favorite (benchmark)", type: "scatter", mode: "lines+markers",
        line: { color: GRAY, width: 2.5 }, marker: { size: 6, symbol: "square", color: GRAY },
        hovertemplate: "favorite %{y:+.1%}<extra></extra>"
      };
      var edgeTr = {
        x: H, y: edge,
        name: "Edge = mirror − favorite", type: "scatter", mode: "lines+markers",
        line: { color: GOLD, width: 2.5, dash: "dash" },
        marker: { size: 7, symbol: "triangle-up", color: GOLD },
        hovertemplate: "edge %{y:+.1%}<extra></extra>"
      };
      Plotly.react("chart-edge", [band, mirror, fav, edgeTr], baseLayout({
        xaxis: { title: { text: "Holding horizon (hours)" }, dtick: H.length > 24 ? 4 : 1 },
        yaxis: { title: { text: "Gross return" }, tickformat: "+.0%" },
        shapes: [zeroLine()]
      }), PLOTCFG);
    }

    draw("m");
    var radios = document.querySelectorAll('input[name="ci-cluster"]');
    Array.prototype.forEach.call(radios, function (r) {
      r.addEventListener("change", function () { draw(this.value); });
    });

    // numbers table
    var tb = el("edge-tbody");
    tb.innerHTML = ec.map(function (r) {
      function sign(x) { return x > 0 ? "pos" : (x < 0 ? "neg" : ""); }
      return "<tr>" +
        "<td class='num'>" + r.h + " h</td>" +
        "<td class='num'>" + fmtInt(r.n_cells) + "</td>" +
        "<td class='num'>" + fmtInt(r.n_wallets) + "</td>" +
        "<td class='num " + sign(r.strat_cell) + "'>" + spct(r.strat_cell) + "</td>" +
        "<td class='num'>" + spct(r.bench_cell) + "</td>" +
        "<td class='num " + sign(r.edge_m) + "'>" + spct(r.edge_m) + "</td>" +
        "<td class='num'>[" + spct(r.lo_m) + ", " + spct(r.hi_m) + "]</td>" +
        "<td class='num" + (r.p_m < 0.05 ? " sig" : "") + "'>" + fmtP(r.p_m) + "</td>" +
        "<td class='num'>[" + spct(r.lo_w) + ", " + spct(r.hi_w) + "]</td>" +
        "<td class='num" + (r.p_w < 0.05 ? " sig" : "") + "'>" + fmtP(r.p_w) + "</td>" +
        "</tr>";
    }).join("");
  }

  // ---- IS IT REAL --------------------------------------------------------------
  function renderReal(D) {
    var ec = D.edge_curve;
    var H = ec.map(function (r) { return r.h; });

    var byMarket = {
      x: H, y: ec.map(function (r) { return r.edge_m; }),
      error_y: {
        type: "data", symmetric: false,
        array: ec.map(function (r) { return r.hi_m - r.edge_m; }),
        arrayminus: ec.map(function (r) { return r.edge_m - r.lo_m; }),
        color: GOLD, thickness: 1.5, width: 4
      },
      name: "edge ±95% CI (by market)", type: "scatter", mode: "markers",
      marker: { color: GOLD, size: 8 },
      hovertemplate: "edge %{y:+.1%}<extra>by market</extra>"
    };
    var byWallet = {
      x: H.map(function (h) { return h + 0.22; }),
      y: ec.map(function (r) { return r.edge_w; }),
      error_y: {
        type: "data", symmetric: false,
        array: ec.map(function (r) { return r.hi_w - r.edge_w; }),
        arrayminus: ec.map(function (r) { return r.edge_w - r.lo_w; }),
        color: GRAY, thickness: 1.5, width: 4
      },
      name: "edge ±95% CI (by wallet)", type: "scatter", mode: "markers",
      marker: { color: GRAY, size: 8, symbol: "diamond" },
      hovertemplate: "edge %{y:+.1%}<extra>by wallet</extra>"
    };
    Plotly.newPlot("chart-ci", [byMarket, byWallet], baseLayout({
      xaxis: { title: { text: "Holding horizon (hours)" }, dtick: H.length > 24 ? 4 : 1 },
      yaxis: { title: { text: "Edge (gross)" }, tickformat: "+.0%" },
      shapes: [zeroLine(BRICK, "dash")],
      hovermode: "closest"
    }), PLOTCFG);

    // p-value strip
    var pm = ec.map(function (r) { return r.p_m; });
    var pw = ec.map(function (r) { return r.p_w; });
    Plotly.newPlot("chart-pvals", [
      { x: H, y: pm, type: "bar", name: "by market",
        marker: { color: pm.map(function (p) { return p < 0.05 ? BRICK : GRAY; }) },
        hovertemplate: "p = %{y:.3f}<extra>by market</extra>" },
      { x: H, y: pw, type: "bar", name: "by wallet",
        marker: { color: pw.map(function (p) { return p < 0.05 ? BRICK : "#B7BCC4"; }) },
        hovertemplate: "p = %{y:.3f}<extra>by wallet</extra>" }
    ], baseLayout({
      barmode: "group",
      xaxis: { title: { text: "Horizon (hours)" }, dtick: H.length > 24 ? 4 : 1 },
      yaxis: { title: { text: "two-sided p" }, range: [0, 1.05] },
      shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: 0.05, y1: 0.05,
                 line: { color: BRICK, width: 1, dash: "dash" } }],
      margin: { l: 50, r: 10, t: 10, b: 42 }
    }), PLOTCFG);

    // verdict (data-driven)
    var nSig = ec.filter(function (r) { return r.lo_m > 0 && r.lo_w > 0; }).length;
    var nSigNeg = ec.filter(function (r) { return r.hi_m < 0 && r.hi_w < 0; }).length;
    var nNegM = ec.filter(function (r) { return r.hi_m < 0; }).length;
    var minP = Math.min.apply(null, pm.concat(pw).filter(function (p) { return p === p; }));
    var v = "Verdict so far: <strong>" + nSig + " of " + ec.length + "</strong> horizons clear " +
      "that bar (smallest p-value anywhere: " + fmtP(minP) + ").";
    if (nSig === 0) {
      if (nSigNeg > 0) {
        v += " If anything, the evidence points the other way: at " + nSigNeg + " horizon" +
          (nSigNeg === 1 ? "" : "s") + " the edge is significantly <em>negative</em> under " +
          "both clusterings: copying hurt.";
      } else if (nNegM > 0) {
        v += " Where the interval does exclude zero (by market, " + nNegM + " horizon" +
          (nNegM === 1 ? "" : "s") + "), the edge is <em>negative</em>: copying hurt, " +
          "though the wallet-clustered bars still include zero.";
      } else {
        v += " The live edge is indistinguishable from luck.";
      }
    }
    el("real-verdict").innerHTML = v;
  }

  // ---- WALLETS --------------------------------------------------------------------
  function renderWallets(D) {
    var pw = D.per_wallet.slice().sort(function (a, b) { return a.n_positions - b.n_positions; });
    Plotly.newPlot("chart-wallet-bars", [{
      y: pw.map(function (w) { return w.name; }),
      x: pw.map(function (w) { return w.n_positions; }),
      type: "bar", orientation: "h",
      marker: { color: GREEN },
      text: pw.map(function (w) { return fmtInt(w.n_positions); }),
      textposition: "outside", cliponaxis: false,
      hovertemplate: "%{y}: %{x:,} positions<extra></extra>"
    }], baseLayout({
      xaxis: { title: { text: "Positions captured" } },
      yaxis: { automargin: true },
      margin: { l: 110, r: 40, t: 10, b: 42 },
      hovermode: "closest", showlegend: false
    }), PLOTCFG);

    var traces = D.per_wallet.map(function (w, i) {
      return {
        x: w.curve.map(function (r) { return r.h; }),
        y: w.curve.map(function (r) { return r.edge; }),
        name: w.name, type: "scatter", mode: "lines+markers",
        line: { color: WALLET_COLORS[i % WALLET_COLORS.length], width: 2 },
        marker: { size: 5 },
        hovertemplate: w.name + " edge %{y:+.1%}<extra></extra>"
      };
    });
    Plotly.newPlot("chart-wallet-curves", traces, baseLayout({
      xaxis: { title: { text: "Holding horizon (hours)" } },
      yaxis: { title: { text: "Edge (gross)" }, tickformat: "+.0%" },
      shapes: [zeroLine()]
    }), PLOTCFG);
  }

  // ---- MARKET EXPLORER --------------------------------------------------------------
  var mState = { q: "", key: "n_fills", dir: -1, h: null };

  function renderMarketControls(D) {
    var hs = D.markets.length ? Object.keys(D.markets[0].edge) : [];
    var sel = el("market-horizon");
    sel.innerHTML = hs.map(function (h) {
      return "<option value='" + h + "'>" + h + " h</option>";
    }).join("");
    mState.h = hs.length ? hs[hs.length - 1] : null;
    if (mState.h !== null) sel.value = mState.h;
    sel.addEventListener("change", function () { mState.h = this.value; renderMarketTable(D); });
    el("market-search").addEventListener("input", function () {
      mState.q = this.value.toLowerCase(); renderMarketTable(D);
    });
    el("market-thead").addEventListener("click", function (e) {
      var th = e.target.closest("th.sortable");
      if (!th) return;
      var k = th.getAttribute("data-key");
      if (mState.key === k) { mState.dir = -mState.dir; }
      else { mState.key = k; mState.dir = (k === "title") ? 1 : -1; }
      renderMarketTable(D);
    });
  }

  function renderMarketTable(D) {
    var rows = D.markets.filter(function (m) {
      return !mState.q || (m.title || "").toLowerCase().indexOf(mState.q) >= 0 ||
             (m.slug || "").toLowerCase().indexOf(mState.q) >= 0;
    });
    var k = mState.key, dir = mState.dir, h = mState.h;
    function val(m) {
      if (k === "edge") return m.edge[h];
      if (k === "resolved") return m.resolved ? 1 : 0;
      return m[k];
    }
    rows.sort(function (a, b) {
      var va = val(a), vb = val(b);
      var aN = (va === null || va === undefined), bN = (vb === null || vb === undefined);
      if (aN && bN) return 0;
      if (aN) return 1;            // nulls always last
      if (bN) return -1;
      if (typeof va === "string") return dir * va.localeCompare(vb);
      return dir * (va - vb);
    });

    // header arrows
    Array.prototype.forEach.call(
      el("market-thead").querySelectorAll("th.sortable"),
      function (th) {
        var base = th.textContent.replace(/[▲▼]\s*$/, "").trim();
        th.innerHTML = esc(base) +
          (th.getAttribute("data-key") === k
            ? " <span class='arrow'>" + (dir > 0 ? "▲" : "▼") + "</span>" : "");
      });

    el("market-count").textContent =
      fmtInt(rows.length) + " of " + fmtInt(D.markets.length) + " markets";

    el("market-tbody").innerHTML = rows.map(function (m) {
      var e = m.edge[h];
      var cls = (e === null || e === undefined) ? "" : (e > 0 ? "pos" : (e < 0 ? "neg" : ""));
      return "<tr>" +
        "<td class='wraptext'>" + esc(m.title) + "</td>" +
        "<td class='num'>" + fmtInt(m.n_fills) + "</td>" +
        "<td class='num'>" + fmtInt(m.n_cells) + "</td>" +
        "<td>" + (m.resolved ? "<span class='ink-green'>✓ settled</span>" : "open") + "</td>" +
        "<td class='num'>" + fmtNum(m.entry, 2) + "</td>" +
        "<td class='num " + cls + "'>" + ((e === null || e === undefined) ? "-" : spct(e)) + "</td>" +
        "</tr>";
    }).join("");
  }

  // ---- DISTRIBUTIONS -------------------------------------------------------------
  function renderFan(D) {
    var ds = D.dist;
    var H = ds.map(function (r) { return r.h; });
    function band(get, lo, hi, color, group, name, showLeg) {
      return {
        x: H.concat(H.slice().reverse()),
        y: ds.map(function (r) { return get(r)[hi]; })
            .concat(ds.map(function (r) { return get(r)[lo]; }).reverse()),
        fill: "toself", fillcolor: color, line: { width: 0 },
        legendgroup: group, name: name, showlegend: !!showLeg,
        hoverinfo: "skip", type: "scatter"
      };
    }
    function median(get, color, group, name) {
      return {
        x: H, y: ds.map(function (r) { return get(r)[2]; }),
        type: "scatter", mode: "lines+markers",
        line: { color: color, width: 2.5 }, marker: { size: 5 },
        legendgroup: group, name: name,
        hovertemplate: name + " median %{y:+.1%}<extra></extra>"
      };
    }
    var gS = function (r) { return r.strat; }, gB = function (r) { return r.bench; };
    Plotly.newPlot("chart-fan", [
      band(gS, 0, 4, GREEN_BAND_LT, "mir", "Mirror 5-95%", false),
      band(gS, 1, 3, GREEN_BAND_MD, "mir", "Mirror 25-75%", false),
      median(gS, GREEN, "mir", "Mirror"),
      band(gB, 0, 4, GRAY_BAND_LT, "fav", "Favorite 5-95%", false),
      band(gB, 1, 3, GRAY_BAND_MD, "fav", "Favorite 25-75%", false),
      median(gB, GRAY, "fav", "Favorite")
    ], baseLayout({
      xaxis: { title: { text: "Holding horizon (hours)" }, dtick: H.length > 24 ? 4 : 1 },
      yaxis: { title: { text: "Per-bet gross return" }, tickformat: "+.0%" },
      shapes: [zeroLine()]
    }), PLOTCFG);
  }

  function renderEntryHist(D) {
    var hgram = D.entry_hist;
    var centers = [], w = [];
    for (var i = 0; i < hgram.counts.length; i++) {
      centers.push((hgram.edges[i] + hgram.edges[i + 1]) / 2);
      w.push(hgram.edges[i + 1] - hgram.edges[i]);
    }
    Plotly.newPlot("chart-entry-hist", [{
      x: centers, y: hgram.counts, width: w, type: "bar",
      marker: { color: GREEN, line: { color: "#FFFFFF", width: 1 } },
      hovertemplate: "price %{x:.2f}: %{y:,} trades<extra></extra>"
    }], baseLayout({
      xaxis: { title: { text: "Entry price (implied probability)" }, range: [0, 1] },
      yaxis: { title: { text: "Positions" } },
      shapes: [{ type: "line", x0: 0.5, x1: 0.5, yref: "paper", y0: 0, y1: 1,
                 line: { color: GOLD, width: 1.5, dash: "dash" } }],
      showlegend: false, hovermode: "closest",
      margin: { l: 55, r: 10, t: 10, b: 48 }
    }), PLOTCFG);
  }

  function renderTimeline(D) {
    var tl = D.entries_timeline;
    Plotly.newPlot("chart-timeline", [{
      x: tl.map(function (r) { return r.h; }),
      y: tl.map(function (r) { return r.n; }),
      type: "bar", marker: { color: GREEN },
      hovertemplate: "hour %{x}: %{y:,} positions<extra></extra>"
    }], baseLayout({
      xaxis: { title: { text: "Hour of experiment" }, dtick: tl.length > 24 ? 6 : 2 },
      yaxis: { title: { text: "Positions opened" } },
      showlegend: false, hovermode: "closest",
      margin: { l: 55, r: 10, t: 10, b: 48 }
    }), PLOTCFG);
  }

  // ---- DATA QUALITY -----------------------------------------------------------------
  function renderMarkSrc(D) {
    var ms = D.mark_sources;
    var H = ms.map(function (r) { return r.h; });
    Plotly.newPlot("chart-marksrc", [
      { x: H, y: ms.map(function (r) { return r.history; }), name: "live history mark",
        type: "bar", marker: { color: GREEN },
        hovertemplate: "%{y:,} history marks<extra></extra>" },
      { x: H, y: ms.map(function (r) { return r.settle; }), name: "settled at $1/$0",
        type: "bar", marker: { color: GOLD },
        hovertemplate: "%{y:,} settled marks<extra></extra>" },
      { x: H, y: ms.map(function (r) { return r.missing; }), name: "missing / not yet due",
        type: "bar", marker: { color: GRAY },
        hovertemplate: "%{y:,} missing<extra></extra>" }
    ], baseLayout({
      barmode: "stack",
      xaxis: { title: { text: "Horizon (hours)" }, dtick: H.length > 24 ? 4 : 1 },
      yaxis: { title: { text: "Positions" } }
    }), PLOTCFG);
  }

  // ---- STUDY 1 -----------------------------------------------------------------------
  function renderStudy1(D) {
    var s1 = D.study1;
    if (!s1 || s1.pending || !s1.rows || !s1.rows.length) {
      el("study1-pending").classList.remove("hidden");
      return;
    }
    var ps = s1.rows.map(function (r) {
      var v = r.p_value !== undefined ? r.p_value
            : r.p !== undefined ? r.p
            : r.pval !== undefined ? r.pval
            : r.p_perm;
      return typeof v === "number" ? v : null;
    }).filter(function (v) { return v !== null; });
    if (!ps.length) { el("study1-pending").classList.remove("hidden"); return; }
    el("chart-study1").classList.remove("hidden");
    Plotly.newPlot("chart-study1", [{
      x: ps, type: "histogram",
      xbins: { start: 0, end: 1, size: 0.05 },
      marker: { color: GRAY, line: { color: "#FFFFFF", width: 1 } },
      hovertemplate: "%{y:,} wallets<extra></extra>"
    }], baseLayout({
      xaxis: { title: { text: "luck-test p-value (10,000 shuffles)" }, range: [0, 1] },
      yaxis: { title: { text: "Wallets" } },
      shapes: [{ type: "line", x0: 0.05, x1: 0.05, yref: "paper", y0: 0, y1: 1,
                 line: { color: BRICK, width: 1.5, dash: "dash" } }],
      showlegend: false, hovermode: "closest",
      margin: { l: 55, r: 10, t: 10, b: 48 }
    }), PLOTCFG);
  }

  // ---- boot ------------------------------------------------------------------------
  function showError(err) {
    el("err-card").classList.remove("hidden");
    el("err-detail").textContent = String(err);
  }

  fetch("data.json")
    .then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status + " fetching data.json");
      return resp.json();
    })
    .then(function (D) {
      renderHero(D);
      renderEdge(D);
      renderReal(D);
      renderWallets(D);
      renderMarketControls(D);
      renderMarketTable(D);
      renderFan(D);
      renderEntryHist(D);
      renderTimeline(D);
      renderMarkSrc(D);
      renderStudy1(D);
    })
    .catch(function (err) { showError(err); });
})();
