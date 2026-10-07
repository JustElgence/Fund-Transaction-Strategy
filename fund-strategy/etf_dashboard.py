#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 网格 + 趋势跟踪混合策略 —— HTML 看板生成器 v2.0

v2.0 更新（相对 v1.0）：
  1. 标的池从固定 25 只扩到 **25 只核心池 + 全市场自动选池**（默认 Top60）：
       东财拉全市场 ETF → 剔除货币/现金/债券类 → 同名去重（同标的只留成交额最大的）
       → 按成交额排序取 TopN。核心池强制保底，保证与原 STRATEGY.md 口径连续。
     可用 --pool-size N 调整，--core-only 退回只用核心 25 只。
  2. K 线缓存升级：同日缓存可直接复用（命中秒开），跨交易日自动失效，缺哪只补哪只；
     --force 强制全量重抓。
  3. 页面每 30 秒自动刷新实时价：用腾讯 qt.gtimg.cn 的 <script> 接口（无 CORS 限制）
     拉取盘中实时报价，自动更新大盘、Top3 卡片与候选池的价格/涨跌幅/时间戳。
  4. 前置密码门禁：页面正文用 PBKDF2-SHA256 + SHA256-CTR 流密钥**整体加密**，
     浏览器侧用原生 WebCrypto 解密。**拿到 HTML 文件本体也读不到内容**。
     （不需要密码：--no-auth 输出明文页）

设计要点：
  * 零第三方依赖：只用 Python 标准库（urllib / json / hashlib / math）。
  * 数据源：日K 用腾讯（前复权），失败降级新浪；实时价用腾讯 qt.gtimg.cn。
  * 指标与打分口径完全对齐 fund-strategy/STRATEGY.md v1.1 / etf_daily_report.py。
  * 输出单文件自包含 HTML（fund-strategy/dashboard/index.html），无 CDN 依赖。

用法：
    python fund-strategy/etf_dashboard.py                 # 生成看板（首次会要求设置口令）
    python fund-strategy/etf_dashboard.py --pool-size 100 # 扩到 100 只
    python fund-strategy/etf_dashboard.py --core-only     # 只用核心 25 只
    python fund-strategy/etf_dashboard.py --force         # 忽略当日缓存，全量重抓
    python fund-strategy/etf_dashboard.py --offline       # 不联网，用缓存重绘
    python fund-strategy/etf_dashboard.py --set-password  # 修改口令
    python fund-strategy/etf_dashboard.py --no-auth       # 不加密码（明文页）
    python fund-strategy/etf_dashboard.py --serve         # 生成后起本地服务并打开浏览器
    环境变量 ETF_DASHBOARD_PASSWORD 可用于自动化免交互。

