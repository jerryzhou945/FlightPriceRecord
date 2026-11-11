// 航价记前端：读取 /api/*，渲染总览、走势、低价日历、弹性行程与提醒。
const $ = (id) => document.getElementById(id);
const DAY = 864e5;
const WD = "日一二三四五六";

const toMs = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
const toStr = (ms) => new Date(ms).toISOString().slice(0, 10);
const addDays = (s, n) => toStr(toMs(s) + n * DAY);
const md = (s) => `${+s.slice(5, 7)}/${+s.slice(8, 10)}`;
const wd = (s) => "周" + WD[new Date(toMs(s)).getUTCDay()];
const yuan = (n) => (n == null ? "—" : "¥" + Math.round(n).toLocaleString("en-US"));
const tok = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}
function relTime(iso) {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return "刚刚";
  if (m < 60) return `${m} 分钟前`;
  if (m < 60 * 24) return `${Math.round(m / 60)} 小时前`;
  return `${Math.round(m / 1440)} 天前`;
}
function ctripUrl(o, d, dep, ret) {
  return `https://flights.ctrip.com/online/list/round-${o.toLowerCase()}-${d.toLowerCase()}?depdate=${dep}_${ret}&cabin=y_s&adult=1&child=0&infant=0`;
}

async function api(path, params, opts) {
  const qs = params ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== "")) : "";
  const res = await fetch(path + qs, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    const err = new Error(typeof d === "string" ? d : `请求失败（${res.status}）`);
    err.status = res.status;
    throw err;
  }
  return body;
}

let toastTimer;
function toast(msg, isErr = false) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 3200);
}

const state = {
  routes: [], route: null, flex: null, history: null, sel: null,
  mode: "combo", days: 14, calDir: "out", calMonth: null, status: null,
};
let trendChart, heatChart;

/* ---------------- routes & form ---------------- */
function defaultWindow() {
  const now = new Date();
  const first = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
  const last = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 2, 0));
  return [toStr(first.getTime()), toStr(last.getTime())];
}
function setRoute(route) {
  state.route = route;
  const f = route.flex || {};
  const [ds, de] = defaultWindow();
  $("fStart").value = f.start || ds;
  $("fEnd").value = f.end || de;
  $("fMin").value = f.min_days || 25;
  $("fMax").value = f.max_days || 35;
  $("fIdeal").value = f.ideal_days || "";
  state.calMonth = $("fStart").value.slice(0, 7);
  state.sel = null;
  const [o, d] = [route.origin, route.dest];
  $("kOutLabel").textContent = `窗口内去程最低 · ${o}→${d}`;
  $("kRetLabel").textContent = `对应回程最低 · ${d}→${o}`;
}
function readForm() {
  let start = $("fStart").value, end = $("fEnd").value;
  let min = +$("fMin").value, max = +$("fMax").value;
  if (!start || !end || !min || !max) return null;
  if (start > end) [start, end] = [end, start];
  if (min > max) [min, max] = [max, min];
  return { start, end, min, max, ideal: +$("fIdeal").value || null };
}

/* ---------------- loaders ---------------- */
async function loadStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    const dot = $("statusDot");
    if (!s.last_ok_at) {
      dot.className = "dot idle";
      $("statusText").textContent = "还没有抓取过";
    } else {
      dot.className = "dot" + (s.fail_streak ? " bad" : "");
      $("statusText").textContent = s.fail_streak
        ? `最近 ${s.fail_streak} 次抓取失败`
        : `上次抓取 ${relTime(s.last_ok_at)}`;
    }
    $("runsSub").textContent = `共 ${s.n_runs} 次抓取 · ${s.total_quotes.toLocaleString()} 条单程报价`;
    const labels = { ok: "成功", partial: "部分成功", failed: "失败", running: "进行中" };
    $("runsBody").innerHTML = s.runs.length
      ? s.runs.map((r) => `<tr>
          <td class="num">${fmtTime(r.started_at)}</td>
          <td><span class="pill ${esc(r.status)}">${labels[r.status] || esc(r.status)}</span></td>
          <td class="num price">${r.n_quotes}</td>
          <td class="wrap">${esc(r.error || "")}</td></tr>`).join("")
      : `<tr><td colspan="4" class="wrap">还没有抓取记录</td></tr>`;
    $("empty").hidden = s.total_quotes > 0;
  } catch (e) {
    $("statusText").textContent = "连不上服务";
    $("statusDot").className = "dot bad";
  }
}

