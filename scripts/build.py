# -*- coding: utf-8 -*-
"""
Quantum Wire —— 静态页面生成器

把采集结果渲染成「内容内嵌」的静态页（不是 SPA 空壳），
每家企业、每个国家地区各一个可索引 URL，外加 sitemap 与 robots。

用法：python scripts/build.py [--base https://your-domain.com]
"""
import json
import os
import re
import html
import argparse
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
DATA = os.path.join(SITE, "data")

DEFAULT_BASE = "https://quantumwire.vercel.app"


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def slugify(abbr, used):
    s = re.sub(r"[^a-z0-9]+", "-", str(abbr).lower()).strip("-") or "co"
    base, i = s, 2
    while s in used:
        s = "%s-%d" % (base, i)
        i += 1
    used.add(s)
    return s


TPL = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canonical}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta name="robots" content="index,follow">
<link rel="stylesheet" href="../style.css">
<script type="application/ld+json">{ld}</script>
</head>
<body>
<header>
  <div class="wrap hrow">
    <div class="brand">
      <div class="mark"></div>
      <div><div class="bname">Quantum Wire</div><div class="bsub">量子企业发布索引</div></div>
    </div>
    <nav class="mainnav">
      <a href="../index.html">信息流</a>
      <a href="../index.html#companies">企业库</a>
      <a href="../specs.html">技术参数</a>
      <a href="../index.html#about">关于</a>
    </nav>
    <div class="tools">
      <button class="tbtn" onclick="(function(d){{d.documentElement.setAttribute('data-theme',d.documentElement.getAttribute('data-theme')==='dark'?'light':'dark')}})(document)">◐</button>
    </div>
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer>
  <div class="wrap">
    <div class="fwrap">
      <div><strong>Quantum Wire</strong><br>全球量子科技企业发布索引</div>
      <div>核实层：企业官网 / SEC EDGAR / PR 通讯社<br>发现层：Google News</div>
      <div>每日自动更新两次<br><span style="opacity:.8">静态站点 · 无追踪</span></div>
    </div>
    <div class="note">本站仅提供标题、摘要与原文链接的索引服务，不存储、不转载、不翻译全文。所有内容著作权归原始发布方所有，点击「原文」将跳转至发布方网站。</div>
  </div>