⚠️ 仅用于策略研究参考，不构成投资建议。
"""
import os
import sys
import json
import math
import gzip
import base64
import hashlib
import secrets
import getpass
import urllib.request
import webbrowser
import http.server
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- 路径配置
BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "dashboard")
OUT_HTML = os.path.join(OUT_DIR, "index.html")
CACHE_PATH = os.path.join(OUT_DIR, ".kline_cache.json")
STATE_PATH = os.path.join(OUT_DIR, ".state.json")
SECRET_PATH = os.path.join(OUT_DIR, ".secret.json")

DAYS = 200                 # 日K 抓取天数（>=70 才能算 MA60）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 15
PBKDF2_ITER = 150000
AUTH_TAG = b"ETF-DASH-V1"
FILTER_BUFFER = 30             # 自动选池时多取的候选数（用于相关性去重）
REFRESH_SECONDS = 30           # 页面实时价刷新间隔（登录成功后才启动）

# ---------------------------------------------------------------- 核心池（与 STRATEGY.md 一致，强制保底）
CORE_POOL = [
    ("510300", "沪深300ETF", "A股宽基", "sh"),
    ("510050", "上证50ETF", "A股宽基", "sh"),
    ("510500", "中证500ETF", "A股宽基", "sh"),
    ("512100", "中证1000ETF", "A股宽基", "sh"),
    ("588000", "科创50ETF", "A股成长", "sh"),
    ("159915", "创业板ETF", "A股成长", "sz"),
    ("512880", "证券ETF", "A股行业", "sh"),
    ("512760", "芯片ETF", "A股行业", "sh"),
    ("512660", "军工ETF", "A股行业", "sh"),
    ("512010", "医药ETF", "A股行业", "sh"),
    ("512170", "医疗ETF", "A股行业", "sh"),
    ("159928", "消费ETF", "A股行业", "sz"),
    ("512690", "酒ETF", "A股行业", "sh"),
    ("510880", "红利ETF", "A股行业", "sh"),
    ("515790", "光伏ETF", "A股行业", "sh"),
    ("515030", "新能源车ETF", "A股行业", "sh"),
    ("516160", "新能源ETF", "A股行业", "sh"),
    ("513180", "恒生科技ETF", "港股", "sh"),
    ("159920", "恒生ETF", "港股", "sz"),
    ("513050", "中概互联ETF", "港股", "sh"),
    ("513500", "标普500ETF", "美股", "sh"),
    ("513100", "纳指100ETF", "美股", "sh"),
    ("159941", "纳指ETF", "美股", "sz"),
    ("511010", "国债ETF", "债券", "sh"),
    ("518880", "黄金ETF", "商品", "sh"),
]

# 自动选池：默认排除货币/现金/短债类（日内套利品种不适合本策略）
EXCLUDE_KW = ("货币", "现金添富", "添益", "日利", "保证金", "逆回购", "短融",
              "同业存单", "人民币", "国债逆")
EXCLUDE_PREFIX = ("5118", "5119", "1590")
BOND_KW = ("债", "国债", "城投", "政金", "转债", "信用", "利率", "利息",
           "久期", "30年", "十年", "五年", "地方政府")
FUND_COMPANIES = (
    "华夏基金", "易方达", "广发基金", "华泰柏瑞", "南方基金", "嘉实基金", "天弘基金",
    "博时基金", "汇添富", "工银瑞信", "大成基金", "国泰基金", "银华基金", "华安基金",
    "富国基金", "招商基金", "平安基金", "鹏华基金", "万家基金", "建信基金", "华宝基金",
    "兴证全球", "景顺长城", "浦银安盛", "上投摩根", "摩根基金", "诺安基金", "融通基金",
    "银河基金", "国联基金", "中信保诚", "中欧基金", "财通基金", "申万菱信", "长盛基金",
    "光大保德信", "海富通", "金鹰基金", "前海开源", "国寿安保", "泰康资产", "方正富邦",
    "西部利得", "创金合信", "永赢基金", "中信建投", "太平基金", "中金基金", "安信基金",
    "国投瑞银", "中银基金", "交银施罗德", "华夏", "广发", "南方", "嘉实", "天弘", "博时",
    "大成", "国泰", "银华", "华安", "富国", "招商", "平安", "鹏华", "万家", "建信",
    "华宝", "工银", "诺安", "融通", "银河", "中欧", "财通", "长盛", "金鹰", "永赢",
    "中金", "安信", "中银", "交银",
)


# ================================================================ 一、HTTP
def http_get(url, retries=2, referer="https://gu.qq.com/"):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Referer": referer})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except Exception as e:                                  # noqa: BLE001
            last = e
    raise last


# ================================================================ 二、标的池发现
def classify(name):
    """按名称粗分类（用于 Top3 的类别分散约束）"""
    if any(k in name for k in BOND_KW):
        return "债券"
    if any(k in name for k in ("恒生", "港股", "中概", "国企", "H股", "HK")):
        return "港股"
    if any(k in name for k in ("纳指", "纳斯达克", "标普", "美股", "美国", "海外")):
        return "美股"
    if any(k in name for k in ("黄金", "豆粕", "有色", "油气", "原油", "能源化工", "商品")):
        return "商品"
    if any(k in name for k in ("债", "国债", "城投", "政金")):
        return "债券"
    if any(k in name for k in ("沪深300", "中证500", "中证1000", "中证2000", "上证50",
                               "深证100", "创业板", "科创50", "科创100", "科创综指",
                               "A50", "MSCI", "A500", "中证A", "北证", "上证180", "ZZ")):
        return "A股宽基"
    if any(k in name for k in ("红利", "低波", "价值100", "自由现金流", "基本面", "质量")):
        return "A股因子"
    return "A股行业"


def base_key(name):
    """归一化：去掉基金公司后缀与 ETF/基金等词缀，用于识别『同一指数的多只 ETF』"""
    s = name
    for c in sorted(set(FUND_COMPANIES), key=len, reverse=True):
        if s.endswith(c) and len(s) > len(c):
            s = s[:-len(c)]
            break
    for t in ("ETF基金", "ETF", "LOF", "基金", "指数", "交易型开放式"):
        s = s.replace(t, "")
    if s.startswith("中证"):
        s = s[2:]
    return s.strip()


def fetch_universe_sina(pages=4, pz=100):
    """新浪 ETF 清单（按成交额降序），返回 [(code, name, prefix, amount, scale)]"""
    rows = []
    for pn in range(1, pages + 1):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={pn}&num={pz}&sort=amount&asc=0&node=etf_hq_fund")
        try:
            obj = json.loads(http_get(url, referer="https://finance.sina.com.cn/"))
        except Exception as e:                                  # noqa: BLE001
            print(f"  ⚠ 新浪 ETF 清单第 {pn} 页失败: {e}")
            break
        if not isinstance(obj, list):
            break
        for x in obj:
            try:
                rows.append((str(x["code"]), str(x["name"]),
                             str(x.get("symbol", ""))[:2] or ("sz" if str(x["code"])[0] in "15" else "sh"),
                             float(x.get("amount") or 0), float(x.get("nmc") or 0)))
            except Exception:                                   # noqa: BLE001
                continue
    return rows


def fetch_universe_em(pages=3, pz=100):
    """东财 ETF 清单（备用源），同结构返回"""
    rows = []
    for pn in range(1, pages + 1):
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               f"?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f6"
               "&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024"
               "&fields=f2,f3,f6,f12,f13,f14,f21")
        try:
            obj = json.loads(http_get(url, referer="https://quote.eastmoney.com/"))
        except Exception as e:                                  # noqa: BLE001
            print(f"  ⚠ 东财 ETF 清单第 {pn} 页失败: {e}")
            continue
        diff = ((obj.get("data") or {}).get("diff")) or []
        for x in diff:
            try:
                rows.append((str(x["f12"]), str(x["f14"]),
                             "sz" if x.get("f13") == 0 else "sh",
                             float(x.get("f6") or 0), float(x.get("f21") or 0)))
            except Exception:                                   # noqa: BLE001
                continue
    return rows


def fetch_universe():
    """全市场 ETF 清单：新浪为主，失败降级东财；按成交额降序"""
    rows = fetch_universe_sina()
    src = "新浪"
    if len(rows) < 50:
        em = fetch_universe_em()
        if em:
            rows, src = em, "东财"
    rows.sort(key=lambda x: -x[3])
    return rows, src


def build_pool(top_n):
    """核心池保底 + 全市场自动选池（按成交额，去重，剔除货币/现金/债券类）"""
    core = [(c, n, cat, p) for c, n, cat, p in CORE_POOL]
    if not top_n or top_n <= len(core):
        return core, []

    out, seen_base, added = list(core), set(), []
    for _, n, _, _ in core:
        seen_base.add(base_key(n))

    try:
        universe, src = fetch_universe()
        print(f"   清单来源：{src}，共 {len(universe)} 只")
    except Exception as e:                                      # noqa: BLE001
        print(f"  ⚠ 全市场清单获取失败，退回核心池: {e}")
        return core, []

    for code, name, prefix, amount, scale in universe:
        if len(out) >= top_n:
            break
        if code in {c for c, _, _, _ in out}:
            continue
        if any(k in name for k in EXCLUDE_KW) or code.startswith(EXCLUDE_PREFIX):
            continue
        cat = classify(name)
        if cat == "债券":
            continue                      # 自动票池不引入债券（核心池已含国债ETF）
        key = base_key(name)
        if key in seen_base:
            continue                      # 同一指数的重复标的，只保留成交额最大的那只
        seen_base.add(key)
        out.append((code, name, cat, prefix))
        added.append(name)
    return out, added


def _daily_rets(rows, n=60):
    closes = [r["close"] for r in rows][-(n + 1):]
    return [(closes[i + 1] / closes[i] - 1) for i in range(len(closes) - 1)]


def _corr(a, b):
    if len(a) != len(b) or not a:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da and db else 0.0


def diversify(pool, kmap, limit, core_codes, thr=0.98):
    """用近 60 日收益率相关性去重：核心池保底，其余按原顺序挑选，相关性过高则跳过"""
    core_codes = set(core_codes)
    accepted, seen = [], []
    for code, name, cat, prefix in pool:
        rows = kmap.get(code)
        is_core = code in core_codes
        if len(accepted) >= limit and not is_core:
            break
        if not rows or len(rows) < 70:
            if is_core:
                accepted.append((code, name, cat, prefix))
            continue
        rets = _daily_rets(rows)
        if not is_core and any(_corr(rets, r) > thr for r in seen):
            continue
        accepted.append((code, name, cat, prefix))
        seen.append(rets)
    return accepted


# ================================================================ 三、K 线抓取与缓存
def fetch_kline(symbol):
    rows = []
    try:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={symbol},day,,,{DAYS},qfq")
        obj = json.loads(http_get(url))
        node = (obj.get("data") or {}).get(symbol) or {}
        arr = node.get("qfqday") or node.get("day") or []
        rows = [{"date": str(x[0])[:10], "open": float(x[1]), "close": float(x[2]),
                 "high": float(x[3]), "low": float(x[4]), "volume": float(x[5])}
                for x in arr if len(x) >= 6]
    except Exception:                                           # noqa: BLE001
        rows = []
    if len(rows) < 70:
        try:
            url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={DAYS}")
            obj = json.loads(http_get(url))
            rows = [{"date": str(x["day"])[:10], "open": float(x["open"]),
                     "close": float(x["close"]), "high": float(x["high"]),
                     "low": float(x["low"]), "volume": float(x["volume"])}
                    for x in obj]
        except Exception:                                       # noqa: BLE001
            pass
    return rows


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:                                           # noqa: BLE001
        return {}
    meta = raw.get("_meta") or {}
    if meta.get("date") != datetime.now().strftime("%Y-%m-%d"):
        return {}                       # 跨交易日自动失效
    return {k: v for k, v in raw.items() if k != "_meta"}


def save_cache(kmap):
    try:
        payload = {"_meta": {"date": datetime.now().strftime("%Y-%m-%d"), "count": len(kmap)}}
        payload.update(kmap)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:                                      # noqa: BLE001
        print(f"  ⚠ 缓存写入失败: {e}")


def ensure_klines(pool, workers=10):
    """按需抓取：缓存命中的直接用，只补缺失的"""
    cache = load_cache()
    need = [(code, f"{prefix}{code}") for code, _, _, prefix in pool if code not in cache]
    if need:
        def one(item):
            code, sym = item
            try:
                return code, fetch_kline(sym)
            except Exception as e:                              # noqa: BLE001
                print(f"  ⚠ {code} 抓取失败: {e}")
                return code, []
        t0 = datetime.now()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for code, data in ex.map(one, need):
                cache[code] = data
        print(f"  ⏱ 补抓 {len(need)} 只，用时 {(datetime.now() - t0).total_seconds():.1f}s")
    save_cache(cache)
    return cache


# ================================================================ 四、技术指标（纯 Python）
def _sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _ema(vals, alpha):
    out, started, acc = [], False, 0.0
    for v in vals:
        if v is None:
            out.append(None)
            continue
        if not started:
            acc, started = v, True
        else:
            acc = alpha * v + (1 - alpha) * acc
        out.append(acc)
    return out


def _rolling_std(vals, n, ddof=1):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        m = sum(w) / n
        var = sum((x - m) ** 2 for x in w) / (n - ddof)
        out[i] = math.sqrt(var)
    return out


def compute_indicators(rows):
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    vols = [r["volume"] for r in rows]
    n = len(closes)
    i, prev = n - 1, n - 2

    ma5, ma20, ma60 = _sma(closes, 5), _sma(closes, 20), _sma(closes, 60)
    ema12, ema26 = _ema(closes, 2 / 13), _ema(closes, 2 / 27)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 2 / 10)
    macd = [(a - b) * 2 for a, b in zip(dif, dea)]

    gains, losses = [None], [None]
    for k in range(1, n):
        d = closes[k] - closes[k - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = _ema(gains[1:], 1 / 14), _ema(losses[1:], 1 / 14)
    rsi = [None] + [100.0 if (b_ == 0 and a_ > 0) else (50.0 if b_ == 0 else 100 - 100 / (1 + a_ / b_))
                    for a_, b_ in zip(ag, al)]

    std20 = _rolling_std(closes, 20)
    upper = [(m + 2 * s) if (m is not None and s is not None) else None for m, s in zip(ma20, std20)]
    lower = [(m - 2 * s) if (m is not None and s is not None) else None for m, s in zip(ma20, std20)]

    tr = []
    for k in range(n):
        if k == 0:
            tr.append(highs[k] - lows[k])
        else:
            tr.append(max(highs[k] - lows[k],
                          abs(highs[k] - closes[k - 1]),
                          abs(lows[k] - closes[k - 1])))
    atr_s = _ema(tr, 1 / 14)

    vol_ma5 = sum(vols[-5:]) / 5 if n >= 5 else 0.0
    c = closes[i]

    golden_recent = cross50_recent = False
    for k in range(max(2, i - 2), i + 1):
        if dif[k] > dea[k] and dif[k - 1] <= dea[k - 1]:
            golden_recent = True
        if rsi[k] is not None and rsi[k - 1] is not None and rsi[k] >= 50 > rsi[k - 1]:
            cross50_recent = True

    ret5 = (c / closes[-6] - 1) * 100 if n > 6 else None
    ret20 = (c / closes[-21] - 1) * 100 if n > 21 else None
    win = closes[-20:] if n >= 20 else closes
    high20, low20 = max(win), min(win)

    def r(x, nd=4):
        return None if x is None else round(float(x), nd)

    return {
        "date": rows[i]["date"], "close": r(c),
        "ma5": r(ma5[i]), "ma20": r(ma20[i]), "ma60": r(ma60[i]),
        "ma20_slope_up": bool(ma20[i] > ma20[prev]) if ma20[i] is not None and ma20[prev] is not None else False,
        "dif": r(dif[i]), "dea": r(dea[i]), "macd": r(macd[i]),
        "macd_golden": bool(dif[i] > dea[i] and dif[prev] <= dea[prev]),
        "macd_dead": bool(dif[i] < dea[i] and dif[prev] >= dea[prev]),
        "macd_golden_recent": golden_recent,
        "rsi": round(float(rsi[i]), 1) if rsi[i] is not None else None,
        "rsi_cross50": cross50_recent,
        "boll_upper": r(upper[i]), "boll_mid": r(ma20[i]), "boll_lower": r(lower[i]),
        "atr": r(atr_s[i]),
        "vol_ratio": round(float(vols[i] / vol_ma5), 2) if vol_ma5 > 0 else None,
        "pct_chg": round(float((c / closes[prev] - 1) * 100), 2),
        "ret5": round(float(ret5), 2) if ret5 is not None else None,
        "ret20": round(float(ret20), 2) if ret20 is not None else None,
        "high20": r(high20), "low20": r(low20),
        "breakout20": bool(c >= high20 * 0.999),
        "series_close": [round(v, 4) for v in closes[-60:]],
        "series_ma20": [None if m is None else round(m, 4) for m in ma20[-60:]],
    }


# ================================================================ 五、策略规则
def trend_state(ind):
    c, m20, m60 = ind["close"], ind["ma20"], ind["ma60"]
    if m20 is None or m60 is None:
        return "震荡", "样本不足"
    if c > m20 > m60 and ind["ma20_slope_up"]:
        return "多头", f"价>MA20({m20})>MA60({m60})，均线向上"
    if c < m20 < m60 or (m20 < m60 and not ind["ma20_slope_up"]) or (c < m20 and not ind["ma20_slope_up"]):
        return "空头", f"MA20({m20})<MA60({m60})或价<MA20，均线向下"
    return "震荡", f"MA20({m20})走平缠绕"


def rank_pct(values):
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return [rank / n for rank in ranks]


def score_etf(ind, ctx):
    if ind["ma20"] is None or ind["ma60"] is None:
        return 0.0, "样本不足"
    s, reasons = 0.0, []
    if ind["ma20"] > ind["ma60"]:
        s += 1.5
        reasons.append("MA20>MA60")
    if ind["close"] > ind["ma20"]:
        s += 0.75
        reasons.append("价>MA20")
    if ind["ma20_slope_up"]:
        s += 0.75
        reasons.append("MA20向上")
    s += ctx["ret5_rank"] * 1.5
    s += ctx["ret20_rank"] * 1.5
    if ind["ret5"] is not None:
        reasons.append(f"5日{ind['ret5']:+.1f}%（池内{ctx['ret5_rank'] * 100:.0f}分位）")
    if ind["ret20"] is not None:
        reasons.append(f"20日{ind['ret20']:+.1f}%（池内{ctx['ret20_rank'] * 100:.0f}分位）")
    if ind["close"] <= ind["boll_lower"] * 1.01:
        s += 2
        reasons.append("布林下轨（买点区）")
    elif ind["close"] <= ind["boll_mid"] * 1.02:
        s += 1
        reasons.append("布林中轨附近")
    else:
        reasons.append("接近上轨（偏高）")
    if ind["macd_golden_recent"]:
        s += 1
        reasons.append("近3日MACD金叉")
    if ind["breakout20"]:
        if ind["vol_ratio"] and ind["vol_ratio"] >= 1.1:
            s += 1
            reasons.append(f"放量突破20日新高（量比{ind['vol_ratio']}）")
        else:
            s += 0.5
            reasons.append("触及20日新高")
    if ind["rsi_cross50"]:
        s += 0.5
        reasons.append("RSI上穿50")
    if ind["vol_ratio"] and ind["vol_ratio"] >= 1.5 and ind["pct_chg"] > 0:
        s += 0.5
        reasons.append("放量上涨")
    if ind["close"] < ind["ma60"]:
        s -= 2
        reasons.append("跌破MA60（扣分）")
    return round(min(s, 10.0), 1), "、".join(reasons)


def action_advice(state, ind):
    if ind["close"] < ind["ma60"]:
        return "清仓", f"跌破MA60({ind['ma60']})，趋势走坏，次日开盘无条件清仓"
    rsi = ind["rsi"]
    if state == "多头":
        if rsi is not None and rsi >= 70:
            return "减仓", f"RSI{rsi} 超买 + 逼近布林上轨({ind['boll_upper']})，分批止盈"
        if rsi is not None and rsi <= 45 and ind["close"] <= ind["boll_mid"]:
            return "加仓", f"多头回调至MA20附近（RSI{rsi}），分批买入区"
        return "持有", "多头趋势完好，持有底仓，网格仓高抛低吸"
    if state == "震荡":
        if rsi is not None and rsi < 30:
            return "网格买入", f"RSI{rsi} 超卖 + 触及布林下轨({ind['boll_lower']})，网格买1格"
        if rsi is not None and rsi > 70:
            return "网格卖出", f"RSI{rsi} 超买 + 触及布林上轨({ind['boll_upper']})，网格卖1格"
        return "观望", "震荡无趋势，只做网格或不动"
    return "回避", f"空头趋势（MA20={ind['ma20']} 之下），暂停网格，反弹减仓"


def _fmt(v):
    if v is None:
        return "—"
    return f"{v:.2f}" if abs(v) >= 20 else f"{v:.3f}"


def buy_plan(state, ind, score):
    c = ind["close"]
    m20, m60 = ind["ma20"], ind["ma60"]
    boll_up, boll_mid, boll_lo = ind["boll_upper"], ind["boll_mid"], ind["boll_lower"]

    if state == "多头" and m20 is not None:
        lo, hi = m20 * 0.985, m20 * 1.010
        basis = "回踩MA20分批接"
    else:
        lo, hi = boll_lo * 0.998, (boll_mid * 1.005 if boll_mid else c)
        basis = "布林下轨~中轨网格"
    if lo >= hi:
        lo, hi = min(lo, c), max(hi, c)

    t1 = max(boll_up, c * 1.05) if boll_up else c * 1.05
    t2 = max(ind["high20"] * 1.02, t1 * 1.03)
    stop = min(m60 * 0.995, c * 0.92) if (m60 and m60 < c) else c * 0.92

    if state == "多头" and score >= 6.0:
        base_pos, grid_pos = 9.0, 6.0
    elif state == "多头":
        base_pos, grid_pos = 6.0, 4.0
    elif state == "震荡":
        base_pos, grid_pos = 0.0, 6.0
    else:
        base_pos, grid_pos = 0.0, 0.0

    return {
        "basis": basis, "buy_lo": _fmt(lo), "buy_hi": _fmt(hi),
        "target1": _fmt(t1), "target2": _fmt(t2),
        "stop": _fmt(stop), "stop_pct": round((stop / c - 1) * 100, 1),
        "upside": round((t1 / c - 1) * 100, 1),
        "total_pos": base_pos + grid_pos, "base_pos": base_pos, "grid_pos": grid_pos,
        "grid_cell": round(grid_pos / 3.0, 1) if grid_pos else 0.0,
        "grid_buy": [_fmt(c * (1 - k)) for k in (0.03, 0.06, 0.09)],
        "nd": 2 if abs(c) >= 20 else 3,
    }


def sentiment_scan(rows):
    bull = [r for r in rows if r["state"] == "多头"]
    bear = [r for r in rows if r["state"] == "空头"]
    flat = [r for r in rows if r["state"] == "震荡"]
    rsis = [r["ind"]["rsi"] for r in rows if r["ind"].get("rsi") is not None]
    avg_rsi = round(sum(rsis) / len(rsis), 1) if rsis else None
    total = len(rows) or 1
    hs300 = next((r for r in rows if r["code"] == "510300"), None)
    market = hs300["state"] if hs300 else "未知"
    market_reason = hs300["state_reason"] if hs300 else ""

    fear_greed = int(round(len(bull) / total * 60 + (avg_rsi / 100) * 40)) if avg_rsi \
        else int(round(len(bull) / total * 100))
    if fear_greed >= 70:
        fg_state, fg_desc = "贪婪", "偏热，注意分批止盈"
    elif fear_greed <= 30:
        fg_state, fg_desc = "恐慌", "偏冷，留意超卖机会"
    else:
        fg_state, fg_desc = "中性", "不极端，按规则执行"

    risks = []
    for r in rows:
        if r["state"] == "空头":
            risks.append(f"{r['name']} 空头趋势")
        elif r["ind"].get("rsi") is not None and r["ind"]["rsi"] > 72:
            risks.append(f"{r['name']} RSI超买({r['ind']['rsi']})")
    if avg_rsi and avg_rsi > 70:
        risks.append(f"池内平均RSI {avg_rsi} 过热")
    return {
        "market": market, "market_reason": market_reason,
        "bull": len(bull), "bear": len(bear), "flat": len(flat), "total": len(rows),
        "avg_rsi": avg_rsi, "fear_greed": fear_greed,
        "fg_state": fg_state, "fg_desc": fg_desc,
        "risks": risks[:6] if risks else ["今日无显著风险点"],
        "strong5": sorted([r for r in rows if r["ind"].get("ret5") is not None],
                          key=lambda x: -x["ind"]["ret5"])[:3],
        "strong20": sorted([r for r in rows if r["ind"].get("ret20") is not None],
                           key=lambda x: -x["ind"]["ret20"])[:3],
    }


def pick_top3(rows, prev_codes):
    prev_codes = prev_codes or set()
    ranked = sorted(rows, key=lambda x: (-x["score"], x["code"] in prev_codes, x["code"]))
    picks, picked, cnt = [], set(), {}
    for cap in (1, 2):
        for r in ranked:
            if len(picks) >= 3:
                break
            if r["code"] in picked or cnt.get(r["cat"], 0) >= cap:
                continue
            picks.append(r)
            picked.add(r["code"])
            cnt[r["cat"]] = cnt.get(r["cat"], 0) + 1
        if len(picks) >= 3:
            break
    return picks


def build_rows(pool, kmap):
    rows = []
    for code, name, cat, prefix in pool:
        data = kmap.get(code)
        if not data or len(data) < 70:
            rows.append({"code": code, "name": name, "cat": cat, "ok": False,
                         "state": "数据缺失", "ind": {}, "score": 0.0, "prefix": prefix})
            continue
        ind = compute_indicators(data)
        state, reason = trend_state(ind)
        rows.append({"code": code, "name": name, "cat": cat, "ok": True, "prefix": prefix,
                     "state": state, "state_reason": reason, "ind": ind,
                     "score": 0.0, "reason": "", "action": "", "advice": ""})

    valid = [r for r in rows if r["ok"]]
    r5 = rank_pct([r["ind"]["ret5"] or 0.0 for r in valid])
    r20 = rank_pct([r["ind"]["ret20"] or 0.0 for r in valid])
    for r, a, b in zip(valid, r5, r20):
        r["score"], r["reason"] = score_etf(r["ind"], {"ret5_rank": a, "ret20_rank": b})
        r["ret5_rank_pct"] = int(round(a * 100))
        r["ret20_rank_pct"] = int(round(b * 100))
        r["ind"]["nd"] = 2 if abs(r["ind"]["close"]) >= 20 else 3
        r["action"], r["advice"] = action_advice(r["state"], r["ind"])
        r["plan"] = buy_plan(r["state"], r["ind"], r["score"])
    return rows


# ================================================================ 六、口令与加密
def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    return base64.b64decode(s)


def _derive(password, salt, iter_n=PBKDF2_ITER, dklen=32):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iter_n, dklen=dklen)


def _keystream(key, n):
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(key + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:n])


def encrypt(plain_bytes, password, salt=None):
    salt = salt or secrets.token_bytes(16)
    key = _derive(password, salt)
    ks = _keystream(key, len(plain_bytes))
    ct = bytes(a ^ b for a, b in zip(plain_bytes, ks))
    auth = hashlib.sha256(AUTH_TAG + key).digest()
    return {"v": 1, "salt": _b64(salt), "iter": PBKDF2_ITER, "auth": _b64(auth), "ct": _b64(ct)}


def load_secret():
    if os.path.exists(SECRET_PATH):
        try:
            with open(SECRET_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:                                       # noqa: BLE001
            return {}
    return {}


def save_secret(sec):
    with open(SECRET_PATH, "w", encoding="utf-8") as f:
        json.dump(sec, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except Exception:                                           # noqa: BLE001
        pass


def _ask_password():
    pwd = getpass.getpass("设置看板访问口令（至少 6 位，输入不回显）：")
    if len(pwd) < 6:
        print("❌ 口令太短，已取消")
        sys.exit(1)
    if getpass.getpass("再输一次确认：") != pwd:
        print("❌ 两次输入不一致，已取消")
        sys.exit(1)
    return pwd


def obtain_password(existing, force_set=False):
    """返回口令：优先环境变量 → 本机已保存 → 交互索取 → 无终端时随机生成"""
    env = os.environ.get("ETF_DASHBOARD_PASSWORD")
    if env:
        return env
    if existing and not force_set:
        return existing
    try:
        interactive = bool(sys.stdin) and sys.stdin.isatty()
    except Exception:                                           # noqa: BLE001
        interactive = False
    if interactive:
        return _ask_password()
    pwd = secrets.token_urlsafe(10)
    print("\n⚠ 当前非交互终端，已自动生成随机访问口令（见下方 AUTH_CODE）。")
    print("   本机已保存到 dashboard/.secret.json；如需自定义，请在终端里运行：")
    print("   python fund-strategy/etf_dashboard.py --set-password")
    return pwd


# ================================================================ 七、页面渲染
STATE_STYLE = {"多头": ("bull", "#e5484d"), "震荡": ("flat", "#b78103"), "空头": ("bear", "#17a673")}
ACTION_STYLE = {"加仓": "buy", "网格买入": "buy", "持有": "hold",
                "观望": "muted", "减仓": "sell", "网格卖出": "sell",
                "清仓": "sell", "回避": "sell"}


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def col(v):
    if v is None:
        return "#6b7280"
    return "#e5484d" if v > 0 else ("#17a673" if v < 0 else "#6b7280")


def pct_txt(v):
    return "—" if v is None else f"{v:+.2f}%"


def sparkline_svg(closes, ma20s, w=360, h=84):
    pts = [c for c in closes if c is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    for m in ma20s:
        if m:
            lo, hi = min(lo, m), max(hi, m)
    span = (hi - lo) or 1e-9
    pad = 6
    sx = lambda i: pad + i * (w - 2 * pad) / (len(closes) - 1)          # noqa: E731
    sy = lambda v: pad + (hi - v) / span * (h - 2 * pad)                # noqa: E731
    line = " ".join(f"{sx(i):.1f},{sy(c):.1f}" for i, c in enumerate(closes) if c is not None)
    rising = closes[-1] >= closes[0]
    c_line = "#e5484d" if rising else "#17a673"
    gid = "g" + hashlib.md5(line.encode()).hexdigest()[:8]
    area = f"{sx(0):.1f},{h - pad:.1f} {line} {sx(len(closes) - 1):.1f},{h - pad:.1f}"
    ma_pts = " ".join(f"{sx(i):.1f},{sy(m):.1f}" for i, m in enumerate(ma20s) if m is not None)
    last_x, last_y = sx(len(closes) - 1), sy(closes[-1])
    return f'''<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="近60日走势">
  <defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{c_line}" stop-opacity="0.20"/>
    <stop offset="100%" stop-color="{c_line}" stop-opacity="0.02"/></linearGradient></defs>
  <polygon points="{area}" fill="url(#{gid})"/>
  <polyline points="{ma_pts}" fill="none" stroke="#8b93a7" stroke-width="1.2" stroke-dasharray="3 3" opacity="0.85"/>
  <polyline points="{line}" fill="none" stroke="{c_line}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3" fill="{c_line}"/>
</svg>'''


def gauge_svg(value, w=200, h=112):
    cx, cy, r = w / 2, h - 18, 76

    def pt(v):
        th = math.radians(180 - 1.8 * max(0, min(100, v)))
        return cx + r * math.cos(th), cy - r * math.sin(th)

    x0, y0 = pt(0)
    x1, y1 = pt(100)
    xv, yv = pt(value)
    c = "#e5484d" if value >= 70 else ("#17a673" if value <= 30 else "#2563eb")
    return f'''<svg class="gauge" viewBox="0 0 {w} {h}" role="img" aria-label="恐慌贪婪指数 {value}">
  <path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 0 1 {x1:.1f},{y1:.1f}" fill="none" stroke="#eceff3" stroke-width="11" stroke-linecap="round"/>
  <path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 {1 if value > 50 else 0} 1 {xv:.1f},{yv:.1f}" fill="none" stroke="{c}" stroke-width="11" stroke-linecap="round"/>
  <circle cx="{xv:.1f}" cy="{yv:.1f}" r="5.5" fill="#ffffff" stroke="{c}" stroke-width="3"/>
  <text x="{cx}" y="{cy - 26}" text-anchor="middle" class="gauge-val" fill="{c}">{value}</text>
  <text x="{cx - 4}" y="{cy - 8}" text-anchor="middle" class="gauge-sub">/100</text>
  <text x="16" y="{h - 2}" class="gauge-lab">恐慌</text>
  <text x="{w - 16}" y="{h - 2}" text-anchor="end" class="gauge-lab">贪婪</text>
</svg>'''


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#f4f6f9;--card:#fff;--line:#e6e9ef;--line2:#f0f2f6;--tx:#1d2330;--tx2:#5b6472;
--tx3:#8a93a3;--up:#e5484d;--down:#17a673;--flat:#b78103;--brand:#2563eb;
--shadow:0 1px 2px rgba(16,24,40,.04),0 8px 24px rgba(16,24,40,.06);--radius:16px}
body{background:var(--bg);color:var(--tx);font-size:14px;line-height:1.6;padding:28px 20px 48px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto}
.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:18px}
.title{font-size:24px;font-weight:700;letter-spacing:-.4px}
.title .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--up);margin-right:9px;vertical-align:2px}
.sub{color:var(--tx2);font-size:13px;margin-top:4px}
.stamp{text-align:right;color:var(--tx3);font-size:12.5px;line-height:1.6}
.stamp b{color:var(--tx);font-weight:600}
.live{display:inline-flex;align-items:center;gap:6px;background:#eef8f3;border:1px solid #cfead9;
color:#137a52;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600}
.live .pulse{width:6px;height:6px;border-radius:50%;background:#17a673;animation:pulse 1.6s infinite}
.lock{margin-left:8px;border:1px solid var(--line);background:#fff;color:var(--tx2);border-radius:8px;
padding:1px 9px;font-size:12px;line-height:1.6;cursor:pointer;font-family:inherit;transition:all .15s}
.lock:hover{border-color:#cfd6e2;color:var(--tx);background:#fafbfd}
@keyframes pulse{0%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(1.5)}100%{opacity:1;transform:scale(1)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.sentiment{padding:20px 22px 18px;margin-bottom:18px}
.sec-title{font-size:14px;font-weight:700;display:flex;align-items:center;gap:8px;margin-bottom:14px}
.sec-title .bar{width:3px;height:14px;border-radius:2px;background:var(--brand)}
.sec-title .tail{margin-left:auto;font-weight:400;color:var(--tx3);font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.kpi{background:#fafbfd;border:1px solid var(--line2);border-radius:12px;padding:14px 16px}
.kpi .lab{color:var(--tx3);font-size:12px;letter-spacing:.3px}
.kpi .val{font-size:22px;font-weight:700;margin:6px 0 2px;font-variant-numeric:tabular-nums}
.kpi .desc{font-size:12px;color:var(--tx2)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:2px 10px;border-radius:999px;font-size:12.5px;font-weight:600}
.pill.bull{background:#fdecec;color:var(--up)}.pill.flat{background:#fdf6e6;color:var(--flat)}
.pill.bear{background:#e8f7f0;color:var(--down)}
.dist{display:flex;height:8px;border-radius:999px;overflow:hidden;background:#eceff3;margin:10px 0 8px}
.dist i{display:block;height:100%}
.dist .i-bull{background:var(--up)}.dist .i-flat{background:#e2c96a}.dist .i-bear{background:var(--down)}
.dist-lab{display:flex;gap:14px;font-size:12px;color:var(--tx2)}
.dist-lab b{font-variant-numeric:tabular-nums}
.dist-lab span::before{content:'';display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px}
.dist-lab .l-bull::before{background:var(--up)}.dist-lab .l-flat::before{background:#e2c96a}
.dist-lab .l-bear::before{background:var(--down)}
.gauge-wrap{display:flex;align-items:center;gap:12px}
.gauge{width:150px;height:84px;flex:none}
.gauge-val{font-size:28px;font-weight:700}.gauge-sub{font-size:11px;fill:#8a93a3}.gauge-lab{font-size:11px;fill:#a3abba}
.risks{margin-top:16px;padding-top:14px;border-top:1px dashed var(--line)}
.risks .lab{font-size:12px;color:var(--tx3);margin-bottom:8px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:#f6f8fb;border:1px solid var(--line2);color:var(--tx2);border-radius:8px;padding:3px 10px;font-size:12px}
.chip.warn{background:#fdf3f3;border-color:#f7dede;color:#b3392f}
.top3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.etf{padding:0;overflow:hidden;display:flex;flex-direction:column;transition:transform .18s ease,box-shadow .18s ease}
.etf:hover{transform:translateY(-2px);box-shadow:0 4px 10px rgba(16,24,40,.06),0 16px 34px rgba(16,24,40,.09)}
.etf-head{padding:16px 18px 12px;border-bottom:1px solid var(--line2)}
.row1{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.rank{width:22px;height:22px;border-radius:6px;background:#1d2330;color:#fff;font-size:12px;font-weight:700;
display:flex;align-items:center;justify-content:center;flex:none}
.rank.r1{background:linear-gradient(135deg,#f0a020,#e5484d)}
.rank.r2{background:linear-gradient(135deg,#9aa4b5,#6b7280)}
.rank.r3{background:linear-gradient(135deg,#c79a68,#a3733c)}
.ename{font-size:16px;font-weight:700}.ecode{color:var(--tx3);font-size:12px;font-variant-numeric:tabular-nums}
.cat{margin-left:auto;font-size:11.5px;color:var(--tx2);background:#f1f4f9;border:1px solid var(--line2);
border-radius:6px;padding:2px 8px;white-space:nowrap}
.row2{display:flex;align-items:baseline;gap:10px}
.price{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.chg{font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}
.spark-wrap{padding:6px 0 0;background:linear-gradient(180deg,#fbfcfe,#fff)}
.spark{display:block;width:100%;height:88px}
.body{padding:14px 18px 16px;display:flex;flex-direction:column;gap:12px;flex:1}
.score-row{display:flex;align-items:center;gap:10px}
.score-row .s-lab{font-size:12px;color:var(--tx3);flex:none}
.bar{flex:1;height:7px;border-radius:999px;background:#eceff3;overflow:hidden}
.bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#8aa7f5,#2563eb)}
.score-row .s-val{font-weight:700;font-variant-numeric:tabular-nums;color:var(--brand);font-size:14px;flex:none}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px}
.kv{display:flex;justify-content:space-between;align-items:baseline;font-size:12.5px;
border-bottom:1px dotted var(--line);padding-bottom:4px}
.kv .k{color:var(--tx3)}.kv .v{font-variant-numeric:tabular-nums;font-weight:600}
.rsi-bar{position:relative;height:6px;border-radius:999px;margin-top:5px;
background:linear-gradient(90deg,#17a673 0%,#e2c96a 45%,#e2c96a 60%,#e5484d 100%);opacity:.85}
.rsi-bar i{position:absolute;top:-3px;width:2px;height:12px;background:#1d2330;border-radius:2px}
.plan{background:#fafbfd;border:1px solid var(--line2);border-radius:12px;padding:12px 14px}
.plan-hd{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.act{padding:2px 11px;border-radius:999px;font-size:13px;font-weight:700;flex:none}
.act.buy{background:#fdecec;color:#c2342d}.act.hold{background:#eaf1fd;color:#2a55b8}
.act.sell{background:#e8f7f0;color:#137a52}.act.muted{background:#f1f3f6;color:#6b7280}
.plan-hd .why{font-size:12px;color:var(--tx2);line-height:1.4}
.plan-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px}
.cell{background:#fff;border:1px solid var(--line2);border-radius:9px;padding:8px 10px}
.cell .cl{font-size:11px;color:var(--tx3)}
.cell .cv{font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:1px}
.cell .cx{font-size:11px;color:var(--tx3)}
.pos{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--tx2)}
.pos b{color:var(--tx);font-variant-numeric:tabular-nums}
.gline{display:flex;gap:6px;margin-top:8px;font-size:11.5px;color:var(--tx3);flex-wrap:wrap}
.gline code{background:#fff;border:1px solid var(--line2);border-radius:5px;padding:1px 6px;color:var(--tx2);
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.reasons{display:flex;flex-wrap:wrap;gap:6px}
.reason{font-size:11.5px;color:var(--tx2);background:#f6f8fb;border-radius:6px;padding:2px 8px}
.cand{padding:16px 20px 18px;margin-top:18px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:600;color:var(--tx3);font-size:12px;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line2);font-variant-numeric:tabular-nums}
td.nm{font-variant-numeric:normal;font-weight:600}
td .cd{color:var(--tx3);font-weight:400;font-size:12px;margin-left:6px}
.dot-s{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}
tbody tr:hover{background:#fafbfd}
.foot{margin-top:20px;color:var(--tx3);font-size:12px;line-height:1.7}
.foot b{color:var(--tx2)}
@media(max-width:960px){.top3{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){body{padding:16px 12px 32px}.kpis{grid-template-columns:1fr}.plan-grid{grid-template-columns:1fr}}
/* -------- 登录门禁 -------- */
#gate{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
background:linear-gradient(160deg,#eef2f8,#f7f9fc);z-index:99}
.gate-box{width:340px;background:#fff;border:1px solid var(--line);border-radius:18px;
box-shadow:0 20px 60px rgba(16,24,40,.14);padding:28px 26px 24px}
.gate-box .logo{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,#2563eb,#4f7df0);
display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:17px;margin-bottom:14px}
.gate-box h2{font-size:17px;font-weight:700}
.gate-box p{color:var(--tx2);font-size:12.5px;margin:4px 0 18px}
.gate-box input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-size:14px;
outline:none;transition:border-color .15s,box-shadow .15s}
.gate-box input:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.gate-box button{width:100%;margin-top:12px;padding:10px;border:none;border-radius:10px;background:var(--brand);
color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:background .15s}
.gate-box button:hover{background:#1d4fd8}
.gate-box button:disabled{background:#9db4ef;cursor:default}
.gate-err{color:#c2342d;font-size:12.5px;margin-top:10px;min-height:18px}
.gate-opt{display:flex;align-items:center;gap:6px;margin-top:12px;font-size:12px;color:var(--tx3)}
.gate-tip{margin-top:14px;padding-top:12px;border-top:1px dashed var(--line);font-size:11.5px;color:var(--tx3);line-height:1.6}
#app{display:none}
"""