async function loadFlex() {
  const q = readForm();
  if (!q || !state.route) return;
  $("flexError").hidden = true;
  document.querySelector(".planner").classList.add("loading");
  try {
    const f = await api("/api/flex", { origin: state.route.origin, dest: state.route.dest, ...q, top: 8 });
    state.flex = f;
    const keep = state.sel && f.cells.length && f.top.concat(f.by_days).some((t) => t.depart === state.sel.depart && t.return === state.sel.return);
    if (!keep) state.sel = f.best;
    renderPlanner();
    renderLegs();
    await loadHistory();
  } catch (e) {
    $("flexError").textContent = e.message;
    $("flexError").hidden = false;
  } finally {
    document.querySelector(".planner").classList.remove("loading");
  }
}

async function loadHistory() {
  const t = state.sel;
  if (!t) {
    state.history = null;
    renderTrend();
    renderKpis();
    return;
  }
  try {
    state.history = await api("/api/history", {
      origin: state.route.origin, dest: state.route.dest, depart: t.depart, ret: t.return, window: 14,
    });
  } catch (e) {
    state.history = null;
    toast(e.message, true);
  }
  renderTrend();
  renderKpis();
}

async function loadCal() {
  if (!state.route || !state.calMonth) return;
  const [o, d] = state.calDir === "out" ? [state.route.origin, state.route.dest] : [state.route.dest, state.route.origin];
  const [y, m] = state.calMonth.split("-").map(Number);
  $("calLabel").textContent = `${y} 年 ${m} 月 · ${o}→${d}`;
  try {
    const c = await api("/api/calendar", { origin: o, dest: d, month: state.calMonth });
    renderCal(c, y, m);
  } catch (e) {
    $("cal").innerHTML = `<p class="hint">${esc(e.message)}</p>`;
  }
}

async function loadAlerts() {
  try {
    const a = await api("/api/alerts", state.route ? { origin: state.route.origin, dest: state.route.dest } : null);
    renderAlerts(a);
  } catch (e) {
    $("rules").innerHTML = `<p class="hint">${e.status === 404 ? "提醒功能还没有启用。" : esc(e.message)}</p>`;
    $("alertLog").innerHTML = "";
  }
}

async function loadAll() {
  await loadStatus();
  await Promise.all([loadFlex(), loadCal(), loadAlerts()]);
}

/* ---------------- renderers ---------------- */
function baseText() {
  return { color: tok("--muted"), fontFamily: "Sora, PingFang SC, Microsoft YaHei, sans-serif" };
}
function tooltipStyle() {
  return { backgroundColor: tok("--glass-strong"), borderColor: tok("--stroke"), textStyle: { color: tok("--fg") } };
}

