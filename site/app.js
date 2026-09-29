/* Quantum Wire —— 前端渲染（无框架，无依赖） */
const TY2CLS = {
  "融资并购": "t-funding", "产品发布": "t-product", "财报业绩": "t-earnings",
  "合作签约": "t-partner", "技术进展": "t-tech",
};
const S = { type: "all", track: "all", cty: "all", q: "", view: "feed", onlyOfficial: true };
let ITEMS = [], COMP = {}, CTRY = {};

const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));

function today() {
  return new Date().toISOString().slice(0, 10);
}
function dayLabel(d) {
  if (!d) return "近期";
  const t = today();
  if (d === t) return "今天";
  const y = new Date(Date.now() - 864e5).toISOString().slice(0, 10);
  if (d === y) return "昨天";
  return d;
}

async function boot() {
  try {
    const [a, b] = await Promise.all([
      fetch("./data/items.json?t=" + Date.now()).then(r => r.json()),
      fetch("./data/companies.json?t=" + Date.now()).then(r => r.json()),
    ]);
    ITEMS = a.items || [];
    COMP = b.companies || {};
    CTRY = b.countries || {};
  } catch (e) {
    $("feed").innerHTML = '<div class="empty">数据加载失败。若为本地打开，请用 <code>python -m http.server</code> 起一个本地服务再访问。</div>';
    return;
  }
  buildFilters();
  renderStats();
  apply();
}

/* ---------- 筛选器 ---------- */
function chipRow(host, items, key) {
  const h = ['<button class="chip on" data-f="' + key + '" data-v="all">不限<span class="n">' + items.length + '</span></button>'];
  items.forEach(([v, n]) => {
    h.push('<button class="chip" data-f="' + key + '" data-v="' + esc(v) + '">' + esc(v) + '<span class="n">' + n + '</span></button>');
  });
  $(host).innerHTML = h.join("");
}

function tally(fn) {
  const m = {};
  ITEMS.forEach(i => { const k = fn(i); if (k) m[k] = (m[k] || 0) + 1; });
  return Object.entries(m).sort((a, b) => b[1] - a[1]);
}

function buildFilters() {
  chipRow("fType", tally(i => i.type), "type");
  chipRow("fTrack", tally(i => i.track), "track");
  const cc = tally(i => i.country).map(([k, v]) => [k, v]);
  chipRow("fCty", cc.map(([k, v]) => [k, v]), "cty");
  document.querySelectorAll(".chip").forEach(b => {
    b.onclick = () => { S[b.dataset.f] = b.dataset.v; apply(); };
  });
}

/* ---------- 统计 ---------- */
function renderStats() {
  const off = ITEMS.filter(i => i.tier === "official").length;
  $("s-items").textContent = ITEMS.length;
  $("s-official").textContent = off;
  $("s-co").textContent = Object.keys(COMP).length;
  $("s-cty").textContent = Object.keys(CTRY).length;
  const g = new Date().toISOString().replace("T", " ").slice(0, 16);
  $("upd").textContent = "数据更新于 " + g + " UTC · 每日自动采集两次";

  $("rank").innerHTML = tally(i => i.company).slice(0, 8).map(([k, v], x) => {
    const c = COMP[k];
    return '<div class="rk"><span class="n">' + (x + 1) + '</span><span class="nm">' +
      '<i style="display:inline-block;width:7px;height:7px;border-radius:2px;background:' + (c ? c.color : "#888") + ';margin-right:7px"></i>' +
      esc(k) + '</span><span class="c">' + v + '</span></div>' +
      '<div class="bar" style="width:' + (v / tally(i => i.company)[0][1] * 100) + '%"></div>';
  }).join("");

  const ct = tally(i => i.country);
  const mx = ct.length ? ct[0][1] : 1;
  $("ctylist").innerHTML = ct.map(([k, v]) => {
    const c = CTRY[k];
    return '<div class="tr"><span class="nm"><i style="background:' + (c ? c[2] : "#888") + '"></i>' +
      esc(c ? c[0] : k) + '</span><span class="b"><i style="width:' + (v / mx * 100) + '%"></i></span>' +
      '<span class="v">' + v + '</span></div>';
  }).join("");

  const tt = tally(i => i.track), tm = tt.length ? tt[0][1] : 1;
  $("tracks").innerHTML = tt.map(([k, v]) =>
    '<div class="tr"><span class="nm"><i style="background:var(--ac2)"></i>' + esc(k) + '</span>' +
    '<span class="b"><i style="width:' + (v / tm * 100) + '%"></i></span><span class="v">' + v + '</span></div>').join("");

  const st = tally(i => i.source), sm = st.length ? st[0][1] : 1;
  $("srclist").innerHTML = st.map(([k, v]) =>
    '<div class="tr"><span class="nm"><i style="background:var(--ac)"></i>' + esc(k) + '</span>' +
    '<span class="b"><i style="width:' + (v / sm * 100) + '%"></i></span><span class="v">' + v + '</span></div>').join("");
}