LOGIN_JS = r"""
const el = id => document.getElementById(id);

// ---- 安全的 sessionStorage 包装：iframe / 沙箱环境下访问会抛异常，必须挡住 ----
const store = {
  get(k){ try{ return sessionStorage.getItem(k); }catch(e){ return null; } },
  set(k, v){ try{ sessionStorage.setItem(k, v); }catch(e){} }
};
function hasSubtle(){
  try{ return !!(window.crypto && crypto.subtle && crypto.subtle.deriveBits); }
  catch(e){ return false; }
}

function b64d(s){
  const bin = atob(s), arr = new Uint8Array(bin.length);
  for (let i=0;i<bin.length;i++) arr[i] = bin.charCodeAt(i);
  return arr;
}
function b64e(arr){
  let s = '';
  for (let i=0;i<arr.length;i++) s += String.fromCharCode(arr[i]);
  return btoa(s);
}
function cat(a,b){ const o = new Uint8Array(a.length+b.length); o.set(a,0); o.set(b,a.length); return o; }

// ============ 纯 JS SHA-256 / HMAC / PBKDF2（WebCrypto 不可用时的兜底）============
const SHA_K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);

function sha256Sync(msg){
  const ml = msg.length;
  const buf = new Uint8Array((((ml + 8) >> 6) + 1) << 6);
  buf.set(msg);
  buf[ml] = 0x80;
  const dv = new DataView(buf.buffer);
  const bitLen = ml * 8;
  dv.setUint32(buf.length - 8, Math.floor(bitLen / 4294967296));
  dv.setUint32(buf.length - 4, bitLen >>> 0);
  const H = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                             0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
  const w = new Uint32Array(64);
  const rotr = (x,n) => ((x >>> n) | (x << (32 - n))) >>> 0;
  for (let off = 0; off < buf.length; off += 64){
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++){
      const x = w[i-15], y = w[i-2];
      const s0 = (rotr(x,7) ^ rotr(x,18) ^ (x >>> 3)) >>> 0;
      const s1 = (rotr(y,17) ^ rotr(y,19) ^ (y >>> 10)) >>> 0;
      w[i] = (w[i-16] + s0 + w[i-7] + s1) >>> 0;
    }
    let a=H[0],b=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7];
    for (let i = 0; i < 64; i++){
      const S1 = (rotr(e,6) ^ rotr(e,11) ^ rotr(e,25)) >>> 0;
      const ch = ((e & f) ^ (~e & g)) >>> 0;
      const t1 = (h + S1 + ch + SHA_K[i] + w[i]) >>> 0;
      const S0 = (rotr(a,2) ^ rotr(a,13) ^ rotr(a,22)) >>> 0;
      const maj = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
      const t2 = (S0 + maj) >>> 0;
      h=g; g=f; f=e; e=(d+t1)>>>0; d=c; c=b; b=a; a=(t1+t2)>>>0;
    }
    H[0]=(H[0]+a)>>>0; H[1]=(H[1]+b)>>>0; H[2]=(H[2]+c)>>>0; H[3]=(H[3]+d)>>>0;
    H[4]=(H[4]+e)>>>0; H[5]=(H[5]+f)>>>0; H[6]=(H[6]+g)>>>0; H[7]=(H[7]+h)>>>0;
  }
  const out = new Uint8Array(32), odv = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) odv.setUint32(i * 4, H[i]);
  return out;
}

function hmacSha256Sync(key, msg){
  const BS = 64;
  let k = key;
  if (k.length > BS) k = sha256Sync(k);
  const pad = new Uint8Array(BS); pad.set(k);
  const ipad = new Uint8Array(BS), opad = new Uint8Array(BS);
  for (let i = 0; i < BS; i++){ ipad[i] = pad[i] ^ 0x36; opad[i] = pad[i] ^ 0x5c; }
  const inner = new Uint8Array(BS + msg.length);
  inner.set(ipad); inner.set(msg, BS);
  const ih = sha256Sync(inner);
  const outer = new Uint8Array(BS + 32);
  outer.set(opad); outer.set(ih, BS);
  return sha256Sync(outer);
}

function pbkdf2Sync(pwBytes, salt, iter){
  const block = new Uint8Array(salt.length + 4);
  block.set(salt);
  block[salt.length] = 0; block[salt.length+1] = 0;
  block[salt.length+2] = 0; block[salt.length+3] = 1;
  let u = hmacSha256Sync(pwBytes, block);
  const t = new Uint8Array(u);
  for (let i = 1; i < iter; i++){
    u = hmacSha256Sync(pwBytes, u);
    for (let j = 0; j < 32; j++) t[j] ^= u[j];
  }
  return t;
}
// ================================================================================

async function derive(pw, salt, iter){
  const enc = new TextEncoder();
  if (hasSubtle()){
    try{
      const base = await crypto.subtle.importKey('raw', enc.encode(pw), 'PBKDF2', false, ['deriveBits']);
      const bits = await crypto.subtle.deriveBits({name:'PBKDF2', salt, iterations:iter, hash:'SHA-256'}, base, 256);
      return new Uint8Array(bits);
    }catch(e){ /* 降级到纯 JS 实现 */ }
  }
  return pbkdf2Sync(enc.encode(pw), salt, iter);
}
async function sha256(buf){
  if (hasSubtle()){
    try{ return new Uint8Array(await crypto.subtle.digest('SHA-256', buf)); }
    catch(e){ /* 降级 */ }
  }
  return sha256Sync(buf);
}

function keystream(key, n){
  // 密钥流块数很多（正文 40KB ≈ 1250 块），用同步实现，避免上千次 await 的微任务开销
  const out = new Uint8Array(n);
  const buf = new Uint8Array(36);
  let off = 0, i = 0;
  while (off < n){
    buf.set(key, 0);
    buf[32] = (i >>> 24) & 255; buf[33] = (i >>> 16) & 255;
    buf[34] = (i >>> 8) & 255;  buf[35] = i & 255;
    const h = sha256Sync(buf);
    const take = Math.min(32, n - off);
    out.set(h.subarray(0, take), off);
    off += take; i++;
  }
  return out;
}

async function tryUnlock(pw){
  const key = await derive(pw, b64d(window.__CFG__.salt), window.__CFG__.iter);
  const tag = await sha256(cat(new TextEncoder().encode('ETF-DASH-V1'), key));
  if (b64e(tag) !== window.__CFG__.auth) return null;
  const ct = b64d(window.__CFG__.ct), ks = keystream(key, ct.length);
  const pt = new Uint8Array(ct.length);
  for (let i=0;i<ct.length;i++) pt[i] = ct[i] ^ ks[i];
  return new TextDecoder('utf-8').decode(pt);
}

function startLive(){
  const cnode = el('codes');
  const syms = cnode ? JSON.parse(cnode.dataset.codes) : [];
  const intervalMs = Math.max(5, parseInt((cnode && cnode.dataset.interval) || '30', 10)) * 1000;
  const set = (id, txt, color) => {
    const n = el(id); if (!n) return;
    n.textContent = txt;
    if (color) n.style.color = color;
  };
  const fmtTime = t => t && t.length === 14
    ? t.slice(0,4)+'-'+t.slice(4,6)+'-'+t.slice(6,8)+' '+t.slice(8,10)+':'+t.slice(10,12)+':'+t.slice(12,14)
    : '--';
  const kb = n => (n / 1024).toFixed(n >= 1024 * 1024 ? 1 : 0) + (n >= 1024 * 1024 ? ' MB' : ' KB');

  // ---- 定时器的统一管理：后台自动暂停、离开页面立即停止 ----
  let pollTimer = null, cdTimer = null, inFlight = false, running = false;
  let polls = 0, bytes = 0;

  function stopLive(msg){
    if (pollTimer){ clearInterval(pollTimer); pollTimer = null; }
    if (cdTimer){ clearInterval(cdTimer); cdTimer = null; }
    running = false;
    if (msg) set('rt-state', msg, '#8a93a3');
  }

  function schedule(){
    stopLive();                       // 防止重复调用叠加定时器
    running = true;
    pollTimer = setInterval(tick, intervalMs);
    let left = intervalMs / 1000;
    set('rt-tick', String(Math.round(left)));
    cdTimer = setInterval(() => {
      left = left <= 1 ? intervalMs / 1000 : left - 1;
      set('rt-tick', String(Math.round(left)));
    }, 1000);
  }

  async function tick(){
    if (inFlight || !running) return;          // 上一轮没回来就不再发下一次请求
    inFlight = true;
    const s = document.createElement('script');
    s.charset = 'GBK';
    s.src = 'https://qt.gtimg.cn/q=' + syms.join(',') + '&_=' + Date.now();
    let ok = true;
    try{
      await new Promise((res, rej) => { s.onload = res; s.onerror = rej; document.head.appendChild(s); });
    }catch(e){ ok = false; }
    try{ s.remove(); }catch(e){}

    if (!ok){
      inFlight = false;
      set('rt-state', '⚠ 实时行情未连通（显示的是生成时快照）', '#b78103');
      return;
    }
    let t = '', got = 0;
    syms.forEach(sym => {
      const raw = window['v_' + sym];
      if (!raw) return;
      const f = raw.split('~');
      const code = sym.slice(2);
      const price = f[3], pct = parseFloat(f[32]);
      if (price && price !== '0') set('rt-' + code + '-p', (+price).toFixed(+price >= 20 ? 2 : 3));
      if (!isNaN(pct)){
        const c = pct > 0 ? '#e5484d' : (pct < 0 ? '#17a673' : '#6b7280');
        set('rt-' + code + '-c', (pct > 0 ? '+' : '') + pct.toFixed(2) + '%', c);
      }
      t = f[30] || t;
      try{ got += raw.length; }catch(e){}
    });
    polls += 1; bytes += got;
    set('rt-time', fmtTime(t));
    set('rt-state', '实时行情已更新 · 已拉取 ' + polls + ' 次 / 约 ' + kb(bytes), '#137a52');
    inFlight = false;
  }

  // ① 标签页切到后台 / 窗口最小化 → 暂停；切回来 → 立即拉一次并恢复
  const onVis = () => {
    if (document.hidden){ stopLive('⏸ 页面不可见，已暂停刷新'); }
    else if (!running){ schedule(); tick(); }
  };
  if (document.addEventListener) document.addEventListener('visibilitychange', onVis);

  // ② 离开页面（关闭/前进后退/导航）→ 真正停表，不再产生任何请求
  const leave = () => stopLive();
  if (window.addEventListener){
    window.addEventListener('pagehide', leave);
    window.addEventListener('beforeunload', leave);
  }

  // ③ 手动锁定：停定时器 + 清会话口令 + 回到登录页
  const lock = el('lockBtn');
  if (lock){
    lock.addEventListener('click', () => {
      stopLive();
      try{ sessionStorage.removeItem('etf_pw'); }catch(e){}
      try{ location.reload(); }catch(e){}
    });
  }

  if (!document.hidden){
    schedule();                        // 先排定时器并置 running=true
    tick();                            // 再立即拉第一次（顺序不能反，否则首次会被 running 拦掉）
  } else {
    set('rt-state', '⏸ 页面不可见，已暂停刷新', '#8a93a3');
  }
}

async function submit(){
  const pw = el('pw').value, btn = el('btn'), err = el('err');
  if (!pw) return;
  btn.disabled = true; btn.textContent = '解密中…'; err.textContent = '';
  try{
    const html = await tryUnlock(pw);
    if (html === null){
      err.textContent = '口令不对，请重试';
      btn.disabled = false; btn.textContent = '进入看板';
      el('pw').select();
      return;
    }
    el('app').innerHTML = html;
    el('app').style.display = 'block';
    const gate = el('gate');
    if (gate) gate.remove();
    const rm = el('remember');
    if (rm && rm.checked) store.set('etf_pw', pw);
    startLive();
  }
  catch(e){
    err.textContent = '解密失败：' + ((e && e.message) ? e.message : String(e));
    btn.disabled = false; btn.textContent = '进入看板';
    if (window.console) console.error(e);
  }
}

// 脚本位于 body 末尾，DOM 已就绪，直接绑定；不再依赖 load 事件
function wire(){
  if (!el('pw')){                                  // 明文模式（--no-auth）
    const app = el('app'); if (app) app.style.display = 'block';
    startLive(); return;
  }
  if (!hasSubtle() && el('tip')){
    el('tip').innerHTML = '当前环境未提供 WebCrypto，已切换到内置 JS 解密实现（略慢，功能不变）。';
  }
  el('pw').addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  el('btn').addEventListener('click', submit);
  el('pw').focus();
  const saved = store.get('etf_pw');
  if (saved){ el('pw').value = saved; submit(); }
}
try{ wire(); }catch(e){ if (window.console) console.error(e); }
"""

