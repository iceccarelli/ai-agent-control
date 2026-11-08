"use strict";
/* Owner control centre: renders GET /api/state. Read-only. No framework, no eval, no innerHTML, no inline handlers, no request except same-origin GET /api/state. */
(function () {
  var VIEWS = ["overview", "research", "portfolio", "execution", "risk", "evidence"];
  var TITLES = { overview: "Overview", research: "Research", portfolio: "Portfolio", execution: "Execution", risk: "Risk", evidence: "Evidence" };
  var STATE = null, ERR = null, LOADING = true;
  var viewEl = document.getElementById("view");

  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) { Object.keys(attrs).forEach(function (k) { if (k === "class") { el.className = attrs[k]; } else if (attrs[k] !== null && attrs[k] !== undefined) { el.setAttribute(k, attrs[k]); } }); }
    for (var i = 2; i < arguments.length; i++) { add(el, arguments[i]); }
    return el;
  }
  function add(el, c) {
    if (c === null || c === undefined || c === false) { return; }
    if (Array.isArray(c)) { c.forEach(function (x) { add(el, x); }); return; }
    el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
  function badge(basis) { return h("span", { "class": "badge b-" + basis, title: meaning(basis) }, basis); }
  function meaning(basis) { var f = (STATE && STATE.legend.basis || []).filter(function (b) { return b.code === basis; })[0]; return f ? f.meaning : basis; }
  function pill(status, text) { return h("span", { "class": "pill s-" + status }, text || status.replace("_", " ")); }
  var nf = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  function fmt(v, unit) {
    if (typeof v === "number") {
      if (unit === "USD") { return (v < 0 ? "-$" : "$") + nf.format(Math.abs(v)); }
      return nf.format(v) + (unit ? " " + unit : "");
    }
    return String(v) + (unit ? " " + unit : "");
  }
  function ageText(ms) {
    if (typeof ms !== "number") { return "unknown age"; }
    var s = Math.round(ms / 1000);
    if (s < 0) { return "in the future"; }
    if (s < 120) { return s + " s old"; }
    if (s < 7200) { return Math.round(s / 60) + " min old"; }
    if (s < 172800) { return Math.round(s / 3600) + " h old"; }
    return Math.round(s / 86400) + " days old";
  }
  /* A value always carries its basis badge; a null value is a dash with MISSING. */
  function V(x) {
    if (!x || typeof x !== "object" || !("v" in x)) { return h("span", { "class": "val" }, h("span", { "class": "num" }, "—"), badge("MISSING")); }
    var shown = x.v === null ? "—" : (typeof x.v === "boolean" ? (x.v ? "yes" : "no") : fmt(x.v, x.unit));
    return h("span", { "class": "val", title: x.note || null }, h("span", { "class": "num" }, shown), badge(x.basis));
  }
  function empty(msg) { return h("div", { "class": "empty" }, msg); }
  function errbox(msg) { return h("div", { "class": "errbox", role: "alert" }, h("strong", null, "Error: "), msg); }
  function stateBox(s) {
    if (!s) { return empty("No data."); }
    if (s.state === "ERROR") { return errbox(s.error || "unknown error"); }
    if (s.state === "NO_DATA") { return empty(s.empty || "No data captured yet."); }
    return null;
  }
  function table(cols, rows, caption) {
    var t = h("table", null, caption ? h("caption", { "class": "muted small" }, caption) : null,
      h("thead", null, h("tr", null, cols.map(function (c) { return h("th", { scope: "col" }, c.label); }))),
      h("tbody", null, rows.map(function (r) { return h("tr", null, cols.map(function (c) { return h("td", { "data-label": c.label }, c.render(r)); })); })));
    return t;
  }
  function kv(items) { return h("div", { "class": "kv" }, items.map(function (i) { return h("div", null, h("div", { "class": "statlabel" }, i[0]), h("div", { "class": "stat" }, i[1]), i[2] ? h("div", { "class": "small muted" }, i[2]) : null); })); }
  function list(items) { return h("ul", { "class": "plain" }, items.map(function (x) { return h("li", null, x); })); }
  function staleNote(s) { return s && s.generated_ms ? h("p", { "class": "muted small" }, "Source file " + s.file + ": " + ageText(s.age_ms) + (s.stale ? " (stale)" : "") + ". Declared basis: " + s.basis + ".") : null; }

  /* ---------- shared panels */
  function actionsPanel(actions) {
    return h("section", { "aria-labelledby": "act-h" }, h("h2", { id: "act-h" }, "Actions: what is permitted now"),
      h("p", { "class": "muted small" }, actions.note),
      h("ul", { "class": "flat" }, actions.items.map(function (a) {
        return h("li", null, h("strong", null, a.label + " "), pill(a.permitted ? "PERMITTED" : "BLOCKED", a.permitted ? "PERMITTED" : "BLOCKED"),
          h("ul", { "class": "plain small" }, a.reasons.map(function (r) { return h("li", null, r); })));
      })));
  }
  function attention(att) {
    if (!att.length) { return empty("No failures or warnings were raised by the checks that could run. This is not a statement about checks that had no data."); }
    return h("ul", { "class": "flat" }, att.map(function (a) { return h("li", null, pill(a.severity), " ", h("strong", null, a.source + ": "), a.text); }));
  }

  /* ---------- views */
  function overview(s) {
    var o = s.overview;
    return h("div", null,
      h("div", { "class": "banner s-" + o.headline.status }, h("h2", null, "System status: ", pill(o.headline.status), " ", o.headline.text),
        h("p", { "class": "muted small" }, "Green appears only when a checker returned it from real data. Missing data is shown as NO DATA, never as a pass.")),
      h("div", { "class": "grid" }, o.cards.map(function (c) {
        return h("a", { "class": "card", href: "#/" + c.link }, h("h3", null, c.title, pill(c.status)), h("p", null, c.text), badge(c.basis));
      })),
      h("h2", null, "Needs attention"), attention(o.attention),
      actionsPanel(s.actions));
  }

  function candidateTable(rows) {
    return table([
      { label: "Candidate", render: function (r) { return h("div", null, h("strong", null, r.name), h("div", { "class": "small muted mono" }, r.candidate)); } },
      { label: "Stage", render: function (r) { return pill(r.rejected ? "FAIL" : "NO_DATA", r.rejected ? "REJECTED" : r.stage); } },
      { label: "Ladder verdict", render: function (r) { return r.ladder_verdict; } },
      { label: "Evidence sufficient", render: function (r) { return r.evidence_sufficient ? "Yes (not authorisation)" : "No"; } },
      { label: "Executable on qualified venues", render: function (r) { return r.executable; } },
      { label: "Edge vs costs", render: function (r) { return V(r.edge_vs_costs); } },
      { label: "Why not authorised", render: function (r) { return h("details", null, h("summary", null, "Reason chain (" + r.why_not_authorised.length + ")"), list(r.why_not_authorised)); } }
    ], rows, "Candidates under study and the reasons none is authorised");
  }
  function research(s) {
    var r = s.research, out = [];
    if (r.state === "ERROR") { return errbox(r.error); }
    out.push(h("p", null, r.note));
    out.push(h("h2", null, "Candidates"));
    out.push(r.candidates.length ? candidateTable(r.candidates) : empty("No candidate specifications found in research/candidates."));
    out.push(h("h2", null, "Rejected, and why"));
    out.push(r.rejected.length ? table([
      { label: "Candidate", render: function (x) { return x.candidate; } }, { label: "What", render: function (x) { return x.what; } },
      { label: "Why", render: function (x) { return x.why; } }, { label: "Basis", render: function (x) { return badge(x.basis); } }], r.rejected)
      : empty("No rejection is on record in the sources loaded. " + (r.economics_loaded ? "" : "Corpus economics are not loaded; start with --with-economics to include them.")));
    out.push(h("h2", null, "Sufficient evidence"));
    out.push(r.sufficient_evidence.length ? list(r.sufficient_evidence) : empty("No candidate has sufficient evidence. Every candidate stays at research stage until a prospective result and the owner's attestation exist."));
    out.push(h("h2", null, "Candidate competition"));
    var c = r.competition, cb = stateBox(c);
    if (cb) { out.push(cb); } else {
      out.push(staleNote(c));
      out.push(table([
        { label: "Candidate", render: function (x) { return x.candidate_id; } }, { label: "Type", render: function (x) { return x.label; } },
        { label: "Status", render: function (x) { return pill(x.status === "REJECTED" ? "FAIL" : x.status === "SUFFICIENT_EVIDENCE" ? "INFO" : "NO_DATA", x.status); } },
        { label: "Gross (before costs)", render: function (x) { return V(x.gross); } }, { label: "Net (after costs)", render: function (x) { return V(x.net); } },
        { label: "Reason", render: function (x) { return x.reason || "—"; } }], c.candidates));
    }
    out.push(h("h2", null, "Estimated net opportunity"));
    var o = r.opportunities, ob = stateBox(o);
    if (ob) { out.push(ob); } else {
      out.push(staleNote(o));
      out.push(kv([["Items scanned", String(o.n_items)], ["Eligible with a known net", String(o.n_eligible)], ["Positive net", String(o.n_positive_net)], ["Best eligible net", V(o.best_net)], ["Estimated net per day", V(o.est_net_usd_per_day)]]));
      out.push(table([
        { label: "Candidate", render: function (x) { return x.candidate_id; } }, { label: "Symbols", render: function (x) { return x.symbols.join(", "); } }, { label: "Type", render: function (x) { return x.label; } },
        { label: "Gross (before costs)", render: function (x) { return V(x.gross); } }, { label: "Net (after costs)", render: function (x) { return V(x.net); } },
        { label: "Capacity", render: function (x) { return V(x.capacity); } }, { label: "Excluded because", render: function (x) { return x.exclusions.length ? x.exclusions.join("; ") : "—"; } }], o.items));
    }
    return h("div", null, out);
  }

  function portfolio(s) {
    var p = s.portfolio, out = [], b = stateBox(p);
    out.push(h("h2", null, "Exposure"));
    if (b) { out.push(b); } else {
      out.push(staleNote(p));
      out.push(kv([["Equity", V(p.equity)], ["Gross exposure", V(p.gross_exposure)], ["Net exposure", V(p.net_exposure)], ["Unrealised P&L", V(p.unrealised)]]));
      out.push(p.positions.length ? table([
        { label: "Symbol", render: function (x) { return x.symbol; } }, { label: "Type", render: function (x) { return x.label; } }, { label: "Side", render: function (x) { return x.side; } },
        { label: "Notional", render: function (x) { return V(x.notional); } }, { label: "Mark", render: function (x) { return V(x.mark_price); } }, { label: "Unrealised", render: function (x) { return V(x.unrealised); } }], p.positions)
        : empty("The portfolio file lists no open positions."));
    }
    out.push(h("h2", null, "Profit and loss"));
    var n = p.pnl, nb = stateBox(n);
    if (nb) { out.push(nb); } else {
      out.push(staleNote(n));
      var sim = n.kind === "SIMULATED_NOT_REALISED";
      out.push(h("p", { "class": "muted" }, sim ? "These figures are SIMULATED. They are not realised cash." : (n.kind === "OWNER_IMPORT_UNVERIFIED" ? "These figures come from an owner import that is not reconciled with the venue." : "Account figures as stated by the ledger.")));
      var g = sim ? "simulated" : "realised";
      out.push(kv([[(sim ? "Simulated" : "Realised") + " gross (before fees)", V(n[g + "_gross"])], ["Fees", V(n[g + "_fees"])], ["Funding", V(n[g + "_funding"])],
        [(sim ? "Simulated" : "Realised") + " net (after fees)", V(n[g + "_net"])], ["Unrealised (mark-to-market)", V(n.unrealised)]].concat(sim ? [["Simulated net, total (realised + unrealised)", V(n.total_net)]] : [])));
    }
    return h("div", null, out);
  }

  function execution(s) {
    var e = s.execution, out = [], env = s.environment;
    out.push(h("div", { "class": "banner s-INFO" }, h("h2", null, "Mode: ", e.mode), h("p", null, e.note)));
    out.push(kv([["Live trading", env.live_authorized ? "ARMED" : "Not armed", env.reason], ["Promotion gate", env.gate.decision, env.gate.complete + " of " + env.gate.total + " items complete"],
      ["Capital allocated", V(env.capital_usd)], ["Testnet qualification (offline)", V(e.testnet_offline_qualification)]]));
    out.push(actionsPanel(s.actions));
    out.push(h("h2", null, "Replay results"));
    var b = stateBox(e);
    if (b) { out.push(b); } else {
      out.push(staleNote(e));
      out.push(h("p", { "class": e.survivorship && e.survivorship.survivorship_free ? "muted" : "errbox" }, e.survivorship.text));
      out.push(kv([["Order groups", String(e.n_groups)], ["Unhedged or unknown outcome", String(e.unhedged_or_unknown)], ["Gross (before fees)", V(e.gross)], ["Fees", V(e.fees)], ["Net (after fees)", V(e.net)]]));
      var gs = Object.keys(e.groups_by_state);
      if (gs.length) { out.push(table([{ label: "Group state", render: function (k) { return k; } }, { label: "Count", render: function (k) { return String(e.groups_by_state[k]); } }], gs)); }
      out.push(e.symbols.length ? table([
        { label: "Symbol", render: function (x) { return x.symbol; } }, { label: "Type", render: function (x) { return x.label; } }, { label: "Groups", render: function (x) { return String(x.n_groups); } },
        { label: "Gross", render: function (x) { return V(x.gross); } }, { label: "Fees", render: function (x) { return V(x.fees); } }, { label: "Net", render: function (x) { return V(x.net); } }], e.symbols) : null);
    }
    return h("div", null, out);
  }

  function risk(s) {
    var r = s.risk, out = [], b = stateBox(r);
    out.push(h("h2", null, "Needs attention"));
    out.push(attention(s.overview.attention));
    out.push(h("h2", null, "Risk state"));
    if (b) { out.push(b); } else {
      out.push(staleNote(r));
      out.push(h("div", { "class": "banner s-" + (r.halted ? "FAIL" : "INFO") }, h("strong", null, r.halted ? "Trading is HALTED. " : "Not halted. "), r.halt_reasons.join("; ")));
      out.push(r.limits.length ? table([
        { label: "Limit", render: function (x) { return x.name; } }, { label: "Limit value", render: function (x) { return V(x.limit); } }, { label: "Current", render: function (x) { return V(x.value); } },
        { label: "Status", render: function (x) { return pill(x.status === "OK" ? "OK" : x.status === "BREACH" ? "FAIL" : "NO_DATA", x.status); } }], r.limits) : empty("The risk file lists no limits."));
      out.push(r.alerts.length ? table([{ label: "Severity", render: function (x) { return pill(x.severity === "CRITICAL" ? "FAIL" : x.severity === "WARN" ? "ATTENTION" : "INFO", x.severity); } },
        { label: "Code", render: function (x) { return x.code; } }, { label: "Detail", render: function (x) { return x.detail; } }], r.alerts) : empty("No alerts in the risk file."));
    }
    out.push(h("h2", null, "Risk policy"));
    out.push(h("p", null, r.policy.text, " ", badge(r.policy.basis)));
    out.push(kv([["Proposed max hold", V(r.policy.proposed_max_hold_days)], ["Proposed drawdown limit", V(r.policy.proposed_drawdown_bps_of_notional)]]));
    if (r.policy.refusals_without_policy.length) { out.push(h("p", { "class": "muted small" }, "Without an approved policy the evaluator refuses: ")); out.push(list(r.policy.refusals_without_policy)); }
    if (r.policy.unmeasured_limits.length) { out.push(h("details", null, h("summary", null, "Limits that cannot yet be measured"), list(r.policy.unmeasured_limits))); }
    return h("div", null, out);
  }

  function evidence(s) {
    var ev = s.evidence, out = [], f = ev.freshness;
    out.push(h("h2", null, "Market data freshness"));
    var fb = f.state === "ERROR" ? errbox(f.error) : null;
    if (fb) { out.push(fb); } else if (f.state === "NO_DATA") { out.push(empty(f.empty || "No data captured yet. Run tools/market_collector.py.")); } else {
      out.push(kv([["Data freshness", pill(f.verdict === "HEALTHY" ? "OK" : f.verdict === "PARTIAL" ? "ATTENTION" : "FAIL", f.verdict)], ["Research readiness", pill(f.readiness === "READY" ? "OK" : "NO_DATA", f.readiness)],
        ["Code correctness", f.code_correctness.replace("_", " "), "not assessed here"], ["Datasets re-hashed this run", f.verified_this_run ? "Yes" : "No", f.verified_this_run ? "" : "timestamps are labelled UNVERIFIED"]]));
      if (f.latest_cycle) { out.push(h("p", null, "Newest complete collection cycle: ", V(f.latest_cycle.age_ms), " ago")); }
      out.push(f.datasets.length ? table([{ label: "Dataset", render: function (x) { return x.dataset; } }, { label: "Status", render: function (x) { return x.status; } }, { label: "Symbols", render: function (x) { return String(x.n_symbols); } },
        { label: "Stale", render: function (x) { return String(x.stale_symbols); } }, { label: "Incomplete", render: function (x) { return String(x.incomplete_symbols); } }, { label: "Newest row age", render: function (x) { return V(x.age_ms); } }], f.datasets) : null);
      out.push(f.issues.length ? h("details", null, h("summary", null, "Audit issues (" + f.issues.length + ")"), list(f.issues.map(function (i) { return i.level + " " + i.code + ": " + i.detail; }))) : null);
    }
    out.push(h("h2", null, "Universe capture"));
    var c = ev.universe_capture, cb = stateBox(c);
    if (cb) { out.push(cb); } else {
      out.push(kv([["Integrity", V(c.integrity)], ["Dataset status", c.dataset_status], ["Survivorship", c.survivorship, c.survivorship_note]]));
    }
    out.push(h("h2", null, "Artifact files"));
    out.push(table([{ label: "File", render: function (x) { return h("span", { "class": "mono" }, x.file); } }, { label: "State", render: function (x) { return pill(x.state === "OK" ? "INFO" : x.state === "ERROR" ? "FAIL" : "NO_DATA", x.state === "OK" ? "LOADED" : x.state); } },
      { label: "Declared basis", render: function (x) { return x.basis ? badge(x.basis) : "—"; } }, { label: "Age", render: function (x) { return x.state === "OK" ? ageText(x.age_ms) + (x.stale ? " (stale)" : "") : "—"; } },
      { label: "Problem", render: function (x) { return x.error || "—"; } }], ev.sources));
    out.push(h("h2", null, "Owner console read-out"));
    var oc = ev.owner_console;
    out.push(kv([["Prospective evidence", oc.evidence.status, oc.evidence.why], ["Backup", oc.backup.status, oc.backup.why], ["Owner account P&L", oc.owner_pnl.status, oc.owner_pnl.why]]));
    out.push(h("h2", null, "How to read this"));
    out.push(list(ev.caveats));
    return h("div", null, out);
  }

  var RENDER = { overview: overview, research: research, portfolio: portfolio, execution: execution, risk: risk, evidence: evidence };

  function currentView() { var m = /^#\/(\w+)/.exec(location.hash || ""); return m && VIEWS.indexOf(m[1]) >= 0 ? m[1] : "overview"; }
  function render() {
    var v = currentView();
    document.title = TITLES[v] + " - Owner control centre";
    Array.prototype.forEach.call(document.querySelectorAll(".rail a"), function (a) { if (a.getAttribute("data-view") === v) { a.setAttribute("aria-current", "page"); } else { a.removeAttribute("aria-current"); } });
    while (viewEl.firstChild) { viewEl.removeChild(viewEl.firstChild); }
    if (STATE) {
      var hl = document.getElementById("headline");
      hl.className = "pill s-" + STATE.overview.headline.status; hl.textContent = STATE.overview.headline.status.replace("_", " ");
      document.getElementById("asof").textContent = "as of " + new Date(STATE.as_of_ms).toISOString().replace("T", " ").slice(0, 19) + " UTC";
      var lg = document.getElementById("legend");
      if (!lg.firstChild) {
        var ld = document.getElementById("legend-def");
        STATE.legend.basis.forEach(function (b) { lg.appendChild(h("li", null, badge(b.code))); ld.appendChild(h("dt", null, badge(b.code))); ld.appendChild(h("dd", null, b.meaning)); });
      }
      viewEl.appendChild(h("h2", { id: "view-h", tabindex: "-1" }, TITLES[v]));
      try { viewEl.appendChild(RENDER[v](STATE)); } catch (e) { viewEl.appendChild(errbox("This view could not be drawn: " + e.message)); }
    } else if (ERR) {
      viewEl.appendChild(errbox(ERR + " The backend is unreachable or returned an error; nothing is shown rather than a guess."));
    } else {
      viewEl.appendChild(empty("Loading state from the local backend."));
    }
  }
  function load() {
    LOADING = true;
    fetch("/api/state", { cache: "no-store", credentials: "same-origin" }).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    }).then(function (j) { STATE = j; ERR = null; render(); }).catch(function (e) { ERR = "Could not load /api/state (" + e.message + ")."; render(); }).then(function () { LOADING = false; });
  }
  window.addEventListener("hashchange", function () { render(); var hd = document.getElementById("view-h"); if (hd) { hd.focus(); } });
  document.getElementById("refresh").addEventListener("click", load);
  render();
  load();
  window.setInterval(load, 30000);
})();