function renderPlanner() {
  const f = state.flex;
  const o = state.route.origin, d = state.route.dest;
  if (!f.best) {
    $("bestV").textContent = "无数据";
    $("bestTrip").textContent = f.observed_at ? "调整窗口或天数试试" : "先点“立即抓取”";
    $("bestLink").removeAttribute("href");
    $("manualBtn").disabled = true;
  } else {
    $("bestV").textContent = yuan(f.best.best_price);
    $("bestTrip").textContent = `${md(f.best.depart)} ${wd(f.best.depart)} → ${md(f.best.return)} ${wd(f.best.return)} · ${f.best.days} 天`;
    $("bestLink").href = f.best.ctrip_url;
    $("manualBtn").disabled = false;
  }
  $("tips").innerHTML = f.suggestions.map((s) => `<li>${esc(s)}</li>`).join("");
  $("heatHint").textContent = f.observed_at
    ? `数据 ${fmtTime(f.observed_at)} · 估价 = 去程单程 + 回程单程 · 点格子看走势`
    : "还没有数据";

  // heatmap
  const dates = f.depart_dates, stays = f.stays;
  const vals = f.cells.map((c) => c[2]);
  heatChart.setOption({
    animation: false,
    textStyle: baseText(),
    grid: { left: 46, right: 10, top: 6, bottom: 66 },
    tooltip: {
      ...tooltipStyle(),
      formatter: (p) => {
        const dep = dates[p.value[0]], s = stays[p.value[1]], ret = addDays(dep, s);
        return `${md(dep)} ${wd(dep)} → ${md(ret)} ${wd(ret)}<br>${s} 天 · <b>${yuan(p.value[2])}</b>`;
      },
    },
    xAxis: { type: "category", data: dates.map(md), axisTick: { show: false }, axisLine: { show: false },
      axisLabel: { color: tok("--muted"), interval: dates.length > 20 ? 1 : 0, fontSize: 10 } },
    yAxis: { type: "category", data: stays.map((s) => s + "天"), axisTick: { show: false }, axisLine: { show: false },
      axisLabel: { color: tok("--muted"), fontSize: 10, interval: stays.length > 20 ? 1 : 0 } },
    visualMap: {
      show: vals.length > 0, min: vals.length ? Math.min(...vals) : 0, max: vals.length ? Math.max(...vals) : 1,
      orient: "horizontal", left: "center", bottom: 0, itemWidth: 10, itemHeight: 140,
      text: ["贵", "便宜"], textStyle: { color: tok("--muted"), fontSize: 11 },
      inRange: { color: [tok("--heat-0"), tok("--heat-1"), tok("--heat-2")] },
    },
    series: [{ type: "heatmap", data: f.cells, itemStyle: { borderColor: tok("--bg"), borderWidth: 2, borderRadius: 3 },
      emphasis: { itemStyle: { borderColor: tok("--fg"), borderWidth: 1.5 } } }],
  }, true);
  heatChart.off("click");
  heatChart.on("click", (p) => {
    const dep = dates[p.value[0]], s = stays[p.value[1]];
    select({ depart: dep, return: addDays(dep, s), days: s, best_price: p.value[2], ctrip_url: ctripUrl(o, d, dep, addDays(dep, s)) });
  });

  renderTop();
}

function renderTop() {
  const f = state.flex;
  if (!f.top.length) {
    $("top").innerHTML = `<tr><td colspan="7" class="wrap">当前窗口没有可用报价</td></tr>`;
    return;
  }
  $("top").innerHTML = f.top.map((t, i) => {
    const sel = state.sel && state.sel.depart === t.depart && state.sel.return === t.return;
    return `<tr data-i="${i}" class="${sel ? "sel" : ""}" tabindex="0">
      <td class="num">${md(t.depart)} <span style="color:var(--faint)">${wd(t.depart)}</span></td>
      <td class="num">${md(t.return)} <span style="color:var(--faint)">${wd(t.return)}</span></td>
      <td class="num">${t.days}</td>
      <td class="num price" style="font-weight:400;color:var(--muted)">${yuan(t.out)}</td>
      <td class="num price" style="font-weight:400;color:var(--muted)">${yuan(t.ret)}</td>
      <td class="num price">${yuan(t.price)}</td>
      <td class="num price" style="color:var(--down)">${t.rt_price ? yuan(t.rt_price) : ""}</td></tr>`;
  }).join("");
  $("top").querySelectorAll("tr[data-i]").forEach((tr) => {
    const pick = () => select(f.top[+tr.dataset.i]);
    tr.addEventListener("click", pick);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  });
}

function renderLegs() {
  const l = state.flex.leg_lows || {};
  $("kOut").textContent = l.out ? yuan(l.out.price) : "—";
  $("kOutD").textContent = l.out ? `${md(l.out.date)} ${wd(l.out.date)} 出发` : "暂无数据";
  $("kRet").textContent = l.ret ? yuan(l.ret.price) : "—";
  $("kRetD").textContent = l.ret ? `${md(l.ret.date)} ${wd(l.ret.date)} 返回` : "暂无数据";
}

function renderKpis() {
  const t = state.sel, h = state.history, s = h && h.stats;
  if (!t) {
    $("ovSub").textContent = "还没有可用组合";
    ["kNow", "kPct", "kMin", "kMax"].forEach((id) => ($(id).textContent = "—"));
    $("kNowDelta").hidden = true;
    $("kNowTrip").textContent = "";
    return;
  }
  $("ovSub").textContent = `${md(t.depart)}（${wd(t.depart)}）出发 → ${md(t.return)}（${wd(t.return)}）返回 · ${t.days} 天`;
  $("kNow").textContent = yuan(s ? s.current : t.best_price);
  $("kNowTrip").textContent = `${md(t.depart)} → ${md(t.return)}`;
  const delta = $("kNowDelta");
  if (s && s.change_24h != null && s.change_24h !== 0) {
    delta.hidden = false;
    delta.className = "delta " + (s.change_24h < 0 ? "down" : "up");
    delta.textContent = (s.change_24h < 0 ? "↓ " : "↑ ") + yuan(Math.abs(s.change_24h)) + " 24h";
  } else {
    delta.hidden = true;
  }
  if (s && s.n > 1) {
    $("kPct").textContent = s.is_new_low ? "历史新低" : `比 ${s.cheaper_than_pct}% 时间便宜`;
    $("kPctMark").style.left = (s.high === s.low ? 50 : ((s.current - s.low) / (s.high - s.low)) * 100) + "%";
    $("kMin").textContent = yuan(s.low);
    $("kMax").textContent = yuan(s.high);
  } else {
    $("kPct").textContent = "数据不足";
    $("kPctMark").style.left = "50%";
    $("kMin").textContent = "—";
    $("kMax").textContent = "—";
  }
}