GATE_HTML = '''
<div id="gate">
  <div class="gate-box">
    <div class="logo">ET</div>
    <h2>ETF 策略看板</h2>
    <p>本页内容已加密，请输入访问口令</p>
    <input id="pw" type="password" placeholder="访问口令" autocomplete="current-password">
    <button id="btn">进入看板</button>
    <div class="gate-err" id="err"></div>
    <label class="gate-opt"><input type="checkbox" id="remember"> 本次浏览器会话内记住（关闭窗口失效）</label>
    <div class="gate-tip" id="tip">口令保存在本机 dashboard/.secret.json（已加入 .gitignore，请勿外传）</div>
  </div>
</div>
'''


def render_body(rows, senti, top3, candidates, data_date, pool_size, core_size, live_codes):
    tot = max(senti["total"], 1)

    def pw(x):
        return round(x / tot * 100, 2)

    hs300 = next((r for r in rows if r["code"] == "510300" and r["ok"]), None)
    hs_price = _fmt(hs300["ind"]["close"]) if hs300 else "—"
    hpct = hs300["ind"]["pct_chg"] if hs300 else None
    s5 = "、".join(f"{r['name']} {r['ind']['ret5']:+.1f}%" for r in senti["strong5"])
    s20 = "、".join(f"{r['name']} {r['ind']['ret20']:+.1f}%" for r in senti["strong20"])
    fg = senti["fear_greed"]

    kpis = f'''
    <div class="kpi">
      <div class="lab">大盘趋势 · 沪深300ETF</div>
      <div class="val" style="font-size:20px">
        <span class="pill {STATE_STYLE.get(senti['market'], ('flat', '#b78103'))[0]}">{senti['market']}</span>
      </div>
      <div class="desc">{esc(senti['market_reason'] or '—')}</div>
      <div class="desc" style="margin-top:6px">现价 <b id="rt-510300-p">{hs_price}</b>
        <span id="rt-510300-c" style="color:{col(hpct)};font-weight:600">{pct_txt(hpct)}</span></div>
    </div>
    <div class="kpi">
      <div class="lab">恐慌贪婪指数</div>
      <div class="gauge-wrap">
        {gauge_svg(fg)}
        <div><div style="font-weight:700;font-size:15px">{senti['fg_state']}</div>
          <div class="desc">{esc(senti['fg_desc'])}</div>
          <div class="desc" style="margin-top:4px">平均RSI <b>{senti['avg_rsi']}</b></div></div>
      </div>
    </div>
    <div class="kpi">
      <div class="lab">池内多空分布（{tot} 只）</div>
      <div class="val" style="font-size:19px">
        <span style="color:var(--up)">{senti['bull']}</span> /
        <span style="color:#c9a227">{senti['flat']}</span> /
        <span style="color:var(--down)">{senti['bear']}</span></div>
      <div class="dist"><i class="i-bull" style="width:{pw(senti['bull'])}%"></i>
        <i class="i-flat" style="width:{pw(senti['flat'])}%"></i>
        <i class="i-bear" style="width:{pw(senti['bear'])}%"></i></div>
      <div class="dist-lab"><span class="l-bull">多头 <b>{pw(senti['bull']):.0f}%</b></span>
        <span class="l-flat">震荡 <b>{pw(senti['flat']):.0f}%</b></span>
        <span class="l-bear">空头 <b>{pw(senti['bear']):.0f}%</b></span></div>
    </div>
    <div class="kpi">
      <div class="lab">相对强弱（轮动视角）</div>
      <div class="desc" style="margin-top:6px"><b style="color:var(--tx3)">近5日</b><br>{esc(s5)}</div>
      <div class="desc" style="margin-top:8px"><b style="color:var(--tx3)">近20日</b><br>{esc(s20)}</div>
    </div>'''

    risk_chips = "".join(f'<span class="chip warn">{esc(r)}</span>' for r in senti["risks"])

    cards = []
    for idx, r in enumerate(top3, 1):
        ind, plan = r["ind"], r["plan"]
        st_cls = STATE_STYLE.get(r["state"], ("flat", "#b78103"))[0]
        act_cls = ACTION_STYLE.get(r["action"], "muted")
        fmt = (lambda v: f"{v:.3f}") if plan["nd"] == 3 else (lambda v: f"{v:.2f}")
        kv = [("MA20", _fmt(ind["ma20"]), "inherit"),
              ("MA60", _fmt(ind["ma60"]), "inherit"),
              ("RSI(14)", str(ind["rsi"]), "inherit"),
              ("MACD", f"{ind['macd']:+.4f}", col(ind["macd"])),
              ("布林上下轨", f"{_fmt(ind['boll_lower'])}~{_fmt(ind['boll_upper'])}", "inherit"),
              ("量比", str(ind["vol_ratio"]), "inherit"),
              ("近5日", pct_txt(ind["ret5"]), col(ind["ret5"])),
              ("近20日", pct_txt(ind["ret20"]), col(ind["ret20"]))]
        kv_html = "".join(f'<div class="kv"><span class="k">{k}</span>'
                          f'<span class="v" style="color:{c}">{v}</span></div>' for k, v, c in kv)
        rsi = ind["rsi"] or 50
        ma_pos = "价在MA20上方" if (ind["ma20"] and ind["close"] > ind["ma20"]) else "价在MA20下方"
        reason_chips = "".join(f'<span class="reason">{esc(x)}</span>'
                               for x in r["reason"].split("、") if x)
        grid_html = ('<div class="gline">网格挂单 '
                     f'<code>{plan["grid_buy"][0]}</code> <code>{plan["grid_buy"][1]}</code> '
                     f'<code>{plan["grid_buy"][2]}</code>　每格 {plan["grid_cell"]}% 仓位</div>'
                     if plan["grid_pos"] else "")
        cards.append(f'''
      <div class="card etf">
        <div class="etf-head">
          <div class="row1"><span class="rank r{idx}">{idx}</span>
            <span class="ename">{esc(r['name'])}</span><span class="ecode">{r['code']}</span>
            <span class="cat">{esc(r['cat'])}</span></div>
          <div class="row2"><span class="price" id="rt-{r['code']}-p">{fmt(ind['close'])}</span>
            <span class="chg" id="rt-{r['code']}-c" style="color:{col(ind['pct_chg'])}">{pct_txt(ind['pct_chg'])}</span>
            <span class="pill {st_cls}">{r['state']}</span></div>
        </div>
        <div class="spark-wrap">{sparkline_svg(ind['series_close'], ind['series_ma20'])}</div>
        <div class="body">
          <div class="score-row"><span class="s-lab">综合评分</span>
            <span class="bar"><i style="width:{min(max(r['score'], 0), 10) * 10:.0f}%"></i></span>
            <span class="s-val">{r['score']}/10</span></div>
          <div class="grid2">{kv_html}</div>
          <div><div class="kv" style="border:none;padding:0"><span class="k">RSI 位置</span>
            <span class="v">{rsi}</span></div>
            <div class="rsi-bar"><i style="left:{max(0, min(100, rsi)):.1f}%"></i></div></div>
          <div class="plan">
            <div class="plan-hd"><span class="act {act_cls}">{esc(r['action'])}</span>
              <span class="why">{esc(r['advice'])}</span></div>
            <div class="plan-grid">
              <div class="cell"><div class="cl">建议买入区间</div>
                <div class="cv">{plan['buy_lo']} ~ {plan['buy_hi']}</div>
                <div class="cx">{esc(plan['basis'])} · {ma_pos}</div></div>
              <div class="cell"><div class="cl">目标位</div><div class="cv">{plan['target1']}</div>
                <div class="cx">激进 {plan['target2']} ｜ 空间 {plan['upside']:+.1f}%</div></div>
              <div class="cell"><div class="cl">止损位</div><div class="cv">{plan['stop']}</div>
                <div class="cx">触发即离场 · {plan['stop_pct']:+.1f}%</div></div>
            </div>
            <div class="pos">仓位建议 <b>{plan['total_pos']:g}%</b>
              <span>（底仓 {plan['base_pos']:g}% + 网格 {plan['grid_pos']:g}%，单只上限 15%）</span></div>
            {grid_html}
          </div>
          <div class="reasons">{reason_chips}</div>
        </div>
      </div>''')

    cand_rows = []
    cmap = {"多头": "#e5484d", "震荡": "#e2c96a", "空头": "#17a673"}
    for i, r in enumerate(candidates, 1):
        ind = r["ind"]
        fmt = (lambda v: f"{v:.3f}") if r["plan"]["nd"] == 3 else (lambda v: f"{v:.2f}")
        cand_rows.append(
            f'<tr><td style="color:var(--tx3)">{i}</td>'
            f'<td class="nm"><span class="dot-s" style="background:{cmap.get(r["state"], "#ccc")}"></span>'
            f'{esc(r["name"])}<span class="cd">{r["code"]} · {esc(r["cat"])}</span></td>'
            f'<td id="rt-{r["code"]}-p">{fmt(ind["close"])}</td>'
            f'<td id="rt-{r["code"]}-c" style="color:{col(ind["pct_chg"])};font-weight:600">{pct_txt(ind["pct_chg"])}</td>'
            f'<td>{r["score"]}</td><td>{r["state"]}</td><td>{esc(r["action"])}</td>'
            f'<td style="color:{col(ind["ret5"])}">{pct_txt(ind["ret5"])}</td>'
            f'<td style="color:{col(ind["ret20"])}">{pct_txt(ind["ret20"])}</td></tr>')

    return f'''
  <div class="top">
    <div>
      <div class="title"><span class="dot"></span>ETF 购买策略看板</div>
      <div class="sub">网格 + 趋势跟踪混合策略 v1.1 ｜ 标的池 <b>{pool_size}</b> 只
        （核心 {core_size} 只 + 全市场成交额优选 {max(pool_size - core_size, 0)} 只）</div>
    </div>
    <div class="stamp">
      <span class="live"><span class="pulse"></span>每 {REFRESH_SECONDS} 秒自动刷新 · <span id="rt-tick">{REFRESH_SECONDS}</span>s</span>
      <button class="lock" id="lockBtn" type="button" title="停止刷新并回到登录页">锁定</button><br>
      数据日期 <b>{data_date}</b> ｜ 实时 <b id="rt-time">—</b><br>
      <span id="rt-state">等待首次拉取…</span>
    </div>
  </div>

  <section class="card sentiment">
    <div class="sec-title"><span class="bar"></span>今日市场情绪扫描</div>
    <div class="kpis">{kpis}</div>
    <div class="risks"><div class="lab">主要风险点</div><div class="chips">{risk_chips}</div></div>
  </section>

  <div class="sec-title" style="margin:22px 0 14px"><span class="bar"></span>今日重点推荐 ETF · Top 3</div>
  <section class="top3">{"".join(cards)}</section>

  <section class="card cand">
    <div class="sec-title"><span class="bar"></span>候选池 Top 10（从全池 {pool_size} 只打分产出）
      <span class="tail">价格每 30 秒同步刷新</span></div>
    <table>
      <thead><tr><th>#</th><th>名称</th><th>现价</th><th>涨跌</th><th>得分</th>
        <th>状态</th><th>建议</th><th>5日</th><th>20日</th></tr></thead>
      <tbody>{"".join(cand_rows)}</tbody>
    </table>
  </section>

  <div class="foot">
    <b>执行铁律</b>：单只 ETF ≤ 15% ｜ 总仓位 ≤ 90%（现金 ≥ 10%） ｜ 每次操作只动 1 份 ｜ 同时持仓 ≤ 5 只。<br>
    <b>风控</b>：网格单 -5% 止损、趋势单 -8% 止损、收盘跌破 MA60 次日清仓；账户回撤 ≥8% 降仓至 50%、≥12% 降至 30%、≥15% 清仓休整。<br>
    <b>选池</b>：{core_size} 只核心池固定保底 + 东财全市场 ETF 按成交额降序补充，剔除货币/现金/债券类，同一指数只保留成交额最大的一只。<br>
    <b>数据</b>：日K 用腾讯/新浪公开行情（前复权）算指标，实时价来自腾讯 qt.gtimg.cn，每 {REFRESH_SECONDS} 秒重拉；共 {tot} 只样本。<br>
    <b>流量控制</b>：登录成功前不发起任何行情请求；<b>标签页切到后台自动暂停</b>、<b>离开页面立即停止</b>，
    上一轮未返回不发下一轮；点右上角「锁定」可随时停止刷新并回到登录页。右上角状态栏显示已拉取次数与累计流量。<br>
    <b>刷新</b>：重新运行 <code style="font-family:ui-monospace,Consolas,monospace">python fund-strategy/etf_dashboard.py</code> 更新指标与打分。<br>
    ⚠️ 本页面仅为量化策略研究参考，基于公开市场数据生成，<b>不构成任何投资建议</b>。市场有风险，投资需谨慎。
  </div>
  <div id="codes" hidden data-codes='{json.dumps(live_codes)}' data-interval='{REFRESH_SECONDS}'></div>'''


