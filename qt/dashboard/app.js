"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "-" : Number(v).toFixed(d);
const money = (v) => (v === null || v === undefined || isNaN(v)) ? "-" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
const sgn = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const pct = (v) => (v === null || v === undefined || isNaN(v)) ? "-" : (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%";
let current = "overview";

async function api(path, opts = {}) {
  const r = await fetch(path, {credentials: "same-origin", ...opts,
    headers: {"Content-Type": "application/json", "X-Requested-With": "quant-trader", ...(opts.headers || {})}});
  if (r.status === 401) { showLogin(); throw new Error("auth"); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${body.detail || r.statusText || "request failed"} (HTTP ${r.status} on ${path})`);
  return body;
}
function showLogin() { $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); }
function showApp() {
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();  // closes the phone keyboard
  window.scrollTo(0, 0);  // the login form may have left the page scrolled past the top
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", {method: "POST", body: JSON.stringify({username: $("#u").value, password: $("#p").value})});
    $("#p").value = ""; showApp(); refresh();
  } catch (err) {
    $("#loginErr").textContent = err.message === "auth" ? "Wrong username or password (after 8 tries, wait 15 minutes)" : err.message;
  }
});

document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  b.classList.add("active"); current = b.dataset.tab; $("#" + current).classList.add("active"); refresh();
}));

function kpi(label, value, cls = "") { return `<div class="kpi"><div class="l">${esc(label)}</div><div class="v ${cls}">${value}</div></div>`; }
function table(cols, rows, onclick) {
  const h = cols.map((c) => `<th>${esc(c[0])}</th>`).join("");
  const b = rows.map((r, i) => `<tr ${onclick ? `class="click" data-i="${i}"` : ""}>${cols.map((c) => `<td data-label="${esc(c[0])}">${c[1](r)}</td>`).join("")}</tr>`).join("");
  return `<div class="tw"><table><thead><tr>${h}</tr></thead><tbody>${b || `<tr><td colspan="${cols.length}" class="muted">No data yet</td></tr>`}</tbody></table></div>`;
}
function bindRows(el, rows, fn) { el.querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => fn(rows[+tr.dataset.i]))); }
function modal(html) { $("#modalBody").innerHTML = html + `<div class="row" style="margin-top:12px"><button class="btn" id="mclose">Close</button></div>`;
  $("#modal").classList.remove("hidden"); $("#mclose").onclick = () => $("#modal").classList.add("hidden"); }

function chart(points) {
  if (!points || points.length < 2) return `<p class="muted">Equity curve appears after the engine records snapshots.</p>`;
  const W = 800, H = 220, P = 30, ys = points.map((p) => p.equity);
  const mn = Math.min(...ys), mx = Math.max(...ys), rng = (mx - mn) || 1;
  const xy = points.map((p, i) => [P + (i / (points.length - 1)) * (W - 2 * P), H - P - ((p.equity - mn) / rng) * (H - 2 * P)]);
  const d = xy.map((q, i) => (i ? "L" : "M") + q[0].toFixed(1) + "," + q[1].toFixed(1)).join(" ");
  const col = ys[ys.length - 1] >= ys[0] ? "var(--pos)" : "var(--neg)";
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <text x="4" y="${P}" fill="#8b98a8" font-size="11">${money(mx)}</text><text x="4" y="${H - P + 12}" fill="#8b98a8" font-size="11">${money(mn)}</text>
    <path d="${d}" fill="none" stroke="${col}" stroke-width="2"/></svg>`;
}

async function renderOverview() {
  const o = await api("/api/overview");
  $("#mode").textContent = o.mode.toUpperCase(); $("#dataLabel").textContent = o.data_label; $("#now").textContent = o.now_pt;
  const s = o.schedule_pt || {};
  $("#overview").innerHTML = `<div class="grid">
    ${kpi("Starting capital", money(o.starting_capital))}${kpi("Current equity", money(o.equity), sgn(o.equity - o.starting_capital))}
    ${kpi("Available cash", money(o.cash))}${kpi("Settled funds", money(o.settled))}${kpi("Unsettled (T+1)", money(o.unsettled))}
    ${kpi("Buying power", money(o.buying_power))}${kpi("Realized P&L", money(o.realized), sgn(o.realized))}
    ${kpi("Unrealized P&L", money(o.unrealized), sgn(o.unrealized))}${kpi("Today", money(o.today), sgn(o.today))}
    ${kpi("This week", money(o.week), sgn(o.week))}${kpi("This month", money(o.month), sgn(o.month))}
    ${kpi("Total return", pct(o.total_return_pct), sgn(o.total_return_pct))}${kpi("Drawdown", pct(-(o.drawdown_pct || 0)), (o.drawdown_pct > 0 ? "neg" : ""))}
    ${kpi("Peak equity", money(o.peak_equity))}${kpi("Fees paid", money(o.fees))}</div>
    <div class="card"><h2>Account equity</h2>${chart(o.equity_curve)}</div>
    <div class="card"><h2>Today's schedule (Pacific Time)</h2>${s.trading_day === false ? `<p class="muted">Market closed today.</p>` :
      `<div class="kv"><div>Premarket scan</div><div>${esc(s.premarket_research)}</div><div>Open</div><div>${esc(s.open)}</div>
       <div>Entry cutoff</div><div>${esc(s.entry_cutoff)}</div><div>Flatten intraday</div><div>${esc(s.flatten)}</div>
       <div>Close</div><div>${esc(s.close)}${s.early_close ? " (early close)" : ""}</div><div>Daily report</div><div>${esc(s.daily_report)}</div>
       <div>Research</div><div>${esc(s.research)}</div></div>`}</div>`;
}

async function renderScanner() {
  const d = await api("/api/scanner");
  const st = d.stage1 || {}, sm = d.summary || {};
  const rows = d.opportunities;
  const mk = d.market || {}, up = d.upcoming || {}, cr = d.crypto || {};
  $("#scanner").innerHTML = `<div class="grid">${kpi("US stocks", esc(mk.us_stocks || "-"), mk.us_stocks === "open" ? "pos" : "")}
    ${kpi(mk.us_stocks === "open" ? "Closes" : "Next open", esc((mk.us_stocks === "open" ? mk.closes_pt : mk.next_open_pt) || "-"))}
    ${kpi("Crypto", esc(mk.crypto || "-"), "pos")}${kpi("Crypto pairs evaluated", cr.evaluated ?? "-")}</div>
    <div class="grid">${kpi("Assets scanned (stage 1)", st.scanned ?? "-")}${kpi("Evaluated (stage 2)", sm.evaluated ?? "-")}
    ${kpi("Signals", sm.signals ?? "-")}${kpi("Executed", sm.executed ?? "-")}</div>
    <div class="card"><h2>Why setups were not traded (last analysis)</h2>${table([["Reason", (r) => esc(r[0])], ["Count", (r) => r[1]]],
      d.rejection_reasons || [])}</div>
    <div class="card"><h2>Upcoming swing opportunities${up.next_session ? " for " + esc(up.next_session) : ""}</h2>
      <p class="muted small">Daily signals on the latest closed bar; they are entered at the next open if they still qualify then. As of ${esc(up.as_of || "-")}.</p>
      ${table([["Symbol", (r) => esc(r.symbol)], ["Strategy", (r) => esc(r.strategy)], ["Status", (r) => esc(r.status)],
      ["Close", (r) => num(r.close)], ["Stop", (r) => num(r.stop)], ["Real paper order", (r) => r.real_paper_order ? "yes" : "book only"]],
      up.signals || [])}</div>
    <div class="card"><h2>Crypto (24/7, hourly bars)</h2><p class="muted small">${esc(cr.pairs ?? "-")} pairs listed, ${esc(cr.scanned ?? "-")} with data,
      ${esc(cr.evaluated ?? "-")} liquid enough to evaluate. Last bar ${esc(cr.bar || "-")}. Crypto runs in its strategies' simulated books only.</p>
      ${table([["Symbol", (r) => esc(r.symbol)], ["Strategy", (r) => esc(r.strategy)], ["Price", (r) => num(r.price)],
      ["Stop", (r) => num(r.stop)], ["Spread", (r) => num(r.spread_bps, 1) + "bps"], ["Taken", (r) => r.taken ? "yes" : "no (cost/spread)"]],
      cr.signals || [])}</div>
    <div class="card"><h2>Watchlist</h2><p>${(sm.watchlist || []).map(esc).join(", ") || "-"}</p><p class="muted small">${esc(d.note)} Last analysis: ${esc(sm.ts || "-")}</p></div>
    <div class="card"><h2>Opportunities</h2>${table([
      ["Status", (r) => `<span class="tag ${esc(r.status)}">${esc(r.status)}</span>`], ["Symbol", (r) => esc(r.symbol)],
      ["Strategy", (r) => esc(r.strategy)], ["Price", (r) => num(r.price)], ["Chg", (r) => `<span class="${sgn(r.change_pct)}">${pct(r.change_pct)}</span>`],
      ["RelVol", (r) => num(r.relvol)], ["Mom 30m", (r) => pct(r.momentum)], ["Regime", (r) => esc(r.regime)], ["Score", (r) => num(r.score, 1)],
      ["Est. cost", (r) => num(r.est_cost_bps, 1) + "bps"], ["Reason", (r) => `<span class="small">${esc(r.reason).slice(0, 120)}</span>`]], rows, true)}</div>
    <div class="card"><h2>Stage 1 top movers</h2>${table([["#", (r) => r.rank], ["Symbol", (r) => esc(r.symbol)], ["Price", (r) => num(r.price)],
      ["Chg", (r) => pct(r.change_pct)], ["Gap", (r) => pct(r.gap_pct)], ["RelVol (IEX)", (r) => num(r.relvol_iex)],
      ["IEX spread", (r) => num(r.spread_bps_iex, 1) + "bps"], ["Score", (r) => num(r.stage1_score)]], st.top || [])}</div>`;
  bindRows($("#scanner"), rows, (r) => modal(`<h2>${esc(r.symbol)} / ${esc(r.strategy)}</h2><pre>${esc(JSON.stringify(r.detail, null, 1))}</pre>`));
}

async function renderBooks() {
  const d = await api("/api/books");
  const rows = (d.strategies || []).slice().sort((a, b) => (b.return_pct - a.return_pct) || (b.closed_trades - a.closed_trades));
  const eng = (e) => ({day: "Day trading", swing: "Swing", crypto: "Crypto 24/7", options: "Options (simulated)"}[e] || e);
  const sum = (e) => rows.filter((r) => r.engine === e);
  const tot = (e) => sum(e).reduce((a, r) => a + (r.equity - d.capital), 0);
  const pr = d.promote_rule || {}, dr = d.demote_rule || {};
  $("#books").innerHTML = `<div class="grid">${["day", "swing", "crypto", "options"].map((e) => kpi(eng(e) + " books P&L", money(tot(e)), sgn(tot(e)))).join("")}
    ${kpi("Open simulated trades", (d.open || []).length)}</div>
    <div class="card"><h2>Strategy portfolios ($${num(d.capital, 0)} each)</h2><p class="muted small">${esc(d.note)} Updated ${esc(d.as_of || "-")}.</p>
    ${table([["Strategy", (r) => esc(r.strategy)], ["Engine", (r) => esc(eng(r.engine))],
      ["Equity", (r) => `${money(r.equity)} <span class="${sgn(r.return_pct)}">(${pct(r.return_pct)})</span>`],
      ["Trades", (r) => r.closed_trades + (r.open_trades ? ` (+${r.open_trades} open)` : "") + (r.mean_r === null ? "" : `, avg ${num(r.mean_r)}R`)],
      ["Status", (r) => `<span class="tag">${esc(r.forward)}</span> ${r.real_paper_orders ? "real paper orders" : "book only"}`]], rows, true)}
    <p class="muted small">Tap a strategy for win rate, profit factor, drawdown and research results.</p>
    <p class="muted small">Promotion needs at least ${esc(pr.min_trades)} forward trades, Holm-adjusted p at most ${esc(pr.max_holm_p)}, profit factor at least
      ${esc(pr.min_pf)}, average R at least ${esc(pr.min_mean_r)} and a positive return. Demotion after ${esc(dr.min_trades)} trades if average R is at or below
      ${esc(dr.max_mean_r)} or drawdown reaches ${esc(dr.max_drawdown_pct)}%.</p></div>
    <div class="card"><h2>Open simulated trades</h2>${table([["Strategy", (r) => esc(r.strategy)], ["Symbol", (r) => esc(r.symbol)],
      ["Entry", (r) => num(r.entry_price)], ["Now", (r) => num(r.current_price)], ["Stop", (r) => num(r.stop)],
      ["P&L", (r) => `<span class="${sgn(r.pnl_now)}">${money(r.pnl_now)}</span>`], ["Since", (r) => esc((r.entry_time || "").slice(0, 16))]], d.open || [])}</div>
    <div class="card"><h2>Options book (simulated only)</h2><p class="muted small">Buys one call on each swing signal the bot trades,
      if a contract with 21-60 days and delta 0.3-0.7 fits half the book. Buys at the ask, sells at the bid; stop at half the premium.
      Never sends an order. Last check: ${esc((d.options && d.options.last && d.options.last.at) || "-")}
      ${((d.options && d.options.last && d.options.last.log) || []).map((x) => esc(x.symbol + ": " + x.result)).join("; ")}</p>
      ${table([["Contract", (r) => esc(r.contract)], ["Signal", (r) => esc(r.signal_strategy)], ["Status", (r) => esc(r.status)],
      ["Paid", (r) => money(r.entry_premium * 100)], ["Bid now / exit", (r) => money((r.status === "open" ? r.last_bid : r.exit_premium) * 100)],
      ["Stop", (r) => money(r.stop_premium * 100)], ["P&L", (r) => `<span class="${sgn(r.pnl)}">${money(r.pnl)}</span>`],
      ["Exit", (r) => esc(r.exit_reason || "-")]], (d.options && d.options.trades) || [])}</div>
    <div class="card"><h2>Recent closed simulated trades</h2>${table([["Strategy", (r) => esc(r.strategy)], ["Symbol", (r) => esc(r.symbol)],
      ["P&L", (r) => `<span class="${sgn(r.pnl)}">${money(r.pnl)}</span>`], ["R", (r) => num(r.r)], ["Exit", (r) => esc(r.exit_reason)],
      ["Closed", (r) => esc((r.exit_time || "").slice(0, 16))]], d.closed || [])}</div>`;
  bindRows($("#books"), rows, (r) => modal(`<h2>${esc(r.strategy)}</h2><p>${esc(r.description)}</p><p class="small">${esc(r.forward_why || "")}</p>
    <div class="kv"><div>Engine</div><div>${esc(eng(r.engine))}</div><div>Equity</div><div>${money(r.equity)} (${pct(r.return_pct)})</div>
    <div>Win rate</div><div>${r.win_rate === null ? "-" : num(r.win_rate, 0) + "%"}</div><div>Average R</div><div>${num(r.mean_r)}</div>
    <div>Profit factor</div><div>${r.profit_factor === 99 ? "no losses yet" : num(r.profit_factor)}</div><div>Max drawdown</div><div>${pct(-r.max_dd_pct)}</div>
    <div>Forward test</div><div>${esc(r.forward)}</div><div>Research status</div><div>${esc(r.research_status)}</div>
    <div>Research holdout avg R</div><div>${num(r.holdout_mean_r)}</div><div>Holdout trades</div><div>${esc(r.holdout_trades ?? "-")}</div>
    <div>Fees paid</div><div>${money(r.fees)}</div><div>p-value (unadjusted)</div><div>${num(r.p_value, 3)}</div><div>Book started</div><div>${esc(r.since || "-")}</div></div>`));
}

async function renderPositions() {
  const d = await api("/api/positions");
  $("#positions").innerHTML = `<div class="card"><h2>Live positions</h2><p class="muted small">Prices: ${esc(d.price_source)} as of ${esc(d.as_of || "-")}</p>
  ${table([["Asset", (r) => esc(r.symbol)], ["Status", (r) => esc(r.status)], ["Entry", (r) => num(r.entry_price)], ["Current", (r) => num(r.current_price)],
    ["Size", (r) => num(r.qty, 6) + ` (${money(r.notional)})`], ["P&L", (r) => `<span class="${sgn(r.pnl_now)}">${money(r.pnl_now)}</span>`],
    ["Stop", (r) => num(r.stop_price)], ["Trail", (r) => num(r.trail_stop)], ["Target", (r) => num(r.target)],
    ["Held", (r) => num(r.holding_minutes, 0) + "m"], ["Strategy", (r) => esc(r.strategy)]], d.positions, true)}</div>`;
  bindRows($("#positions"), d.positions, (r) => modal(`<h2>${esc(r.symbol)}</h2><div class="kv"><div>Thesis</div><div>${esc(r.thesis)}</div>
    <div>Exit conditions</div><div>${esc(r.exit_plan)}</div></div><h3>Recorded decision data</h3><pre>${esc(JSON.stringify(r.explanation, null, 1))}</pre>`));
}

async function renderGuardian() {
  const d = await api("/api/guardian"), g = d.status || {}, L = d.limits || {};
  $("#guardian").innerHTML = `<div class="grid">${kpi("Status", g.can_open_new ? "Trading allowed" : "Entries blocked", g.can_open_new ? "pos" : "neg")}
    ${kpi("Starting capital", money(g.starting_capital))}${kpi("Current equity", money(g.equity))}${kpi("Peak equity", money(g.peak_equity))}
    ${kpi("Protected floor", g.principal_protection ? money(g.protected_floor) : "off")}${kpi("Risk per trade now", money(g.risk_capacity_per_trade))}
    ${kpi("Daily budget left", money(g.daily_budget_left))}${kpi("Weekly budget left", money(g.weekly_budget_left))}
    ${kpi("Drawdown", pct(-(g.drawdown_pct || 0)))}${kpi("Drawdown budget left", money(g.drawdown_budget_left))}
    ${kpi("Profit preservation", esc(g.profit_preservation || "-"))}</div>
    <div class="card"><h2>Blocking reasons</h2>${(g.blocking_reasons || []).length ? "<ul>" + g.blocking_reasons.map((x) => `<li>${esc(x)}</li>`).join("") + "</ul>" : `<p class="pos">None</p>`}</div>
    <div class="card"><h2>Active limits</h2><div class="kv">${Object.entries(L).map(([k, v]) => `<div>${esc(k)}</div><div>${esc(v)} <span class="muted small">(ceiling ${esc(d.ceiling[k])})</span></div>`).join("")}</div>
    <p class="muted small">${esc(d.principal_note)}</p></div>`;
}

async function renderTournament() {
  const d = await api("/api/tournament");
  const rows = d.strategies;
  $("#tournament").innerHTML = `<div class="card"><h2>Strategy Tournament</h2><p class="muted small">Champion = best validated strategy by out-of-sample expectancy. Last research: ${esc(d.last_run?.at || "-")}</p>
  ${table([["", (r) => r.champion ? "🏆" : ""], ["Strategy", (r) => esc(r.strategy)], ["Status", (r) => `<span class="tag ${esc(r.status)}">${esc(r.status)}</span>`],
    ["Approved", (r) => r.approved ? "yes" : "no"], ["OOS trades", (r) => r.oos_metrics?.trades ?? "-"], ["OOS exp (R)", (r) => num(r.oos_metrics?.expectancy_r, 3)],
    ["OOS PF", (r) => num(r.oos_metrics?.profit_factor)], ["Win rate", (r) => pct((r.oos_metrics?.win_rate || 0) * 100)],
    ["Holdout PF", (r) => num(r.holdout_metrics?.profit_factor)], ["Holdout exp (R)", (r) => num(r.holdout_metrics?.expectancy_r, 3)],
    ["Reason", (r) => `<span class="small">${esc(r.reason).slice(0, 140)}</span>`]], rows, true)}</div>
  <div class="card"><h2>Forward results</h2><h3>Paper (executed)</h3>${table([["Strategy", (r) => esc(r.strategy)], ["Trades", (r) => r.n], ["P&L", (r) => money(r.pnl)], ["Avg R", (r) => num(r.avg_r, 3)]], d.paper_forward)}
  <h3>Shadow (signals tracked, no orders)</h3>${table([["Strategy", (r) => esc(r.strategy)], ["Trades", (r) => r.n], ["Avg R", (r) => num(r.avg_r, 3)]], d.shadow_forward)}</div>`;
  bindRows($("#tournament"), rows, (r) => modal(`<h2>${esc(r.strategy)}</h2>${r.status === "validated" && !r.approved ?
    `<div class="row"><button class="btn primary" id="appr">Approve for paper trading</button></div>` : ""}
    <h3>Out-of-sample</h3><pre>${esc(JSON.stringify(r.oos_metrics, null, 1))}</pre><h3>Holdout</h3><pre>${esc(JSON.stringify(r.holdout_metrics, null, 1))}</pre>
    <h3>Version history</h3><pre>${esc(JSON.stringify(r.history, null, 1))}</pre>`) || bindApprove(r));
}
function bindApprove(r) { const b = $("#appr"); if (b) b.onclick = () => sendCmd("approve_strategy", {strategy: r.strategy}, `APPROVE ${r.strategy}`); }

async function renderResearch() {
  const d = await api("/api/research");
  $("#research").innerHTML = `<div class="grid">${kpi("Experiments", d.experiments.length)}${kpi("Validated", d.experiments.filter((e) => e.status === "validated").length)}
    ${kpi("Shadow", d.experiments.filter((e) => e.status === "shadow").length)}${kpi("Failed", d.experiments.filter((e) => e.status === "rejected").length)}
    ${kpi("Pending approvals", d.pending_approvals.length)}</div>
    <div class="card"><h2>Experiment registry (every experiment, including failures)</h2>${table([["Run", (r) => esc(r.run_id)], ["Strategy", (r) => esc(r.strategy)],
      ["Version", (r) => esc(r.version)], ["Test period", (r) => esc(r.test_period)], ["Status", (r) => `<span class="tag ${esc(r.status)}">${esc(r.status)}</span>`],
      ["Decision", (r) => `<span class="small">${esc(r.decision).slice(0, 160)}</span>`]], d.experiments, true)}</div>
    <div class="card"><h2>Research runs</h2>${table([["When", (r) => esc(r.created_at)], ["Holdout", (r) => esc(r.period)],
      ["Download", (r) => `<a href="/api/download/report/${r.id}" class="small">JSON</a>`]], d.runs)}</div>`;
  bindRows($("#research"), d.experiments, async (r) => { const x = await api("/api/experiment/" + r.id); modal(`<h2>${esc(x.strategy)}</h2><pre>${esc(JSON.stringify(x.results, null, 1))}</pre>`); });
}

async function renderTrades() {
  const d = await api("/api/trades");
  $("#trades").innerHTML = `<div class="card"><div class="row" style="justify-content:space-between"><h2>Trade history</h2><a class="btn" href="/api/download/trades.csv">Download CSV</a></div>
  ${table([["Entry", (r) => esc(r.entry_time || "-").slice(0, 19)], ["Exit", (r) => esc(r.exit_time || "-").slice(0, 19)], ["Asset", (r) => esc(r.symbol)],
    ["Strategy", (r) => esc(r.strategy)], ["Entry px", (r) => num(r.entry_price)], ["Exit px", (r) => num(r.exit_price)], ["Size", (r) => money(r.notional)],
    ["Costs", (r) => money(r.fees)], ["Net P&L", (r) => `<span class="${sgn(r.pnl)}">${money(r.pnl)}</span>`], ["R", (r) => num(r.r_multiple)],
    ["Exit reason", (r) => esc(r.exit_reason)]], d.trades, true)}</div>`;
  bindRows($("#trades"), d.trades, (r) => modal(`<h2>${esc(r.symbol)} ${esc(r.strategy)}</h2><div class="kv"><div>Entry reason</div><div>${esc(r.entry_reason)}</div>
    <div>Exit reason</div><div>${esc(r.exit_reason)}</div><div>MFE / MAE (R)</div><div>${num(r.mfe)} / ${num(r.mae)}</div></div><pre>${esc(JSON.stringify(r.explanation, null, 1))}</pre>`));
}

async function renderReports() {
  const d = await api("/api/reports");
  $("#reports").innerHTML = `<div class="card"><h2>Reports</h2>${table([["Kind", (r) => esc(r.kind)], ["Period", (r) => esc(r.period)], ["Created", (r) => esc(r.created_at).slice(0, 19)],
    ["Download", (r) => `<a href="/api/download/report/${r.id}?fmt=csv">CSV</a> · <a href="/api/download/report/${r.id}">JSON</a>`]], d.reports, true)}</div>`;
  bindRows($("#reports"), d.reports, async (r) => { const x = await api("/api/report/" + r.id); modal(`<h2>${esc(x.kind)} ${esc(x.period)}</h2><pre>${esc(JSON.stringify(x.body, null, 1))}</pre>`); });
}

async function renderSystem() {
  const d = await api("/api/system");
  const eng = (d.heartbeats || []).find((h) => h.component === "engine");
  const age = eng ? (Date.now() - Date.parse(eng.ts)) / 1000 : null;
  const ok = eng && age < 120 && eng.status === "ok";
  $("#botStatus").className = "dot " + (ok ? "ok" : "bad");
  const integ = (d.live && d.live.integrity) || {};
  $("#system").innerHTML = `<div class="grid">${kpi("Bot", ok ? "Online" : "Offline", ok ? "pos" : "neg")}${kpi("Last heartbeat", age === null ? "-" : num(age, 0) + "s ago")}
    ${kpi("Trading mode", esc(d.mode.toUpperCase()))}${kpi("Broker failures", d.live?.broker_failures ?? "-")}${kpi("Errors (24h)", d.errors_24h, d.errors_24h ? "neg" : "")}
    ${kpi("Pending orders", d.pending_orders.length)}${kpi("Avg ack latency", num(d.latency?.ack, 0) + "ms")}${kpi("Avg slippage", num(d.latency?.slip, 1) + "bps")}</div>
    <div class="card"><h2>Integrity</h2>${Object.keys(integ).length ? "<ul>" + Object.values(integ).map((x) => `<li class="neg">${esc(x)}</li>`).join("") + "</ul>" : `<p class="pos">All checks passing</p>`}
    <div class="kv"><div>Live data</div><div>${esc(d.data_feeds.live)}</div><div>Research data</div><div>${esc(d.data_feeds.research)}</div></div></div>
    <div class="card"><h2>Events</h2>${table([["Time", (r) => esc(r.ts).slice(0, 19)], ["Level", (r) => esc(r.level)], ["Kind", (r) => esc(r.kind)], ["Message", (r) => esc(r.message)]], d.events)}</div>`;
}

function renderControl() {
  $("#control").innerHTML = `<div class="card"><h2>Trading</h2><div class="row">
    <button class="btn primary" data-c="start_paper">Start paper trading</button><button class="btn" data-c="pause">Pause new entries</button>
    <button class="btn" data-c="resume">Resume trading</button><button class="btn" data-c="cancel_pending">Cancel pending orders</button>
    <button class="btn" data-c="run_backtest">Run research / backtests</button></div></div>
  <div class="card"><h2>Risk settings</h2><p class="muted small">You can tighten limits here. Loosening beyond the approved ceiling needs a server config change.</p>
    <div class="row"><label>Risk per trade %<input id="rpt" type="number" step="0.05" min="0.05" max="0.5" value="0.5"></label>
    <label>Max positions<input id="mp" type="number" min="1" max="2" value="2"></label>
    <label>Principal floor $<input id="pf" type="number" step="1" value="95"></label>
    <label><input id="ppm" type="checkbox"> Principal protection</label>
    <label><input id="pgb" type="checkbox" checked> Profit giveback guard</label>
    <button class="btn" id="saveRisk">Save (type CHANGE RISK)</button></div></div>
  <div class="card"><h2>Emergency</h2><p class="muted small">Emergency shutdown stops new entries; open positions stay managed with their stops. Closing all positions is a separate action.</p>
    <div class="row"><button class="btn danger" data-c="emergency_shutdown" data-k="EMERGENCY">Emergency shutdown</button>
    <button class="btn" data-c="clear_emergency">Clear emergency</button><button class="btn danger" data-c="close_all" data-k="CLOSE ALL">Close all positions</button>
    <button class="btn" data-c="reset_drawdown_halt" data-k="RESET">Reset drawdown halt</button></div>
    <p class="muted small">Live (real-money) trading cannot be enabled from this dashboard.</p></div><p id="cmdMsg"></p>`;
  document.querySelectorAll("#control [data-c]").forEach((b) => b.addEventListener("click", () => sendCmd(b.dataset.c, {}, b.dataset.k)));
  $("#saveRisk").onclick = () => sendCmd("set_risk_limits", {risk_per_trade_pct: +$("#rpt").value, max_positions: +$("#mp").value,
    principal_floor: +$("#pf").value, principal_protection_enabled: $("#ppm").checked, profit_giveback_enabled: $("#pgb").checked}, "CHANGE RISK");
}

async function sendCmd(command, args, phrase) {
  let confirm = null;
  if (phrase) { confirm = window.prompt(`Type "${phrase}" to confirm ${command}`); if (confirm === null) return; }
  try { const r = await api("/api/command", {method: "POST", body: JSON.stringify({command, args, confirm})});
    const m = $("#cmdMsg"); if (m) { m.className = "pos"; m.textContent = `${command}: ${r.note || r.queued || "ok"}`; }
  } catch (e) { const m = $("#cmdMsg"); if (m) { m.className = "neg"; m.textContent = e.message; } else alert(e.message); }
}

const renderers = {overview: renderOverview, books: renderBooks, scanner: renderScanner, positions: renderPositions, guardian: renderGuardian,
  tournament: renderTournament, research: renderResearch, trades: renderTrades, reports: renderReports, system: renderSystem, control: renderControl};
// A tab that fails to load shows the reason instead of a blank page, and keeps retrying every 15 seconds.
const reported = new Set();
function reportProblem(tab, message) {  // tell the server, so a broken dashboard shows up in the health report
  const key = tab + ":" + message; if (reported.has(key)) return; reported.add(key);
  fetch("/api/client_error", {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Requested-With": "quant-trader"},
    body: JSON.stringify({tab, message: String(message).slice(0, 400)})}).catch(() => {});
}
function showTabError(tab, e) {
  const el = $("#" + tab); if (!el) return;
  reportProblem(tab, e && e.message ? e.message : e);
  el.innerHTML = `<div class="card"><h2>This section could not load</h2><p class="neg">${esc(e && e.message ? e.message : e)}</p>
    <p class="muted small">Retrying automatically every 15 seconds. Other tabs may still work.</p>
    <div class="row"><button class="btn" id="retryTab">Retry now</button></div></div>`;
  const b = $("#retryTab"); if (b) b.onclick = () => refresh();
}
async function refresh() {
  const tab = current;
  try {
    await renderers[tab](); if (tab !== "system") renderSystemDot();
    const el = $("#" + tab);  // rendered but nothing visible = the "blank screen" case: report it
    if (tab === current && el && !$("#app").classList.contains("hidden") && el.getBoundingClientRect().height < 40) reportProblem(tab, "tab rendered blank");
  }
  catch (e) { if (e.message !== "auth") { console.warn(e); showTabError(tab, e); } }
}
window.addEventListener("error", (ev) => { if (!$("#app").classList.contains("hidden")) showTabError(current, ev.error || ev.message); });
window.addEventListener("unhandledrejection", (ev) => { if (!$("#app").classList.contains("hidden") && ev.reason?.message !== "auth") showTabError(current, ev.reason); });
async function renderSystemDot() { try { const h = await fetch("/api/health"); $("#botStatus").className = "dot " + (h.ok ? "ok" : "bad"); } catch (e) {} }
setInterval(() => { if (!$("#app").classList.contains("hidden") && current !== "control" && $("#modal").classList.contains("hidden")) refresh(); }, 15000);
// signed in already -> open the app; a server error (not a sign-in problem) opens the app with the error shown
api("/api/overview").then(() => { showApp(); refresh(); })
  .catch((e) => { if (e.message === "auth") return; showApp(); showTabError(current, e); });