function renderTrend() {
  const fg = tok("--fg"), down = tok("--down");
  let pts = [], hint, manual = [];
  if (state.mode === "combo") {
    const h = state.history;
    pts = h ? h.series.map(([t, p]) => [new Date(t).getTime(), p]) : [];
    manual = h ? h.manual.map((m) => [new Date(m.observed_at).getTime(), m.price, m.note || ""]) : [];
    hint = state.sel ? `${md(state.sel.depart)} → ${md(state.sel.return)} 的往返估价，每次抓取记录一个点` : "选择一个组合";
  } else {
    const lh = state.flex ? state.flex.low_history : [];
    pts = lh.map((p) => [new Date(p.t).getTime(), p.price, p.depart, p.return]);
    hint = "每次抓取时，弹性窗口内最便宜组合的价格";
  }
  if (state.days && pts.length) {
    const end = pts[pts.length - 1][0];
    pts = pts.filter((p) => p[0] >= end - state.days * DAY);
    manual = manual.filter((p) => p[0] >= end - state.days * DAY);
  }
  if (pts.length < 2) hint += " · 数据还少，多抓几次就能看到走势";
  $("trendHint").textContent = hint;
  const target = state.route && state.route.target_price;
  const series = [{
    name: state.mode === "combo" ? "往返估价" : "窗口最低", type: "line", data: pts, showSymbol: pts.length < 3,
    symbolSize: 6, smooth: 0.15, lineStyle: { width: 2, color: fg }, itemStyle: { color: fg },
    areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: tok("--area") }, { offset: 1, color: "rgba(0,0,0,0)" }]) },
    markPoint: pts.length > 1 ? {
      symbol: "circle", symbolSize: 9, itemStyle: { color: down, borderColor: tok("--bg"), borderWidth: 2 },
      label: { show: true, position: "bottom", distance: 8, color: down, fontWeight: 600, formatter: (p) => "最低 " + yuan(p.value) },
      data: [{ type: "min" }],
    } : undefined,
    markLine: target ? {
      symbol: "none", silent: true, lineStyle: { type: "dashed", color: down, opacity: 0.8 },
      label: { color: down, position: "insideEndTop", formatter: "目标 " + yuan(target) }, data: [{ yAxis: target }],
    } : undefined,
  }];
  if (manual.length) {
    series.push({ name: "手动核价", type: "scatter", data: manual, symbol: "diamond", symbolSize: 11,
      itemStyle: { color: down, borderColor: tok("--bg"), borderWidth: 1.5 } });
  }
  trendChart.setOption({
    animationDuration: 400,
    textStyle: baseText(),
    grid: { left: 52, right: 16, top: 26, bottom: 30 },
    legend: manual.length ? { right: 0, top: 0, textStyle: { color: tok("--muted") }, itemWidth: 12, itemHeight: 8 } : undefined,
    tooltip: {
      trigger: "axis", ...tooltipStyle(), axisPointer: { lineStyle: { color: tok("--faint") } },
      formatter: (ps) => {
        const p = ps[0];
        const head = fmtTime(new Date(p.value[0]).toISOString());
        const rows = ps.map((x) => {
          const extra = x.seriesName === "窗口最低" && x.value[2] ? ` <span style="opacity:.6">${md(x.value[2])}→${md(x.value[3])}</span>` : "";
          const note = x.seriesName === "手动核价" && x.value[2] ? ` <span style="opacity:.6">${esc(x.value[2])}</span>` : "";
          return `${x.marker}${x.seriesName} <b>${yuan(x.value[1])}</b>${extra}${note}`;
        });
        return [head, ...rows].join("<br>");
      },
    },
    xAxis: { type: "time", minInterval: DAY / 2, axisLine: { lineStyle: { color: tok("--stroke") } }, axisTick: { show: false },
      axisLabel: { color: tok("--muted"), formatter: "{M}/{d}", hideOverlap: true }, splitLine: { show: false } },
    yAxis: { type: "value", scale: true, splitNumber: 4,
      axisLabel: { color: tok("--muted"), formatter: (v) => "¥" + (v / 1000).toFixed(1) + "k" },
      splitLine: { lineStyle: { color: tok("--grid") } } },
    series,
  }, true);
}

