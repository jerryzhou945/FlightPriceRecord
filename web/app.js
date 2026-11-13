// 航价记前端：读取 /api/*，渲染总览、走势、低价日历、弹性行程与提醒。
const $ = (id) => document.getElementById(id);
const DAY = 864e5;
const HOUR = 36e5;
const WD = "日一二三四五六";

const toMs = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
const toStr = (ms) => new Date(ms).toISOString().slice(0, 10);
const addDays = (s, n) => toStr(toMs(s) + n * DAY);
const md = (s) => `${+s.slice(5, 7)}/${+s.slice(8, 10)}`;
const wd = (s) => "周" + WD[new Date(toMs(s)).getUTCDay()];
const yuan = (n) => (n == null ? "—" : "¥" + Math.round(n).toLocaleString("en-US"));
const tok = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pad = (n) => String(n).padStart(2, "0");

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}
function fmtClock(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
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
// 航司标签：leg 是“去程 / 回程”这样的前缀，可不传
function airlineLabel(a) {
  return a.exact === false ? `参考航司 ${a.name} ${yuan(a.price)} 起` : a.name;
}
function airTag(a, leg) {
  const legHtml = leg ? `<span class="leg">${leg}</span>` : "";
  if (!a) return leg ? `<span class="air unknown">${legHtml}航司待确认</span>` : "";
  const label = esc(airlineLabel(a));
  return `<span class="air" title="${label}（${esc(a.code)}）"><b>${esc(a.code)}</b>${legHtml}${label}</span>`;
}
const airCode = (a) => (a ? `<span class="air-code" title="${esc(airlineLabel(a))}">${a.exact === false ? "参考 " : ""}${esc(a.code)}</span>` : "");

async function api(path, params, opts) {
  const qs = params ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== "")) : "";
  const res = await fetch(path + qs, opts);
  const body = res.status === 204 ? {} : await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    const err = new Error(typeof d === "string" ? d : `请求失败（${res.status}）`);
    err.status = res.status;
    throw err;
  }
  return body;
}
const sendJson = (path, method, data) =>
  api(path, null, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

let toastTimer;
function toast(msg, isErr = false) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 3600);
}

const state = {
  routes: [], route: null, flex: null, history: null, sel: null, alerts: null,
  mode: "combo", days: 7, calDir: "out", calMonth: null, status: null,
  view: "overview", lastRunId: null, lastAlertId: null,
};
let trendChart, heatChart;

/* ---------------- 页签 ---------------- */
const VIEWS = ["overview", "flex", "alerts", "runs"];
function showView(name, push = true) {
  if (!VIEWS.includes(name)) name = "overview";
  state.view = name;
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === name));
  if (push) history.replaceState(null, "", "#" + name);
  if (name === "alerts") { state.unseenAlerts = 0; renderBadge(); }
  // 图表在隐藏的容器里量不到尺寸，切到对应页签后再画
  requestAnimationFrame(() => {
    if (name === "overview" && trendChart) trendChart.resize();
    if (name === "flex") {
      if (!heatChart) heatChart = echarts.init($("heat"), null, { renderer: "svg" });
      if (state.flex) renderHeat();
      heatChart.resize();
    }
  });
  scrollTo({ top: 0 });
}
function renderBadge() {
  const b = $("alertBadge");
  b.hidden = !state.unseenAlerts;
  b.textContent = state.unseenAlerts || "";
}

/* ---------------- 航线与表单 ---------------- */
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
  $("heroRoute").textContent = route.name || `${o} ⇄ ${d}`;
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

