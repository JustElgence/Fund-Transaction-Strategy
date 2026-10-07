# Fund-Transaction-Strategy

> 基金（ETF）购买策略 + 股票购买策略 —— 统一整合的量化交易策略项目
>
> ⚠️ 本项目仅为量化策略框架与工具集合，**不构成任何投资建议**。市场有风险，投资需谨慎。

---

## 📁 项目结构

```
Fund-Transaction-Strategy/
├── README.md                      # 本文件（项目总览）
├── fund-strategy/                 # 💰 基金（ETF）购买策略
│   ├── STRATEGY.md                # ETF 网格 + 趋势跟踪混合策略 v1.1（完整规则）
│   ├── etf_daily_report.py        # 每日 ETF 策略报告生成脚本（Markdown）
│   ├── etf_dashboard.py           # 🆕 HTML 看板生成器（零第三方依赖）
│   ├── dashboard/index.html       # 🆕 单文件自包含看板（双击即开）
│   └── reports/                   # 每日报告输出样例（Markdown）
├── stock-strategy/                # 📈 股票购买策略
│   ├── STOCK_STRATEGY.md          # A股个股购买策略（选股/买卖点/风控）
│   ├── daily_stock_analysis.py    # 每日A股盘面分析 + TOP3 个股推荐脚本
│   └── examples/                  # 报告样例
├── skills-reference/              # 🧰 相关技能/工具文档（选股·监控·深度分析）
├── docs/                          # 补充文档（历史任务定义等）
└── .gitignore
```

---

## 💰 一、基金（ETF）购买策略

**`fund-strategy/STRATEGY.md` —— ETF 网格 + 趋势跟踪混合策略 v1.1**

- **标的池**：25 只 ETF，覆盖 A股宽基/成长/行业、港股、美股、债券、商品。
- **仓位铁律**：单只 ≤ 15%，总仓位 ≤ 90%（现金 ≥ 10%），每次只动 1 份。
- **趋势判断**：MA20 / MA60 划分多头 / 震荡 / 空头；MACD / RSI / 布林带辅助。
- **网格交易**：每只 ETF 拆 3 格（±3% / ±6% / ±9%），高抛低吸降低成本。
- **加仓/减仓**：回踩 MA20 + RSI 低位加仓；触上轨 + RSI 超买减仓。
- **风控**：网格单 -5% 止损、趋势单 -8% 止损、账户回撤 8%/12%/15% 分档降仓。
- **每日精选**：25 只全池打分，取 Top3（趋势 3 + 相对动量 3 + 位置 2 + 新鲜信号 2），类别分散 + 轮动。

```bash
# 生成每日 ETF 策略报告（Markdown，依赖 akshare）
python3 fund-strategy/etf_daily_report.py
# 报告输出到 fund-strategy/reports/etf_report_YYYY-MM-DD.md

# 🆕 生成 ETF 策略 HTML 看板（无需安装任何第三方包）
python3 fund-strategy/etf_dashboard.py
# 输出到 fund-strategy/dashboard/index.html
```

### 📺 HTML 看板（`fund-strategy/etf_dashboard.py`，v2.0）

- **零依赖**：只用 Python 标准库。日K 用腾讯 / 新浪，ETF 清单用新浪（东财兜底）。
- **标的池可扩展**：默认 **25 只核心池（STRATEGY.md 固定清单，强制保底）+ 全市场自动优选 35 只 = 60 只**。
  自动选池流程：拉全市场 ETF → 剔除货币/现金/债券类 → 同名指数去重 → 按成交额降序取候选 →
  抓 K 线后按近 60 日收益率相关性（>0.98）再去掉一批高重复标的。
  `--pool-size 100` 可放宽（实测 100 只全量重抓约 2~4 秒），`--core-only` 退回固定 25 只。