</footer>
</body>
</html>
"""


def timeline(items, limit=None):
    """事件时间线"""
    ls = items if limit is None else items[:limit]
    if not ls:
        return '<div class="empty">暂无收录条目</div>'
    h = ['<ol class="tl">']
    for i in ls:
        tag = "一手发布" if i.get("tier") == "official" else "媒体线索"
        h.append(
            '<li class="tl-i"><div class="tl-d">%s</div>'
            '<div class="tl-c"><div class="tl-t"><a href="%s" target="_blank" rel="noopener">%s</a></div>'
            '<div class="tl-m"><span class="tl-tag %s">%s</span>'
            '<span class="tl-tag">%s</span>'
            '<span class="tl-tag">%s</span>'
            '<span class="tl-src">%s</span></div></div></li>'
            % (esc(i.get("date") or "近期"), esc(i.get("url")), esc(i.get("title_en")),
               ("t-official" if i.get("tier") == "official" else "t-media"), tag,
               esc(i.get("type")), esc(i.get("track") or "—"),
               esc(i.get("source"))))
    h.append("</ol>")
    return "".join(h)


def agg_specs(items):
    """企业维度汇总技术参数：数值类取最大（保守），金额按时间倒序保留全部"""
    agg = {}
    for i in items:
        co = i.get("company")
        if not co or not i.get("specs"):
            continue
        a = agg.setdefault(co, {"qubits": None, "logical_qubits": None, "fidelity": None,
                                "error_rate": None, "amounts": [], "evidence": {}})
        sp = i["specs"]
        for k in ("qubits", "logical_qubits", "fidelity"):
            v = sp.get(k)
            if v is None:
                continue
            cur = a.get(k)
            if cur is None or v > cur:
                a[k] = v
                a["evidence"][k] = {"title": i.get("title_en"), "url": i.get("url"),
                                    "date": i.get("date"), "source": i.get("source"),
                                    "ctx": (i.get("spec_ctx") or {}).get(k, "")}
        if "amount" in sp:
            m = sp["amount"]
            a["amounts"].append({"value": m["value"], "currency": m["currency"], "raw": m["raw"],
                                 "title": i.get("title_en"), "url": i.get("url"),
                                 "date": i.get("date"), "source": i.get("source")})
    for a in agg.values():
        a["amounts"].sort(key=lambda x: x.get("date") or "", reverse=True)
    return agg


def fmt_money(v, cur):
    if v >= 1e9:
        return "%s%.2fB" % (cur, v / 1e9)
    if v >= 1e6:
        return "%s%.0fM" % (cur, v / 1e6)
    return "%s%.0f" % (cur, v)


def fmt_num(v):
    if v is None:
        return "—"
    return ("%g" % v)


def spec_block(name, sp):
    """企业页的技术参数区块"""
    if not sp:
        return ""
    rows = []
    if sp.get("qubits") is not None:
        rows.append(("量子比特", fmt_num(sp["qubits"]), "qubits"))
    if sp.get("logical_qubits") is not None:
        rows.append(("逻辑比特", fmt_num(sp["logical_qubits"]), "logical_qubits"))
    if sp.get("fidelity") is not None:
        rows.append(("保真度", fmt_num(sp["fidelity"]) + "%", "fidelity"))
    if not rows and not sp.get("amounts"):
        return ""
    h = ['<div class="ph">技术参数（自动抽取）</div>']
    if rows:
        h.append('<div class="specgrid">')
        for label, val, key in rows:
            ev = (sp.get("evidence") or {}).get(key) or {}
            link = ('<a class="specev" href="%s" target="_blank" rel="noopener">%s · %s</a>'
                    % (esc(ev.get("url")), esc(ev.get("source")), esc(ev.get("date") or ""))
                    ) if ev.get("url") else ""
            ctxhtml = ('<div class="specctx">原文语境：…%s…</div>'
                       % esc((ev.get("ctx") or "")[:150])) if ev.get("ctx") else ""
            h.append('<div class="speccard"><span class="sp-k">%s</span>'
                     '<b class="sp-v">%s</b>%s%s</div>'
                     % (esc(label), esc(val), ctxhtml, link))
        h.append("</div>")
    if sp.get("amounts"):
        h.append('<div class="ph2">公开披露金额</div><div class="amtlist">')
        for m in sp["amounts"][:6]:
            h.append('<a class="amtrow" href="%s" target="_blank" rel="noopener">'
                     '<span class="amt-v">%s</span>'
                     '<span class="amt-t">%s</span>'
                     '<span class="amt-d">%s</span></a>'
                     % (esc(m.get("url")), esc(fmt_money(m["value"], m["currency"])),
                        esc((m.get("title") or "")[:110]), esc(m.get("date") or "")))
        h.append("</div>")
    h.append('<div class="disclaim">参数由程序从发布标题与摘要中自动抽取，'
             '仅反映公开披露口径，可能不完整或存在误差。点击任意条目可核对原文。</div>')
    return "".join(h)


def crumb(*parts):
    h = ['<div class="crumb">']
    h.append('<a href="../index.html">首页</a>')
    for p in parts:
        if len(p) == 2:
            h.append('<span class="sep">/</span><a href="%s">%s</a>' % (esc(p[1]), esc(p[0])))
        else:
            h.append('<span class="sep">/</span><span>%s</span>' % esc(p[0]))
    h.append("</div>")
    return "".join(h)


def statbox(pairs):
    h = ['<div class="stats">']
    for v, k in pairs:
        h.append('<div class="stat"><b>%s</b><span>%s</span></div>' % (esc(v), esc(k)))
    h.append("</div>")
    return "".join(h)


def build(base):
    with open(os.path.join(DATA, "items.json"), encoding="utf-8") as f:
        payload = json.load(f)
    with open(os.path.join(DATA, "companies.json"), encoding="utf-8") as f:
        cd = json.load(f)
    items = payload.get("items", [])
    COMP = cd.get("companies", {})
    CTRY = cd.get("countries", {})

    by_co = {}
    for i in items:
        if i.get("company"):
            by_co.setdefault(i["company"], []).append(i)
    for k in by_co:
        by_co[k].sort(key=lambda x: x.get("date") or "", reverse=True)

    SPECS = agg_specs(items)
    with open(os.path.join(DATA, "specs.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": payload.get("generated_at"), "companies": SPECS},
                  f, ensure_ascii=False, indent=1)

    # slug 写回 companies.json，供首页前端链接使用
    used = set()
    for name in COMP:
        COMP[name]["slug"] = slugify(COMP[name]["abbr"], used)
    with open(os.path.join(DATA, "companies.json"), "w", encoding="utf-8") as f:
        json.dump(cd, f, ensure_ascii=False, indent=1)

    os.makedirs(os.path.join(SITE, "company"), exist_ok=True)
    os.makedirs(os.path.join(SITE, "country"), exist_ok=True)

    urls = [(base + "/", "1.0")]

    # ---------- 企业页 ----------
    for name, c in COMP.items():
        slug = c["slug"]
        cy = CTRY.get(c["country"], [c["country"], c["country"], "#888"])
        own = by_co.get(name, [])
        off = [i for i in own if i.get("tier") == "official"]
        last = own[0]["date"] if own else "—"
        srcs = {}
        for i in own:
            srcs[i["source"]] = srcs.get(i["source"], 0) + 1

        # 相关企业：同技术路线
        rel = [(n, cc) for n, cc in COMP.items()
               if n != name and cc.get("track") == c.get("track")]
        rel = sorted(rel, key=lambda kv: -len(by_co.get(kv[0], [])))[:8]

        body = [crumb((cy[0], "../country/%s.html" % c["country"]), (name,))]
        body.append('<div class="phead">')
        body.append('<span class="pbadge" style="background:%s">%s</span>' % (esc(c["color"]), esc(c["abbr"][:3])))
        body.append('<div><h1>%s</h1><div class="pen">%s · %s · %s</div></div>'
                    % (esc(name), esc(cy[0]), esc(c.get("city", "")), esc(c.get("track", ""))))
        body.append("</div>")
        body.append(statbox([(len(own), "收录条目"), (len(off), "一手发布"),
                             (last, "最近发布"), (len(srcs), "数据来源")]))

        if srcs:
            body.append('<div class="ph">数据来源构成</div><div class="srcrow">')
            for k, v in sorted(srcs.items(), key=lambda x: -x[1]):
                body.append('<span class="srcpill">%s · %d</span>' % (esc(k), v))
            body.append("</div>")

        body.append(spec_block(name, SPECS.get(name)))
        body.append('<div class="ph">发布记录时间线</div>')
        body.append(timeline(own))

        if rel:
            body.append('<div class="ph">同技术路线的其他企业</div><div class="relgrid">')
            for n, cc in rel:
                body.append('<a class="relcard" href="%s.html">'
                            '<i style="background:%s">%s</i>'
                            '<b>%s</b><span>%s · %d 条</span></a>'
                            % (esc(cc["slug"]), esc(cc["color"]), esc(cc["abbr"][:2]),
                               esc(n), esc(CTRY.get(cc["country"], [cc["country"]])[0]),
                               len(by_co.get(n, []))))
            body.append("</div>")

        body.append('<div class="disclaim">本页内容由 Quantum Wire 自动采集与整理，仅提供标题、摘要与原文链接。'
                    '点击条目标题可跳转至发布方原始页面，著作权归原发布方所有。</div>')

        desc = "%s（%s · %s）的量子科技发布记录索引，收录 %d 条一手信息与动态。" % (
            name, cy[0], c.get("track", ""), len(own))
        ld = json.dumps({
            "@context": "https://schema.org", "@type": "Organization",
            "name": name, "address": {"addressCountry": c["country"], "addressLocality": c.get("city", "")},
            "description": desc,
        }, ensure_ascii=False)

        out = TPL.format(title="%s · 量子科技发布记录 | Quantum Wire" % esc(name),
                         desc=esc(desc), canonical="%s/company/%s.html" % (base, slug),
                         ld=ld, body="".join(body))
        with open(os.path.join(SITE, "company", slug + ".html"), "w", encoding="utf-8") as f:
            f.write(out)
        urls.append(("%s/company/%s.html" % (base, slug), "0.8"))

    # ---------- 国家 / 地区页 ----------
    for code, cy in CTRY.items():
        cos = {n: c for n, c in COMP.items() if c["country"] == code}
        if not cos:
            continue
        citems = []
        for n in cos:
            citems.extend(by_co.get(n, []))
        citems.sort(key=lambda x: x.get("date") or "", reverse=True)
        tracks = {}
        for n, c in cos.items():
            tracks[c["track"]] = tracks.get(c["track"], 0) + 1

        body = [crumb((cy[0],))]
        body.append('<div class="phead">')
        body.append('<span class="pbadge" style="background:%s">%s</span>' % (esc(cy[2]), esc(code)))
        body.append('<div><h1>%s 量子科技企业</h1><div class="pen">%s · Quantum companies in %s</div></div>'
                    % (esc(cy[0]), esc(cy[1]), esc(cy[1])))
        body.append("</div>")
        body.append(statbox([(len(cos), "收录企业"), (len(citems), "发布条目"),
                             (len(tracks), "技术路线"), (citems[0]["date"] if citems else "—", "最近发布")]))

        if tracks:
            body.append('<div class="ph">技术路线分布</div><div class="srcrow">')
            for k, v in sorted(tracks.items(), key=lambda x: -x[1]):
                body.append('<span class="srcpill">%s · %d</span>' % (esc(k), v))
            body.append("</div>")

        body.append('<div class="ph">收录企业</div><div class="relgrid">')
        for n, c in sorted(cos.items(), key=lambda kv: -len(by_co.get(kv[0], []))):
            body.append('<a class="relcard" href="../company/%s.html">'
                        '<i style="background:%s">%s</i><b>%s</b>'
                        '<span>%s · %d 条</span></a>'
                        % (esc(c["slug"]), esc(c["color"]), esc(c["abbr"][:2]), esc(n),
                           esc(c["track"]), len(by_co.get(n, []))))
        body.append("</div>")

        body.append('<div class="ph">%s 企业最近发布</div>' % esc(cy[0]))
        body.append(timeline(citems, 25))

        desc = "%s量子科技企业发布索引：收录 %d 家企业、%d 条发布记录，覆盖 %s 等技术路线。" % (
            cy[0], len(cos), len(citems), "、".join(list(tracks)[:4]))
        ld = json.dumps({"@context": "https://schema.org", "@type": "CollectionPage",
                         "name": "%s 量子科技企业" % cy[0], "description": desc}, ensure_ascii=False)
        out = TPL.format(title="%s 量子科技企业发布索引 | Quantum Wire" % esc(cy[0]),
                         desc=esc(desc), canonical="%s/country/%s.html" % (base, code),
                         ld=ld, body="".join(body))
        with open(os.path.join(SITE, "country", code + ".html"), "w", encoding="utf-8") as f:
            f.write(out)
        urls.append(("%s/country/%s.html" % (base, code), "0.7"))

    # ---------- 技术参数对比页 ----------
    by_track = {}
    for name, sp in SPECS.items():
        c = COMP.get(name)
        if not c:
            continue
        if not any([sp.get("qubits"), sp.get("logical_qubits"),
                    sp.get("fidelity"), sp.get("amounts")]):
            continue
        by_track.setdefault(c["track"], []).append((name, c, sp))
    n_with = sum(len(v) for v in by_track.values())

    def cell(sp, key, suffix=""):
        v = sp.get(key)
        if v is None:
            return "—"
        ev = (sp.get("evidence") or {}).get(key) or {}
        s = fmt_num(v) + suffix
        if ev.get("url"):
            return '<a href="%s" target="_blank" rel="noopener">%s</a>' % (esc(ev["url"]), esc(s))
        return esc(s)

    body = [crumb(("技术参数",))]
    body.append('<div class="phead"><span class="pbadge" style="background:#5EEAD4">SPEC</span>'
                '<div><h1>量子企业技术参数对比</h1>'
                '<div class="pen">从企业公开发布中自动抽取 · 每条数值可点击核对原文</div></div></div>')
    body.append(statbox([(n_with, "有参数企业"),
                         (sum(1 for s in SPECS.values() if s.get("qubits") is not None), "披露比特数"),
                         (sum(1 for s in SPECS.values() if s.get("logical_qubits") is not None), "披露逻辑比特"),
                         (sum(1 for s in SPECS.values() if s.get("amounts")), "披露金额")]))
    body.append('<div class="disclaim" style="margin-top:0;margin-bottom:6px">'
                '参数由程序从企业公开发布的标题与摘要中自动抽取，只反映<strong>公开披露口径</strong>，'
                '不同企业口径未必可比，也不代表实测性能。点击数值可跳转原文核对。</div>')

    for track in sorted(by_track, key=lambda t: -len(by_track[t])):
        rows = sorted(by_track[track], key=lambda r: -(r[2].get("qubits") or 0))
        body.append('<div class="ph">%s · %d 家</div>' % (esc(track), len(rows)))
        body.append('<div class="tblwrap"><table class="spectbl"><thead><tr>'
                    '<th>企业</th><th>国家 / 地区</th><th>量子比特</th><th>逻辑比特</th>'
                    '<th>保真度</th><th>最近披露金额</th></tr></thead><tbody>')
        for name, c, sp in rows:
            cy = CTRY.get(c["country"], [c["country"]])
            amt = "—"
            if sp.get("amounts"):
                m = sp["amounts"][0]
                amt = '<a href="%s" target="_blank" rel="noopener">%s</a>' % (
                    esc(m["url"]), esc(fmt_money(m["value"], m["currency"])))
            body.append('<tr><td><a href="company/%s.html"><b>%s</b></a></td><td>%s</td>'
                        '<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                        % (esc(c["slug"]), esc(name), esc(cy[0]),
                           cell(sp, "qubits"), cell(sp, "logical_qubits"),
                           cell(sp, "fidelity", "%"), amt))
        body.append("</tbody></table></div>")

    desc = "量子科技企业技术参数对比：量子比特数、逻辑比特、保真度与公开披露金额，每条数值可追溯到原文。"
    ld = json.dumps({"@context": "https://schema.org", "@type": "Dataset",
                     "name": "量子企业技术参数对比", "description": desc}, ensure_ascii=False)
    out = TPL.format(title="量子企业技术参数对比 | Quantum Wire", desc=esc(desc),
                     canonical="%s/specs.html" % base, ld=ld, body="".join(body))
    with open(os.path.join(SITE, "specs.html"), "w", encoding="utf-8") as f:
        f.write(out)
    urls.append((base + "/specs.html", "0.9"))

    # ---------- sitemap / robots ----------
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u, p in urls:
        sm.append("  <url><loc>%s</loc><lastmod>%s</lastmod><priority>%s</priority></url>"
                  % (esc(u), now, p))
    sm.append("</urlset>")
    with open(os.path.join(SITE, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(sm))

    with open(os.path.join(SITE, "robots.txt"), "w", encoding="utf-8") as f:
        f.write("User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % base)

    n_co = len([1 for c in COMP.values() if c.get("slug")])
    n_cty = len([1 for cy in CTRY if any(c["country"] == cy for c in COMP.values())])
    print("生成完成：企业页 %d 个，国家页 %d 个，sitemap %d 条 URL"
          % (n_co, n_cty, len(urls)))
    print("输出目录：%s" % SITE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("QW_BASE", DEFAULT_BASE),
                    help="站点根地址，用于 canonical 与 sitemap")
    build(ap.parse_args().base.rstrip("/"))


if __name__ == "__main__":
    main()