function renderCal(c, y, m) {
  const first = Date.UTC(y, m - 1, 1);
  const daysIn = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const byDate = Object.fromEntries(c.days.map((x) => [x.date, x]));
  let html = "一二三四五六日".split("").map((ch) => `<div class="wd">${ch}</div>`).join("");
  const lead = (new Date(first).getUTCDay() + 6) % 7;
  html += "<div></div>".repeat(lead);
  for (let i = 1; i <= daysIn; i++) {
    const ds = toStr(first + (i - 1) * DAY), x = byDate[ds];
    if (!x) {
      html += `<div class="day none" title="${ds} 暂无报价"><span class="d">${i}</span><span class="p num">—</span></div>`;
      continue;
    }
    let cls = "day" + (x.tier === 0 ? " t0" : x.tier === 4 ? " t4" : "");
    if (c.low && c.low.date === ds) cls += " best";
    html += `<a class="${cls}" href="${esc(x.ctrip_url)}" target="_blank" rel="noopener" title="${ds} ${wd(ds)} ${yuan(x.price)}">
      <span class="d">${i}</span><span class="p num">${(x.price / 1000).toFixed(1)}k</span></a>`;
  }
  $("cal").innerHTML = html;
}

function renderAlerts(a) {
  $("channelHint").textContent = a.channel_label || "";
  $("alertsSub").textContent = a.configured ? "满足任意一条就推送到微信" : "还没有配置微信推送，见 README“微信提醒”一节";
  $("rules").innerHTML = a.rules.map((r) => `<div class="rule">
      <div><div class="t">${esc(r.title)}</div><div class="s">${esc(r.detail)}</div></div>
      <span class="pill ${r.enabled ? "ok" : "partial"}" style="margin-left:auto">${r.enabled ? "已开启" : "未开启"}</span></div>`).join("");
  const labels = { sent: "已发送", failed: "失败", deferred: "延后" };
  $("alertLog").innerHTML = a.log.length
    ? a.log.map((l) => `<tr><td class="num">${fmtTime(l.sent_at)}</td><td>${esc(l.rule_label || l.rule)}</td>
        <td class="wrap">${esc(l.message || "")}</td><td><span class="pill ${esc(l.status)}">${labels[l.status] || esc(l.status)}</span></td></tr>`).join("")
    : `<tr><td colspan="4" class="wrap">还没有推送记录</td></tr>`;
}

/* ---------------- interactions ---------------- */
function select(t) {
  state.sel = t;
  renderTop();
  loadHistory();
}

function bindSeg(attr, fn) {
  document.querySelectorAll(`[data-${attr}]`).forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(`[data-${attr}]`).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    fn(b.dataset[attr]);
  }));
}

async function collectNow() {
  const btns = [$("collectBtn"), $("emptyCollect")];
  btns.forEach((b) => { b.disabled = true; b.textContent = "抓取中…"; });
  try {
    const r = await api("/api/collect", null, { method: "POST" });
    const label = { ok: "抓取成功", partial: "部分成功", failed: "抓取失败" }[r.status];
    toast(`${label}：${r.n_quotes} 条报价${r.errors.length ? " · " + r.errors[0] : ""}`, r.status === "failed");
    await loadAll();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btns.forEach((b) => { b.disabled = false; b.textContent = "立即抓取"; });
  }
}

function openManual() {
  const t = state.sel || (state.flex && state.flex.best);
  if (t) {
    $("mDepart").value = t.depart;
    $("mReturn").value = t.return;
  }
  $("mPrice").value = "";
  $("mNote").value = "";
  $("manualError").hidden = true;
  $("manualDlg").showModal();
  $("mPrice").focus();
}