def build_html(body, password, secret):
    if not password:
        return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETF 购买策略看板</title>
<style>{CSS}</style></head>
<body><div class="wrap" id="app">{body}</div>
<script>{LOGIN_JS}</script>
</body></html>'''

    payload = encrypt(body.encode("utf-8"), password,
                      salt=_unb64(secret["salt"]) if secret.get("salt") else None)
    if not secret.get("salt"):
        secret.update({"salt": payload["salt"], "iter": payload["iter"], "password": password})
        save_secret(secret)
    cfg = json.dumps({"salt": payload["salt"], "iter": payload["iter"],
                      "auth": payload["auth"], "ct": payload["ct"]}, ensure_ascii=False)
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>ETF 购买策略看板</title>
<style>{CSS}</style></head>
<body>
{GATE_HTML}
<div id="app" class="wrap"></div>
<script>window.__CFG__ = {cfg};</script>
<script>{LOGIN_JS}</script>
</body></html>'''


# ================================================================ 八、主流程
def load_prev_codes(today):
    if not os.path.exists(STATE_PATH):
        return set()
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
        return set(st.get("codes", [])) if st.get("date") != today else set()
    except Exception:                                           # noqa: BLE001
        return set()


def save_state(today, codes, data_date):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"date": today, "data_date": data_date, "codes": list(codes)},
                      f, ensure_ascii=False, indent=2)
    except Exception:                                           # noqa: BLE001
        pass


