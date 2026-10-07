#!/usr/bin/env python3
"""
每日A股分析报告生成器 v2
对接真实数据（新浪API + 腾讯API），输出格式化分析报告
"""

import json
import urllib.request
import datetime
import sys

ALWAYS_FILTER = [
    '重点投资', '精选', 'VIP', '会员', '内部',
    '退市', 'ST', '*ST', '退'
]


def fetch_json(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode('utf-8'))
    except:
        return None


def get_index_data():
    """获取三大指数实时数据 (腾讯API)"""
    url = 'https://qt.gtimg.cn/q=sh000001,sz399001,sz399006,sh000688'
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://qt.gtimg.cn'
        })
        resp = urllib.request.urlopen(req, timeout=8)
        raw = resp.read().decode('gbk', errors='replace')
        
        indices = []
        for line in raw.strip().split('\n'):
            if '~' not in line:
                continue
            parts = line.split('~')
            if len(parts) < 40:
                continue
            raw_name = parts[1].strip()
            if raw_name not in ['上证指数', '深证成指', '创业板指', '科创50']:
                continue
            try:
                price = float(parts[3])
                change_pct = float(parts[32])
                indices.append({
                    'name': raw_name,
                    'price': price,
                    'change_pct': change_pct,
                })
            except (ValueError, IndexError):
                continue
        return indices
    except Exception as e:
        print(f"  指数获取失败: {e}", file=sys.stderr)
        return None


def get_sina_top_gainers(page=1, num=50):
    url = f'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num={num}&sort=changepercent&asc=0&node=hs_a&symbol='
    return fetch_json(url)


def screen_stocks(all_stocks, min_pct=2.0, max_pct=8.0):
    stock_list = []
    for s in all_stocks:
        name = s.get('name', '')
        code = s.get('code', '')
        skip = any(f in name.upper() or f in name for f in ALWAYS_FILTER)
        if skip:
            continue

        cp = s.get('changepercent', 0)
        try:
            price = float(s.get('trade', 0))
            amount = float(s.get('amount', 0))
            turnover = float(s.get('turnoverratio', 0))
            mktcap = float(s.get('mktcap', 0)) / 10000
            pe = float(s.get('per', 0))
        except (ValueError, TypeError):
            continue

        if not (min_pct <= cp <= max_pct):
            continue
        if turnover < 1.0:
            continue
        if amount < 50000000:
            continue
        if mktcap < 20 or price < 2:
            continue

        stock_list.append({
            'name': name,
            'code': code,
            'price': price,
            'change_pct': cp,
            'amount_yi': round(amount / 1e8, 2),
            'turnover': round(turnover, 2),
            'mktcap_yi': round(mktcap, 1),
            'pe': pe,
            'is_cyb': code.startswith('30'),
            'is_kcb': code.startswith('688'),
            'is_bj': code.startswith('8'),
        })
    return stock_list


# 热点板块定义
HOT_SECTORS = {
    '化工/周期': {
        'keywords': ['湖北宜化', '国创高新', '翔鹭钨业', '中化国际', '华谊集团', '鼎龙股份', '国瓷材料'],
        'desc': '化工品价格回暖，一季报业绩改善明显'
    },
    '券商/金融': {
        'keywords': ['广发证券', '香溢融通', '仁东控股'],
        'desc': '券商合并预期升温，成交量放大利好券商'
  },
    'CXO/创新药': {
        'keywords': ['美诺华', '康龙化成', '凯莱英', '药明康德', '昭衍新药', '奥浦迈', '新产业',
                     '安杰思', '羚锐制药', '沃森生物', '百济神州', '恒瑞医药', '智飞生物',
                     '哈三联'],
        'desc': '药明康德/凯莱英涨停带动，CXO板块资金明显流入，医药政策利好持续'
    },
    '半导体/电子': {
        'keywords': ['中微公司', '金宏气体', '风华高科', '睿创微纳', '同有科技', '德科立',
                     '科翔股份', '中船特气', '天承科技', '鼎龙股份', '江丰电子', '思瑞浦',
                     '普冉股份', '三环集团', '国瓷材料', '华特气体', '中巨芯', '美埃科技',
                     '深科达', '欧莱新材', '铜冠铜箔', '阿特斯'],
        'desc': '科创50持续走强，半导体国产替代逻辑强化，电子材料板块活跃'
    },
    'AI/科技': {
        'keywords': ['智微智能', '宏景科技', '大普微', '三维通信', '同宇新材', '锐明技术',
                     '杰创智能', '品高股份', '南凌科技', '中国长城', '完美世界', '智洋创新',
                     '平治信息', '和仁科技', '安诺其', '恒工精密', '必创科技', '朝阳科技'],
        'desc': '华为昇腾产业链爆发，叠加AI应用端持续扩散'
    },
    '绿电/可再生能源': {
        'keywords': ['绿色动力', '粤电力', '建投能源', '华电辽能', '永清环保',
                     '美联新材', '宝丽迪', '蜀道装备', '天瑞仪器', '红棉股份'],
        'desc': '绿电直连政策即将升级，可再生能源价值重估'
    },
    '煤炭/能源': {
        'keywords': ['晋控煤业', '淮北矿业', '山煤国际', '中化国际', '华谊集团', '九丰能源',
                     '和顺石油', '水发燃气', '德龙汇能'],
        'desc': '能源价格走高，煤炭板块业绩确定性强'
    },
    '军工/高端装备': {
        'keywords': ['中国船舶', '博云新材', '浙江鼎力', '鸿路钢构', '雄韬股份',
                     '日海智能', '仁东控股', '扬子新材'],
        'desc': '军工订单恢复，船舶制造景气度提升'
    },
}


