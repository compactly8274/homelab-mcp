// homelab-mcp webui — minimal vanilla-JS controller.
// Fetches /api/* endpoints, renders DOM, wires form submissions.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  view: "dashboard",
  dashboardCache: null,
  stacksCache: null,
};

function fmtNum(n) { return n === null || n === undefined ? "—" : n.toLocaleString(); }

function badge(text, kind) {
  return `<span class="badge ${kind}">${text}</span>`;
}

function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { _raw: text }; }
  if (!r.ok) {
    const msg = data?.error || data?._raw || `HTTP ${r.status}`;
    throw new Error(`${path}: ${msg}`);
  }
  return data;
}

function showView(name) {
  state.view = name;
  $$(".view").forEach(v => v.classList.add("hidden"));
  $(`#view-${name}`).classList.remove("hidden");
  $$("header nav button").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  const loaders = {
    dashboard: loadDashboard,
    pendings: loadPendings,
    history: loadHistory,
    containers: loadContainerView,
    notifier: loadNotifier,
  };
  loaders[name]?.();
}

// ----- dashboard -----

async function loadDashboard() {
  const data = await api("GET", "/api/dashboard");
  state.dashboardCache = data;
  const s = data.summary;
  $("#dashboard-summary").innerHTML = `
    <div class="card"><div class="num">${fmtNum(s.total_hosts)}</div><div class="label">hosts</div></div>
    <div class="card"><div class="num">${fmtNum(s.reachable)}</div><div class="label">reachable</div></div>
    <div class="card ${s.unhealthy ? "unhealthy" : ""}"><div class="num">${fmtNum(s.unhealthy)}</div><div class="label">unhealthy</div></div>
    <div class="card"><div class="num">${fmtNum(s.total_containers)}</div><div class="label">containers</div></div>
    <div class="card"><div class="num">${fmtNum(s.running)}</div><div class="label">running</div></div>
    <div class="card"><div class="num">${fmtNum(s.stopped)}</div><div class="label">stopped</div></div>
    <div class="card"><div class="num">${fmtNum(s.total_stacks)}</div><div class="label">stacks</div></div>
  `;

  const hostDiv = $("#dashboard-hosts");
  hostDiv.innerHTML = "";
  for (const h of data.hosts) {
    const block = el("div", { class: "host-block" });
    block.innerHTML = `
      <h4>${h.host} ${h.reachable ? badge("reachable", "ok") : badge("unreachable", "danger")}</h4>
      <div>${badge(h.error || "ok", h.error ? "danger" : "muted")}</div>
      <div>${fmtNum(h.containers?.running)} running, ${fmtNum(h.containers?.stopped)} stopped, ${fmtNum(h.containers?.unhealthy)} unhealthy</div>
      <div>${fmtNum(h.stacks?.total)} stacks, ${fmtNum(h.stacks?.with_pending_updates)} with pending updates</div>
    `;
    if (h.top_problems?.length) {
      const ul = el("div");
      for (const p of h.top_problems) {
        ul.appendChild(el("div", { class: "problem" + (p.kind === "blocker" ? " blocker" : "") }, p.text));
      }
      block.appendChild(ul);
    }
    hostDiv.appendChild(block);
  }
}

// ----- pendings -----

async function loadStacks() {
  if (state.stacksCache) return state.stacksCache;
  state.stacksCache = await api("GET", "/api/stacks");
  return state.stacksCache;
}

function fillHostSelect(sel) {
  loadStacks().then(({ hosts }) => {
    sel.innerHTML = "";
    for (const h of hosts) {
      sel.appendChild(el("option", { value: h.name }, h.name));
    }
  });
}

