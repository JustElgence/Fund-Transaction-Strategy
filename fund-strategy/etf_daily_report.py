#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 网格 + 趋势跟踪混合策略 —— 每日报告生成器 v1.1
v1.1 更新（解决 Top3 固定不变问题）：
  1. 标的池 10 → 25 只（宽基/成长/行业/港股/美股/债券/商品）
  2. 打分加入"相对强弱"（5日/20日涨幅池内排名，随市场轮动）
  3. 加入"新鲜信号"加分（近3日MACD金叉、放量突破20日新高、RSI上穿50）
  4. Top3 选择加入类别分散约束（每类最多1只，不足时放宽到2只/类）
数据源：新浪 (akshare fund_etf_hist_sina)，仅公开技术指标
输出：~/.openclaw/workspace/etf-strategy/reports/etf_report_YYYY-MM-DD.md
"""
import os, sys, json, math
from datetime import datetime

import akshare as ak
import pandas as pd
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE, "reports")
os.makedirs(REPORT_DIR, exist_ok=True)

# ---------- 标的池（25只，覆盖主要资产类别） ----------
ETF_POOL = [
    # A股宽基
    ("510300", "沪深300ETF", "A股宽基", "sh"),
    ("510050", "上证50ETF", "A股宽基", "sh"),
    ("510500", "中证500ETF", "A股宽基", "sh"),
    ("512100", "中证1000ETF", "A股宽基", "sh"),
    # A股成长
    ("588000", "科创50ETF", "A股成长", "sh"),
    ("159915", "创业板ETF", "A股成长", "sz"),
    # A股行业
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
    # 港股
    ("513180", "恒生科技ETF", "港股", "sh"),
    ("159920", "恒生ETF", "港股", "sz"),
    ("513050", "中概互联ETF", "港股", "sh"),
    # 美股
    ("513500", "标普500ETF", "美股", "sh"),
    ("513100", "纳指100ETF", "美股", "sh"),
    ("159941", "纳指ETF", "美股", "sz"),
    # 债券 / 商品
    ("511010", "国债ETF", "债券", "sh"),
    ("518880", "黄金ETF", "商品", "sh"),
]

# ---------- 技术指标 ----------
def compute_indicators(df):
    """输入日K(含close/high/low/volume)，输出指标dict"""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float)

    def ma(n):
        return close.rolling(n).mean()

    ma5, ma20, ma60 = ma(5), ma(20), ma(60)

    # MACD (12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2

    # RSI(14) - Wilder
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)

    # BOLL(20,2)
    mid = ma20
    std = close.rolling(20).std()
    upper = mid + 2 * std
    lower = mid - 2 * std

    # ATR(14)
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()

    i = len(df) - 1
    prev = i - 1
    c = close.iloc[i]
    vol_ma5 = vol.rolling(5).mean().iloc[i]

    # 近3日是否出现 MACD金叉 / RSI上穿50（新鲜信号）
    golden_recent = False
    cross50_recent = False
    for k in range(max(2, i - 2), i + 1):
        if dif.iloc[k] > dea.iloc[k] and dif.iloc[k - 1] <= dea.iloc[k - 1]:
            golden_recent = True
        rk, rk1 = rsi.iloc[k], rsi.iloc[k - 1]
        if not math.isnan(rk) and not math.isnan(rk1) and rk >= 50 > rk1:
            cross50_recent = True

    ret5 = (c / close.iloc[-6] - 1) * 100 if len(close) > 6 else None
    ret20 = (c / close.iloc[-21] - 1) * 100 if len(close) > 21 else None
    high20 = float(close.iloc[-20:].max())
    breakout20 = bool(c >= high20 * 0.999)  # 触及/突破20日新高

    return {
        "close": round(float(c), 4),
        "ma5": round(float(ma5.iloc[i]), 4) if not math.isnan(ma5.iloc[i]) else None,
        "ma20": round(float(ma20.iloc[i]), 4) if not math.isnan(ma20.iloc[i]) else None,
        "ma60": round(float(ma60.iloc[i]), 4) if not math.isnan(ma60.iloc[i]) else None,
        "ma20_slope_up": bool(ma20.iloc[i] > ma20.iloc[prev]) if not math.isnan(ma20.iloc[i]) else False,
        "dif": round(float(dif.iloc[i]), 4),
        "dea": round(float(dea.iloc[i]), 4),
        "macd": round(float(macd.iloc[i]), 4),
        "macd_golden": bool(dif.iloc[i] > dea.iloc[i] and dif.iloc[prev] <= dea.iloc[prev]),
        "macd_dead": bool(dif.iloc[i] < dea.iloc[i] and dif.iloc[prev] >= dea.iloc[prev]),
        "macd_golden_recent": golden_recent,
        "rsi": round(float(rsi.iloc[i]), 1) if not math.isnan(rsi.iloc[i]) else None,
        "rsi_cross50": cross50_recent,
        "boll_upper": round(float(upper.iloc[i]), 4),
        "boll_mid": round(float(mid.iloc[i]), 4),
        "boll_lower": round(float(lower.iloc[i]), 4),
        "atr": round(float(atr.iloc[i]), 4),
        "vol_ratio": round(float(vol.iloc[i] / vol_ma5), 2) if vol_ma5 > 0 else None,
        "pct_chg": round(float((c / close.iloc[prev] - 1) * 100), 2),
        "ret5": round(float(ret5), 2) if ret5 is not None else None,
        "ret20": round(float(ret20), 2) if ret20 is not None else None,
        "high20": high20,
        "breakout20": breakout20,
        "low20": float(close.iloc[-20:].min()),
        "date": str(df["date"].iloc[i]),
    }

# ---------- 状态判断 ----------
def trend_state(ind):
    c, m20, m60 = ind["close"], ind["ma20"], ind["ma60"]
    if m20 is None or m60 is None:
        return "震荡", "数据不足"
    if c > m20 > m60 and ind["ma20_slope_up"]:
        return "多头", f"价>MA20({m20})>MA60({m60})"
    if c < m20 < m60 or (m20 < m60 and not ind["ma20_slope_up"]) or (c < m20 and not ind["ma20_slope_up"]):
        return "空头", f"MA20({m20})<MA60({m60})或价<MA20，均线向下"
    return "震荡", f"MA20({m20})走平缠绕"

# ---------- 打分模型 v1.1 ----------
def score_etf(ind, ctx):
    """
    ctx: {"ret5_rank": 0-1, "ret20_rank": 0-1} 池内相对强弱百分位
    满分10分：趋势3 + 相对动量3 + 位置2 + 新鲜信号2 + 加分0.5，跌破MA60扣2
    """
    if ind["ma20"] is None or ind["ma60"] is None:
        return 0, "数据不足"
    s = 0.0
    reasons = []
    # 趋势 3分
    if ind["ma20"] > ind["ma60"]:
        s += 1.5; reasons.append("MA20>MA60")
    if ind["close"] > ind["ma20"]:
        s += 0.75; reasons.append("价>MA20")
    if ind["ma20_slope_up"]:
        s += 0.75; reasons.append("MA20向上")
    # 相对动量 3分（谁当下更强谁得分高 → 随市场轮动）
    s += ctx["ret5_rank"] * 1.5
    s += ctx["ret20_rank"] * 1.5
    reasons.append(f"5日{ind['ret5']:+.1f}%(池内{ctx['ret5_rank']*100:.0f}分位)")
    reasons.append(f"20日{ind['ret20']:+.1f}%(池内{ctx['ret20_rank']*100:.0f}分位)")
    # 位置 2分
    if ind["close"] <= ind["boll_lower"] * 1.01:
        s += 2; reasons.append("布林下轨(买点区)")
    elif ind["close"] <= ind["boll_mid"] * 1.02:
        s += 1; reasons.append("布林中轨附近")
    else:
        s += 0; reasons.append("接近上轨(偏高)")
    # 新鲜信号 2分
    if ind["macd_golden_recent"]:
        s += 1; reasons.append("近3日MACD金叉")
    if ind["breakout20"]:
        if ind["vol_ratio"] and ind["vol_ratio"] >= 1.1:
            s += 1; reasons.append(f"放量突破20日新高(量比{ind['vol_ratio']})")
        else:
            s += 0.5; reasons.append("触及20日新高")
    if ind["rsi_cross50"]:
        s += 0.5; reasons.append("RSI上穿50")
    # 加分
    if ind["vol_ratio"] and ind["vol_ratio"] >= 1.5 and ind["pct_chg"] > 0:
        s += 0.5; reasons.append("放量上涨")
    # 破MA60 出局
    if ind["close"] < ind["ma60"]:
        s -= 2; reasons.append("跌破MA60(扣分)")
    return round(min(s, 10.0), 1), "、".join(reasons)

# ---------- 情绪扫描 ----------
def sentiment(rows):
    bull = sum(1 for r in rows if r["state"] == "多头")
    bear = sum(1 for r in rows if r["state"] == "空头")
    rs = [r["ind"]["rsi"] for r in rows if r["ind"] and r["ind"]["rsi"] is not None]
    avg_rsi = round(sum(rs) / len(rs), 1) if rs else None
    # 大盘 = 沪深300ETF
    hs300 = next(r for r in rows if r["code"] == "510300")
    market = hs300["state"]
    # 恐慌贪婪：用多头占比 + 平均RSI估算 0-100
    bull_ratio = bull / len(rows)
    if avg_rsi:
        fear_greed = int(round(bull_ratio * 60 + (avg_rsi / 100) * 40))
    else:
        fear_greed = int(round(bull_ratio * 100))
    if fear_greed >= 70: fg_state = "贪婪(偏高，注意止盈)"
    elif fear_greed <= 30: fg_state = "恐慌(偏低，关注超卖机会)"
    else: fg_state = "中性"
    risks = []
    for r in rows:
        if r["state"] == "空头":
            risks.append(f"{r['name']}空头趋势")
        elif r["ind"] and r["ind"]["rsi"] is not None and r["ind"]["rsi"] > 72:
            risks.append(f"{r['name']}RSI超买({r['ind']['rsi']})")
    if avg_rsi and avg_rsi > 70:
        risks.append(f"池内平均RSI {avg_rsi} 过热")
    return {
        "market": market,
        "bull": bull, "bear": bear,
        "avg_rsi": avg_rsi,
        "fear_greed": fear_greed, "fg_state": fg_state,
        "risks": risks[:6] if risks else ["无显著风险点"],
    }

# ---------- 操作建议 ----------
def action_advice(r, ind):
    st = r["state"]
    name = r["name"]
    if ind["close"] < ind["ma60"]:
        return "减仓/清仓", f"跌破MA60({ind['ma60']})，趋势走坏，次日清仓"
    if st == "多头":
        if ind["rsi"] is not None and ind["rsi"] >= 70:
            return "减仓", f"RSI{ind['rsi']}超买+接近布林上轨({ind['boll_upper']})，分批止盈"
        if ind["rsi"] is not None and ind["rsi"] <= 45 and ind["close"] <= ind["boll_mid"]:
            return "加仓", f"多头回调至MA20附近(RSI{ind['rsi']})，分批买入区"
        return "持有", f"多头趋势完好，持有底仓；网格仓高抛低吸"
    if st == "震荡":
        if ind["rsi"] is not None and ind["rsi"] < 30:
            return "加仓(网格)", f"RSI{ind['rsi']}超卖+布林下轨({ind['boll_lower']})，网格买入1格"
        if ind["rsi"] is not None and ind["rsi"] > 70:
            return "减仓(网格)", f"RSI{ind['rsi']}超买+布林上轨({ind['boll_upper']})，网格卖出1格"
        return "观望", "震荡无趋势，只做网格或不动"
    return "观望/减仓", f"空头趋势({ind['ma20']}之下)，暂停网格，反弹减仓"

# ---------- 主流程 ----------
def fetch_all():
    raw = []
    for code, name, cat, prefix in ETF_POOL:
        try:
            sym = prefix + code
            df = ak.fund_etf_hist_sina(symbol=sym)
            if df is None or len(df) < 70:
                raw.append({"code": code, "name": name, "cat": cat, "state": "震荡",
                            "ind": None, "score": 0, "reason": "数据不足", "action": "跳过", "advice": "数据获取失败"})
                continue
            ind = compute_indicators(df)
            state, state_reason = trend_state(ind)
            raw.append({"code": code, "name": name, "cat": cat, "state": state,
                        "state_reason": state_reason, "ind": ind, "score": 0,
                        "reason": "", "action": "", "advice": ""})
        except Exception as e:
            raw.append({"code": code, "name": name, "cat": cat, "state": "震荡",
                        "ind": None, "score": 0, "reason": f"抓取失败:{e}", "action": "跳过", "advice": "数据获取失败"})

    # 池内相对强弱排名（百分位 0-1）
    valid = [r for r in raw if r["ind"]]
    r5 = pd.Series([r["ind"]["ret5"] for r in valid]).rank(pct=True).tolist()
    r20 = pd.Series([r["ind"]["ret20"] for r in valid]).rank(pct=True).tolist()
    for r, a, b in zip(valid, r5, r20):
        ctx = {"ret5_rank": float(a), "ret20_rank": float(b)}
        score, reason = score_etf(r["ind"], ctx)
        r["score"] = score
        r["reason"] = reason
        r["ret5_rank_pct"] = int(round(float(a) * 100))
        r["ret20_rank_pct"] = int(round(float(b) * 100))
        action, advice = action_advice({"state": r["state"], "name": r["name"]}, r["ind"])
        r["action"] = action
        r["advice"] = advice
    return raw

def load_prev_top3(date_str):
    """读取上一份报告(今天之前)的 Top3 代码，用于轮换 tie-break"""
    import glob
    files = sorted(glob.glob(os.path.join(REPORT_DIR, "etf_report_*.md")))
    prev = None
    for f in files:
        base = os.path.basename(f).replace("etf_report_", "").replace(".md", "")
        if base < date_str:
            prev = f
    if not prev:
        return set()
    codes = set()
    in_top3 = False
    with open(prev, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("## 🎯"):
                in_top3 = True
                continue
            if in_top3:
                if line.startswith("## "):
                    break
                parts = line.strip().split("|")
                if len(parts) >= 3 and parts[2].strip().isdigit() and len(parts[2].strip()) == 6:
                    codes.add(parts[2].strip())
    return codes

def pick_top3(rows, prev_codes=None):
    """Top3 选择：得分排序 + 类别分散（每类最多1只，不足放宽到2只/类）
    + 轮换 tie-break（得分相同时优先选上一期不在Top3的）"""
    prev_codes = prev_codes or set()
    ranked = sorted([r for r in rows if r["ind"]],
                    key=lambda x: (-x["score"], x["code"] in prev_codes, x["code"]))
    picks, picked, cat_count = [], set(), {}
    # 第一轮：每类最多 1 只
    for r in ranked:
        if len(picks) >= 3:
            break
        if r["code"] in picked or cat_count.get(r["cat"], 0) >= 1:
            continue
        picks.append(r); picked.add(r["code"])
        cat_count[r["cat"]] = cat_count.get(r["cat"], 0) + 1
    # 第二轮：不足 3 只时放宽（每类最多 2 只）
    if len(picks) < 3:
        for r in ranked:
            if len(picks) >= 3:
                break
            if r["code"] in picked or cat_count.get(r["cat"], 0) >= 2:
                continue
            picks.append(r); picked.add(r["code"])
            cat_count[r["cat"]] = cat_count.get(r["cat"], 0) + 1
    return picks

def build_report(rows, date_str):
    senti = sentiment(rows)
    top3 = pick_top3(rows, load_prev_top3(date_str))
    ranked_all = sorted([r for r in rows if r["ind"]], key=lambda x: -x["score"])

    L = []
    A = L.append
    A(f"# 📊 ETF 网格+趋势 每日策略报告 — {date_str}")
    A(f"\n> ⚠️ 仅供策略研究参考，不构成投资建议 ｜ 数据：新浪公开行情 ｜ 标的池 {len([r for r in rows if r['ind']])} 只")
    A(f"\n## 📊 今日市场情绪扫描")
    A(f"- 大盘趋势(沪深300ETF)：**{senti['market']}**")
    A(f"- 恐慌贪婪指数：**{senti['fear_greed']}/100（{senti['fg_state']}）**")
    A(f"- 池内状态：多头 {senti['bull']} 只 ｜ 空头 {senti['bear']} 只 ｜ 平均RSI {senti['avg_rsi']}")
    A(f"- 主要风险点：{('；'.join(senti['risks']))}")

    A(f"\n## 🔥 相对强弱观察（市场轮动视角）")
    strong5 = sorted([r for r in ranked_all if r["ind"]["ret5"] is not None], key=lambda x: -x["ind"]["ret5"])[:3]
    strong20 = sorted([r for r in ranked_all if r["ind"]["ret20"] is not None], key=lambda x: -x["ind"]["ret20"])[:3]
    s5 = '、'.join(f"{r['name']}({r['ind']['ret5']:+.1f}%)" for r in strong5)
    s20 = '、'.join(f"{r['name']}({r['ind']['ret20']:+.1f}%)" for r in strong20)
    A(f"- 近5日最强：{s5}")
    A(f"- 近20日最强：{s20}")

    A(f"\n## 🎯 今日重点推荐 ETF (Top 3)")
    A(f"| ETF代码 | 名称 | 类别 | 当前价 | 状态 | 得分 | 操作建议 | 触发条件 | 网格参考区间 |")
    A(f"| :------ | :--- | :-- | :----: | :--: | :--: | :------- | :------- | :----------- |")
    for r in top3:
        ind = r["ind"]
        grid_low = round(ind["boll_lower"], 3)
        grid_high = round(ind["boll_upper"], 3)
        A(f"| {r['code']} | {r['name']} | {r['cat']} | {ind['close']} | {r['state']} | {r['score']} | **{r['action']}** | {r['advice']} | {grid_low}~{grid_high} |")

    A(f"\n### Top3 技术指标明细")
    for r in top3:
        ind = r["ind"]
        A(f"- **{r['name']}({r['code']})**：MA20={ind['ma20']} MA60={ind['ma60']} ｜ RSI={ind['rsi']} ｜ MACD={ind['macd']}({'金叉' if ind['macd_golden'] else '死叉' if ind['macd_dead'] else '中性'}) ｜ 布林 {ind['boll_lower']}~{ind['boll_upper']} ｜ 量比={ind['vol_ratio']} ｜ 5日{ind['ret5']:+.1f}%(池内{r['ret5_rank_pct']}分位)/20日{ind['ret20']:+.1f}%(池内{r['ret20_rank_pct']}分位) ｜ 评分依据：{r['reason']}")

    A(f"\n## 📋 全池打分排名（{len(ranked_all)} 只，市场全景）")
    A(f"| 排名 | 代码 | 名称 | 类别 | 现价 | 状态 | 得分 | 5日% | 20日% | 操作 |")
    A(f"| :--: | :-- | :--- | :-- | :--: | :--: | :--: | :--: | :--: | :---- |")
    for i, r in enumerate(ranked_all, 1):
        ind = r["ind"]
        A(f"| {i} | {r['code']} | {r['name']} | {r['cat']} | {ind['close']} | {r['state']} | {r['score']} | {ind['ret5']:+.1f} | {ind['ret20']:+.1f} | {r['action']} |")

    A(f"\n## ⚖️ 持仓调整计划")
    adds = [r for r in rows if r["ind"] and r["action"].startswith("加仓")]
    cuts = [r for r in rows if r["ind"] and r["action"].startswith("减仓")]
    holds = [r for r in rows if r["ind"] and r["action"].startswith("持有")]
    waits = [r for r in rows if r["ind"] and r["action"].startswith("观望")]

    if adds:
        A(f"\n**🟢 加仓区**（跌到支撑，分批买入）：")
        for r in adds:
            A(f"- {r['name']}({r['code']})：{r['advice']}")
    else:
        A(f"\n**🟢 加仓区**：今日无（多头回调或超卖买点出现时再分批介入）")
    if cuts:
        A(f"\n**🔴 减仓区**（涨到压力/破位，分批止盈）：")
        for r in cuts:
            A(f"- {r['name']}({r['code']})：{r['advice']}")
    else:
        A(f"\n**🔴 减仓区**：今日无（无超买或破位信号）")
    if holds:
        A(f"\n**🟡 持有区**：{'、'.join(r['name'] + '(' + r['code'] + ')' for r in holds)}")
    if waits:
        A(f"\n**⚪ 观望区**：{'、'.join(r['name'] + '(' + r['code'] + ')' for r in waits)}")

    A(f"\n## 🛠️ 策略逻辑复盘")
    if top3 and top3[0]["ind"]:
        t = top3[0]
        A(f"今日最高分 {t['name']}({t['code']}) 得 {t['score']} 分：{t['reason']}。"
          f"{t['advice']}。整体市场为{senti['market']}格局，"
          f"操作上{'顺势持有/低吸为主' if senti['bull'] >= senti['bear'] else '控制仓位、防守为主'}，"
          f"严格执行单只≤15%、总仓位≤90%、现金≥10% 三大铁律。")

    A(f"\n## 📋 今日执行清单")
    A(f"1. 对照 Top3 与持仓，仅执行符合规则的加减仓（每次≤1份/10%）。")
    A(f"2. 检查止损：收盘跌破MA60的持仓 → 次日开盘清仓。")
    A(f"3. 检查回撤：总资产较最高点回撤≥8%降仓至50%、≥12%降仓至30%、≥15%清仓休整。")
    A(f"4. 记录操作到 trades.log。")
    A(f"\n---\n*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    return "\n".join(L)

def main():
    date_str = datetime.now().strftime("%Y-%m-%d")
    rows = fetch_all()
    report = build_report(rows, date_str)
    path = os.path.join(REPORT_DIR, f"etf_report_{date_str}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"✅ 报告已生成: {path}")
    print(report)

if __name__ == "__main__":
    main()
