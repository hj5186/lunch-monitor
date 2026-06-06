#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订餐网页结构诊断工具
====================
跑一次, 自动分析网页表格结构, 输出你需要填入 catering_monitor.py 的配置值。

用法:
    python catering_diagnose.py
"""

import os
import urllib.request
import urllib.error
import ssl
import re
from html.parser import HTMLParser

URL = "http://dosh.paradesh.com/nj/index.php"

# ============================================================
# HTML 解析
# ============================================================

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self._cur_table = []
        self._cur_row = []
        self._in_td = False
        self._in_th = False
        self._text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._cur_table = []
        elif tag == "tr":
            self._cur_row = []
        elif tag in ("td", "th"):
            self._text = ""
            self._in_td = (tag == "td")
            self._in_th = (tag == "th")

    def handle_endtag(self, tag):
        if tag == "table":
            if self._cur_table:
                self.tables.append(self._cur_table)
                self._cur_table = []
        elif tag == "tr":
            if self._cur_row:
                self._cur_table.append(self._cur_row)
            self._cur_row = []
        elif tag in ("td", "th"):
            self._cur_row.append(self._text.strip())
            self._in_td = False
            self._in_th = False
            self._text = ""

    def handle_data(self, data):
        if self._in_td or self._in_th:
            self._text += data


def fetch():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    print(f"正在访问: {URL}")
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        html = resp.read()

    for enc in ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]:
        try:
            return html.decode(enc)
        except UnicodeDecodeError:
            continue
    return html.decode("utf-8", errors="replace")


# ============================================================
# 分析
# ============================================================

def analyze():
    try:
        html = fetch()
    except Exception as e:
        print(f"\n!!! 无法访问网页: {e}")
        print("请确认:")
        print("  1. 电脑是否已连接公司内网")
        print("  2. 浏览器能否打开 http://dosh.paradesh.com/nj/index.php")
        return

    parser = TableParser()
    parser.feed(html)
    tables = parser.tables

    print(f"\n找到 {len(tables)} 个表格\n")
    print("=" * 70)

    candidates = []  # 候选配置

    for ti, table in enumerate(tables):
        if len(table) < 2:
            continue

        print(f"\n{'='*70}")
        print(f"表格 #{ti+1}  ({len(table)} 行 x {max(len(r) for r in table) if table else 0} 列)")
        print(f"{'='*70}")

        # 打印表头
        if table:
            header = table[0]
            print(f"\n  表头: {header}")

        # 打印数据行
        for ri, row in enumerate(table[1:], 1):
            # 标记可能包含餐厅名称的行
            markers = []
            for cell in row:
                cell_lower = cell.lower()
                if any(kw in cell for kw in ["江燕", "麦当劳", "麥當勞", "点餐", "合计",
                                              "人数", "名额", "限量", "total", "count",
                                              "江", "燕", "麦", "当", "劳"]):
                    markers.append(f"'{cell}'")
                # 检测纯数字 (可能是人数)
                nums = re.findall(r'\d+', cell)
                if nums and len(cell.replace(' ', '')) <= 5:
                    markers.append(f"[数字: {cell}]")

            marker_str = "  ← " + " | ".join(markers) if markers else ""
            print(f"  行{ri}: {row}{marker_str}")

            # 存储候选
            for ci, cell in enumerate(row):
                row_text = "|".join(row)
                if any(kw in cell for kw in ["江燕", "麦当劳", "麥當勞"]):
                    desc = "餐厅名在第 1 列" if ci == 0 else f"餐厅名在第 {ci+1} 列"
                    # 找数字列
                    num_col = None
                    for cj, c in enumerate(row):
                        if re.match(r'^\d+$', c.strip()):
                            num_col = cj
                            break
                    candidates.append({
                        "keyword": cell,
                        "name_col": ci,
                        "count_col": num_col if num_col is not None else -1,
                        "row_sample": " | ".join(row),
                    })

    # ============================================================
    # 输出建议
    # ============================================================
    print(f"\n\n{'='*70}")
    print(" 配置建议")
    print(f"{'='*70}")

    # 按餐厅名分组
    jyl_keywords = set()
    mcd_a_keywords = set()
    mcd_b_keywords = set()
    count_col = None

    for c in candidates:
        name = c["keyword"]
        if "江燕" in name or "江" in name or "燕" in name:
            jyl_keywords.add(name)
            if c["count_col"] >= 0:
                count_col = c["count_col"] + 1  # 转成人类可读的列号
        elif "A" in name or "a" in name.replace("麦当劳A", "A").replace("麥當勞A", "A"):
            mcd_a_keywords.add(name)
            if c["count_col"] >= 0:
                count_col = c["count_col"] + 1
        elif "B" in name or "b" in name:
            mcd_b_keywords.add(name)
            if c["count_col"] >= 0:
                count_col = c["count_col"] + 1

    print(f"""