async function loadPendings() {
  fillHostSelect($("#pendings-host"));
  const host = $("#pendings-host").value;
  if (!host) return;
  const data = await api("GET", `/api/pendings?host=${encodeURIComponent(host)}`);
  const list = $("#pendings-list");
  list.innerHTML = "";
  if (!data.count) {
    list.appendChild(el("div", { class: "empty" }, "No pending updates for " + host));
    return;
  }
  const tbl = el("table");
  tbl.innerHTML = `
    <thead><tr>
      <th>Stack</th><th>Container</th><th>Current digest</th><th>Latest digest</th><th>Actions</th>
    </tr></thead>
    <tbody></tbody>`;
  const tbody = tbl.querySelector("tbody");
  for (const r of data.rows) {
    const tr = el("tr");
    tr.innerHTML = `
      <td>${r.stack}</td>
      <td>${r.container || "—"}</td>
      <td><code>${(r.current_digest || "").slice(7, 19)}</code></td>
      <td><code>${(r.latest_digest || "").slice(7, 19)}</code></td>
    `;
    const td = el("td", { class: "actions" });
    td.appendChild(el("button", {
      onclick: () => showApplyDialog(host, r.stack, r.latest_digest),
    }, "Apply"));
    td.appendChild(el("button", {
      class: "secondary",
      onclick: () => previewApply(host, r.stack, r.latest_digest),
    }, "Preview"));
    td.appendChild(el("button", {
      class: "danger",
      onclick: () => dismissPending(host, r.stack, r.latest_digest),
    }, "Dismiss"));
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  list.appendChild(tbl);
}

async function showApplyDialog(host, stack, latest_digest) {
  const pre = window.prompt(
    `Apply update for ${host}/${stack}?\n` +
    `Latest digest: ${(latest_digest || "").slice(0, 19)}\n\n` +
    `Type "yes" to confirm (with preflight gate). ` +
    `Type "skip" to bypass the gate. Anything else cancels.`,
  );
  if (pre === "yes") {
    const r = await api("POST", "/api/apply", { host, stack, require_approval: true });
    alert(JSON.stringify(r, null, 2));
  } else if (pre === "skip") {
    const r = await api("POST", "/api/apply", { host, stack, require_approval: false });
    alert(JSON.stringify(r, null, 2));
  }
}

async function previewApply(host, stack, latest_digest) {
  const r = await api("GET", `/api/preflight?host=${encodeURIComponent(host)}&stack=${encodeURIComponent(stack)}&action=apply_update`);
  alert(JSON.stringify(r, null, 2));
}

async function dismissPending(host, stack, latest_digest) {
  if (!confirm(`Dismiss pending update for ${host}/${stack}?`)) return;
  const r = await api("POST", "/api/dismiss", { host, stack, latest_digest });
  alert(JSON.stringify(r, null, 2));
  loadPendings();
}

// ----- history -----

async function loadHistory() {
  fillHostSelect($("#history-host"));
  const host = $("#history-host").value;
  const stack = $("#history-stack").value.trim();
  if (!host || !stack) return;
  const data = await api("GET", `/api/history?host=${encodeURIComponent(host)}&stack=${encodeURIComponent(stack)}`);
  const list = $("#history-list");
  list.innerHTML = "";
  if (!data.rows?.length) {
    list.appendChild(el("div", { class: "empty" }, "No history for " + host + "/" + stack));
    return;
  }
  const tbl = el("table");
  tbl.innerHTML = `
    <thead><tr>
      <th>When</th><th>Status</th><th>From</th><th>To</th><th>Reason</th>
    </tr></thead>
    <tbody></tbody>`;
  const tbody = tbl.querySelector("tbody");
  for (const r of data.rows) {
    const tr = el("tr");
    const kind =
      r.status === "applied" ? "ok" :
      r.status === "rolled_back" || r.status === "apply_failed" || r.status === "failed" ? "danger" :
      "muted";
    tr.innerHTML = `
      <td>${(r.started_at || "").replace("T", " ").replace("Z", "")}</td>
      <td>${badge(r.status, kind)}</td>
      <td><code>${(r.from_digest || "").slice(7, 19)}</code></td>
      <td><code>${(r.to_digest || "").slice(7, 19)}</code></td>
      <td class="muted">${(r.reason || "").slice(0, 80)}</td>
    `;
    tbody.appendChild(tr);
  }
  list.appendChild(tbl);
}

// ----- container actions -----

async function loadContainerView() {
  fillHostSelect($("#ca-host"));
}

$("#container-action-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = $("#ca-result");
  result.textContent = "running...";
  try {
    const r = await api("POST", "/api/container_action", {
      host: $("#ca-host").value,
      target: $("#ca-target").value.trim(),
      action: $("#ca-action").value,
      require_approval: $("#ca-approval").checked,
    });
    result.textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    result.textContent = "ERROR: " + e.message;
  }
});

// ----- notifier -----

async function loadNotifier() {
  const data = await api("GET", "/api/notifier");
  const list = $("#notifier-list");
  list.innerHTML = "";
  const header = el("div", { class: "row" });
  header.innerHTML = `
    <div>Configured: <b>${data.configured_count}</b> backend(s)</div>
    <div>Healthy: ${data.healthy ? badge("yes", "ok") : badge("no", "danger")}</div>
  `;
  list.appendChild(header);

  if (data.configured?.length) {
    const tbl = el("table");
    tbl.innerHTML = `<thead><tr><th>Backend</th><th>Details</th></tr></thead><tbody></tbody>`;
    const tbody = tbl.querySelector("tbody");
    for (const b of data.configured) {
      const details = Object.entries(b)
        .filter(([k]) => k !== "backend")
        .map(([k, v]) => `${k}=${v}`)
        .join("  ");
      const tr = el("tr");
      tr.innerHTML = `<td>${b.backend}</td><td><code>${details}</code></td>`;
      tbody.appendChild(tr);
    }
    list.appendChild(tbl);
  }

  if (data.missing_env_hints?.length) {
    const h = el("h4", {}, "Missing env vars (most common)");
    const ul = el("ul");
    for (const v of data.missing_env_hints) {
      ul.appendChild(el("li", {}, v));
    }
    list.appendChild(h);
    list.appendChild(ul);
  }
}

// ----- wire up -----

$$("header nav button").forEach(b => {
  b.addEventListener("click", () => showView(b.dataset.view));
});
$("#pendings-refresh").addEventListener("click", loadPendings);
$("#pendings-host").addEventListener("change", loadPendings);
$("#history-refresh").addEventListener("click", loadHistory);
$("#history-host").addEventListener("change", loadHistory);

loadStacks();
showView("dashboard");