/* ---------- 信息流 ---------- */
function match(i) {
  if (S.onlyOfficial && i.tier !== "official") return false;
  if (S.type !== "all" && i.type !== S.type) return false;
  if (S.track !== "all" && i.track !== S.track) return false;
  if (S.cty !== "all" && i.country !== S.cty) return false;
  if (S.q) {
    const c = COMP[i.company] || {};
    const cy = CTRY[i.country];
    const hay = [i.title_en, i.summary_en, i.company, i.track, i.type, c.abbr, cy && cy[0], cy && cy[1]]
      .filter(Boolean).join(" ").toLowerCase();
    if (!hay.includes(S.q.toLowerCase())) return false;
  }
  return true;
}

function render() {
  const items = ITEMS.filter(match);
  if (!items.length) { $("feed").innerHTML = '<div class="empty">没有匹配的条目，试试放宽筛选条件</div>'; return; }
  const byDay = {};
  items.forEach(i => { (byDay[i.date || ""] = byDay[i.date || ""] || []).push(i); });
  let h = "";
  Object.keys(byDay).sort((a, b) => (b < a ? -1 : 1)).forEach(d => {
    const list = byDay[d];
    h += '<div class="daygroup"><div class="dhead"><h3>' + esc(dayLabel(d)) + '</h3>' +
      '<span class="cnt">' + list.length + ' 条</span><span class="rule"></span></div>';
    list.forEach(i => {
      const c = COMP[i.company] || {}, cy = CTRY[i.country];
      h += '<article class="item"><div class="itop">' +
        '<span class="co"><i style="background:' + (c.color || "#64748B") + '">' + esc((c.abbr || "··").slice(0, 2)) + '</i>' + esc(i.company || "未识别") + '</span>' +
        (cy ? '<span class="cty">' + esc(cy[0]) + '</span>' : "") +
        '<span class="tags">' +
        '<span class="tier ' + i.tier + '">' + (i.tier === "official" ? "一手发布" : "媒体线索") + '</span>' +
        '<span class="tag ' + (TY2CLS[i.type] || "t-tech") + '">' + esc(i.type) + '</span>' +
        (i.track ? '<span class="tag t-tech">' + esc(i.track) + '</span>' : "") +
        '</span></div>' +
        '<h4><a href="' + esc(i.url) + '" target="_blank" rel="noopener">' + esc(i.title_en) + '</a></h4>' +
        (i.summary_en ? '<p class="sm">' + esc(i.summary_en.slice(0, 240)) + '</p>' : "") +
        '<div class="ibot"><div class="meta">' +
        (c.city ? '<span>' + esc(c.city) + '</span><span class="dot"></span>' : "") +
        '<span>' + esc(i.date || "近期") + '</span><span class="dot"></span>' +
        '<span>' + esc(i.source) + '</span>' +
        '</div><a class="arrow" href="' + esc(i.url) + '" target="_blank" rel="noopener">原文 ↗</a></div></article>';
    });
    h += '</div>';
  });
  $("feed").innerHTML = h;
}