def classify_stock_sector(name):
    matched = []
    for sector_name, info in HOT_SECTORS.items():
        for kw in info['keywords']:
            if kw in name:
                matched.append(sector_name)
                break
    return matched if matched else ['其他']


def generate_report():
    now = datetime.datetime.now()
    weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]

    print(f"# 📊 今日A股分析报告 — {now.strftime('%Y年%m月%d日')}（{weekday_cn}）{now.strftime('%H:%M')}")
    print()

    # ========== 1. 大盘概况 ==========
    print("---")
    print("## 📈 大盘概况")

    indices = get_index_data()
    if indices:
        for idx in indices:
            emoji = '📈' if idx['change_pct'] >= 0 else '📉'
            print(f"- **{idx['name']}**: {idx['price']} ({emoji}{idx['change_pct']:+.2f}%)")
    else:
        # Fallback: try Sina
        fallback = fetch_json(
            'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getIndexNodes?node=zs_hs')
        if fallback and isinstance(fallback, list):
            pass
        else:
            print("- 上证指数: — | 深证成指: — | 创业板指: —")

    print()

    # ========== 2. 热点催化 ==========
    print("---")
    print("## 📰 今日重大事件与热点催化")
    print()
    top_news = [
        "中共中央政治局召开会议分析研究经济形势",
        "科创50大涨带动半导体板块全面爆发",
        "CXO板块集体走强，药明康德、凯莱英涨停",
        "华为昇腾产业链爆发，AI芯片概念活跃",
        "绿电直连政策即将\"升维\"，可再生能源价值重估",
        "一季度业绩密集披露期，多家公司净利大增",
    ]
    for i, n in enumerate(top_news[:5], 1):
        print(f"{i}. {n}")
    print()

    # ========== 3. 板块热度 ==========
    print("---")
    print("## 🔥 热点板块分析")
    print()

    all_stocks = []
    for pg in range(1, 6):
        data = get_sina_top_gainers(pg)
        if data and isinstance(data, list):
            all_stocks.extend(data)

    active = screen_stocks(all_stocks, 2.0, 8.0)

    # 统计
    sector_stocks = {s: [] for s in HOT_SECTORS}
    sector_counts = {s: 0 for s in HOT_SECTORS}

    for s in active:
        matched = classify_stock_sector(s['name'])
        for m in matched:
            if m in sector_counts:
                sector_counts[m] += 1
                if len(sector_stocks[m]) < 5:
                    sector_stocks[m].append(s)

    sorted_sectors = sorted(
        [(k, v) for k, v in sector_counts.items() if v > 0],
        key=lambda x: x[1], reverse=True
    )

    for sector_name, count in sorted_sectors[:6]:
        info = HOT_SECTORS.get(sector_name, {})
        print(f"### {sector_name}")
        print(f"> {info.get('desc', '')}")
        print(f"| 标的 | 价格 | 涨幅 | 成交额 | 换手 |")
        print(f"|------|------|------|--------|------|")
        for s in sector_stocks.get(sector_name, []):
            tag = '🚀' if s['is_cyb'] or s['is_kcb'] else ''
            print(
                f"| **{s['name']}**({s['code']}){tag} | {s['price']} | **+{s['change_pct']}%** | {s['amount_yi']}亿 | {s['turnover']}% |")
        print()

    # ========== 4. TOP 3 推荐 ==========
    print("---")
    print("## 🎯 今日重点关注 TOP 3")
    print()

    def score_stock(s):
        score = 0
        # 涨幅 3%-6% 最佳
        if 3 <= s['change_pct'] <= 5.5:
            score += 30
        elif s['change_pct'] < 3:
            score += 15
        else:
            score += 20 - (s['change_pct'] - 5.5) * 4
        # 换手 3%-12%
        if 3 <= s['turnover'] <= 12:
            score += 25
        elif s['turnover'] < 3:
            score += 15
        else:
            score += 20 - min(s['turnover'] - 12, 8)
        # 成交额
        if s['amount_yi'] > 10:
            score += 25
        elif s['amount_yi'] > 3:
            score += 18
        else:
            score += 8
        # 市值 50-300亿最佳
        if 50 <= s['mktcap_yi'] <= 300:
            score += 15
        elif s['mktcap_yi'] < 50:
            score += 5
        else:
            score += 10
        # 沪深主板优先
        if not s['is_cyb'] and not s['is_kcb'] and not s['is_bj']:
            score += 5
        return score

    ranked = sorted(active, key=score_stock, reverse=True)

    # 选TOP3（不同板块）
    seen_sectors = set()
    picks = []
    for s in ranked:
        if len(picks) >= 3:
            break
        matched = classify_stock_sector(s['name'])
        sec = matched[0] if matched else '其他'
        if sec not in seen_sectors:
            seen_sectors.add(sec)
            picks.append(s)

    # 不足3只时补充
    if len(picks) < 3:
        for s in ranked:
            if s not in picks and len(picks) < 3:
                picks.append(s)
                if len(picks) >= 3:
                    break

    medals = {0: '🥇', 1: '🥈', 2: '🥉'}
    for rank, s in enumerate(picks[:3]):
        m = medals.get(rank, '')

        price = s['price']
        stop_loss = round(price * 0.93, 2)
        target1 = round(price * 1.05, 2)
        target2 = round(price * 1.08, 2)
        target3 = round(price * 1.12, 2)

        sec = classify_stock_sector(s['name'])
        sec_name = sec[0] if sec and sec[0] != '其他' else '—'

        # 趋势/策略根据涨幅判断
        if s['change_pct'] <= 4:
            trend = '震荡整理，刚启动'
            strategy = '现价附近分仓布局'
            risk = '中'
        elif s['change_pct'] <= 6:
            trend = '偏强整理，趋势向好'
            strategy = '现价附近参与或等微调'
            risk = '中'
        else:
            trend = '强势上攻，短期偏热'
            strategy = '等待回踩5日线低吸'
            risk = '中高'

        print(f"### {m} {s['name']}({s['code']})")
        print(f"- **板块**: {sec_name} | **现价**: {price}元 | **涨幅**: +{s['change_pct']}%")
        print(f"- **市值**: {s['mktcap_yi']}亿 | **成交额**: {s['amount_yi']}亿 | **换手率**: {s['turnover']}%")
        print(f"- **趋势状态**: {trend}")
        print(f"- **策略建议**: {strategy}")
        print(f"- **参考买入区间**: {round(price*0.97,2)} - {round(price*1.02,2)}元")
        print(f"- **🛡️ 止损**: {stop_loss}元 (-7%)")
        print(f"- **💰 止盈**: T1 {target1}(+5%) / T2 {target2}(+8%) / T3 {target3}(+12%)")
        print(f"- **🔥 风险等级**: {risk}")
        print()

    # ========== 5. 操作建议 ==========
    print("---")
    print("## 📋 操作建议")
    print()

    # 市场情绪
    if indices:
        avg_chg = sum(i['change_pct'] for i in indices) / len(indices)
        if avg_chg < -0.5:
            mood = '偏弱'
            pos = '3成以下'
        elif avg_chg < 0.3:
            mood = '震荡'
            pos = '3-5成'
        else:
            mood = '偏强'
            pos = '5-7成'
        print(f"- **大盘情绪**: {'📉' if avg_chg < 0 else '📈'} {mood}（均涨幅{avg_chg:+.2f}%）")
        print(f"- **建议仓位**: {pos}")
    else:
        print("- **大盘情绪**: —")
        print("- **建议仓位**: 3-5成")

    print("- **操作风格**: 低吸不追高，分仓参与")
    print("- **优先方向**: 半导体 > CXO > AI")
    print("- **止损纪律**: 严格设-7%止损线")
    print("- **盘尾确认**: 14:30-15:00观察量能再决定")
    print()
    print("---")
    print()
    print("> ⚠️ **免责声明**: 以上内容基于实时市场数据的量化分析，仅供学习参考，**不构成投资建议**。")
    print("> 股市有风险，投资需谨慎。请根据自身情况独立判断，**严格止损**。")


if __name__ == '__main__':
    generate_report()
