/* Lumen Cloud: Outbound Email Gate.
 *
 * Presentation only. Every decision shown here comes from the wrapper's POST /decide, and every human review goes
 * through POST /review. The page never decides, never calls the Ezio mock, and never runs send_email itself.
 */
"use strict";
(() => {
  const PERSONA = "customer_support";
  const MAX_CHARS = 20000;
  const SLOW_MS = 8000;
  const DECIDE_TIMEOUT_MS = 75000; // harness allows 60 s per decision; the wrapper's Ezio budget is ~45 s
  const SIM_CONCURRENCY = 3;

  const DECISIONS = {
    APPROVE: { icon: "✓", word: "APPROVED", tone: "ok", short: "Approved" },
    HOLD_FOR_REVIEW: { icon: "⏸", word: "HELD FOR REVIEW", tone: "hold", short: "Held" },
    BLOCK: { icon: "✕", word: "BLOCKED", tone: "block", short: "Blocked" },
  };
  const VERDICTS = {
    verified: { icon: "✓", word: "Verified", tone: "ok", rank: 1 },
    supported: { icon: "✓", word: "Supported", tone: "ok", rank: 1 },
    review: { icon: "⚠", word: "Needs review", tone: "hold", rank: 2 },
    unverifiable: { icon: "?", word: "Unverifiable", tone: "hold", rank: 2 },
    contradicted: { icon: "✕", word: "Contradicted", tone: "block", rank: 3 },
  };
  const CHECKS = {
    pass: { icon: "✓", word: "Pass", tone: "ok" },
    warn: { icon: "⚠", word: "Warning", tone: "hold" },
    fail: { icon: "✕", word: "Fail", tone: "block" },
    skip: { icon: "–", word: "Skipped", tone: "muted" },
  };
  const TIERS = { GREEN: "●", YELLOW: "▲", RED: "■" };
  const KIND_LABELS = {
    price_monthly: "Monthly price",
    price_annual: "Annual-billing price",
    price_annual_per_month: "Annual-billing price (per month)",
    price_annual_total: "Annual-billing price (per year)",
    price: "Price",
    discount_loyalty_pct: "Loyalty discount",
    discount_annual_pct: "Annual-billing discount",
    discount_pct: "Discount",
    discount_eligibility: "Discount eligibility",
    loyalty_tenure: "Loyalty tenure requirement",
    stacking: "Discount stacking",
    refund_window: "Refund window",
    refund: "Refund policy",
    trial_days: "Free trial length",
    trial: "Free trial",
    trial_card: "Card required for trial",
    response_hours: "First-response time",
    enterprise_pricing: "Enterprise pricing",
    commitment: "Commitment / promise",
    exception: "Policy exception",
    tone: "Tone / hype",
    flattery: "Flattery / agreement",
    ezio_claim: "Public-knowledge claim (Ezio)",
  };

  // ---------------------------------------------------------------- helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "dataset") Object.assign(node.dataset, v);
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const str = (v) => (v == null ? "" : String(v));
  const arr = (v) => (Array.isArray(v) ? v : []);
  const fmtTime = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? str(iso) : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch { /* storage unavailable: fine */ } },
  };

  function pill(meta, text) {
    const m = meta || { icon: "•", word: "Unknown", tone: "muted" };
    return el("span", { class: `pill ${m.tone}` }, el("span", { "aria-hidden": "true" }, m.icon), text || m.word);
  }
  const decisionMeta = (d) => DECISIONS[d] || { icon: "?", word: str(d) || "UNKNOWN", tone: "muted", short: str(d) || "Unknown" };
  const verdictMeta = (v) => VERDICTS[v] || VERDICTS.review;
  const actionRan = (r) => {
    if (r && r.action && typeof r.action.executed === "boolean") return r.action.executed;
    return !!(r && r.action_executed === true);
  };

  // ---------------------------------------------------------------- API (wrapper endpoints only)
  class ApiError extends Error {
    constructor(message, status, body) { super(message); this.status = status; this.body = body; }
  }
  async function api(path, { method = "GET", body, timeout = 15000 } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    let res;
    let data = null;
    try {
      res = await fetch(path, {
        method,
        headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
        cache: "no-store",
      });
      try { data = await res.json(); } catch { data = null; }
    } catch (e) {
      if (e && e.name === "AbortError") throw new ApiError(`No answer from the gate after ${Math.round(timeout / 1000)} seconds.`, 0);
      throw new ApiError("Can't reach the gate. Is `python3 gate.py serve --persona customer_support` still running?", 0);
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      const msg = (data && (typeof data.error === "string" ? data.error : data.error && data.error.message)) || `Request failed (HTTP ${res.status}).`;
      throw new ApiError(msg, res.status, data);
    }
    return data;
  }
  const decide = (text, context) =>
    api("/decide", { method: "POST", body: { text, persona: PERSONA, context }, timeout: DECIDE_TIMEOUT_MS });

  // ---------------------------------------------------------------- status pill
  function setStatus(kind, text) {
    const pillEl = $("#status");
    pillEl.className = `status-pill status-${kind}`;
    $(".status-text", pillEl).textContent = text;
  }
  async function pollStatus() {
    try {
      const s = await api("/api/status", { timeout: 6000 });
      if (s && s.ezio === "ok") setStatus("ok", "Ezio fact-check: online");
      else setStatus("down", "Ezio fact-check: offline. Our own checks still run; drafts they can't clear are held");
    } catch (e) {
      if (e.status === 404) {
        try { await api("/health", { timeout: 4000 }); setStatus("unknown", "Gate online · Ezio status not reported"); }
        catch { setStatus("down", "Gate unreachable"); }
      } else {
        setStatus("down", "Gate unreachable. Start gate.py serve");
      }
    }
  }

  // ---------------------------------------------------------------- tabs
  const loaded = { sim: false, policy: false };
  function selectTab(tab, focus) {
    $$('[role="tab"]').forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) tab.focus();
    if (tab.id === "tab-sim" && !loaded.sim) loadSamples();
    if (tab.id === "tab-audit") refreshAudit();
    if (tab.id === "tab-policy" && !loaded.policy) loadPolicy();
  }
  function setupTabs() {
    const tabs = $$('[role="tab"]');
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => selectTab(t));
      t.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        e.preventDefault();
        const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        selectTab(next, true);
      });
    });
    document.addEventListener("click", (e) => {
      const go = e.target.closest("[data-goto]");
      if (go) selectTab(document.getElementById(go.dataset.goto), true);
    });
  }

  // ---------------------------------------------------------------- compose
  let busy = false;
  let lastText = "";

  function readContext() {
    const ctx = {};
    const plan = $("#plan").value;
    if (plan) ctx.plan = plan;
    const t = $("#tenure").value.trim();
    if (t !== "") {
      const n = Number(t);
      if (Number.isFinite(n) && n >= 0) ctx.customer_tenure_months = Math.round(n);
    }
    return ctx;
  }
  function writeContext(ctx) {
    const c = ctx || {};
    const plan = str(c.plan);
    $("#plan").value = ["Starter", "Pro", "Business", "Enterprise"].includes(plan) ? plan : "";
    $("#tenure").value = isNum(c.customer_tenure_months) ? String(c.customer_tenure_months) : "";
  }
  function updateCounter() {
    const value = $("#draft").value;
    const len = value.length;
    const over = len > MAX_CHARS;
    const empty = value.trim().length === 0;
    const counter = $("#counter");
    counter.textContent = `${len.toLocaleString()} / ${MAX_CHARS.toLocaleString()}`;
    counter.classList.toggle("over", over);
    const hint = $("#draft-hint");
    hint.classList.toggle("warn", over);
    hint.textContent = empty
      ? "Write or paste the agent's draft to check it."
      : over
        ? `Too long by ${(len - MAX_CHARS).toLocaleString()} characters. The gate accepts at most 20,000; trim the draft first.`
        : "Ready. The gate checks prices, discounts, refunds, trials, response times, promises and tone.";
    $("#check").disabled = busy || over || empty;
    return !(over || empty);
  }
  function setBusy(on) {
    busy = on;
    updateCounter();
    $("#clear").disabled = on;
    $("#check").textContent = on ? "Checking…" : "Check & send";
  }
  function showError(err, where) {
    const box = where || $("#error");
    const status = err && err.status;
    const hint = status === 400
      ? "The gate rejected the request. Check that the draft is not empty and under 20,000 characters."
      : status >= 500
        ? "The gate hit an internal error. Nothing was sent. Check the terminal running gate.py serve."
        : status === 0
          ? "Nothing was sent."
          : "";
    box.replaceChildren(el("div", null, "⚠ ", str(err && err.message) || "Something went wrong."), hint ? el("div", { class: "small" }, hint) : null);
    box.hidden = false;
  }
  function clearError() { const b = $("#error"); b.hidden = true; b.replaceChildren(); }

  function busyView() {
    const secs = el("span", { class: "score" }, "0");
    const slow = el("p", { class: "slow-note", hidden: true },
      "Ezio is slow. Still waiting: the gate retries automatically, for up to about 45 seconds.");
    const view = el("div", { class: "busy", role: "status" },
      el("div", { class: "spinner", "aria-hidden": "true" }),
      el("p", null, el("strong", null, "Checking the draft"), " · ", secs, " s"),
      el("p", { class: "muted small" }, "Calling Ezio and checking the price sheet and policies…"),
      slow);
    const started = Date.now();
    const timer = setInterval(() => {
      const ms = Date.now() - started;
      secs.textContent = String(Math.floor(ms / 1000));
      if (ms >= SLOW_MS) slow.hidden = false;
    }, 250);
    return { view, stop: () => clearInterval(timer) };
  }

  async function runCheck() {
    if (busy || !updateCounter()) return;
    const text = $("#draft").value;
    const context = readContext();
    clearError();
    setBusy(true);
    const out = $("#result");
    const b = busyView();
    out.replaceChildren(b.view);
    const started = performance.now();
    try {
      const result = await decide(text, context);
      lastText = text;
      renderResult(text, context, result, performance.now() - started);
      out.focus({ preventScroll: true });
      if (window.matchMedia("(max-width: 900px)").matches) out.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      out.replaceChildren(el("div", { class: "empty-state" },
        el("div", { class: "empty-icon", "aria-hidden": "true" }, "⛔"),
        el("p", null, el("strong", null, "No decision. The email was not sent.")),
        el("p", { class: "muted" }, "Fix the problem above and check again.")));
      showError(err);
    } finally {
      b.stop();
      setBusy(false);
    }
  }

  // ---------------------------------------------------------------- result rendering
  function signalsOf(r) {
    if (r && r.ezio_signals && typeof r.ezio_signals === "object") return r.ezio_signals;
    // Display-only fallback built from the raw mock response the wrapper returned (no decision logic here).
    const e = (r && r.ezio) || {};
    if (!e.verdict || !e.dimensions) return { status: "down", error: str(e.error) || "Ezio response missing" };
    const a = e.dimensions.accuracy || {};
    const dim = (k) => (e.dimensions[k] || {}).score;
    return {
      status: a.status === "unavailable" ? "degraded" : "ok", tier: e.verdict.tier, score: e.verdict.score, label: e.verdict.label,
      summary: e.verdict.summary, accuracy_status: a.status, claims_total: a.claims_total, claims_verified: a.claims_verified,
      claims_contradicted: a.claims_contradicted, tone: dim("tone"), bias: dim("bias"), agreement: dim("agreement"),
      claims: arr(e.claims), error: null, thresholds: null,
    };
  }

  function renderResult(text, context, r, ms) {
    const result = r || {};
    const sig = signalsOf(result);
    const meta = decisionMeta(result.decision);
    const nodes = [
      banner(result, meta, ms),
      actionRow(result),
      degradedNotice(sig),
      consequenceBox(result, meta),
      result.decision === "HOLD_FOR_REVIEW" ? reviewPanel(result) : null,
      result.decision === "BLOCK" ? blockPanel(result, text) : null,
      section("Draft, with every checked claim marked", highlightedDraft(text, result.claims)),
      section("Evidence: what the email says vs. our source of truth", evidenceTable(result)),
      arr(result.checks).length ? section("Checks the gate ran", checksList(result.checks)) : null,
      section("Ezio signals", ezioCard(sig)),
      evidenceLog(result),
      rawDetails(result),
    ];
    $("#result").replaceChildren(...nodes.filter(Boolean));
  }

  function banner(r, meta, ms) {
    const elapsed = isNum(r.elapsed_ms) ? r.elapsed_ms : ms;
    return el("div", { class: `banner tone-${meta.tone}` },
      el("div", { class: "banner-icon", "aria-hidden": "true" }, meta.icon),
      el("div", { class: "banner-word" }, meta.word),
      el("div", { class: "banner-headline" }, str(r.headline) || str(r.reason) || "No reason returned."),
      r.headline && r.reason && r.reason !== r.headline ? el("div", { class: "banner-reason" }, r.reason) : null,
      el("div", { class: "banner-meta" },
        isNum(elapsed) ? `Decided in ${(elapsed / 1000).toFixed(1)} s` : "",
        r.decision_id ? ` · Decision ${r.decision_id}` : ""));
  }

  function actionRow(r) {
    const ran = actionRan(r);
    const a = r.action || {};
    const to = str(a.to) || "customer@example.com";
    if (ran) {
      return el("div", { class: "action-row sent", role: "status" },
        el("span", { class: "ai", "aria-hidden": "true" }, "✉"),
        el("span", null, `Email SENT to ${to}`),
        el("span", { class: "small" }, `send_email ran · subject "${str(a.subject) || "Re: your question"}"`));
    }
    const why = r.decision === "HOLD_FOR_REVIEW" ? "waiting for your review below" : "stopped before send_email ran";
    return el("div", { class: "action-row notsent", role: "status" },
      el("span", { class: "ai", "aria-hidden": "true" }, "⛔"),
      el("span", null, "Email NOT sent"),
      el("span", { class: "small" }, `${why}; nothing reached ${to}`));
  }

  function degradedNotice(sig) {
    if (!sig || sig.status === "ok") return null;
    const down = sig.status === "down";
    return el("div", { class: "notice degraded", role: "note" },
      el("h3", null, down ? "⚠ Ezio didn't answer" : "⚠ Ezio answered, but its fact-check was unavailable"),
      el("p", null, down
        ? `The gate couldn't get a usable response from Ezio${sig.error ? ` (${sig.error})` : ""}. Our own price-sheet and policy checks still ran: a wrong fact is still blocked, and anything they can't fully clear is held for a person instead of being sent.`
        : "Ezio couldn't check public-knowledge claims (laws, regulations). Our price-sheet and policy checks still ran; anything they can't fully clear is held for a person."));
  }

  function consequenceBox(r, meta) {
    if (!r.consequence) return null;
    const title = r.decision === "APPROVE" ? "What happened" : "If this email were sent";
    return el("div", { class: `notice consequence c-${meta.tone}` },
      el("h3", null, title), el("p", null, r.consequence));
  }

  function section(title, body) {
    return el("div", { class: "section" }, el("h3", null, title), body);
  }

  // Highlight claim spans. Overlaps are resolved per character segment by the most severe verdict.
  function highlightedDraft(text, claims) {
    const t = str(text);
    const valid = arr(claims).filter((c) => c && Array.isArray(c.span) && c.span.length === 2
      && Number.isInteger(c.span[0]) && Number.isInteger(c.span[1]) && c.span[0] >= 0 && c.span[1] <= t.length && c.span[1] > c.span[0]);
    const box = el("div", { class: "draft-view" });
    if (!valid.length) {
      box.append(t);
    } else {
      const cuts = new Set([0, t.length]);
      valid.forEach((c) => { cuts.add(c.span[0]); cuts.add(c.span[1]); });
      const points = Array.from(cuts).sort((a, b) => a - b);
      const segs = [];
      for (let i = 0; i < points.length - 1; i++) {
        const [a, b] = [points[i], points[i + 1]];
        let best = null;
        for (const c of valid) {
          if (c.span[0] <= a && c.span[1] >= b && (!best || verdictMeta(c.verdict).rank > verdictMeta(best.verdict).rank)) best = c;
        }
        const prev = segs[segs.length - 1];
        if (prev && prev.claim === best) prev.end = b;
        else segs.push({ start: a, end: b, claim: best });
      }
      for (const s of segs) {
        const piece = t.slice(s.start, s.end);
        if (!s.claim) { box.append(piece); continue; }
        const v = verdictMeta(s.claim.verdict);
        const tip = claimTip(s.claim);
        box.append(el("mark", { class: `hl hl-${v.tone}`, tabindex: "0", title: tip, dataset: { tip }, "aria-label": `${v.word}: “${piece}”. ${tip}` },
          piece, el("span", { class: "hl-icon", "aria-hidden": "true" }, v.icon)));
      }
    }
    const legend = el("div", { class: "legend", "aria-hidden": "true" },
      el("span", null, pill(VERDICTS.verified)), el("span", null, pill(VERDICTS.review)), el("span", null, pill(VERDICTS.contradicted)),
      el("span", null, "Hover or tab to a marked phrase for details."));
    return el("div", null, box, valid.length ? legend : el("div", { class: "legend" }, "No individual claims were marked in this draft."));
  }
  function claimTip(c) {
    const parts = [];
    const label = str(c.label) || KIND_LABELS[c.kind] || str(c.kind).replace(/_/g, " ");
    if (label) parts.push(label + (c.plan ? ` (${c.plan})` : ""));
    if (c.message) parts.push(str(c.message));
    else if (c.expected != null && c.verdict === "contradicted") parts.push(`Source of truth: ${c.expected}`);
    if (c.source) parts.push(`Source: ${c.source}`);
    return parts.join(" · ");
  }

  function evidenceTable(r) {
    const claims = arr(r.claims).slice().sort((a, b) => verdictMeta(b.verdict).rank - verdictMeta(a.verdict).rank);
    if (!claims.length) {
      return el("p", { class: "muted" },
        "No prices, discounts, refunds, trials, response times or commitments were found to check. ",
        "An email with no checkable claims is fine to send when nothing else is wrong (business rule 5).");
    }
    const rows = claims.map((c) => {
      const v = verdictMeta(c.verdict);
      const label = str(c.label) || KIND_LABELS[c.kind] || str(c.kind).replace(/_/g, " ") || "Claim";
      return el("tr", { class: `row-${v.tone}` },
        el("td", { "data-label": "Claim" }, label, c.plan ? el("span", { class: "sub" }, `Plan: ${c.plan}`) : null),
        el("td", { "data-label": "What the email says" }, c.stated != null && c.stated !== "" ? str(c.stated) : `“${str(c.text)}”`,
          c.stated != null && c.text ? el("span", { class: "sub" }, `“${str(c.text)}”`) : null),
        el("td", { "data-label": "Source of truth" }, c.expected != null && c.expected !== "" ? str(c.expected) : el("span", { class: "muted" }, "—"),
          c.source ? el("span", { class: "sub mono" }, str(c.source)) : null),
        el("td", { "data-label": "Verdict" }, pill(v), c.message ? el("span", { class: "sub" }, str(c.message)) : null));
    });
    return el("div", { class: "table-wrap" },
      el("table", { class: "data stack" },
        el("thead", null, el("tr", null, el("th", { scope: "col" }, "Claim"), el("th", { scope: "col" }, "What the email says"),
          el("th", { scope: "col" }, "Source of truth"), el("th", { scope: "col" }, "Verdict"))),
        el("tbody", null, rows)));
  }

  function checksList(checks) {
    return el("ul", { class: "checks" }, arr(checks).map((c) => {
      const m = CHECKS[c && c.status] || CHECKS.skip;
      return el("li", null, pill(m), el("div", null, el("span", { class: "cname" }, str(c && c.name) || "Check"),
        c && c.detail ? el("span", { class: "cdetail" }, str(c.detail)) : null));
    }));
  }

  function ezioCard(sig) {
    const s = sig || {};
    const card = el("div", { class: "ezio" });
    const head = el("div", { class: "ezio-head" }, el("h3", null, "Ezio mock analysis"));
    if (s.tier) head.append(el("span", { class: `tier tier-${s.tier}` }, el("span", { "aria-hidden": "true" }, TIERS[s.tier] || "•"), s.tier));
    if (isNum(s.score)) head.append(el("span", { class: "score" }, `${s.score} / 100`));
    if (s.label) head.append(el("span", { class: "muted small" }, s.label));
    card.append(head);
    if (s.status === "down" || (!s.tier && !isNum(s.score))) {
      card.append(el("p", { class: "muted" }, "No Ezio signals for this draft", s.error ? ` (${s.error})` : "", ". The decision rests on our own checks."));
      return card;
    }
    if (s.summary) card.append(el("p", { class: "small" }, `“${s.summary}”`));
    const acc = s.accuracy_status;
    const accText = acc === "scored"
      ? `Scored: ${isNum(s.claims_verified) ? s.claims_verified : "?"} of ${isNum(s.claims_total) ? s.claims_total : "?"} claims supported, ${isNum(s.claims_contradicted) ? s.claims_contradicted : 0} contradicted.`
      : acc === "not_scored"
        ? "Not scored: Ezio found no public-knowledge claims to check. A GREEN tier here is not proof the prices or policies are right; that's what our source-of-truth check is for."
        : acc === "unavailable"
          ? "Unavailable: Ezio's fact-check service was down for this draft."
          : "Not reported.";
    card.append(el("p", { class: "small" }, el("strong", null, "Accuracy: "), accText));

    const th = s.thresholds || {};
    const dims = el("div", { class: "dims" });
    for (const [key, name] of [["tone", "Tone"], ["bias", "Bias"], ["agreement", "Agreement"]]) {
      const v = s[key];
      const limit = isNum(th[key]) ? th[key] : null;
      const low = isNum(v) && limit != null && v < limit;
      const bar = el("div", { class: "bar", role: "img", "aria-label": `${name} ${isNum(v) ? v : "unknown"} out of 100${limit != null ? `, hold threshold ${limit}` : ""}` },
        el("div", { class: `bar-fill${low ? " low" : ""}`, style: `width:${isNum(v) ? Math.max(0, Math.min(100, v)) : 0}%` }),
        limit != null ? el("div", { class: "bar-tick", style: `left:${limit}%`, dataset: { label: `hold < ${limit}` } }) : null);
      dims.append(el("div", { class: "dim" },
        el("span", { class: "dim-name" }, name), bar,
        el("span", { class: `dim-val${low ? " low" : ""}` }, isNum(v) ? String(v) : "—", low ? " ⚠ low" : "")));
    }
    card.append(dims);

    const claims = arr(s.claims);
    if (claims.length) {
      card.append(el("strong", { class: "small" }, "Claims Ezio fact-checked"));
      card.append(el("ul", { class: "ezio-claims" }, claims.map((c) => el("li", null,
        pill(VERDICTS[c && c.verdict] || VERDICTS.unverifiable),
        el("span", null, str(c && c.text)),
        c && c.source && c.source.publisher ? el("span", { class: "muted small", title: str(c.source.url) }, `· ${c.source.publisher}`) : null))));
    }
    return card;
  }

  function evidenceLog(r) {
    const ev = arr(r.evidence);
    if (!ev.length) return null;
    return el("details", { class: "evlog section" },
      el("summary", null, `Evidence log (${ev.length} line${ev.length === 1 ? "" : "s"}, as recorded in the audit log)`),
      el("ul", null, ev.map((line) => el("li", null, typeof line === "string" ? line : JSON.stringify(line)))));
  }

  function rawDetails(r) {
    let json;
    try { json = JSON.stringify(r, null, 2); } catch { json = String(r); }
    return el("details", { class: "raw" }, el("summary", null, "Raw wrapper response (POST /decide)"), el("pre", null, json));
  }

  // ---------------------------------------------------------------- review (HOLD) and block panels
  function reviewPanel(r) {
    const questions = arr(r.review_questions).filter(Boolean);
    const qs = questions.length ? questions : ["Have you read the draft and the flagged items above, and do you accept the risk of sending it?"];
    const status = el("div", { class: "small", "aria-live": "polite" });
    const name = el("input", { type: "text", id: "rv-name", autocomplete: "name", value: store.get("gate.reviewer") || "" });
    const note = el("textarea", { id: "rv-note", rows: "3", required: true, placeholder: "Why are you approving or rejecting? This is logged with the decision." });
    const boxes = qs.map((q, i) => el("input", { type: "checkbox", id: `rv-q${i}` }));
    const approve = el("button", { type: "button", class: "btn btn-ok" }, "✉ Approve & send");
    const reject = el("button", { type: "button", class: "btn btn-block" }, "✕ Reject");
    const refresh = () => {
      const noted = note.value.trim().length > 0;
      const named = name.value.trim().length > 0;
      approve.disabled = !(noted && named && boxes.every((b) => b.checked)) || !r.decision_id;
      reject.disabled = !(noted && named) || !r.decision_id;
    };
    [note, name, ...boxes].forEach((n) => n.addEventListener("input", refresh));
    boxes.forEach((b) => b.addEventListener("change", refresh));

    const panel = el("section", { class: "review-panel", "aria-labelledby": "rv-title" },
      el("h3", { id: "rv-title" }, "⏸ Your review. This draft has not been sent."),
      el("p", { class: "small" }, "Confirm each question before approving. Your name and reason are written to the audit trail."),
      el("ul", { class: "questions" }, qs.map((q, i) => el("li", null, el("label", { for: `rv-q${i}` }, boxes[i], el("span", null, q))))),
      el("div", { class: "field-row" },
        el("div", { class: "field" }, el("label", { for: "rv-name" }, "Reviewer"), name)),
      el("div", { class: "field" }, el("label", { for: "rv-note" }, "Reason (required)"), note),
      el("div", { class: "actions" }, approve, reject),
      status);
    if (!r.decision_id) status.textContent = "This response has no decision id, so it can't be reviewed here. Edit and re-check the draft instead.";

    async function submit(verdict) {
      store.set("gate.reviewer", name.value.trim());
      approve.disabled = reject.disabled = true;
      status.replaceChildren(el("span", { class: "spinner sm", "aria-hidden": "true" }), " Recording your review…");
      try {
        const res = await api("/review", { method: "POST", body: { decision_id: r.decision_id, verdict, reviewer: name.value.trim(), note: note.value.trim() } });
        const rv = (res && res.review) || {};
        const sent = rv.action_executed === true;
        panel.replaceChildren(
          el("h3", null, verdict === "approve" ? "✓ Approved by reviewer" : "✕ Rejected by reviewer"),
          el("div", { class: "review-outcome", role: "status" },
            sent ? `✉ Email SENT to customer@example.com after human approval by ${str(rv.reviewer) || name.value.trim()}.`
              : `⛔ Email NOT sent. Rejected by ${str(rv.reviewer) || name.value.trim()}.`),
          el("p", { class: "small" }, `Reason logged: “${str(rv.note) || note.value.trim()}”`, rv.timestamp ? ` · ${fmtTime(rv.timestamp)}` : ""),
          el("button", { type: "button", class: "linklike", "data-goto": "tab-audit" }, "View in audit trail"));
        refreshAudit();
      } catch (e) {
        const msg = e.status === 404 && /endpoint/i.test(e.message)
          ? "This gate build has no review endpoint. Held drafts stay unsent."
          : e.message;
        status.replaceChildren(el("span", { class: "hl-icon", "aria-hidden": "true" }, "⚠"), " ", msg);
        refresh();
      }
    }
    approve.addEventListener("click", () => submit("approve"));
    reject.addEventListener("click", () => submit("reject"));
    refresh();
    return panel;
  }

  function blockPanel(r, text) {
    const first = arr(r.claims).find((c) => c && c.verdict === "contradicted" && Array.isArray(c.span));
    const edit = el("button", { type: "button", class: "btn" }, "✎ Edit draft and re-check");
    edit.addEventListener("click", () => {
      const ta = $("#draft");
      if (ta.value !== text) ta.value = text;
      updateCounter();
      selectTab($("#tab-compose"));
      ta.focus();
      if (first && first.span[1] <= ta.value.length) ta.setSelectionRange(first.span[0], first.span[1]);
    });
    return el("section", { class: "block-panel", "aria-label": "Blocked draft" },
      el("strong", null, "✕ This draft can't be sent as written."),
      el("p", { class: "small" }, "Blocked drafts can't be overridden here. Correct the contradicted facts (marked in red below) and check again."),
      edit);
  }

  // ---------------------------------------------------------------- simulator
  const sim = { samples: [], results: new Map(), running: false };

  async function loadSamples() {
    const list = $("#sim-list");
    list.replaceChildren(el("p", { class: "muted" }, "Loading sample drafts…"));
    try {
      const data = await api(`/api/samples?persona=${encodeURIComponent(PERSONA)}`);
      sim.samples = arr(data && data.samples);
      loaded.sim = true;
      renderSim();
    } catch (e) {
      list.replaceChildren(el("div", { class: "error", role: "alert" }, `⚠ Couldn't load sample drafts: ${e.message}`));
    }
  }

  function contextChips(ctx) {
    const c = ctx || {};
    const chips = [];
    if (c.plan) chips.push(el("span", { class: "pill muted" }, `Plan: ${c.plan}`));
    if (isNum(c.customer_tenure_months)) chips.push(el("span", { class: "pill muted" }, `Customer ${c.customer_tenure_months} mo`));
    for (const [k, v] of Object.entries(c)) {
      if (k === "plan" || k === "customer_tenure_months") continue;
      chips.push(el("span", { class: "pill muted" }, `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`));
    }
    return chips;
  }

  function simCard(s) {
    const st = sim.results.get(s.id) || {};
    const r = st.result;
    const meta = r ? decisionMeta(r.decision) : null;
    const run = el("button", { type: "button", class: "btn btn-sm btn-primary", disabled: st.pending || sim.running }, st.pending ? "Checking…" : r ? "Run again" : "Run through gate");
    run.addEventListener("click", () => runSample(s));
    const open = el("button", { type: "button", class: "btn btn-sm btn-ghost" }, "Open in composer");
    open.addEventListener("click", () => openInComposer(s, r));
    let resultNode = null;
    if (st.pending) {
      resultNode = el("div", { class: "sim-result", role: "status" }, el("span", null, el("span", { class: "spinner sm", "aria-hidden": "true" }), " Checking…"));
    } else if (st.error) {
      resultNode = el("div", { class: "sim-result" }, el("span", { class: "pill block" }, "⚠ Error"), el("span", null, st.error),
        el("span", { class: "sim-action stopped" }, "⛔ Nothing sent"));
    } else if (r) {
      const ran = actionRan(r);
      resultNode = el("div", { class: "sim-result" },
        el("div", null, pill(meta, meta.word)),
        el("span", null, str(r.headline) || str(r.reason)),
        el("span", { class: `sim-action ${ran ? "ran" : "stopped"}` }, ran ? "✉ send_email ran: email delivered" : "⛔ Never reached send_email"));
    }
    return el("article", { class: `sim-card${meta ? ` c-${meta.tone}` : ""}`, "aria-label": `Sample ${s.id}` },
      el("div", { class: "sim-meta" }, el("span", { class: "sim-id" }, s.id), contextChips(s.context)),
      el("p", { class: "sim-text" }, `“${str(s.text)}”`),
      resultNode,
      el("div", { class: "sim-buttons" }, run, open));
  }

  function renderSim() {
    const list = $("#sim-list");
    if (!sim.samples.length) {
      list.replaceChildren(el("p", { class: "muted" }, "No sample drafts available."));
    } else {
      list.replaceChildren(...sim.samples.map(simCard));
    }
    renderSimSummary();
    $("#run-all").disabled = sim.running || !sim.samples.length;
    $("#reset-sim").disabled = sim.running;
  }

  function renderSimSummary() {
    const box = $("#sim-summary");
    const done = sim.samples.map((s) => sim.results.get(s.id)).filter((x) => x && (x.result || x.error));
    if (!done.length) { box.replaceChildren(); return; }
    let sent = 0, held = 0, blocked = 0, errors = 0;
    for (const x of done) {
      if (x.error) { errors++; continue; }
      if (actionRan(x.result)) sent++;
      if (x.result.decision === "HOLD_FOR_REVIEW") held++;
      if (x.result.decision === "BLOCK") blocked++;
    }
    box.replaceChildren(el("div", { class: "sim-flow" },
      el("strong", null, `${done.length} draft${done.length === 1 ? "" : "s"} checked`),
      el("span", { class: "arrow", "aria-hidden": "true" }, "→"),
      pill(DECISIONS.APPROVE, `${sent} sent`),
      pill(DECISIONS.HOLD_FOR_REVIEW, `${held} held for review`),
      pill(DECISIONS.BLOCK, `${blocked} blocked`),
      errors ? el("span", { class: "pill block" }, `⚠ ${errors} error${errors === 1 ? "" : "s"}`) : null,
      el("span", { class: "muted small" }, `Held and blocked drafts never reached send_email (${held + blocked + errors} stopped).`)));
  }

  async function runSample(s) {
    sim.results.set(s.id, { pending: true });
    renderSim();
    try {
      const result = await decide(s.text, s.context || {});
      sim.results.set(s.id, { result });
    } catch (e) {
      sim.results.set(s.id, { error: e.message });
    }
    renderSim();
  }

  async function runAll() {
    if (sim.running) return;
    sim.running = true;
    sim.samples.forEach((s) => sim.results.set(s.id, { pending: true }));
    renderSim();
    const queue = sim.samples.slice();
    const worker = async () => {
      while (queue.length) {
        const s = queue.shift();
        try { sim.results.set(s.id, { result: await decide(s.text, s.context || {}) }); }
        catch (e) { sim.results.set(s.id, { error: e.message }); }
        renderSim();
      }
    };
    try {
      await Promise.all(Array.from({ length: Math.min(SIM_CONCURRENCY, queue.length) }, worker));
    } finally {
      sim.running = false;
      renderSim();
      refreshAudit();
    }
  }

  function openInComposer(s, result) {
    $("#draft").value = str(s.text);
    writeContext(s.context);
    updateCounter();
    selectTab($("#tab-compose"));
    clearError();
    if (result) renderResult(s.text, s.context, result, null);
    $("#draft").focus();
  }

  // ---------------------------------------------------------------- audit trail
  let auditSeq = 0;
  async function refreshAudit() {
    const body = $("#audit-body");
    const seq = ++auditSeq;
    try {
      const data = await api("/api/audit?limit=50", { timeout: 8000 });
      if (seq !== auditSeq) return;
      renderAudit(arr(data && data.entries));
    } catch (e) {
      if (seq !== auditSeq) return;
      body.replaceChildren(e.status === 404
        ? el("p", { class: "muted" }, "This gate build doesn't expose the audit trail to the page. Every decision is still written to ", el("code", null, "wrapper/audit_log.jsonl"), ".")
        : el("div", { class: "error", role: "alert" }, `⚠ Couldn't load the audit trail: ${e.message}`));
    }
  }

  function renderAudit(entries) {
    const body = $("#audit-body");
    if (!entries.length) {
      body.replaceChildren(el("p", { class: "muted" }, "No decisions recorded yet. Check a draft or run the simulator."));
      return;
    }
    const rows = entries.map((e) => {
      const meta = decisionMeta(e.decision);
      const rv = e.review;
      const reviewCell = rv
        ? el("span", null, pill(rv.verdict === "approve" ? DECISIONS.APPROVE : DECISIONS.BLOCK, rv.verdict === "approve" ? "Approved" : "Rejected"),
          el("span", { class: "sub" }, `${str(rv.reviewer) || "reviewer"}: “${str(rv.note)}”`))
        : e.decision === "HOLD_FOR_REVIEW" ? el("span", { class: "muted" }, "Awaiting review") : el("span", { class: "muted" }, "—");
      const sent = e.action_executed === true || (rv && rv.action_executed === true);
      const more = el("details", { class: "audit-more" },
        el("summary", null, str(e.headline) || str(e.reason).slice(0, 140) || "(no reason)"),
        el("div", { class: "audit-input" }, str(e.input)),
        e.context && Object.keys(e.context).length ? el("div", { class: "small muted" }, contextChips(e.context)) : null,
        e.reason ? el("p", { class: "small" }, el("strong", null, "Reason: "), str(e.reason)) : null,
        e.decision_id ? el("p", { class: "small muted mono" }, e.decision_id) : null);
      return el("tr", { class: `row-${meta.tone}` },
        el("td", { "data-label": "Time" }, fmtTime(e.timestamp)),
        el("td", { "data-label": "Decision" }, pill(meta, meta.short)),
        el("td", { "data-label": "Draft & reason" }, more),
        el("td", { "data-label": "Email" }, sent ? el("span", { class: "pill ok" }, "✉ Sent") : el("span", { class: "pill muted" }, "⛔ Not sent")),
        el("td", { "data-label": "Review" }, reviewCell));
    });
    body.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "data stack" },
      el("thead", null, el("tr", null, ["Time", "Decision", "Draft & reason", "Email", "Review"].map((h) => el("th", { scope: "col" }, h)))),
      el("tbody", null, rows))));
  }

  // ---------------------------------------------------------------- policy / source of truth
  const money = (v) => (isNum(Number(v)) && str(v) !== "" ? `$${Number(v).toFixed(Number(v) % 1 ? 2 : 0)}` : str(v));

  function priceTable(rows, compact) {
    if (!rows.length) return el("p", { class: "muted" }, "Price sheet not available.");
    return el("div", { class: "table-wrap" }, el("table", { class: `data${compact ? "" : " stack"}` },
      el("thead", null, el("tr", null, ["Plan", "Monthly", "Annual (per mo)", "First response", "Free trial"].map((h) => el("th", { scope: "col" }, h)))),
      el("tbody", null, rows.map((p) => el("tr", null,
        el("td", { "data-label": "Plan" }, el("strong", null, str(p.plan))),
        el("td", { "data-label": "Monthly" }, money(p.monthly_price_usd)),
        el("td", { "data-label": "Annual (per mo)" }, money(p.annual_price_per_month_usd)),
        el("td", { "data-label": "First response" }, `${str(p.first_response_hours)} h`),
        el("td", { "data-label": "Free trial" }, Number(p.free_trial_days) > 0 ? `${p.free_trial_days} days` : "None"))))));
  }

  function policyBoxes(pol, thresholds) {
    const p = pol || {};
    const d = p.discounts || {};
    const boxes = [];
    const li = (...k) => el("li", null, ...k);
    boxes.push(el("div", { class: "box" }, el("h3", null, "Discounts"), el("ul", null,
      d.annual_billing ? li(`Annual billing: ${d.annual_billing.percent}% off (${arr(d.annual_billing.applies_to).join(", ")})`) : null,
      d.loyalty ? li(`Loyalty: ${d.loyalty.percent}% (${arr(d.loyalty.applies_to).join(", ")}; customer for ≥ ${d.loyalty.min_tenure_months} months)`) : null,
      "stacking_allowed" in d ? li(d.stacking_allowed ? "Discounts can be combined" : "Discounts cannot be combined") : null)));
    if (p.refunds) boxes.push(el("div", { class: "box" }, el("h3", null, "Refunds"), el("ul", null,
      li(`Monthly plans: within ${p.refunds.monthly_plans_days} days`), li(`Annual plans: within ${p.refunds.annual_plans_days} days`),
      p.refunds.scope ? li(`Applies to the ${p.refunds.scope}`) : null)));
    if (p.free_trial) boxes.push(el("div", { class: "box" }, el("h3", null, "Free trial"), el("ul", null,
      li(`${p.free_trial.days} days on ${arr(p.free_trial.plans).join(", ")}`), li(p.free_trial.card_required ? "Card required" : "No card required"))));
    if (p.enterprise || p.agent_authority) boxes.push(el("div", { class: "box" }, el("h3", null, "Enterprise & agent authority"), el("ul", null,
      p.enterprise ? li(`Enterprise pricing is ${p.enterprise.pricing}; contact ${p.enterprise.contact}`) : null,
      p.agent_authority ? li(p.agent_authority.can_promise_product_features ? "Agents may promise features" : "Agents can't promise product features or dates (held for a manager)") : null,
      p.agent_authority ? li(p.agent_authority.can_offer_discounts_beyond_policy ? "Agents may offer extra discounts" : "Agents can't offer discounts beyond policy") : null)));
    if (thresholds && typeof thresholds === "object" && Object.keys(thresholds).length) {
      boxes.push(el("div", { class: "box" }, el("h3", null, "Gate thresholds"), el("ul", null,
        Object.entries(thresholds).map(([k, v]) => li(el("strong", null, k.replace(/_/g, " ")), ": ", typeof v === "object" ? JSON.stringify(v) : str(v))))));
    }
    return el("div", { class: "policy-grid" }, boxes);
  }

  async function loadPolicy() {
    const body = $("#policy-body");
    const quick = $("#quickref-body");
    try {
      const data = await api("/api/policy", { timeout: 8000 });
      loaded.policy = true;
      const rows = arr(data && data.price_sheet);
      body.replaceChildren(priceTable(rows, false), policyBoxes(data && data.policies, data && data.thresholds));
      quick.replaceChildren(priceTable(rows, true),
        el("p", { class: "small muted" }, "Full policies are on the ", el("button", { type: "button", class: "linklike", "data-goto": "tab-policy" }, "Source of truth"), " tab."));
    } catch (e) {
      const msg = e.status === 404 ? "This gate build doesn't expose the source of truth to the page; see personas/customer_support/." : `Couldn't load: ${e.message}`;
      body.replaceChildren(el("p", { class: "muted" }, msg));
      quick.replaceChildren(el("span", null, msg));
    }
  }

  // ---------------------------------------------------------------- wire up
  function init() {
    setupTabs();
    const draft = $("#draft");
    draft.addEventListener("input", updateCounter);
    draft.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runCheck(); }
    });
    $("#compose").addEventListener("submit", (e) => { e.preventDefault(); runCheck(); });
    $("#clear").addEventListener("click", () => { draft.value = ""; updateCounter(); clearError(); draft.focus(); });
    $("#run-all").addEventListener("click", runAll);
    $("#reset-sim").addEventListener("click", () => { sim.results.clear(); renderSim(); });
    $("#refresh-audit").addEventListener("click", refreshAudit);
    updateCounter();
    pollStatus();
    setInterval(pollStatus, 10000);
    loadPolicy();
  }

  // Exposed for manual inspection only; nothing here decides.
  window.__gateUI = { renderResult, highlightedDraft, signalsOf };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