/* ---------------- 读取 ---------------- */
async function loadStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    renderStatus();
    return s;
  } catch (e) {
    $("statusText").textContent = "连不上服务";
    $("statusDot").className = "dot bad";
    return null;
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
    renderHero();
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
  renderHero();
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

async function loadAlerts(announce = false) {
  try {
    const a = await api("/api/alerts", state.route ? { origin: state.route.origin, dest: state.route.dest } : null);
    const newest = a.log.length ? a.log[0].id : 0;
    if (announce && state.lastAlertId !== null && newest > state.lastAlertId) {
      const fresh = a.log.filter((l) => l.id > state.lastAlertId && l.status === "sent" && l.rule !== "test");
      if (fresh.length) {
        toast("🔔 " + (fresh[0].message || fresh[0].rule_label));
        if (state.view !== "alerts") { state.unseenAlerts = (state.unseenAlerts || 0) + fresh.length; renderBadge(); }
      }
    }
    state.lastAlertId = newest;
    state.alerts = a;
    renderAlerts(a);
  } catch (e) {
    $("watches").innerHTML = `<p class="hint">${esc(e.message)}</p>`;
  }
}

async function loadAll() {
  await loadStatus();
  await Promise.all([loadFlex(), loadCal(), loadAlerts()]);
}

// 每分钟看一眼：有新的抓取（定时器或任务计划抓的）就刷新数据
async function poll() {
  const s = await loadStatus();
  if (!s) return;
  const latest = s.runs.find((r) => r.status !== "running");
  const id = latest ? latest.id : null;
  if (state.lastRunId !== null && id !== state.lastRunId) {
    await Promise.all([loadFlex(), loadCal(), loadAlerts(true)]);
  }
  state.lastRunId = id;
}

/* ---------------- 渲染 ---------------- */
function baseText() {
  return { color: tok("--muted"), fontFamily: "Montserrat, PingFang SC, Microsoft YaHei, sans-serif" };
}
function tooltipStyle() {
  return { backgroundColor: tok("--glass-solid"), borderColor: tok("--stroke"), textStyle: { color: tok("--fg") }, extraCssText: "border-radius:12px;backdrop-filter:blur(12px);" };
}

function renderStatus() {
  const s = state.status;
  const dot = $("statusDot");
  if (s.collecting) {
    dot.className = "dot busy";
    $("statusText").textContent = "正在抓取…";
  } else if (!s.last_ok_at) {
    dot.className = "dot idle";
    $("statusText").textContent = "还没有抓取过";
  } else {
    dot.className = "dot" + (s.fail_streak ? " bad" : "");
    $("statusText").textContent = s.fail_streak ? `最近 ${s.fail_streak} 次抓取失败` : `上次抓取 ${relTime(s.last_ok_at)}`;
  }
  const hours = s.interval_minutes / 60;
  const every = hours === 1 ? "每小时" : Number.isInteger(hours) ? `每 ${hours} 小时` : `每 ${s.interval_minutes} 分钟`;
  const due = s.next_due_at ? new Date(s.next_due_at) : null;
  const overdue = !due || due.getTime() <= Date.now();
  const nextText = overdue ? "即将抓取" : fmtClock(due);
  const nextSub = overdue ? "已到抓取时间，航价记开着就会自动抓" : `大约 ${Math.max(1, Math.round((due - Date.now()) / 60000))} 分钟后`;
  $("kNext").textContent = every;
  $("kNextD").textContent = s.last_ok_at ? `上次 ${fmtClock(s.last_ok_at)} · 下次 ${nextText}` : "还没有抓取过";
  $("rInterval").textContent = every;
  $("rIntervalD").textContent = "航价记开着时自动抓取；也可注册系统定时任务";
  $("rNext").textContent = nextText;
  $("rNextD").textContent = nextSub;
  $("rTotal").textContent = s.n_runs.toLocaleString() + " 次";
  $("rTotalD").textContent = `${s.total_quotes.toLocaleString()} 条单程报价`;
  $("runsSub").textContent = s.last_ok_at ? `最近一次成功 ${fmtTime(s.last_ok_at)}` : "还没有成功的抓取";
  const labels = { ok: "成功", partial: "部分成功", failed: "失败", running: "进行中" };
  $("runsBody").innerHTML = s.runs.length
    ? s.runs.map((r) => `<tr>
        <td class="num">${fmtTime(r.started_at)}</td>
        <td><span class="pill ${esc(r.status)}">${labels[r.status] || esc(r.status)}</span></td>
        <td class="num price">${r.n_quotes}</td>
        <td class="wrap">${esc(r.error || "")}</td></tr>`).join("")
    : `<tr><td colspan="4" class="wrap">还没有抓取记录</td></tr>`;
  $("empty").hidden = s.total_quotes > 0;
}

function renderHero() {
  const t = state.sel, h = state.history, s = h && h.stats;
  const delta = $("kNowDelta");
  if (!t) {
    $("ovSub").textContent = state.flex && state.flex.observed_at ? "当前窗口没有可用组合，去“弹性行程”调整窗口或天数" : "还没有价格数据";
    ["kNow", "kPct", "kMin", "kMax"].forEach((id) => ($(id).textContent = "—"));
    $("heroAir").innerHTML = "";
    delta.hidden = true;
    ["heroLink", "heroWatch", "manualBtn"].forEach((id) => $(id).setAttribute("aria-disabled", "true"));
    return;
  }
  ["heroLink", "heroWatch", "manualBtn"].forEach((id) => $(id).removeAttribute("aria-disabled"));
  $("kNow").textContent = yuan(s ? s.current : t.best_price);
  $("ovSub").textContent = `${md(t.depart)} ${wd(t.depart)}出发 → ${md(t.return)} ${wd(t.return)}返回 · 玩 ${t.days} 天`;
  const outA = h ? h.out_airline : t.out_airline, retA = h ? h.ret_airline : t.ret_airline;
  const legs = h && h.legs;
  $("heroAir").innerHTML =
    airTag(outA, `去程${legs ? " " + yuan(legs.out) : ""} · `) + airTag(retA, `回程${legs ? " " + yuan(legs.ret) : ""} · `);
  $("heroLink").href = t.ctrip_url || ctripUrl(state.route.origin, state.route.dest, t.depart, t.return);
  if (s && s.change_24h != null && s.change_24h !== 0) {
    delta.hidden = false;
    delta.className = "delta " + (s.change_24h < 0 ? "down" : "up");
    delta.textContent = (s.change_24h < 0 ? "↓ " : "↑ ") + yuan(Math.abs(s.change_24h)) + " / 24h";
  } else {
    delta.hidden = true;
  }
  if (s && s.n > 1) {
    $("kPct").textContent = s.is_new_low ? "历史新低"
      : s.high === s.low ? "近期价格持平"
      : s.current >= s.high ? "近期最高点"
      : `比 ${s.cheaper_than_pct}% 的时间便宜`;
    $("kPctMark").style.left = (s.high === s.low ? 50 : ((s.current - s.low) / (s.high - s.low)) * 100) + "%";
    $("kMin").textContent = yuan(s.low);
    $("kMax").textContent = yuan(s.high);
  } else {
    $("kPct").textContent = "数据还少";
    $("kPctMark").style.left = "50%";
    $("kMin").textContent = "—";
    $("kMax").textContent = "—";
  }
}

function renderLegs() {
  const l = state.flex.leg_lows || {};
  $("kOut").textContent = l.out ? yuan(l.out.price) : "—";
  $("kOutD").innerHTML = l.out ? `${md(l.out.date)} ${wd(l.out.date)}出发 ${airTag(l.out.airline)}` : "暂无数据";
  $("kRet").textContent = l.ret ? yuan(l.ret.price) : "—";
  $("kRetD").innerHTML = l.ret ? `${md(l.ret.date)} ${wd(l.ret.date)}返回 ${airTag(l.ret.airline)}` : "暂无数据";
}

function renderPlanner() {
  const f = state.flex;
  if (!f.best) {
    $("bestV").textContent = "无数据";
    $("bestTrip").textContent = f.observed_at ? "调整窗口或天数试试" : "先点“立即抓取”";
    $("bestAir").innerHTML = "";
    $("bestLink").removeAttribute("href");
    $("bestWatch").disabled = true;
  } else {
    $("bestV").textContent = yuan(f.best.best_price);
    $("bestTrip").textContent = `${md(f.best.depart)} ${wd(f.best.depart)} → ${md(f.best.return)} ${wd(f.best.return)} · ${f.best.days} 天`;
    $("bestAir").innerHTML = airTag(f.best.out_airline, "去程 · ") + airTag(f.best.ret_airline, "回程 · ");
    $("bestLink").href = f.best.ctrip_url;
    $("bestWatch").disabled = false;
  }
  $("tips").innerHTML = f.suggestions.map((s) => `<li>${esc(s)}</li>`).join("");
  $("heatHint").textContent = f.observed_at
    ? `数据 ${fmtTime(f.observed_at)} · 估价 = 去程单程 + 回程单程 · 点格子选中该组合`
    : "还没有数据";
  if (heatChart) renderHeat();
  renderTop();
}

function renderHeat() {
  const f = state.flex;
  const o = state.route.origin, d = state.route.dest;
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
    series: [{ type: "heatmap", data: f.cells, itemStyle: { borderColor: tok("--heat-gap"), borderWidth: 2, borderRadius: 4 },
      emphasis: { itemStyle: { borderColor: tok("--accent"), borderWidth: 1.5 } } }],
  }, true);
  heatChart.off("click");
  heatChart.on("click", (p) => {
    const dep = dates[p.value[0]], s = stays[p.value[1]], ret = addDays(dep, s);
    const known = f.top.concat(f.by_days).find((t) => t.depart === dep && t.return === ret);
    select(known || { depart: dep, return: ret, days: s, best_price: p.value[2], ctrip_url: ctripUrl(o, d, dep, ret) });
  });
}