自动识别:
  江燕楼关键词: {jyl_keywords if jyl_keywords else '未识别 → 使用默认'}
  麦当劳A关键词: {mcd_a_keywords if mcd_a_keywords else '未识别 → 使用默认'}
  麦当劳B关键词: {mcd_b_keywords if mcd_b_keywords else '未识别 → 使用默认'}
  人数所在列: 第 {count_col if count_col else 3} 列
""")

    # ============================================================
    # 自动写入 catering_monitor.py
    # ============================================================
    jyl_kw = list(jyl_keywords) if jyl_keywords else ['江燕楼']
    mcda_kw = list(mcd_a_keywords) if mcd_a_keywords else ['麦当劳A', '麥當勞A']
    mcdb_kw = list(mcd_b_keywords) if mcd_b_keywords else ['麦当劳B', '麥當勞B']
    target_col = count_col if count_col else 3  # 第几列, 1-based

    monitor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'catering_monitor.py')
    if not os.path.exists(monitor_path):
        print(f"!!! 未找到 catering_monitor.py, 请确保两个脚本在同一目录")
        print(f"查找路径: {monitor_path}")
        return

    with open(monitor_path, 'r', encoding='utf-8') as f:
        code = f.read()

    # 替换 JIANGYANLOU_KEYWORDS
    code = re.sub(
        r'JIANGYANLOU_KEYWORDS\s*=\s*\[.*?\]',
        f'JIANGYANLOU_KEYWORDS = {jyl_kw}',
        code
    )
    # 替换 MCD_A_KEYWORDS
    code = re.sub(
        r'MCD_A_KEYWORDS\s*=\s*\[.*?\]',
        f'MCD_A_KEYWORDS       = {mcda_kw}',
        code
    )
    # 替换 MCD_B_KEYWORDS
    code = re.sub(
        r'MCD_B_KEYWORDS\s*=\s*\[.*?\]',
        f'MCD_B_KEYWORDS       = {mcdb_kw}',
        code
    )
    # 替换人数所在列 (find_count_in_row 中的 row[2])
    if target_col != 3:
        code = code.replace('row[2]', f'row[{target_col - 1}]')

    with open(monitor_path, 'w', encoding='utf-8') as f:
        f.write(code)

    print(f"[已自动填充] catering_monitor.py 配置已更新:")
    print(f"  JIANGYANLOU_KEYWORDS = {jyl_kw}")
    print(f"  MCD_A_KEYWORDS       = {mcda_kw}")
    print(f"  MCD_B_KEYWORDS       = {mcdb_kw}")
    if target_col != 3:
        print(f"  人数列位置: 第 {target_col} 列 (已修正)")

    if not candidates:
        print(f"\n!!! 注意: 未在网页中发现匹配行, 使用默认关键词 !!!")
        print(f"如果默认值不对, 请用浏览器打开网页, 手动修改 catering_monitor.py")


if __name__ == "__main__":
    analyze()
    print("\n按回车键退出...")
    input()
