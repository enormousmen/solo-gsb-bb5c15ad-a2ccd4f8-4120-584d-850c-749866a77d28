/* 公交刷卡票价核验台 —— 原生前端逻辑（无框架、无构建） */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------------- 通用请求 / 提示 ---------------- */

async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  if (opts.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch (_) { /* 非 JSON 响应 */ }
  if (!resp.ok) {
    const err = new Error("HTTP " + resp.status);
    err.payload = data;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(message, ok = true) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast " + (ok ? "ok" : "bad");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

function putText(path, text) {
  return api(path, { method: "PUT", body: JSON.stringify({ text }) });
}

function yuan(v) {
  return Number(v).toFixed(2);
}

function actionText(a) {
  return a === "IN" ? "进 ▲" : "出 ▼";
}

/* ---------------- 1. 规则参数 ---------------- */

async function loadConfig() {
  const cfg = await api("/api/config");
  $("cfg-split").value = cfg.day_split_hour;
  $("cfg-minutes").value = cfg.transfer_minutes;
  $("cfg-discount").value = cfg.transfer_discount;
  $("cfg-cap").value = cfg.daily_cap;
  return cfg;
}

async function saveConfig() {
  const body = {
    day_split_hour: Number($("cfg-split").value),
    transfer_minutes: Number($("cfg-minutes").value),
    transfer_discount: Number($("cfg-discount").value),
    daily_cap: Number($("cfg-cap").value),
  };
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
    toast("规则参数已保存");
  } catch (e) {
    toast(summaryError(e, "参数校验失败"), false);
  }
}

/* ---------------- 2. 规则试算对比 ---------------- */

function fillTrialInputs(cfg) {
  $("trial-split").value = cfg.day_split_hour;
  $("trial-minutes").value = cfg.transfer_minutes;
  $("trial-discount").value = cfg.transfer_discount;
  $("trial-cap").value = cfg.daily_cap;
}

async function copyCurrentToTrial() {
  fillTrialInputs(await api("/api/config"));
  toast("已将当前已保存参数填入草案，可在此基础上修改");
}

function fmtConfig(c) {
  return `分界 ${c.day_split_hour}:00 · 换乘 ${c.transfer_minutes} 分钟 · ` +
         `抵扣 ${yuan(c.transfer_discount)} 元 · 封顶 ${yuan(c.daily_cap)} 元`;
}

function deltaClass(v) {
  return v > 0 ? "delta-up" : v < 0 ? "delta-down" : "delta-zero";
}

function deltaText(v) {
  return (v > 0 ? "+" : "") + yuan(v);
}

function renderTrialErrors(p) {
  const panel = $("trial-errors");
  panel.hidden = false;
  $("trial-result").hidden = true;

  const sides = [
    ["trial-err-current", p.current],
    ["trial-err-draft", p.draft],
  ];
  for (const [blockId, side] of sides) {
    const block = $(blockId);
    const ul = block.querySelector("ul");
    ul.innerHTML = "";
    const errors = (side && side.errors) || [];
    for (const e of errors) ul.appendChild(renderErrorLi(e));
    block.hidden = side ? side.ok !== false : true;
  }
}

function renderTrialResult(data) {
  $("trial-errors").hidden = true;
  const panel = $("trial-result");
  panel.hidden = false;

  $("trial-params").innerHTML =
    `<div><span class="tag tag-cur">当前配置</span>${fmtConfig(data.current.config)}</div>` +
    `<div><span class="tag tag-draft">草案配置</span>${fmtConfig(data.draft.config)}</div>`;

  const b = data.batch_summary;
  $("trial-batch").innerHTML =
    `全批次 <strong>${b.trip_count}</strong> 笔行程：` +
    `实扣合计 <strong>${yuan(b.current_charge_total)}</strong> 元 → ` +
    `<strong>${yuan(b.draft_charge_total)}</strong> 元，` +
    `<span class="${deltaClass(b.charge_diff)}">实扣差额 ${deltaText(b.charge_diff)} 元</span>` +
    `；优惠合计 ${yuan(b.current_discount_total)} → ${yuan(b.draft_discount_total)} 元` +
    `（优惠差额 ${deltaText(b.discount_diff)} 元，正=优惠更多）`;

  const tb = $("trial-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const r of data.rows) {
    const tr = document.createElement("tr");
    const trip = `${escapeHtml(r.entry_station)} → ${escapeHtml(r.exit_station)}` +
      `<div class="sub">${r.entry_time} ～ ${r.exit_time}</div>`;
    // 优惠差额正负的颜色与实扣相反：优惠变多（正）对乘客是好事，用绿色
    const discCls = r.discount_diff > 0 ? "delta-down"
      : r.discount_diff < 0 ? "delta-up" : "delta-zero";
    tr.innerHTML = `
      <td>${escapeHtml(r.card)}</td>
      <td class="num">${r.seq}</td>
      <td class="trip">${trip}</td>
      <td class="${r.day_changed ? "day-changed" : ""}">${r.current_operating_day}</td>
      <td class="${r.day_changed ? "day-changed" : ""}">${r.draft_operating_day}</td>
      <td class="num discount">-${yuan(r.current_discount)}</td>
      <td class="num discount">-${yuan(r.draft_discount)}</td>
      <td class="num ${discCls}">${deltaText(r.discount_diff)}</td>
      <td class="num">${yuan(r.current_charge)}</td>
      <td class="num charge">${yuan(r.draft_charge)}</td>
      <td class="num ${deltaClass(r.charge_diff)}">${deltaText(r.charge_diff)}</td>
      <td class="wrap">${escapeHtml(r.draft_discount_source)}</td>`;
    tb.appendChild(tr);
  }

  const cb = $("trial-cards-table").querySelector("tbody");
  cb.innerHTML = "";
  for (const c of data.card_summaries) {
    const tr = document.createElement("tr");
    const discCls = c.discount_diff > 0 ? "delta-down"
      : c.discount_diff < 0 ? "delta-up" : "delta-zero";
    tr.innerHTML = `
      <td>${escapeHtml(c.card)}</td>
      <td class="num">${c.trip_count}</td>
      <td class="num">${yuan(c.current_charge_total)}</td>
      <td class="num">${yuan(c.draft_charge_total)}</td>
      <td class="num ${deltaClass(c.charge_diff)}">${deltaText(c.charge_diff)}</td>
      <td class="num discount">${yuan(c.current_discount_total)}</td>
      <td class="num discount">${yuan(c.draft_discount_total)}</td>
      <td class="num ${discCls}">${deltaText(c.discount_diff)}</td>`;
    cb.appendChild(tr);
  }
}

async function runTrialCompare() {
  const status = $("trial-status");
  const body = {
    day_split_hour: Number($("trial-split").value),
    transfer_minutes: Number($("trial-minutes").value),
    transfer_discount: Number($("trial-discount").value),
    daily_cap: Number($("trial-cap").value),
  };
  if (Object.values(body).some((v) => !Number.isFinite(v))) {
    status.textContent = "草案四项参数必须全部填写为有效数字";
    status.className = "status bad";
    return;
  }
  status.textContent = "试算中…";
  status.className = "status";
  $("trial-errors").hidden = true;
  $("trial-result").hidden = true;
  try {
    const data = await api("/api/trial-compare", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderTrialResult(data);
    status.textContent =
      `试算成功：共对比 ${data.batch_summary.trip_count} 笔行程（草案未保存、账单未写入）`;
    status.className = "status ok";
    toast("规则试算完成");
  } catch (e) {
    const p = e.payload;
    if (p && (p.current || p.draft)) {
      renderTrialErrors(p);
      const failed = [];
      if (p.current && p.current.ok === false) failed.push("当前");
      if (p.draft && p.draft.ok === false) failed.push("草案");
      status.textContent =
        `试算未通过：${failed.join("、")}方案存在错误（配置未改动、账单未写入）`;
    } else {
      status.textContent = summaryError(e, "试算请求失败");
    }
    status.className = "status bad";
    toast("规则试算未通过", false);
  }
}

/* ---------------- 2-4. 文本录入与表格渲染 ---------------- */

function renderStations(items) {
  const tb = $("stations-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const s of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(s.name)}</td><td class="num">${s.zone}</td>`;
    tb.appendChild(tr);
  }
}

function renderFares(items) {
  const tb = $("fares-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const f of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="num">${f.from_zone}</td><td class="num">${f.to_zone}</td>
      <td class="num">${yuan(f.price)}</td>`;
    tb.appendChild(tr);
  }
}

function renderRecords(items) {
  const tb = $("records-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const r of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(r.card)}</td><td>${r.time}</td>
      <td>${escapeHtml(r.station)}</td><td>${actionText(r.action)}</td>
      <td class="num">第 ${r.src_line} 行</td>`;
    tb.appendChild(tr);
  }
}

async function saveTextSection(key, path, text, render) {
  try {
    const data = await putText(path, text);
    const fresh = await api(path);
    render(fresh.items);
    toast(`已保存 ${data.count} 条 ${key}`);
  } catch (e) {
    const errs = (e.payload && e.payload.errors) || [];
    if (errs.length) showParseErrors(errs);
    toast(summaryError(e, `保存${key}失败`), false);
  }
}

function showParseErrors(errors) {
  const list = errors.slice(0, 12).map((e) =>
    `第 ${e.location && e.location.line || "?"} 行：${e.message}`).join("\n");
  toast(`录入解析失败：\n${list}`, false);
  // toast 不支持换行样式时，仍可在核验结果区展示
  const ul = $("errors-list");
  ul.innerHTML = "";
  const panel = $("errors-panel");
  for (const e of errors) {
    ul.appendChild(renderErrorLi(e));
  }
  panel.hidden = false;
  $("bills-panel").hidden = true;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadBack(kind) {
  const fresh = await api(`/api/${kind}`);
  let text = "";
  if (kind === "stations") {
    text = fresh.items.map((s) => `${s.name}, ${s.zone}`).join("\n");
    renderStations(fresh.items);
  } else if (kind === "fares") {
    text = fresh.items.map((f) =>
      `${f.from_zone}, ${f.to_zone}, ${yuan(f.price)}`).join("\n");
    renderFares(fresh.items);
  } else if (kind === "records") {
    text = fresh.items.map((r) =>
      `${r.card}, ${r.time}, ${r.station}, ${r.action}`).join("\n");
    renderRecords(fresh.items);
  }
  $(`${kind}-text`).value = text;
  toast("已从数据库回填");
}

/* ---------------- 票价矩阵模板 ---------------- */

function genFareTemplate() {
  const n = Math.max(1, Math.min(20, Number($("fare-zones").value) || 3));
  const lines = [];
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= n; j++) {
      // 模板建议价：同区 2 元，每跨一区 +1 元（用户可自行改价）
      const price = 2 + Math.abs(i - j);
      lines.push(`${i}, ${j}, ${price}.00`);
    }
  }
  $("fares-text").value = lines.join("\n");
  toast(`已生成 ${n}×${n} 对称模板，请核对并保存`);
}

/* ---------------- 8. 核验 ---------------- */

function renderErrorLi(e) {
  const li = document.createElement("li");
  const code = document.createElement("span");
  code.className = "error-code";
  code.textContent = e.code;
  li.appendChild(code);
  li.appendChild(document.createTextNode(e.message));
  if (e.location) {
    const parts = [];
    const loc = e.location;
    if (loc.line !== undefined) parts.push(`文本第 ${loc.line} 行`);
    if (loc.card) parts.push(`卡号 ${loc.card}`);
    if (loc.time) parts.push(`时刻 ${loc.time}`);
    if (loc.station) parts.push(`站点 ${loc.station}`);
    if (parts.length) {
      const span = document.createElement("span");
      span.className = "error-loc";
      span.textContent = "定位：" + parts.join(" · ");
      li.appendChild(span);
    }
  }
  return li;
}

function renderBills(bills) {
  const tb = $("bills-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const b of bills) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(b.card)}</td>
      <td class="num">${b.seq}</td>
      <td>${b.operating_day}</td>
      <td>${b.entry_time}</td>
      <td>${b.exit_time}</td>
      <td>${escapeHtml(b.entry_station)}（${b.from_zone}区）</td>
      <td>${escapeHtml(b.exit_station)}（${b.to_zone}区）</td>
      <td class="num">${yuan(b.base_fare)}</td>
      <td class="num discount">-${yuan(b.discount)}</td>
      <td class="num charge">${yuan(b.charged_fare)}</td>
      <td class="num">${yuan(b.daily_cumulative)} / ${yuan(b.daily_cap)}</td>
      <td class="wrap">${escapeHtml(b.discount_source)}</td>`;
    tb.appendChild(tr);
  }
}

async function runVerify() {
  const status = $("verify-status");
  status.textContent = "核验中…";
  status.className = "status";
  try {
    const data = await api("/api/verify", { method: "POST" });
    renderBills(data.bills || []);
    $("bills-panel").hidden = false;
    $("errors-panel").hidden = true;
    status.textContent = `核验通过，共 ${data.bill_count} 笔行程账单`;
    status.className = "status ok";
    toast(`核验通过：${data.bill_count} 笔行程`);
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    const ul = $("errors-list");
    ul.innerHTML = "";
    for (const err of errors) ul.appendChild(renderErrorLi(err));
    $("errors-panel").hidden = false;
    $("bills-panel").hidden = true;
    status.textContent = `核验未通过：${errors.length} 个错误（未生成账单）`;
    status.className = "status bad";
    toast(`核验未通过：${errors.length} 个错误`, false);
  }
}

/* ---------------- 6. 配对修复预览（只读） ---------------- */

function renderRepairErrors(errors) {
  const ul = $("repair-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("repair-errors").hidden = false;
  $("repair-result").hidden = true;
}

function changeItemHtml(ch) {
  const where = `${escapeHtml(ch.card)} ${ch.time} @ ${escapeHtml(ch.station)}` +
    `<span class="sub">（来源第 ${ch.line} 行）</span>`;
  if (ch.op === "delete") {
    return `<span class="tag tag-del">删除</span>${where}` +
      `<span class="sub">原方向 ${actionText(ch.from_action)}</span>`;
  }
  return `<span class="tag tag-flip">翻转</span>${where}` +
    `<span class="sub">${actionText(ch.from_action)} → ${actionText(ch.to_action)}</span>`;
}

function renderRepairPlans(data) {
  $("repair-errors").hidden = true;
  const panel = $("repair-result");
  panel.hidden = false;

  const summary = $("repair-summary");
  const plansBox = $("repair-plans");
  plansBox.innerHTML = "";

  if (data.min_edits === 0) {
    summary.innerHTML = "✅ 已保存的记录可完整配对，<strong>无需修复</strong>。" +
      "（预览为只读操作，未改动任何数据）";
    return;
  }

  const hiddenNote = data.hidden_plans > 0
    ? `，另有 <strong>${data.hidden_plans}</strong> 个同成本方案未展示` : "";
  summary.innerHTML =
    `最小编辑数 <strong>${data.min_edits}</strong> 次 · ` +
    `共 <strong>${data.total_plans}</strong> 个同成本方案，` +
    `按稳定顺序展示 ${data.shown_plans} 个${hiddenNote}。<br>` +
    `<span class="sub">请人工核对后自行选择：系统不会替您选定方案；` +
    `预览全程只读，未保存记录、未清空账单。</span>`;

  data.plans.forEach((p, idx) => {
    const div = document.createElement("div");
    div.className = "repair-plan";

    const head = document.createElement("div");
    head.className = "repair-plan-head";
    head.innerHTML =
      `<h3>方案 ${idx + 1}</h3>` +
      `<span class="hint">编辑 ${p.edit_count} 次 · 变更 ${p.change_count} 条 · ` +
      `修复后 ${p.kept_count} 条记录 / ${p.trip_count} 笔行程</span>`;
    const btn = document.createElement("button");
    btn.className = "btn btn-ghost";
    btn.type = "button";
    btn.textContent = "📋 复制修复后记录";
    btn.addEventListener("click", () => copyText(p.fixed_text));
    head.appendChild(btn);
    div.appendChild(head);

    const ul = document.createElement("ul");
    ul.className = "change-list";
    for (const ch of p.changes) {
      const li = document.createElement("li");
      li.innerHTML = changeItemHtml(ch);
      ul.appendChild(li);
    }
    div.appendChild(ul);

    const pre = document.createElement("pre");
    pre.className = "fixed-text";
    pre.textContent = p.fixed_text;
    div.appendChild(pre);

    plansBox.appendChild(div);
  });
}

async function copyText(text, message = "已复制修复后的记录文本（仍未保存，可粘贴到记录区核对）") {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    toast(message);
  } catch (_) {
    toast("复制失败，请手动选中文本复制", false);
  }
}

async function runRepairPreview() {
  const status = $("repair-status");
  status.textContent = "搜索最小编辑方案…";
  status.className = "status";
  $("repair-errors").hidden = true;
  $("repair-result").hidden = true;
  try {
    const data = await api("/api/repair-preview", { method: "POST" });
    renderRepairPlans(data);
    if (data.min_edits === 0) {
      status.textContent = "记录已可完整配对，无需修复";
    } else {
      status.textContent =
        `已生成 ${data.shown_plans} 个最小编辑方案` +
        (data.hidden_plans > 0 ? `（另有 ${data.hidden_plans} 个未展示）` : "") +
        "，记录与账单均未改动";
    }
    status.className = "status ok";
    toast("修复预览已生成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderRepairErrors(errors);
      status.textContent =
        `存在 ${errors.length} 个非配对错误，未生成修复方案（记录与账单均未改动）`;
    } else {
      status.textContent = summaryError(e, "修复预览请求失败");
    }
    status.className = "status bad";
    toast("无法生成修复预览", false);
  }
}

/* ---------------- 7. 票价矩阵路径审计（只读） ---------------- */

function auditAffectedHtml(item) {
  // 受影响卡号 + 每程原始行号；occurrences 已按卡号、进站时刻排序。
  const lines = item.occurrences.map((o) =>
    `<li>${escapeHtml(o.card)} · ${o.entry_time} ～ ${o.exit_time} · ` +
    `${escapeHtml(o.entry_station)} → ${escapeHtml(o.exit_station)}` +
    `<span class="sub">（进第 ${o.entry_line} 行 / 出第 ${o.exit_line} 行）</span></li>`);
  const cards = item.affected_cards.map(escapeHtml).join("、");
  return `<div class="audit-affected"><div>卡号：<strong>${cards}</strong>` +
    `（共 ${item.affected_count} 程）</div><ul>${lines.join("")}</ul></div>`;
}

function copyRowBtn(line, label) {
  const btn = document.createElement("button");
  btn.className = "btn btn-ghost btn-mini";
  btn.type = "button";
  btn.textContent = "📋 复制";
  btn.addEventListener("click", () =>
    copyText(line, "已复制建议行（审计只读，请粘贴到第 4 区票价矩阵保存）"));
  btn.title = label;
  return btn;
}

function renderAuditSuggestions(items) {
  const block = $("audit-suggest-block");
  block.hidden = items.length === 0;
  const tb = $("audit-suggest-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const s of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="num"><strong>${s.from_zone} → ${s.to_zone}</strong></td>
      <td class="num charge">${yuan(s.suggested_fare)}</td>
      <td class="num">${s.hop_count}</td>
      <td class="wrap audit-path">${escapeHtml(s.path_text)}</td>
      <td class="wrap">${auditAffectedHtml(s)}</td>
      <td class="num audit-line"><code>${escapeHtml(s.suggested_line)}</code></td>
      <td></td>`;
    tr.lastElementChild.appendChild(
      copyRowBtn(s.suggested_line, "复制建议补录行"));
    tb.appendChild(tr);
  }
}

function renderAuditOverpriced(items) {
  const block = $("audit-over-block");
  block.hidden = items.length === 0;
  const tb = $("audit-over-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const o of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="num"><strong>${o.from_zone} → ${o.to_zone}</strong></td>
      <td class="num">${yuan(o.direct_fare)}</td>
      <td class="num">${yuan(o.indirect_fare)}</td>
      <td class="num delta-up">+${yuan(o.diff)}</td>
      <td class="wrap audit-path">${escapeHtml(o.path_text)}</td>
      <td class="wrap">${auditAffectedHtml(o)}</td>
      <td class="num audit-line"><code>${escapeHtml(o.suggested_line)}</code></td>
      <td></td>`;
    tr.lastElementChild.appendChild(
      copyRowBtn(o.suggested_line, "复制建议改价行"));
    tb.appendChild(tr);
  }
}

function renderAuditMissing(errors) {
  const block = $("audit-missing-block");
  block.hidden = errors.length === 0;
  const ul = $("audit-missing-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
}

function renderAuditResult(data) {
  $("audit-errors").hidden = true;
  const panel = $("audit-result");
  panel.hidden = false;

  const allSuggested = data.suggestions.map((s) => s.suggested_line);
  const copyAllBtn = $("btn-audit-copy-all");
  copyAllBtn.onclick = () => copyText(
    allSuggested.join("\n"),
    `已复制 ${allSuggested.length} 行建议补录票价（审计只读，未修改矩阵）`);

  $("audit-summary").innerHTML =
    `实际行程共 <strong>${data.pair_count}</strong> 个分区对：` +
    `<span class="audit-ok">直达票价合理 <strong>${data.direct_ok_count}</strong></span> · ` +
    `<span class="audit-sugg">缺直达但可达（建议补录）<strong>${data.suggestion_count}</strong></span> · ` +
    `<span class="audit-over">直达偏高（建议下调）<strong>${data.overpriced_count}</strong></span> · ` +
    `<span class="audit-miss">不可达（MISSING_FARE）<strong>${data.missing_count}</strong></span>。` +
    `<br><span class="sub">相同总价按分区序列稳定选路（等价时优先直达）；` +
    `审计全程只读，票价矩阵与账单均未改动。</span>`;

  renderAuditSuggestions(data.suggestions);
  renderAuditOverpriced(data.overpriced);
  renderAuditMissing(data.missing_errors);
}

async function runFareAudit() {
  const status = $("audit-status");
  status.textContent = "路径计算中…";
  status.className = "status";
  $("audit-errors").hidden = true;
  $("audit-result").hidden = true;
  try {
    const data = await api("/api/fare-audit", { method: "POST" });
    renderAuditResult(data);
    if (data.suggestion_count === 0 && data.overpriced_count === 0 &&
        data.missing_count === 0) {
      status.textContent =
        `审计完成：${data.pair_count} 个分区对的直达票价均为最低成本路径`;
    } else {
      status.textContent =
        `审计完成：建议补录 ${data.suggestion_count} · 直达偏高 ` +
        `${data.overpriced_count} · 不可达 ${data.missing_count}`;
    }
    status.className = "status ok";
    toast("票价矩阵路径审计完成（只读，未改动矩阵或账单）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      const ul = $("audit-errors-list");
      ul.innerHTML = "";
      for (const err of errors) ul.appendChild(renderErrorLi(err));
      $("audit-errors").hidden = false;
      status.textContent =
        `存在 ${errors.length} 个数据错误，未执行路径审计（矩阵与账单均未改动）`;
    } else {
      status.textContent = summaryError(e, "路径审计请求失败");
    }
    status.className = "status bad";
    toast("无法执行路径审计", false);
  }
}

/* ---------------- 演示数据 ---------------- */

const DEMO = {
  config: { day_split_hour: 4, transfer_minutes: 90,
            transfer_discount: 1, daily_cap: 10 },
  stations: `科技园, 1
中心公园, 2
大学城, 3`,
  fares: `1, 1, 2.00
1, 2, 3.00
1, 3, 4.00
2, 1, 3.00
2, 2, 2.00
2, 3, 3.00
3, 1, 4.00
3, 2, 3.00
3, 3, 2.00`,
  records: `A001, 2026-09-18 08:00:00, 科技园, 进
A001, 2026-09-18 08:25:00, 中心公园, 出
A001, 2026-09-18 09:10:00, 中心公园, 进
A001, 2026-09-18 09:50:00, 大学城, 出
A001, 2026-09-18 17:30:00, 大学城, 进
A001, 2026-09-18 18:20:00, 科技园, 出
A001, 2026-09-18 19:30:00, 科技园, 进
A001, 2026-09-18 20:00:00, 中心公园, 出
B002, 2026-09-19 03:20:00, 科技园, 进
B002, 2026-09-19 03:45:00, 中心公园, 出`,
};

async function loadDemo() {
  if (!confirm("将用演示数据覆盖当前参数 / 站点 / 票价 / 记录，是否继续？")) return;
  $("cfg-split").value = DEMO.config.day_split_hour;
  $("cfg-minutes").value = DEMO.config.transfer_minutes;
  $("cfg-discount").value = DEMO.config.transfer_discount;
  $("cfg-cap").value = DEMO.config.daily_cap;
  $("stations-text").value = DEMO.stations;
  $("fares-text").value = DEMO.fares;
  $("records-text").value = DEMO.records;

  await saveConfig();
  await putText("/api/stations", DEMO.stations);
  await putText("/api/fares", DEMO.fares);
  await putText("/api/records", DEMO.records);

  renderStations((await api("/api/stations")).items);
  renderFares((await api("/api/fares")).items);
  renderRecords((await api("/api/records")).items);
  toast("演示数据载入完成，开始核验…");
  await runVerify();
}

/* ---------------- 工具 ---------------- */

function summaryError(e, fallback) {
  const errs = e.payload && e.payload.errors;
  if (errs && errs.length) {
    const first = errs[0];
    const line = first.location && first.location.line;
    return `${fallback}：${line ? "第 " + line + " 行 " : ""}${first.message}`;
  }
  if (e.payload && e.payload.detail) return e.payload.detail;
  return fallback;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------- 初始化 ---------------- */

function bind() {
  $("btn-save-config").addEventListener("click", saveConfig);

  $("btn-run-trial").addEventListener("click", runTrialCompare);
  $("btn-trial-copy").addEventListener("click", copyCurrentToTrial);

  $("btn-save-stations").addEventListener("click", () =>
    saveTextSection("站点", "/api/stations", $("stations-text").value, renderStations));
  $("btn-save-fares").addEventListener("click", () =>
    saveTextSection("票价", "/api/fares", $("fares-text").value, renderFares));
  $("btn-save-records").addEventListener("click", () =>
    saveTextSection("记录", "/api/records", $("records-text").value, renderRecords));

  $("btn-load-stations").addEventListener("click", () => loadBack("stations"));
  $("btn-load-fares").addEventListener("click", () => loadBack("fares"));
  $("btn-load-records").addEventListener("click", () => loadBack("records"));

  $("btn-gen-fares").addEventListener("click", genFareTemplate);
  $("btn-repair").addEventListener("click", runRepairPreview);
  $("btn-audit").addEventListener("click", runFareAudit);
  $("btn-verify").addEventListener("click", runVerify);
  $("btn-verify-top").addEventListener("click", runVerify);
  $("btn-demo").addEventListener("click", loadDemo);
}

async function init() {
  bind();
  try {
    const cfg = await loadConfig();
    fillTrialInputs(cfg);
    renderStations((await api("/api/stations")).items);
    renderFares((await api("/api/fares")).items);
    renderRecords((await api("/api/records")).items);
    const bills = await api("/api/bills");
    if (bills.count > 0) {
      renderBills(bills.items);
      $("bills-panel").hidden = false;
      $("verify-status").textContent =
        `已加载上次核验结果（${bills.count} 笔，${bills.last_verified_at || ""}），修改数据后请重新核验`;
      $("verify-status").className = "status";
    }
  } catch (e) {
    toast("初始化加载数据失败：" + e.message, false);
  }
}

document.addEventListener("DOMContentLoaded", init);
