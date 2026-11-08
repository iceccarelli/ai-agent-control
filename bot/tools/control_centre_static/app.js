"use strict";
/* Owner control centre: renders ONE state (GET /api/state). Read-only. No framework, no eval, no innerHTML, no inline handlers, no request except same-origin GET /api/state.
   Every string from the state reaches the page through createTextNode only. */
(function () {
  var VIEWS = ["overview", "research", "portfolio", "execution", "accounting", "risk", "testnet", "evidence"];
  var TITLES = { overview: "Overview", research: "Research", portfolio: "Portfolio", execution: "Execution", accounting: "Accounting", risk: "Risk", testnet: "Testnet", evidence: "Evidence" };
  var POLL_MS = 15000, STALE_FETCH_MS = 90000;
  var STATE = null, ERR = null, LOADED_AT = null;
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
  function meaning(basis) { var f = (STATE && STATE.legend.basis || []).filter(function (b) { return b.code === basis; })[0]; return f ? f.meaning : basis; }
  function badge(basis) { return h("span", { "class": "badge b-" + basis, title: meaning(basis) }, basis); }
  function pill(status, text) { return h("span", { "class": "pill s-" + status }, text || String(status).replace("_", " ")); }
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
  function utc(ms) { return typeof ms === "number" ? new Date(ms).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "unknown"; }
  /* A value always carries its basis badge; a null value is a dash with MISSING. */
  function V(x) {
    if (!x || typeof x !== "object" || !("v" in x)) { return h("span", { "class": "val" }, h("span", { "class": "num" }, "—"), badge("MISSING")); }
    var shown = x.v === null ? "—" : (typeof x.v === "boolean" ? (x.v ? "yes" : "no") : fmt(x.v, x.unit));
    var tip = [x.note, x.src ? "source: " + x.src : null].filter(function (t) { return t; }).join(" | ");
    return h("span", { "class": "val", title: tip || null }, h("span", { "class": "num" }, shown), badge(x.basis), x.v === null && x.note ? h("span", { "class": "small muted" }, x.note) : null);
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
    return h("table", null, caption ? h("caption", { "class": "muted small" }, caption) : null,
      h("thead", null, h("tr", null, cols.map(function (c) { return h("th", { scope: "col" }, c.label); }))),
      h("tbody", null, rows.map(function (r) { return h("tr", null, cols.map(function (c) { return h("td", { "data-label": c.label }, c.render(r)); })); })));
  }
  function kv(items) { return h("div", { "class": "kv" }, items.map(function (i) { return h("div", null, h("div", { "class": "statlabel" }, i[0]), h("div", { "class": "stat" }, i[1]), i[2] ? h("div", { "class": "small muted" }, i[2]) : null); })); }
  function list(items) { return h("ul", { "class": "plain" }, items.map(function (x) { return h("li", null, x); })); }
  function section(title, id) { return h("h2", { id: id || null }, title); }
  function staleNote(s) { return s && s.generated_ms ? h("p", { "class": "muted small" }, "Source file " + s.file + ": " + ageText(s.age_ms) + (s.stale ? " (stale)" : "") + ". Declared basis: " + (s.basis || "none declared") + ".") : null; }
  function mapRows(o) { return Object.keys(o || {}).map(function (k) { return [k, o[k]]; }); }
  function mapTable(o, c1, c2, emptyMsg) {
    var rows = mapRows(o);
    return rows.length ? table([{ label: c1, render: function (r) { return r[0]; } }, { label: c2, render: function (r) { return r[1] === null ? "—" : String(r[1]); } }], rows) : empty(emptyMsg);
  }
  function mono(t) { return h("span", { "class": "mono" }, t === null || t === undefined ? "—" : t); }

  /* ---------- shared banners */
  function coherenceBanner(s) {
    var c = s.coherence;
    if (c.verdict === "INCONSISTENT_RUN") {
      return h("div", { "class": "banner s-FAIL", role: "alert" }, h("h2", null, pill("FAIL", "INCONSISTENT_RUN"), " The artifacts are not from one run"), h("p", null, c.banner),
        h("ul", { "class": "plain small" }, c.mismatches.map(function (m) { return h("li", null, h("strong", null, m.field + ": "), Object.keys(m.values).map(function (f) { return f + " = " + m.values[f]; }).join("; "), m.why ? " (" + m.why + ")" : ""); })));
    }
    if (c.verdict === "UNVERIFIED") { return h("div", { "class": "banner s-ATTENTION" }, h("strong", null, "Run identity UNVERIFIED. "), c.banner); }
    if (c.verdict === "CONSISTENT") { return h("p", { "class": "muted small" }, "Run identity: consistent across " + c.n_compared + " artifact(s). " + c.note); }
    return null;
  }
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

  /* ---------- Overview */
  function overview(s) {
    var o = s.overview, out = [];
    out.push(coherenceBanner(s));
    out.push(h("div", { "class": "banner s-" + o.headline.status }, h("h2", null, "System status: ", pill(o.headline.status), " ", o.headline.text),
      h("p", { "class": "muted small" }, "Green appears only when a checker returned it from real data. Missing data is shown as NO DATA, never as a pass.")));
    out.push(section("In plain words"));
    out.push(h("dl", { "class": "qa" }, o.answers.map(function (a) { return [h("dt", null, a.q), h("dd", null, a.a, " ", badge(a.basis))]; })));
    out.push(section("What must happen next"));
    out.push(h("ol", { "class": "plain" }, o.next_steps.map(function (t) { return h("li", null, t); })));
    out.push(section("Engine and run"));
    var run = o.engine.run;
    out.push(kv([["Mode", o.engine.mode], ["Run", run ? mono(run.run_id) : "no identified run", run ? run.mode + " / candidate " + run.candidate_id : null], ["Code revision", run ? mono(run.code_revision) : "—"],
      ["Last pipeline outcome", o.engine.pipeline_outcome || "none on file"], ["Halted", o.engine.halted === null ? "unknown (no risk state)" : (o.engine.halted ? "YES" : "no")]]));
    out.push(section("Market data"));
    var md = o.market_data;
    if (md.state === "ERROR") { out.push(errbox(md.error)); } else if (md.state === "NO_DATA") { out.push(empty(md.empty || "No market data captured yet.")); } else {
      out.push(kv([["Data freshness", pill(md.verdict === "HEALTHY" ? "OK" : md.verdict === "PARTIAL" ? "ATTENTION" : "FAIL", md.verdict)], ["Research readiness", md.readiness || "—"],
        ["Newest complete cycle", md.latest_cycle ? V(md.latest_cycle.age_ms) : "none"], ["Collector (health.json)", md.collector && md.collector.state === "OK" ? V(md.collector.status) : "no health.json"]]));
    }
    out.push(section("Environment flags (presence only)"));
    out.push(h("p", { "class": "muted small" }, o.environment.note + " Live trading authorised: " + (o.environment.live_authorized ? "YES (stop and check)" : "no") + "."));
    out.push(table([{ label: "Variable", render: function (f) { return mono(f.name); } }, { label: "Presence", render: function (f) { return pill(f.name === "LIVE_TRADING_ACK" && f.presence === "set" ? "FAIL" : "INFO", f.presence); } }], o.environment.flags));
    out.push(section("Active restrictions"));
    out.push(o.restrictions.length ? h("ul", { "class": "flat" }, o.restrictions.map(function (r) { return h("li", null, pill(r.severity), " ", h("strong", null, r.source + ": "), r.text); })) : empty("No restriction is active that the checks know of."));
    out.push(section("Active incidents"));
    out.push(o.incidents.length ? table([{ label: "Severity", render: function (x) { return pill(x.severity === "WARN" ? "ATTENTION" : "FAIL", x.severity); } }, { label: "Code", render: function (x) { return x.code; } },
      { label: "Detail", render: function (x) { return x.detail || "—"; } }, { label: "Source", render: function (x) { return x.source; } }], o.incidents) : empty("No incident from the reconcile report. If no reconcile_report.json exists this is not a statement that none occurred."));
    out.push(section("Checks"));
    out.push(h("div", { "class": "grid" }, o.cards.map(function (c) {
      return h("a", { "class": "card", href: "#/" + c.link }, h("h3", null, c.title, pill(c.status)), h("p", null, c.text), badge(c.basis));
    })));
    out.push(section("Needs attention"));
    out.push(attention(o.attention));
    out.push(actionsPanel(s.actions));
    return h("div", null, out);
  }

  /* ---------- Research */
  function candidateTable(rows) {
    return table([
      { label: "Candidate", render: function (r) { return h("div", null, h("strong", null, r.name), h("div", { "class": "small muted mono" }, r.candidate)); } },
      { label: "Stage", render: function (r) { return pill(r.rejected ? "FAIL" : "NO_DATA", r.rejected ? "REJECTED" : r.stage); } },
      { label: "Ladder verdict", render: function (r) { return r.ladder_verdict; } },
      { label: "Evidence quality", render: function (r) { return r.required_data + " / " + r.point_in_time + (r.evidence_sufficient ? " (sufficient; not authorisation)" : " (not sufficient)"); } },
      { label: "Executable on qualified venues", render: function (r) { return r.executable; } },
      { label: "Edge vs costs", render: function (r) { return V(r.edge_vs_costs); } },
      { label: "Why not authorised", render: function (r) { return h("details", null, h("summary", null, "Reason chain (" + r.why_not_authorised.length + ")"), list(r.why_not_authorised)); } }
    ], rows, "Candidates under study and the reasons none is authorised");
  }
  function research(s) {
    var r = s.research, out = [coherenceBanner(s)];
    out.push(section("Pipeline outcome"));
    var p = r.pipeline, pb = stateBox(p);
    if (pb) { out.push(pb); } else {
      out.push(staleNote(p));
      out.push(h("div", { "class": "banner s-" + (p.blocking ? "ATTENTION" : "INFO") }, h("strong", null, p.outcome), ": " + p.meaning, p.reason ? " Reason: " + p.reason : "", h("p", { "class": "small" }, "Next: " + p.next)));
      out.push(h("h3", null, "Required span"));
      out.push(mapTable(p.required_span, "Field", "Value", "The pipeline stated no required span."));
      out.push(h("h3", null, "Data digest parts"));
      out.push(mapTable(p.data_digest_parts, "Part", "Digest", "No digest parts stated."));
      out.push(h("h3", null, "Evidence basis per artifact"));
      out.push(mapTable(p.evidence_basis, "Artifact", "Basis", "No per-artifact evidence basis stated."));
    }
    out.push(section("Universe scope"));
    var u = r.universe_scope, ub = stateBox(u);
    if (ub) { out.push(ub); } else {
      out.push(staleNote(u));
      out.push(kv([["Scope verdict", u.scope_verdict_shown || "not stated"], ["Corroborated by a verified capture here", u.corroborated_here ? "yes" : "no"]]));
      out.push(h("h3", null, "Eligible perpetual lifecycle universe"));
      out.push(mapTable(u.eligible, "Measure", "Count", "No eligible-universe count stated."));
      out.push(h("h3", null, "Lifecycle classes"));
      out.push(mapTable(u.lifecycle_classes, "Class", "Count", "No lifecycle class counts stated."));
    }
    out.push(section("Funnel"));
    var f = r.funnel, fb = stateBox(f);
    if (fb) { out.push(fb); } else {
      out.push(staleNote(f));
      out.push(f.steps.length ? table([{ label: "Step", render: function (x) { return x.name; } }, { label: "Count", render: function (x) { return String(x.count); } }], f.steps) : empty("The funnel lists no steps."));
      out.push(h("h3", null, "Exclusion reasons"));
      out.push(mapTable(f.exclusion_reasons, "Reason", "Count", "No exclusion reasons stated."));
    }
    if (r.state === "ERROR") { out.push(errbox(r.error)); return h("div", null, out); }
    out.push(h("p", null, r.note));
    out.push(section("Candidates"));
    out.push(r.candidates.length ? candidateTable(r.candidates) : empty("No candidate specifications found in research/candidates."));
    out.push(section("Rejected, and why"));
    out.push(r.rejected.length ? table([
      { label: "Candidate", render: function (x) { return x.candidate; } }, { label: "What", render: function (x) { return x.what; } },
      { label: "Why", render: function (x) { return x.why; } }, { label: "Basis", render: function (x) { return badge(x.basis); } }], r.rejected)
      : empty("No rejection is on record in the sources loaded. " + (r.economics_loaded ? "" : "Corpus economics are not loaded; start with --with-economics to include them.")));
    out.push(section("Sufficient evidence"));
    out.push(r.sufficient_evidence.length ? list(r.sufficient_evidence) : empty("No candidate has sufficient evidence. Every candidate stays at research stage until a prospective result and the owner's attestation exist."));
    out.push(section("Candidate competition"));
    var c = r.competition, cb = stateBox(c);
    if (cb) { out.push(cb); } else {
      out.push(staleNote(c));
      out.push(table([
        { label: "Candidate", render: function (x) { return x.candidate_id; } }, { label: "Type", render: function (x) { return x.label; } },
        { label: "Status", render: function (x) { return pill(x.status === "REJECTED" ? "FAIL" : x.status === "SUFFICIENT_EVIDENCE" ? "INFO" : "NO_DATA", x.status); } },
        { label: "Observations", render: function (x) { return x.n_observations === null ? "unknown" : String(x.n_observations); } },
        { label: "Gross (before costs)", render: function (x) { return V(x.gross); } }, { label: "Net (after costs)", render: function (x) { return V(x.net); } },
        { label: "Reason", render: function (x) { return x.reason || "—"; } }], c.candidates));
    }
    out.push(section("Opportunity ranking and expected net economics"));
    var o = r.opportunities, ob = stateBox(o);
    if (ob) { out.push(ob); } else {
      out.push(staleNote(o));
      out.push(kv([["Items scanned", String(o.n_items)], ["Eligible with a known net", String(o.n_eligible)], ["Positive net", String(o.n_positive_net)], ["Best eligible net", V(o.best_net)], ["Estimated net per day", V(o.est_net_usd_per_day)]]));
      out.push(table([
        { label: "Rank", render: function (x) { return x.rank === null ? "unranked" : String(x.rank); } },
        { label: "Candidate", render: function (x) { return x.candidate_id; } }, { label: "Symbols", render: function (x) { return x.symbols.join(", "); } }, { label: "Type", render: function (x) { return x.label; } },
        { label: "Gross (before costs)", render: function (x) { return V(x.gross); } }, { label: "Net (after costs)", render: function (x) { return V(x.net); } },
        { label: "Capacity", render: function (x) { return V(x.capacity); } }, { label: "Excluded because", render: function (x) { return x.exclusions.length ? x.exclusions.join("; ") : "—"; } }], o.items));
    }
    return h("div", null, out);
  }

  /* ---------- Portfolio */
  function portfolio(s) {
    var p = s.portfolio, out = [coherenceBanner(s)], b = stateBox(p);
    out.push(h("div", { "class": "banner s-INFO" }, "Only fields the portfolio file carries are shown; every other field is MISSING, not zero."));
    out.push(section("Exposure"));
    if (b) { out.push(b); } else {
      out.push(staleNote(p));
      out.push(kv([["Equity", V(p.equity)], ["Available balance", V(p.available_balance)], ["Reserved capital", V(p.reserved_capital)], ["Gross exposure", V(p.gross_exposure)], ["Net exposure", V(p.net_exposure)],
        ["Leverage", V(p.leverage)], ["Margin", V(p.margin)], ["Concentration", V(p.concentration)], ["Drawdown", V(p.drawdown)], ["Unrealised P&L", V(p.unrealised)]]));
      out.push(section("Positions"));
      out.push(p.positions.length ? table([
        { label: "Symbol", render: function (x) { return x.symbol; } }, { label: "Type", render: function (x) { return x.label; } }, { label: "Side", render: function (x) { return x.side; } },
        { label: "Notional", render: function (x) { return V(x.notional); } }, { label: "Mark", render: function (x) { return V(x.mark_price); } }, { label: "Unrealised", render: function (x) { return V(x.unrealised); } }], p.positions)
        : empty("The portfolio file lists no open positions."));
    }
    out.push(section("Profit and loss (ledger)"));
    out.push(pnlBlock(p.pnl));
    return h("div", null, out);
  }
  function pnlBlock(n) {
    var nb = stateBox(n), out = [];
    if (nb) { return nb; }
    out.push(staleNote(n));
    var sim = n.kind === "SIMULATED_NOT_REALISED";
    out.push(h("p", { "class": "muted" }, sim ? "These figures are SIMULATED. They are not realised cash." : (n.kind === "OWNER_IMPORT_UNVERIFIED" ? "These figures come from an owner import that is not reconciled with the venue." : "Account figures as the ledger states them; not reconciled with the venue unless Accounting says so.")));
    var g = sim ? "simulated" : "realised";
    out.push(kv([[(sim ? "Simulated" : "Realised") + " gross (before fees)", V(n[g + "_gross"])], ["Fees", V(n[g + "_fees"])], ["Funding", V(n[g + "_funding"])],
      [(sim ? "Simulated" : "Realised") + " net (after fees)", V(n[g + "_net"])], ["Unrealised (mark-to-market)", V(n.unrealised)], ["Net, total (realised + unrealised)", V(n.total_net)]]));
    return h("div", null, out);
  }

  /* ---------- Execution */
  function execution(s) {
    var e = s.execution, out = [coherenceBanner(s)], env = s.environment;
    out.push(h("div", { "class": "banner s-INFO" }, h("h2", null, "Mode: ", e.mode), h("p", null, e.note)));
    out.push(kv([["Live trading", env.live_authorized ? "ARMED" : "Not armed", env.reason], ["Promotion gate", env.gate.decision, env.gate.complete + " of " + env.gate.total + " items complete"],
      ["Capital allocated", V(env.capital_usd)], ["Testnet qualification (offline)", V(e.testnet_offline_qualification)]]));
    out.push(actionsPanel(s.actions));
    out.push(section("Replay results (SIMULATED)"));
    var b = stateBox(e);
    if (b) { out.push(b); } else {
      out.push(staleNote(e));
      out.push(h("p", { "class": e.survivorship && e.survivorship.survivorship_free ? "muted" : "errbox" }, e.survivorship.text));
      out.push(kv([["Order groups", String(e.n_groups)], ["Unhedged or unknown outcome", String(e.unhedged_or_unknown)], ["Gross (before fees)", V(e.gross)], ["Fees", V(e.fees)], ["Net (after fees)", V(e.net)], ["Order-level detail", V(e.order_level)]]));
      var gs = Object.keys(e.groups_by_state);
      if (gs.length) { out.push(h("h3", null, "Order-group states")); out.push(table([{ label: "Group state", render: function (k) { return k; } }, { label: "Count", render: function (k) { return String(e.groups_by_state[k]); } }], gs)); }
      out.push(e.symbols.length ? table([
        { label: "Symbol", render: function (x) { return x.symbol; } }, { label: "Type", render: function (x) { return x.label; } }, { label: "Groups", render: function (x) { return String(x.n_groups); } },
        { label: "Gross", render: function (x) { return V(x.gross); } }, { label: "Fees", render: function (x) { return V(x.fees); } }, { label: "Net", render: function (x) { return V(x.net); } }], e.symbols) : null);
    }
    out.push(section("Testnet orders and fills (TESTNET, not owner profit)"));
    var t = e.testnet, tb = stateBox(t);
    if (tb) { out.push(tb); } else {
      out.push(kv([["Orders", String(t.counts.orders)], ["Acknowledged", String(t.counts.acked)], ["Filled", String(t.counts.filled)], ["Partially filled", String(t.counts.partial)], ["Cancelled", String(t.counts.cancelled)],
        ["Rejected", String(t.counts.rejected)], ["Open", String(t.counts.open)], ["Executions", String(t.counts.executions)]]));
      out.push(ordersTable(t.orders));
    }
    out.push(section("Reconciliation"));
    var r = e.reconciliation, rb = stateBox(r);
    if (rb) { out.push(rb); } else {
      out.push(staleNote(r));
      out.push(kv([["Halts new exposure", r.halt_new_exposure ? "YES" : "no"], ["Views compared", r.views_compared.join(", ") || "none"], ["Venue view compared", r.has_venue_view ? "yes" : "no (internal only)"], ["Active findings", String(r.active.length)]]));
      out.push(r.findings.length ? table([{ label: "Severity", render: function (x) { return pill(x.severity === "INFO" ? "INFO" : x.severity === "WARN" ? "ATTENTION" : "FAIL", x.severity); } }, { label: "Code", render: function (x) { return x.code; } },
        { label: "Detail", render: function (x) { return x.detail || "—"; } }], r.findings) : empty("The report lists no findings."));
    }
    return h("div", null, out);
  }
  function ordersTable(rows) {
    return rows.length ? table([{ label: "Link id", render: function (x) { return mono(x.link_id); } }, { label: "Order id", render: function (x) { return x.order_id ? mono(x.order_id) : "not acknowledged"; } },
      { label: "Symbol", render: function (x) { return x.symbol; } }, { label: "Side", render: function (x) { return x.side; } }, { label: "Qty", render: function (x) { return V(x.qty); } },
      { label: "Status", render: function (x) { return x.status; } }, { label: "Filled", render: function (x) { return V(x.filled_qty); } }, { label: "Avg price", render: function (x) { return V(x.avg_price); } }], rows) : empty("No order is listed.");
  }

  /* ---------- Accounting */
  function accounting(s) {
    var a = s.accounting, out = [coherenceBanner(s)];
    if (a.state === "NO_DATA") { out.push(empty(a.empty)); }
    out.push(section("Simulated, testnet and mainnet cash are separate"));
    out.push(h("div", { "class": "grid" },
      h("div", { "class": "card" }, h("h3", null, "SIMULATED", badge("SIMULATED")), h("p", { "class": "small" }, a.simulated.label), h("div", null, "Net: ", V(a.simulated.net)), h("div", null, "Unrealised: ", V(a.simulated.unrealised))),
      h("div", { "class": "card" }, h("h3", null, "TESTNET", badge("EXCHANGE_REPORTED")), h("p", { "class": "small" }, a.testnet.label), h("div", null, "Fees: ", V(a.testnet.fees)), h("div", null, "Funding: ", V(a.testnet.funding)), h("div", null, "Price P&L: ", V(a.testnet.price_pnl)), h("p", { "class": "small muted" }, a.testnet.note)),
      h("div", { "class": "card" }, h("h3", null, "REALISED MAINNET CASH", pill("NO_DATA", a.mainnet_cash.status)), h("p", { "class": "small" }, a.mainnet_cash.note), h("div", null, "Realised: ", V(a.mainnet_cash.realised)))));
    out.push(section("Components of the one net"));
    out.push(a.withheld ? errbox("Figures withheld: the artifacts are not from one run (INCONSISTENT_RUN).") : null);
    out.push(table([{ label: "Component", render: function (c) { return c.label; } }, { label: "Value", render: function (c) { return V(c.value); } }, { label: "Source field", render: function (c) { return c.value.src ? mono(c.value.src) : "—"; } }], a.components,
      "Gross and net are different fields; net is never inferred from gross. Slippage, hedge cost and adverse selection have no backend source yet."));
    out.push(section("Identity checks"));
    out.push(h("p", null, "Integrity: ", pill(a.integrity === "PASS" ? "OK" : a.integrity === "FAIL" ? "FAIL" : "NO_DATA", a.integrity)));
    out.push(a.checks.length ? table([{ label: "Check", render: function (c) { return c.label; } }, { label: "Result", render: function (c) { return pill(c.state === "PASS" ? "OK" : c.state === "FAIL" ? "FAIL" : "NO_DATA", c.state); } }, { label: "Detail", render: function (c) { return c.detail; } }], a.checks)
      : empty("No identity can be checked: no accounting figures are loaded."));
    out.push(section("Venue reconciliation"));
    var v = a.venue_reconciliation;
    out.push(h("div", { "class": "banner s-" + (v.status === "VENUE_RECONCILED" ? "OK" : "ATTENTION") }, h("strong", null, v.status), ". " + v.meaning));
    if (v.why_not.length) { out.push(h("p", { "class": "small muted" }, "Why not venue reconciled:")); out.push(list(v.why_not)); }
    out.push(section("Is the P&L complete?"));
    out.push(h("p", null, a.complete ? "Yes: every required component is present, the identities pass and the ledger is venue reconciled." : "No."));
    if (a.why_incomplete.length) { out.push(list(a.why_incomplete)); }
    out.push(section("Ledger"));
    out.push(pnlBlock(s.portfolio.pnl));
    return h("div", null, out);
  }

  /* ---------- Risk */
  function risk(s) {
    var r = s.risk, out = [coherenceBanner(s)], b = stateBox(r);
    out.push(section("Needs attention"));
    out.push(attention(s.overview.attention));
    out.push(section("Halt flags"));
    out.push(table([{ label: "Source", render: function (x) { return x.source; } }, { label: "State", render: function (x) { return pill(x.halted ? "FAIL" : "INFO", x.halted ? "HALTED / CLOSED" : "not halted"); } }, { label: "Why", render: function (x) { return x.why; } }], r.halt_flags));
    out.push(section("Risk state"));
    if (b) { out.push(b); } else {
      out.push(staleNote(r));
      out.push(h("div", { "class": "banner s-" + (r.halted ? "FAIL" : "INFO") }, h("strong", null, r.halted ? "Trading is HALTED. " : "Not halted. "), r.halt_reasons.join("; ")));
      out.push(r.limits.length ? table([
        { label: "Limit", render: function (x) { return x.name; } }, { label: "Limit value", render: function (x) { return V(x.limit); } }, { label: "Current", render: function (x) { return V(x.value); } },
        { label: "Status", render: function (x) { return pill(x.status === "OK" ? "OK" : x.status === "BREACH" ? "FAIL" : "NO_DATA", x.status); } }], r.limits) : empty("The risk file lists no limits."));
      out.push(r.alerts.length ? table([{ label: "Severity", render: function (x) { return pill(x.severity === "CRITICAL" ? "FAIL" : x.severity === "WARN" ? "ATTENTION" : "INFO", x.severity); } },
        { label: "Code", render: function (x) { return x.code; } }, { label: "Detail", render: function (x) { return x.detail; } }], r.alerts) : empty("No alerts in the risk file."));
    }
    out.push(section("Kill switch, stress, exposure and recovery"));
    out.push(kv([["Kill switch", V(r.kill_switch)], ["Stress result", V(r.stress)], ["Gross exposure", V(r.exposure.gross)], ["Net exposure", V(r.exposure.net)],
      ["Recovery status", r.recovery.state, r.recovery.why]]));
    out.push(section("Refusals"));
    out.push(r.refusals.outcome ? h("p", null, "The last pipeline outcome is " + r.refusals.outcome + ".") : null);
    out.push(r.refusals.without_policy.length ? list(r.refusals.without_policy) : empty("No policy refusal is listed."));
    out.push(section("Risk policy"));
    out.push(h("p", null, r.policy.text, " ", badge(r.policy.basis)));
    out.push(kv([["Proposed max hold", V(r.policy.proposed_max_hold_days)], ["Proposed drawdown limit", V(r.policy.proposed_drawdown_bps_of_notional)]]));
    if (r.policy.unmeasured_limits.length) { out.push(h("details", null, h("summary", null, "Limits that cannot yet be measured"), list(r.policy.unmeasured_limits))); }
    return h("div", null, out);
  }

  /* ---------- Testnet */
  function testnet(s) {
    var t = s.testnet, out = [coherenceBanner(s)];
    out.push(h("div", { "class": "banner s-ATTENTION", role: "note" }, h("strong", null, t.banner)));
    out.push(kv([["Venue", t.state === "OK" ? V(t.venue) : "Testnet (no session file)"], ["Session state", pill(t.pill, t.session_state)], ["Offline qualification", V(t.offline_qualification)]]));
    var b = stateBox(t);
    if (b) { out.push(b); return h("div", null, out); }
    out.push(staleNote(t));
    out.push(t.identity_status === "LEGACY_NO_IDENTITY" ? h("div", { "class": "banner s-ATTENTION" }, "UNVERIFIED: this session file carries no run identity.") : null);
    (t.flags || []).forEach(function (f) { out.push(h("div", { "class": "errbox", role: "alert" }, f)); });
    out.push(section("Venue and credentials"));
    out.push(kv([["Endpoint host", mono(t.endpoint_host)], ["Dedicated key present", V(t.credentials.dedicated_key_present)], ["Dedicated secret present", V(t.credentials.dedicated_secret_present)],
      ["Generic credentials ignored", V(t.credentials.generic_credentials_ignored)]]));
    out.push(h("p", { "class": "muted small" }, t.credentials.note));
    out.push(section("Read-only preflight"));
    out.push(t.preflight.status === "NOT_RUN" ? empty("NOT_RUN: the preflight has not been run. This is not a pass.") : h("p", null, "Result: ", pill(t.preflight.status === "PASS" ? "OK" : "FAIL", t.preflight.status), t.preflight.checked_ms ? " checked " + utc(t.preflight.checked_ms) : ""));
    out.push(t.preflight.checks.length ? table([{ label: "Check", render: function (c) { return c.name; } }, { label: "Result", render: function (c) { return pill(c.ok ? "OK" : "FAIL", c.ok ? "ok" : "failed"); } }, { label: "Detail", render: function (c) { return c.detail || "—"; } }], t.preflight.checks) : null);
    out.push(section("Plan and confirmation"));
    if (!t.plan) { out.push(empty("No plan has been made.")); } else {
      out.push(kv([["Plan digest", h("span", { "class": "mono" }, V(t.plan.digest))], ["Manifest digest", h("span", { "class": "mono" }, V(t.plan.manifest_digest))], ["Planned notional", V(t.plan.notional)]]));
      out.push(table([{ label: "Symbol", render: function (l) { return l.symbol; } }, { label: "Side", render: function (l) { return l.side; } }, { label: "Order type", render: function (l) { return l.order_type; } },
        { label: "Qty", render: function (l) { return V(l.qty); } }, { label: "Price", render: function (l) { return V(l.price); } }, { label: "Notional", render: function (l) { return V(l.notional); } }], t.plan.legs));
    }
    out.push(kv([["Confirmation", pill(t.confirmation.state === "CONFIRMED" ? "INFO" : "NO_DATA", t.confirmation.state)], ["Digest confirmed", t.confirmation.plan_digest_confirmed ? mono(t.confirmation.plan_digest_confirmed) : "none"],
      ["Matches the plan", t.confirmation.matches_plan ? "yes" : "no"]]));
    out.push(section("Orders, executions and position"));
    out.push(kv([["Orders", String(t.counts.orders)], ["Acknowledged", String(t.counts.acked)], ["Filled", String(t.counts.filled)], ["Partially filled", String(t.counts.partial)], ["Cancelled", String(t.counts.cancelled)], ["Rejected", String(t.counts.rejected)]]));
    out.push(ordersTable(t.orders));
    out.push(t.executions.length ? table([{ label: "Exec id", render: function (x) { return mono(x.exec_id); } }, { label: "Link id", render: function (x) { return x.link_id ? mono(x.link_id) : "—"; } }, { label: "Symbol", render: function (x) { return x.symbol; } },
      { label: "Side", render: function (x) { return x.side; } }, { label: "Qty", render: function (x) { return V(x.qty); } }, { label: "Price", render: function (x) { return V(x.price); } }, { label: "Fee", render: function (x) { return V(x.fee); } },
      { label: "Time", render: function (x) { return utc(x.ts_ms); } }], t.executions) : empty("No execution is listed."));
    var fpKeys = t.final_position ? Object.keys(t.final_position) : [];
    out.push(t.final_position === null ? empty("No final position was read.") : (fpKeys.length ? table([{ label: "Symbol", render: function (k) { return k; } }, { label: "Quantity", render: function (k) { return V(t.final_position[k]); } }], fpKeys) : empty("The final position read lists no open symbol.")));
    out.push(kv([["Flat verified by the venue's position read", t.flat.verified_flat ? pill("INFO", "flat, verified") : pill(t.flat.declared === null ? "NO_DATA" : "ATTENTION", t.flat.declared === null ? "not verified" : "NOT shown as flat"),
      t.flat.nonzero.length ? "Not flat: " + t.flat.nonzero.join(", ") : "Flat is never inferred from fill arithmetic."]]));
    out.push(section("Testnet result components"));
    out.push(kv([["Fees", V(t.fees)], ["Funding", V(t.funding)], ["Price P&L", V(t.price_pnl)]], null));
    out.push(t.net_note ? h("p", { "class": "muted small" }, t.net_note) : null);
    out.push(section("Session evidence"));
    out.push(kv([["File", mono(t.evidence.file)], ["Declared basis", badge(t.evidence.basis === "NOT_RUN" ? "MISSING" : t.evidence.basis), t.evidence.basis], ["Plan digest", t.evidence.plan_digest ? mono(t.evidence.plan_digest) : "none"],
      ["Manifest digest", t.evidence.manifest_digest ? mono(t.evidence.manifest_digest) : "none"]]));
    return h("div", null, out);
  }

  /* ---------- Evidence and operations */
  function evidence(s) {
    var ev = s.evidence, out = [coherenceBanner(s)], f = ev.freshness;
    out.push(section("Data coverage and market data freshness"));
    if (f.state === "ERROR") { out.push(errbox(f.error)); } else if (f.state === "NO_DATA") { out.push(empty(f.empty || "No data captured yet. Run tools/market_collector.py.")); } else {
      out.push(kv([["Data freshness", pill(f.verdict === "HEALTHY" ? "OK" : f.verdict === "PARTIAL" ? "ATTENTION" : "FAIL", f.verdict)], ["Research readiness", pill(f.readiness === "READY" ? "OK" : "NO_DATA", f.readiness)],
        ["Code correctness", f.code_correctness.replace("_", " "), "not assessed here"], ["Datasets re-hashed this run", f.verified_this_run ? "Yes" : "No", f.verified_this_run ? "" : "timestamps are labelled UNVERIFIED"]]));
      if (f.latest_cycle) { out.push(h("p", null, "Newest complete collection cycle: ", V(f.latest_cycle.age_ms), " ago")); }
      out.push(f.datasets.length ? table([{ label: "Dataset", render: function (x) { return x.dataset; } }, { label: "Status", render: function (x) { return x.status; } }, { label: "Symbols", render: function (x) { return String(x.n_symbols); } },
        { label: "Stale", render: function (x) { return String(x.stale_symbols); } }, { label: "Incomplete", render: function (x) { return String(x.incomplete_symbols); } }, { label: "Newest row age", render: function (x) { return V(x.age_ms); } }], f.datasets) : null);
      out.push(f.issues.length ? h("details", null, h("summary", null, "Audit issues (" + f.issues.length + ")"), list(f.issues.map(function (i) { return i.level + " " + i.code + ": " + i.detail; }))) : null);
    }
    out.push(section("Capture manifest and digests"));
    var c = ev.universe_capture, cb = stateBox(c);
    if (cb) { out.push(cb); } else {
      out.push(kv([["Integrity", V(c.integrity)], ["Dataset status", c.dataset_status], ["Survivorship", c.survivorship, c.survivorship_note], ["Manifest sha256", V(c.manifest_sha256)]]));
    }
    out.push(kv([["Run data digest", ev.digests.run_data_digest ? mono(ev.digests.run_data_digest) : "no identified run"]]));
    out.push(h("h3", null, "Data digest parts (pipeline)"));
    out.push(mapTable(ev.digests.data_digest_parts, "Part", "Digest", "No digest parts on file."));
    out.push(section("Backups, restores and seals"));
    var op = ev.operations;
    out.push(op ? table([{ label: "Record", render: function (k) { return k; } }, { label: "Status", render: function (k) { return op[k] === null ? pill("NO_DATA", "none recorded") : pill("INFO", "recorded"); } }], ["backup", "restore_check", "seal"])
      : empty("No backup, restore-check or seal status: the data root is not audited. A missing record is not a successful backup."));
    out.push(section("Software revision"));
    out.push(ev.software.code_revisions.length ? list(ev.software.code_revisions.map(function (r) { return mono(r); })) : empty("No artifact declares a code revision (no run identity)."));
    out.push(h("p", { "class": "muted small" }, ev.software.note));
    out.push(section("CI status"));
    out.push(empty(ev.ci.text + " (" + ev.ci.state + ")"));
    out.push(section("Artifact files"));
    out.push(table([{ label: "File", render: function (x) { return h("span", { "class": "mono" }, x.file); } }, { label: "State", render: function (x) { return pill(x.state === "OK" ? "INFO" : x.state === "ERROR" ? "FAIL" : "NO_DATA", x.state === "OK" ? "LOADED" : x.state); } },
      { label: "Identity", render: function (x) { return x.state !== "OK" ? "—" : (x.identity_status === "IDENTIFIED" ? h("span", null, x.mode + " ", mono(x.run_id)) : pill("ATTENTION", "UNVERIFIED (no identity)")); } },
      { label: "Declared basis", render: function (x) { return x.basis ? badge(x.basis === "NOT_RUN" ? "MISSING" : x.basis) : "—"; } }, { label: "Age", render: function (x) { return x.state === "OK" ? ageText(x.age_ms) + (x.stale ? " (stale)" : "") : "—"; } },
      { label: "Problem", render: function (x) { return x.error || "—"; } }], ev.sources));
    out.push(section("Owner console read-out"));
    var oc = ev.owner_console;
    out.push(kv([["Prospective evidence", oc.evidence.status, oc.evidence.why], ["Backup", oc.backup.status, oc.backup.why], ["Owner account P&L", oc.owner_pnl.status, oc.owner_pnl.why]]));
    out.push(section("How to read this"));
    out.push(list(ev.caveats));
    return h("div", null, out);
  }

  var RENDER = { overview: overview, research: research, portfolio: portfolio, execution: execution, accounting: accounting, risk: risk, testnet: testnet, evidence: evidence };

  function currentView() { var m = /^#\/(\w+)/.exec(location.hash || ""); return m && VIEWS.indexOf(m[1]) >= 0 ? m[1] : "overview"; }
  function clock(d) { return d.toTimeString().slice(0, 8); }
  /* The status line: when the state was last fetched, and a warning when it is old, when a poll failed or when a source file is stale. */
  function renderStatus() {
    var up = document.getElementById("updated"), st = document.getElementById("stale"), msgs = [];
    if (LOADED_AT) {
      up.textContent = "updated " + clock(new Date(LOADED_AT)) + " (this browser's clock)";
      if (Date.now() - LOADED_AT > STALE_FETCH_MS) { msgs.push("The view is " + Math.round((Date.now() - LOADED_AT) / 1000) + " s old: the backend has not answered."); }
    } else { up.textContent = ERR ? "never updated" : "loading"; }
    if (ERR && LOADED_AT) { msgs.push("The last refresh failed; showing the earlier state."); }
    if (STATE) {
      var old = STATE.evidence.sources.filter(function (x) { return x.state === "OK" && x.stale; }).map(function (x) { return x.file; });
      if (old.length) { msgs.push("Stale source file(s): " + old.join(", ") + "."); }
    }
    st.textContent = msgs.join(" ");
    st.className = msgs.length ? "pill s-ATTENTION" : "hidden";
  }
  function render() {
    var v = currentView();
    document.title = TITLES[v] + " - Owner control centre";
    Array.prototype.forEach.call(document.querySelectorAll(".rail a"), function (a) { if (a.getAttribute("data-view") === v) { a.setAttribute("aria-current", "page"); } else { a.removeAttribute("aria-current"); } });
    while (viewEl.firstChild) { viewEl.removeChild(viewEl.firstChild); }
    if (STATE) {
      var hl = document.getElementById("headline");
      hl.className = "pill s-" + STATE.overview.headline.status; hl.textContent = STATE.overview.headline.status.replace("_", " ");
      document.getElementById("asof").textContent = "state as of " + utc(STATE.as_of_ms);
      var lg = document.getElementById("legend");
      if (!lg.firstChild) {
        var ld = document.getElementById("legend-def");
        STATE.legend.basis.forEach(function (b) { lg.appendChild(h("li", null, badge(b.code))); ld.appendChild(h("dt", null, badge(b.code))); ld.appendChild(h("dd", null, b.meaning)); });
      }
      viewEl.appendChild(h("h2", { id: "view-h", tabindex: "-1" }, TITLES[v]));
      try { viewEl.appendChild(RENDER[v](STATE)); } catch (e) { viewEl.appendChild(errbox("This view could not be drawn: " + e.message)); }
    } else if (ERR) {
      viewEl.appendChild(h("h2", { id: "view-h", tabindex: "-1" }, TITLES[v]));
      viewEl.appendChild(errbox(ERR + " The backend is unreachable or returned an error; nothing is shown rather than a guess."));
    } else {
      viewEl.appendChild(h("h2", { id: "view-h", tabindex: "-1" }, TITLES[v]));
      viewEl.appendChild(empty("Loading state from the local backend."));
    }
    renderStatus();
  }
  function load() {
    fetch("/api/state", { cache: "no-store", credentials: "same-origin" }).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    }).then(function (j) { STATE = j; ERR = null; LOADED_AT = Date.now(); render(); }).catch(function (e) { ERR = "Could not load /api/state (" + e.message + ")."; render(); });
  }
  window.addEventListener("hashchange", function () { render(); var hd = document.getElementById("view-h"); if (hd) { hd.focus(); } });
  document.getElementById("refresh").addEventListener("click", load);
  render();
  load();
  window.setInterval(load, POLL_MS);
  window.setInterval(renderStatus, 10000);
})();
