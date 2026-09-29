# Quantum Wire

全球量子科技企业**一手发布**的结构化索引。中英双语界面，按国家 / 地区、技术路线、事件类型检索，每条附原文链接。

> 只做索引，不转载、不翻译全文。所有内容著作权归原始发布方，流量导回原站。

## 架构：三层交叉，不单点依赖

| 层 | 数据源 | 作用 | 上站级别 |
|---|---|---|---|
| 核实层 | 企业官网 Newsroom、SEC EDGAR、PR Newswire | 企业一手发布 | `official`（默认展示） |
| 发现层 | Google News（行业关键词） | 发现哪家企业有动静 | `media`（可开关） |
| 兜底层 | 发现层有线索、核实层没拿到的 | 标记待补 | 不发布 |

任何一个源断掉，覆盖率不会归零。

## 目录

```
quantum-wire/
├─ scripts/collect.py            采集器（纯标准库，无第三方依赖）
├─ .github/workflows/collect.yml 每天 UTC 03:00 / 15:00 自动跑
├─ site/
│  ├─ index.html                 单页站点
│  ├─ app.js  style.css
│  └─ data/items.json            采集结果（由 Actions 提交）
│     data/companies.json        企业库 + 国家表
└─ README.md
```

## 本地运行

采集一次：

```bash
python scripts/collect.py --days 30
```

起本地服务预览站点（**不能直接双击 index.html**，因为要 fetch 本地 JSON）：

```bash
cd site && python -m http.server 8000
# 打开 http://localhost:8000
```

## 部署到 Vercel

1. 在 Vercel 选择 **Add New → Project**，导入这个 GitHub 仓库
2. **Root Directory** 填 `site`
3. **Framework Preset** 选 `Other`
4. **Build Command** 和 **Output Directory** 都留空
5. 点 Deploy

之后每次 Actions 提交新数据，Vercel 会自动重新部署。

## 已知边界（诚实清单）

- **企业库是种子数据**：当前 48 家覆盖主要玩家，全球量子初创约 400+ 家，持续扩充中
- **官网直抓会断**：网站改版即失效，需要长期维护。当前 10 家站点中 Rigetti 抽不到（页面结构问题）
- **标题与摘要仍是英文**：中文翻译尚未接入，界面与结构化字段（企业名、国家、技术路线、事件类型）已中文化
- **Google News 是媒体线索不是企业发布**：默认折叠，需手动开关才显示

## 下一步

1. 接入翻译（LLM API），把标题与摘要中文化
2. 每家企业单独页面 + 事件时间线（SEO 主力落地页）
3. 技术参数结构化数据库（比特数 / 保真度 / 融资额），这是最难被复制的资产
4. 源健康监控：某源连续 3 天 0 条就告警
5. 补全企业库至 120+ 家

## 合规

- 只收录标题、摘要、时间、来源与原文链接，不存储全文
- 遵守 robots.txt，采集频率每天不超过两次
- 国家和地区标注遵循一个中国原则，中国台湾 / 香港 / 澳门单列为地区条目