const BELL = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9a6 6 0 0 1 12 0c0 6 2.5 7 2.5 7h-17S6 15 6 9"/><path d="M10 20a2 2 0 0 0 4 0"/></svg>`;
function renderTop() {
  const f = state.flex;
  if (!f.top.length) {
    $("top").innerHTML = `<tr><td colspan="8" class="wrap">当前窗口没有可用报价</td></tr>`;
    return;
  }
  $("top").innerHTML = f.top.map((t, i) => {
    const sel = state.sel && state.sel.depart === t.depart && state.sel.return === t.return;
    return `<tr data-i="${i}" class="${sel ? "sel" : ""}" tabindex="0">
      <td class="num">${md(t.depart)} <span style="color:var(--faint)">${wd(t.depart)}</span></td>
      <td class="num">${md(t.return)} <span style="color:var(--faint)">${wd(t.return)}</span></td>
      <td class="num">${t.days}</td>
      <td class="num leg">${yuan(t.out)}${airCode(t.out_airline)}</td>
      <td class="num leg">${yuan(t.ret)}${airCode(t.ret_airline)}</td>
      <td class="num price">${yuan(t.price)}</td>
      <td class="num price" style="color:var(--down)">${t.rt_price ? yuan(t.rt_price) : ""}</td>
      <td class="act"><button class="bell" type="button" data-watch="${i}" title="给这个组合设到价提醒" aria-label="设到价提醒">${BELL}</button></td></tr>`;
  }).join("");
  $("top").querySelectorAll("tr[data-i]").forEach((tr) => {
    const pick = () => select(f.top[+tr.dataset.i]);
    tr.addEventListener("click", pick);
    tr.addEventListener("keydown", (e) => { if (e.target === tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pick(); } });
  });
  $("top").querySelectorAll("[data-watch]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    openWatch(f.top[+b.dataset.watch]);
  }));
}