- **每 30 秒自动刷新实时价**：页面用腾讯 `qt.gtimg.cn` 的 `<script>` 接口（无 CORS 限制）
  定时拉取盘中报价，自动更新上证沪深300、Top3 卡片与候选池的价格/涨跌幅/时间戳。
  **流量控制**（针对有出口流量限制的部署）：
  - 登录成功前**不发起任何行情请求**（页面中也不含任何外部依赖，68KB 单文件全部本地）；
  - **标签页切到后台 / 最小化立即暂停**，切回来才恢复；
  - **离开页面（pagehide / beforeunload）彻底停表**，不会再有任何请求；
  - 上一轮请求未返回不会发下一轮，避免网络慢时堆积；
  - 右上角「锁定」按钮可随时停刷新并回到登录页；
  - 状态栏实时显示「已拉取 N 次 / 约 X KB」，用量可见。
  - ⚠️ 注意：行情请求是**访客浏览器 → 腾讯接口直连**，不经过你的服务器；
    服务器侧只承担页面本身（约 60KB / 次首次加载，后续可被浏览器缓存）。
    实测单次 14 只报价约 7.4KB，30 秒一次约 **0.9MB/小时**。若日后改成走自家服务端代理，上述控制策略同样生效。
- **前置密码门禁**：页面正文用 **PBKDF2-SHA256 + SHA256-CTR 流密钥整体加密**，
  浏览器原生 WebCrypto 解密 —— **拿到 HTML 文件本体也读不到内容**。
  首次运行自动生成本机随机口令（存 `dashboard/.secret.json`，已 gitignore），
  `--set-password` 可改，`--no-auth` 生成明文版，环境变量 `ETF_DASHBOARD_PASSWORD` 支持自动化。
- **K 线缓存**：同交易日复用本地缓存（秒开），跨日自动失效；`--force` 强制全量重抓，`--offline` 纯离线重绘。
- **提示**：若浏览器环境不支持 `crypto.subtle`（个别的 `file://` 场景），
  用 `python3 fund-strategy/etf_dashboard.py --serve` 起本地 http://127.0.0.1 服务打开。
- **页面模块**：市场情绪扫描 + Top3 推荐卡 + 候选池 Top10；颜色遵循 A 股习惯（涨红跌绿）。

---

## 📈 二、股票购买策略

**`stock-strategy/STOCK_STRATEGY.md` —— A股个股购买策略 v1.0**

- **选股流程**：采行情 → 硬性过滤 → 热点板块打分 → 板块内选 TOP3 → 生成买卖点。
- **硬性筛选**：涨幅 2%~8%、换手 >1%、成交额 >5000万、市值 >20亿、股价 >2元。
- **买卖点**：现价附近分仓 / 回踩 5 日线低吸 / 放量突破 20 日新高。
- **风控**：固定止损 -7%、跌破 MA20/MA60 减清仓、分批止盈 +5%/+8%/+12%、移动止盈。
- **仓位**：单只 ≤ 20%，同时 ≤ 5 只，总仓位随大盘情绪 3 成 ~ 7 成。
- **持仓监控**：7 大预警规则（成本% / 涨跌幅 / 量能 / 均线 / RSI / 跳空 / 动态止盈）。

```bash
# 生成每日A股盘面分析报告
python3 stock-strategy/daily_stock_analysis.py
```

---

## 🧰 三、相关工具 / 技能文档

`skills-reference/` 收录了支撑上述两套策略的技能与工具说明：

| 文件 | 内容 |
|------|------|
| `morning-stock-brief_SKILL.md` / `_WORKFLOW.md` | 早盘简报自动化（新闻+板块+选股+策略） |
| `daily-stock-report_SKILL.md` | 每日盘面报告（动态选 3 大热点方向） |
| `stock-deep-analysis_SKILL.md` / `_indicators.md` | 个股 8 维度深度分析 + 指标口径 |
| `stock-monitor_SKILL.md` | 7 大预警规则监控系统 |

---

## 🚀 快速开始

```bash
git clone <repo-url>
cd Fund-Transaction-Strategy

# 依赖：Python 3.9+，requests/akshare（如需 ETF 报告的数据源）
pip install akshare requests

python3 fund-strategy/etf_daily_report.py     # 基金策略报告
python3 stock-strategy/daily_stock_analysis.py # 股票策略报告
```

---

## ⚠️ 免责声明

本项目中所包含的一切策略、指标、脚本、报告均为**量化研究参考**，
基于公开市场数据生成，**不构成任何投资建议**。
据此操作，风险自担。股市/基金有风险，投资需谨慎。