/* ---------- 企业库（按国家分组） ---------- */
function renderCompanies() {
  const keys = Object.keys(COMP).filter(k => {
    const c = COMP[k];
    if (S.cty !== "all" && c.country !== S.cty) return false;
    if (S.track !== "all" && c.track !== S.track) return false;
    if (S.q) {
      const cy = CTRY[c.country];
      const hay = [k, c.abbr, c.track, c.city, cy && cy[0], cy && cy[1]].filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(S.q.toLowerCase())) return false;
    }
    return true;
  });
  const cnt = {}; ITEMS.forEach(i => { if (i.company) cnt[i.company] = (cnt[i.company] || 0) + 1; });
  const last = {}; ITEMS.forEach(i => { if (i.company && (!last[i.company] || i.date > last[i.company])) last[i.company] = i.date; });
  const by = {}; keys.forEach(k => { (by[COMP[k].country] = by[COMP[k].country] || []).push(k); });
  const order = Object.keys(by).sort((a, b) => by[b].length - by[a].length);
  if (!order.length) { $("cgrid").innerHTML = '<div class="empty">没有匹配的企业</div>'; return; }
  let h = "";
  order.forEach(k => {
    const cy = CTRY[k], list = by[k];
    h += '<section class="csec"><div class="csec-h">' +
      '<span class="cbadge" style="background:' + (cy ? cy[2] : "#888") + '">' + esc(k) + '</span>' +
      '<div><a href="country/' + esc(k) + '.html"><b>' + esc(cy ? cy[0] : k) + '</b></a>' +
      '<div class="en2">' + esc(cy ? cy[1] : "") + '</div></div>' +
      '<a class="cn2" href="country/' + esc(k) + '.html">' + list.length + ' 家 →</a></div><div class="grid">';
    list.forEach(kk => {
      const c = COMP[kk];
      h += '<a class="ccard" href="company/' + esc(c.slug || "") + '.html"><div class="ch">' +
        '<i style="background:' + c.color + '">' + esc(c.abbr.slice(0, 2)) + '</i>' +
        '<div><b>' + esc(kk) + '</b><span>' + esc(c.abbr) + '</span></div></div>' +
        '<div class="desc">' + esc(c.city) + '<br>' + esc(c.track) + '</div>' +
        '<div class="kv"><span>收录条目</span><b>' + (cnt[kk] || 0) + '</b></div>' +
        '<div class="kv"><span>最近发布</span><b>' + esc(last[kk] || "—") + '</b></div></a>';
    });
    h += '</div></section>';
  });
  $("cgrid").innerHTML = h;
}

/* ---------- 交互 ---------- */
function apply() {
  document.querySelectorAll(".chip").forEach(b => b.classList.toggle("on", S[b.dataset.f] === b.dataset.v));
  if (S.view === "feed") render();
  else if (S.view === "companies") renderCompanies();
}
$("q").oninput = e => { S.q = e.target.value.trim(); apply(); };
$("onlyOfficial").onchange = e => { S.onlyOfficial = e.target.checked; apply(); };
document.querySelectorAll("nav.mainnav a").forEach(a => {
  a.onclick = ev => {
    ev.preventDefault();
    document.querySelectorAll("nav.mainnav a").forEach(x => x.classList.remove("on"));
    a.classList.add("on");
    S.view = a.dataset.view;
    $("viewFeed").classList.toggle("hidden", S.view !== "feed");
    $("viewCompanies").classList.toggle("hidden", S.view !== "companies");
    $("viewAbout").classList.toggle("hidden", S.view !== "about");
    $("filters").classList.toggle("hidden", S.view === "about");
    apply();
  };
});
$("theme").onclick = () => {
  const nx = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", nx);
  $("themeIcon").textContent = nx === "dark" ? "◐" : "◑";
};
boot();