async function saveManual(e) {
  e.preventDefault();
  try {
    await api("/api/manual-quote", null, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        origin: state.route.origin, dest: state.route.dest,
        depart: $("mDepart").value, ret: $("mReturn").value,
        price: +$("mPrice").value, note: $("mNote").value || null,
      }),
    });
    $("manualDlg").close();
    toast("已记录真实往返价");
    const dep = $("mDepart").value, ret = $("mReturn").value;
    if (!state.sel || state.sel.depart !== dep || state.sel.return !== ret) {
      state.sel = { depart: dep, return: ret, days: Math.round((toMs(ret) - toMs(dep)) / DAY), best_price: null,
        ctrip_url: ctripUrl(state.route.origin, state.route.dest, dep, ret) };
    }
    state.mode = "combo";
    document.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.mode === "combo")));
    loadHistory();
  } catch (err) {
    $("manualError").textContent = err.message;
    $("manualError").hidden = false;
  }
}

function effectiveDark() {
  const a = document.documentElement.getAttribute("data-theme");
  return a ? a === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
}
function rerenderCharts() {
  if (state.flex) renderPlanner();
  renderTrend();
}

let debounce;
function onFormChange() {
  clearTimeout(debounce);
  debounce = setTimeout(() => {
    const q = readForm();
    if (q && q.start.slice(0, 7) !== state.calMonth && state.calDir === "out") {
      state.calMonth = q.start.slice(0, 7);
      loadCal();
    }
    loadFlex();
  }, 350);
}

async function boot() {
  try { const th = localStorage.getItem("fr-theme"); if (th) document.documentElement.setAttribute("data-theme", th); } catch (e) { /* ignore */ }
  if (!window.echarts) {
    toast("图表库加载失败，请刷新页面", true);
    return;
  }
  trendChart = echarts.init($("trend"), null, { renderer: "svg" });
  heatChart = echarts.init($("heat"), null, { renderer: "svg" });
  addEventListener("resize", () => { trendChart.resize(); heatChart.resize(); });

  bindSeg("mode", (v) => { state.mode = v; renderTrend(); });
  bindSeg("days", (v) => { state.days = +v; renderTrend(); });
  bindSeg("cal", (v) => {
    state.calDir = v;
    const q = readForm();
    if (q) state.calMonth = (v === "out" ? q.start : addDays(q.start, q.min)).slice(0, 7);
    loadCal();
  });
  const shiftMonth = (n) => {
    const [y, m] = state.calMonth.split("-").map(Number);
    const d = new Date(Date.UTC(y, m - 1 + n, 1));
    state.calMonth = toStr(d.getTime()).slice(0, 7);
    loadCal();
  };
  $("calPrev").addEventListener("click", () => shiftMonth(-1));
  $("calNext").addEventListener("click", () => shiftMonth(1));
  ["fStart", "fEnd", "fMin", "fMax", "fIdeal"].forEach((id) => $(id).addEventListener("input", onFormChange));
  $("collectBtn").addEventListener("click", collectNow);
  $("emptyCollect").addEventListener("click", collectNow);
  $("manualBtn").addEventListener("click", openManual);
  $("manualForm").addEventListener("submit", saveManual);
  $("manualCancel").addEventListener("click", () => $("manualDlg").close());
  $("themeBtn").addEventListener("click", () => {
    const next = effectiveDark() ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("fr-theme", next); } catch (e) { /* ignore */ }
    rerenderCharts();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rerenderCharts);
  document.querySelectorAll("nav.tabs a").forEach((a) => a.addEventListener("click", () => {
    document.querySelectorAll("nav.tabs a").forEach((x) => x.classList.toggle("on", x === a));
  }));

  try {
    state.routes = await api("/api/routes");
  } catch (e) {
    toast("连不上服务：" + e.message, true);
    return;
  }
  const sel = $("routeSel");
  if (!state.routes.length) {
    sel.innerHTML = `<option>未配置航线</option>`;
    toast("config.yaml 里还没有配置航线", true);
    return;
  }
  sel.innerHTML = state.routes.map((r, i) => `<option value="${i}">${esc(r.origin)} ⇄ ${esc(r.dest)} · ${esc(r.name)}</option>`).join("");
  sel.addEventListener("change", () => { setRoute(state.routes[+sel.value]); loadAll(); });
  setRoute(state.routes[0]);
  await loadAll();
}

boot();