function renderTrend() {
  if (!trendChart) return;
  const accent = tok("--accent"), down = tok("--down");
  let pts = [], hint, manual = [];
  if (state.mode === "combo") {
    const h = state.history;
    pts = h ? h.series.map(([t, p]) => [new Date(t).getTime(), p]) : [];
    manual = h ? h.manual.map((m) => [new Date(m.observed_at).getTime(), m.price, m.note || ""]) : [];
    hint = state.sel ? `${md(state.sel.depart)} → ${md(state.sel.return)} 的往返估价，每次抓取记录一个点` : "还没有选中组合";
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
  const span = pts.length > 1 ? pts[pts.length - 1][0] - pts[0][0] : 0;
  const hourly = span <= 2 * DAY;
  const target = state.route && state.route.target_price;
  const series = [{
    name: state.mode === "combo" ? "往返估价" : "窗口最低", type: "line", data: pts, showSymbol: pts.length < 3,
    symbolSize: 6, smooth: 0.12, lineStyle: { width: 2.2, color: accent }, itemStyle: { color: accent },
    areaStyle: { opacity: 0.28, color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: accent }, { offset: 1, color: "rgba(0,0,0,0)" }]) },
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
    grid: { left: 58, right: 14, top: 22, bottom: 28 },
    legend: manual.length ? { right: 0, top: 0, textStyle: { color: tok("--muted") }, itemWidth: 12, itemHeight: 8 } : undefined,
    tooltip: {
      trigger: "axis", ...tooltipStyle(), axisPointer: { lineStyle: { color: tok("--faint") } },
      formatter: (ps) => {
        const head = fmtTime(new Date(ps[0].value[0]).toISOString());
        const rows = ps.map((x) => {
          const extra = x.seriesName === "窗口最低" && x.value[2] ? ` <span style="opacity:.6">${md(x.value[2])}→${md(x.value[3])}</span>` : "";
          const note = x.seriesName === "手动核价" && x.value[2] ? ` <span style="opacity:.6">${esc(x.value[2])}</span>` : "";
          return `${x.marker}${x.seriesName} <b>${yuan(x.value[1])}</b>${extra}${note}`;
        });
        return [head, ...rows].join("<br>");
      },
    },
    xAxis: { type: "time", minInterval: hourly ? HOUR : DAY, axisLine: { lineStyle: { color: tok("--stroke") } }, axisTick: { show: false },
      axisLabel: { color: tok("--muted"), hideOverlap: true,
        formatter: (v) => { const d = new Date(v); return hourly ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${d.getMonth() + 1}/${d.getDate()}`; } },
      splitLine: { show: false } },
    yAxis: { type: "value", scale: true, splitNumber: 4,
      axisLabel: { color: tok("--muted"), formatter: (v) => yuan(v) },  // 小时级波动常常只有几十元，不能缩写成 k
      splitLine: { lineStyle: { color: tok("--grid") } } },
    series,
  }, true);
}

function renderCal(c, y, m) {
  const first = Date.UTC(y, m - 1, 1);
  const daysIn = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const byDate = Object.fromEntries(c.days.map((x) => [x.date, x]));
  const picked = state.sel ? (state.calDir === "out" ? state.sel.depart : state.sel.return) : null;
  let html = "一二三四五六日".split("").map((ch) => `<div class="wd">${ch}</div>`).join("");
  const lead = (new Date(first).getUTCDay() + 6) % 7;
  html += "<div></div>".repeat(lead);
  for (let i = 1; i <= daysIn; i++) {
    const ds = toStr(first + (i - 1) * DAY), x = byDate[ds];
    if (!x) {
      html += `<div class="day none" title="${ds} 暂无报价"><span class="d">${i}</span><span class="p num">—</span><span class="a"></span></div>`;
      continue;
    }
    let cls = "day" + (x.tier === 0 ? " t0" : x.tier === 4 ? " t4" : "");
    if (c.low && c.low.date === ds) cls += " best";
    if (ds === picked) cls += " picked";
    const a = x.airline;
    const title = `${ds} ${wd(ds)} ${yuan(x.price)} · ${a ? airlineLabel(a) + " " + a.code : "航司待确认"}`;
    html += `<a class="${cls}" href="${esc(x.ctrip_url)}" target="_blank" rel="noopener" title="${esc(title)}">
      <span class="d">${i}</span><span class="p num">${(x.price / 1000).toFixed(1)}k</span><span class="a">${a ? (a.exact === false ? "参考 " : "") + esc(a.code) : ""}</span></a>`;
  }
  $("cal").innerHTML = html;
}

function renderAlerts(a) {
  const s = a.settings;
  $("alertsSub").textContent = `当前通知方式：${a.channel_label}`;
  $("channelHint").textContent = a.channel_label || "";
  // 通知方式
  $("sDesktop").checked = s.desktop;
  $("sDesktop").disabled = !a.desktop_supported;
  if (!a.desktop_supported) $("desktopHint").textContent = "桌面通知目前只支持 Windows";
  const wx = { wxpusher: "WxPusher", pushplus: "PushPlus" }[a.channel];
  $("wechatPill").className = "pill " + (a.configured ? "ok" : "off");
  $("wechatPill").textContent = a.configured ? "已开启" : "未配置";
  $("wechatHint").textContent = a.configured
    ? `通过 ${wx} 推送到微信`
    : wx ? `config.yaml 选了 ${wx}，但 .env 里还没有填密钥` : "可选：在 config.yaml 和 .env 里配置后，手机微信也能收到（见 README）";
  // 规则
  const active = document.activeElement;
  const setVal = (id, v) => { if ($(id) !== active) $(id).value = v ?? ""; };
  setVal("sTarget", s.target_price);
  $("sNewLow").checked = s.new_low;
  setVal("sDrop", s.drop_percent);
  setVal("sFail", s.fail_streak);
  const [qs, qe] = s.quiet_hours ? s.quiet_hours.split("-") : ["", ""];
  $("sQuietOn").checked = !!s.quiet_hours;
  if (s.quiet_hours) { setVal("sQuietStart", qs); setVal("sQuietEnd", qe); }
  else { if (!$("sQuietStart").value) $("sQuietStart").value = "23:00"; if (!$("sQuietEnd").value) $("sQuietEnd").value = "08:00"; }
  $("rulesHint").textContent = state.route ? `针对 ${state.route.name} 弹性窗口里最便宜的组合` : "";
  // 到价提醒
  $("watches").innerHTML = a.watches.length
    ? a.watches.map((w) => {
        const state_ = !w.enabled ? ["off", "已暂停"] : w.hit ? ["hit", "已到价"] : w.current == null ? ["off", "暂无报价"] : ["ok", "监控中"];
        const gap = w.current != null && !w.hit ? ` · 还差 ${yuan(w.current - w.target_price)}` : "";
        return `<div class="watch ${w.enabled ? "" : "off"}" data-id="${w.id}">
          <div>
            <div class="t num">${md(w.depart)} ${wd(w.depart)} → ${md(w.return)} ${wd(w.return)} <span style="color:var(--faint);font-weight:400">· ${w.days} 天 · ${esc(w.origin)}⇄${esc(w.dest)}</span></div>
            <div class="s"><span class="pill ${state_[0]}">${state_[1]}</span>目标 ${yuan(w.target_price)}${gap} ${airCode(w.out_airline)} ${airCode(w.ret_airline)}
              <a href="${esc(w.ctrip_url)}" target="_blank" rel="noopener" style="color:var(--accent)">去携程 ↗</a></div>
          </div>
          <div class="now"><div class="v">${yuan(w.current)}</div><div class="s">当前估价</div></div>
          <span class="switch" title="${w.enabled ? "暂停" : "恢复"}这条提醒"><input type="checkbox" data-toggle ${w.enabled ? "checked" : ""} aria-label="启用提醒"><span></span></span>
          <button class="del" type="button" data-del title="删除这条提醒" aria-label="删除提醒">×</button>
        </div>`;
      }).join("")
    : `<p class="hint">还没有到价提醒。在总览或“弹性行程”里选一个组合，点“设置提醒”或 🔔 就能加。</p>`;
  $("watches").querySelectorAll(".watch").forEach((row) => {
    const id = row.dataset.id;
    row.querySelector("[data-toggle]").addEventListener("change", async (e) => {
      try { await sendJson(`/api/watches/${id}`, "PATCH", { enabled: e.target.checked }); } catch (err) { toast(err.message, true); }
      loadAlerts();
    });
    row.querySelector("[data-del]").addEventListener("click", async () => {
      try { await api(`/api/watches/${id}`, null, { method: "DELETE" }); toast("已删除提醒"); } catch (err) { toast(err.message, true); }
      loadAlerts();
    });
  });
  const labels = { sent: "已发送", failed: "失败", deferred: "延后" };
  $("alertLog").innerHTML = a.log.length
    ? a.log.map((l) => `<tr><td class="num">${fmtTime(l.sent_at)}</td><td>${esc(l.rule_label || l.rule)}</td>
        <td class="wrap">${esc(l.message || "")}</td><td><span class="pill ${esc(l.status)}">${labels[l.status] || esc(l.status)}</span></td></tr>`).join("")
    : `<tr><td colspan="4" class="wrap">还没有提醒记录</td></tr>`;
}

/* ---------------- 交互 ---------------- */
function select(t) {
  state.sel = t;
  renderTop();
  loadHistory();
  if (state.calDir) loadCal();
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
  $("statusDot").className = "dot busy";
  $("statusText").textContent = "正在抓取…";
  try {
    const r = await api("/api/collect", null, { method: "POST" });
    const label = { ok: "抓取成功", partial: "部分成功", failed: "抓取失败" }[r.status];
    toast(`${label}：${r.n_quotes} 条报价${r.errors.length ? " · " + r.errors[0] : ""}`, r.status === "failed");
    state.lastRunId = r.run_id;
    await loadStatus();
    await Promise.all([loadFlex(), loadCal(), loadAlerts(true)]);
  } catch (e) {
    toast(e.message, true);
    loadStatus();
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
    await sendJson("/api/manual-quote", "POST", {
      origin: state.route.origin, dest: state.route.dest,
      depart: $("mDepart").value, ret: $("mReturn").value,
      price: +$("mPrice").value, note: $("mNote").value || null,
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

function openWatch(trip) {
  const t = trip || state.sel || (state.flex && state.flex.best);
  if (!t) { toast("先抓取一次价格，再设置提醒", true); return; }
  const cur = t === state.sel && state.history && state.history.stats ? state.history.stats.current : (t.best_price ?? t.price);
  const existing = state.alerts && state.alerts.watches.find((w) => w.depart === t.depart && w.return === t.return && w.origin === state.route.origin && w.dest === state.route.dest);
  $("wDepart").value = t.depart;
  $("wReturn").value = t.return;
  // 默认比当前价低 3% 左右，取整到 50
  $("wPrice").value = existing ? existing.target_price : cur ? Math.floor((cur * 0.97) / 50) * 50 : "";
  $("watchNow").textContent = cur
    ? `当前往返估价 ${yuan(cur)}。降到目标价或更低时通知你${existing ? "（这个组合已有提醒，保存会更新目标价）" : ""}。`
    : "这个组合的往返估价降到目标价或更低时通知你。";
  $("watchError").hidden = true;
  $("watchDlg").showModal();
  $("wPrice").focus();
  $("wPrice").select();
}

async function saveWatch(e) {
  e.preventDefault();
  try {
    await sendJson("/api/watches", "POST", {
      origin: state.route.origin, dest: state.route.dest,
      depart: $("wDepart").value, ret: $("wReturn").value, target_price: +$("wPrice").value,
    });
    $("watchDlg").close();
    toast(`已设置提醒：低于 ${yuan(+$("wPrice").value)} 时通知你`);
    loadAlerts();
  } catch (err) {
    $("watchError").textContent = err.message;
    $("watchError").hidden = false;
  }
}

async function saveSettings(patch, okMsg) {
  try {
    await sendJson("/api/alerts/settings", "PUT", patch);
    if (okMsg) toast(okMsg);
    if ("target_price" in patch) {
      state.route.target_price = patch.target_price;
      renderTrend();
    }
  } catch (err) {
    toast(err.message, true);
  }
  loadAlerts();
}
function quietValue() {
  if (!$("sQuietOn").checked) return "";
  const a = $("sQuietStart").value, b = $("sQuietEnd").value;
  return a && b ? `${a}-${b}` : null;
}
function bindSettings() {
  $("sDesktop").addEventListener("change", (e) => saveSettings({ desktop: e.target.checked }, e.target.checked ? "桌面通知已开启" : "桌面通知已关闭"));
  $("sNewLow").addEventListener("change", (e) => saveSettings({ new_low: e.target.checked }));
  $("sTarget").addEventListener("change", (e) => {
    const v = e.target.value === "" ? null : +e.target.value;
    if (v !== null && !(v > 0)) { toast("目标价要大于 0", true); return; }
    saveSettings({ target_price: v, origin: state.route.origin, dest: state.route.dest }, v ? `目标价已设为 ${yuan(v)}` : "已取消目标价");
  });
  $("sDrop").addEventListener("change", (e) => saveSettings({ drop_percent: Math.max(0, +e.target.value || 0) }));
  $("sFail").addEventListener("change", (e) => saveSettings({ fail_streak: Math.max(0, Math.round(+e.target.value || 0)) }));
  const quiet = () => { const v = quietValue(); if (v !== null) saveSettings({ quiet_hours: v }); };
  ["sQuietOn", "sQuietStart", "sQuietEnd"].forEach((id) => $(id).addEventListener("change", quiet));
  $("testAlert").addEventListener("click", async () => {
    const b = $("testAlert");
    b.disabled = true;
    try {
      const r = await api("/api/alerts/test", null, { method: "POST" });
      toast(`测试提醒已发到：${r.channel_label}`);
    } catch (err) {
      toast(err.message, true);
    }
    b.disabled = false;
    loadAlerts();
  });
}

function rerenderCharts() {
  if (state.flex && heatChart) renderHeat();
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
  document.querySelectorAll("nav.tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  if (!window.echarts) {
    toast("图表库加载失败，请重新打开", true);
    return;
  }
  trendChart = echarts.init($("trend"), null, { renderer: "svg" });
  addEventListener("resize", () => { trendChart.resize(); if (heatChart) heatChart.resize(); });

  bindSeg("mode", (v) => { state.mode = v; renderTrend(); });
  bindSeg("days", (v) => { state.days = +v; renderTrend(); });
  bindSeg("cal", (v) => {
    state.calDir = v;
    const q = readForm();
    if (state.sel) state.calMonth = (v === "out" ? state.sel.depart : state.sel.return).slice(0, 7);
    else if (q) state.calMonth = (v === "out" ? q.start : addDays(q.start, q.min)).slice(0, 7);
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
  $("heroWatch").addEventListener("click", () => openWatch());
  $("bestWatch").addEventListener("click", () => openWatch(state.flex && state.flex.best));
  $("addWatch").addEventListener("click", () => openWatch());
  $("watchForm").addEventListener("submit", saveWatch);
  $("watchCancel").addEventListener("click", () => $("watchDlg").close());
  bindSettings();
  $("themeBtn").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("fr-theme", next); } catch (e) { /* ignore */ }
    rerenderCharts();
  });
  showView((location.hash || "#overview").slice(1), false);
  addEventListener("hashchange", () => showView((location.hash || "#overview").slice(1), false));

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
  const latest = state.status && state.status.runs.find((r) => r.status !== "running");
  state.lastRunId = latest ? latest.id : null;
  setInterval(poll, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
}

boot();