def serve(port=8899):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=OUT_DIR, **kw)

        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"🌐 本地服务已启动：{url}（Ctrl+C 结束）")
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def main():
    argv = sys.argv
    no_auth = "--no-auth" in argv
    core_only = "--core-only" in argv
    force = "--force" in argv
    offline = "--offline" in argv
    do_serve = "--serve" in argv
    set_pw = "--set-password" in argv

    pool_size = 60
    if "--pool-size" in argv:
        try:
            pool_size = int(argv[argv.index("--pool-size") + 1])
        except Exception:                                       # noqa: BLE001
            pass

    os.makedirs(OUT_DIR, exist_ok=True)
    print("🚀 ETF 策略看板生成中…")

    # ---- 标的池 + K 线 ----
    if offline:
        kmap = load_cache()
        pool = [t for t in CORE_POOL if t[0] in kmap] or list(CORE_POOL)
        print(f"📦 离线模式：缓存 {len(kmap)} 只，本次使用 {len(pool)} 只")
    else:
        if force:
            save_cache({})
        if core_only:
            pool = list(CORE_POOL)
            print(f"📌 核心池模式：固定 {len(pool)} 只")
            kmap = ensure_klines(pool)
        else:
            print("🧭 扫描全市场 ETF 清单…")
            # 先多取一批候选，抓完 K 线再按相关性去重，保证最终接近目标只数
            raw_pool, _ = build_pool(pool_size + max(30, int(pool_size * 0.7)))
            print(f"   候选 {len(raw_pool)} 只，抓取 K 线后去重…")
            kmap = ensure_klines(raw_pool)
            pool = diversify(raw_pool, kmap, pool_size, [c for c, _, _, _ in CORE_POOL])
            print(f"📌 标的池：核心 {len(CORE_POOL)} 只 + 自动优选 {len(pool) - len(CORE_POOL)} 只"
                  f" = {len(pool)} 只（相关性去重剔除 {len(raw_pool) - len(pool)} 只）")

    rows = build_rows(pool, kmap)
    valid = [r for r in rows if r["ok"]]
    failed = [r["name"] for r in rows if not r["ok"]]
    print(f"✅ 有效样本 {len(valid)} / {len(rows)}" + (
        f"　数据不足：{'、'.join(failed[:6])}{'…' if len(failed) > 6 else ''}" if failed else ""))

    senti = sentiment_scan(valid)
    today = datetime.now().strftime("%Y-%m-%d")
    top3 = pick_top3(valid, load_prev_codes(today))
    save_state(today, [r["code"] for r in top3], valid[0]["ind"]["date"] if valid else today)

    ranked = sorted(valid, key=lambda x: -x["score"])
    picked = {t["code"] for t in top3}
    candidates = [r for r in ranked if r["code"] not in picked][:10]

    data_date = max((r["ind"]["date"] for r in valid), default=today)

    # ---- 实时刷新所需代码（随正文一起加密）----
    prefix_of = {c: p for c, _, _, p in CORE_POOL}
    hs300_row = next((x for x in valid if x["code"] == "510300"), None)
    live, seen = [], set()
    for r in ([hs300_row] + list(top3) + candidates):
        if r and r["code"] not in seen:
            live.append(prefix_of.get(r["code"], r.get("prefix", "sh")) + r["code"])
            seen.add(r["code"])

    body = render_body(rows, senti, top3, candidates, data_date, len(valid), len(CORE_POOL), live)

    # ---- 口令 ----
    secret = load_secret()
    password = None
    if not no_auth:
        password = obtain_password(secret.get("password"), force_set=set_pw)
        secret["password"] = password

    html = build_html(body, password, secret)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n📊 大盘：{senti['market']} ｜ 恐慌贪婪 {senti['fear_greed']}/100（{senti['fg_state']}）"
          f" ｜ 多头 {senti['bull']} / 震荡 {senti['flat']} / 空头 {senti['bear']}")
    print(f"🔥 Top3（从 {len(valid)} 只标的中选出）：")
    for i, r in enumerate(top3, 1):
        print(f"   {i}. {r['name']}({r['code']}) {r['score']}分 ｜ {r['state']} ｜ {r['action']}"
              f" ｜ 买区 {r['plan']['buy_lo']}~{r['plan']['buy_hi']} ｜ 目标 {r['plan']['target1']}"
              f" ｜ 止损 {r['plan']['stop']}")
    print(f"\n✅ 看板已生成：{OUT_HTML}")
    if password:
        print("🔐 已加密：打开页面需输入口令（--no-auth 生成明文版；--serve 起本地服务打开）")
        print(f"AUTH_CODE: {password}")
    if do_serve:
        serve()


if __name__ == "__main__":
    main()
