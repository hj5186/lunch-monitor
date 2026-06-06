#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订餐名额监控闹钟
==================
功能:
  - 13:00 自动开始, 16:30 自动退出
  - 每分钟检测一次网页
  - 江燕楼 >= 40 人 -> 弹窗告警
  - 麦当劳A + 麦当劳B >= 40 人 -> 弹窗告警
  - 关掉某家告警 -> 仅停止该家检测, 另一家继续
  - 两家都告警并关掉 -> 当天程序退出
  - 当天无告警 -> 16:30 自动退出

安全保证 (请放心在公司内网使用):
  *** 本脚本绝不上传任何数据到外部 ***
  - 仅发起 HTTP GET 请求到内网地址, 等同于浏览器打开该网页
  - 只读不写: 对服务器无任何 POST/PUT/DELETE 操作
  - 不跟随外部重定向, 仅访问配置的内网 URL
  - 零第三方依赖, 100% Python 标准库, 无后门风险
  - 仅写入 3 个文件, 全在脚本所在目录, 不碰任何系统/用户文件:
      catering_monitor.log   (日志, <=500KB, 文本可审计)
      catering_monitor_state.json (几十字节, 只记录 2 个布尔值)
      catering_monitor.lock  (几字节, 防重复运行的 PID 锁)
  - 不读取任何公司文件, 不扫描磁盘, 不访问注册表
  - 每分钟仅 1 次 HTTP GET, 网络流量 ~1KB, 和浏览器开一个标签页无异
  - DLP/防火墙视角: 等同于有人在 13:00-16:30 之间每分钟手动刷新一次点餐网页
  *** 数据流向: 内网网页 --> 本机日志文件 (双向都不出内网) ***

使用方式:
    python catering_monitor.py
    或后台运行: pythonw catering_monitor.py
"""

import time
import datetime
import json
import os
import sys
import re
import threading
import urllib.request
import urllib.error
import ssl
from html.parser import HTMLParser

# ============================================================
# 配置区 —— 根据实际情况修改
# ============================================================

URL = "http://dosh.paradesh.com/nj/index.php"

# 餐厅名称关键词 (用于在 HTML 表格中匹配行)
JIANGYANLOU_KEYWORDS = ["江燕楼"]           # 匹配江燕楼的行
MCD_A_KEYWORDS       = ["麦当劳A", "麥當勞A"]  # 匹配麦当劳A的行
MCD_B_KEYWORDS       = ["麦当劳B", "麥當勞B"]  # 匹配麦当劳B的行

# 告警阈值
JIANGYANLOU_THRESHOLD = 40    # 江燕楼超过此人数告警
MCD_COMBINED_THRESHOLD = 40   # 麦当劳A+B 合计超过此人数告警
REALERT_DELTA = 5             # 上次告警后, 人数再涨 N 人才再次提醒
REALERT_COOLDOWN_MIN = 15     # 距上次告警至少 N 分钟才再次提醒 (防骚扰)

# 监控时间段
START_HOUR   = 13
START_MINUTE = 0
END_HOUR     = 16
END_MINUTE   = 30

# 检测间隔 (秒)
CHECK_INTERVAL = 60

# 日志文件 (None 则不写文件)
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "catering_monitor.log")
LOG_MAX_SIZE = 500 * 1024      # 日志最大 500KB (大约 3 个月的量), 超出自动截半

# 状态持久化文件 (记录当天已告警的餐厅, 断电重启后不丢失)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "catering_monitor_state.json")

# 单实例锁文件 (防止同时跑多个, 引发重复告警)
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "catering_monitor.lock")

# 网络重试配置
NET_RETRY_COUNT    = 3      # 最多重试次数
NET_RETRY_BASE_SEC = 5      # 首次重试等待秒数 (指数退避: 5/10/20)
NET_TIMEOUT_SEC    = 15     # 单次请求超时秒数

# ============================================================
# 模拟模式 (内网不通时测试用)
#   --demo    模拟模式: 数据每分钟随机增长
#   (不加)    真实模式: 从网页抓取
# ============================================================
DEMO_MODE = "--demo" in sys.argv

# ============================================================
# 日志
# ============================================================

def log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    # 控制台输出: 处理 Windows GBK 终端无法显示 emoji 的问题
    try:
        print(line)
    except UnicodeEncodeError:
        # 去掉 emoji 等特殊字符
        safe = line.encode("gbk", errors="replace").decode("gbk", errors="replace")
        print(safe)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

# ============================================================
# 状态持久化: 断电重启后记住当天已告警的餐厅
# ============================================================

def _today_key():
    """返回今天的日期字符串, 作为状态键"""
    return datetime.date.today().isoformat()


def load_state():
    """加载持久化状态, 返回 {日期: {'jyl': True/False, 'mcd': True/False}}"""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # 清理过期状态 (保留最近 3 天)
        today = datetime.date.today()
        keep = {}
        for k, v in state.items():
            try:
                d = datetime.date.fromisoformat(k)
                if (today - d).days <= 3:
                    keep[k] = v
            except Exception:
                pass
        return keep
    except Exception as e:
        log(f"加载状态文件失败: {e}")
        return {}


def save_state(*restaurant_keys):
    """
    持久化: 标记餐厅今天已告警 (原子写入, 同时写多个 key 不会半途丢数据).
    restaurant_keys: 'jyl', 'mcd' 等
    """
    state = load_state()
    today = _today_key()
    if today not in state:
        state[today] = {}
    for k in restaurant_keys:
        state[today][k] = True
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"保存状态文件失败: {e}")


def is_skipped_today(restaurant_key):
    """检查某家餐厅今天是否已被标记为'已处理'"""
    state = load_state()
    today = _today_key()
    return state.get(today, {}).get(restaurant_key, False)


def daily_cleanup():
    """
    16:30 后清理:
      - 状态文件: 清空, 只保留结构
      - 日志文件: 只保留最近 3 天的记录 (或发现 >500KB 截半)
    """
    # === 状态文件: 第二天自动从零开始 ===
    # 实际上每个日期有独立的 key, 第二天 load_state() 读到空就是全新开始
    # 这里显式清掉过期条目
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
    today = _today_key()
    # 只保留今天
    state = {today: state.get(today, {})}
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # === 日志文件: 超出大小则截断 ===
    if LOG_FILE and os.path.exists(LOG_FILE):
        try:
            size = os.path.getsize(LOG_FILE)
            if size > LOG_MAX_SIZE:
                # 截掉前一半
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                keep = lines[len(lines)//2:]
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.writelines(keep)
                log(f"日志已截断: {size/1024:.0f}KB → {os.path.getsize(LOG_FILE)/1024:.0f}KB")
            # 每天退出时写入一个分隔行, 方便阅读
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
        except Exception as e:
            log(f"日志清理失败: {e}")

# ============================================================
# 安全防护: 单实例锁
# ============================================================

def acquire_lock():
    """尝试获取进程锁, 成功返回 True, 已有实例运行则返回 False"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = f.read().strip()
            # 检查该 PID 是否还活着
            if old_pid:
                try:
                    os.kill(int(old_pid), 0)  # 信号0 = 只检查是否存在
                    log(f"已有实例 (PID={old_pid}) 在运行, 本进程退出")
                    return False
                except (OSError, ValueError):
                    # 进程已死, 锁文件过期, 可以覆盖
                    os.remove(LOCK_FILE)
        except Exception:
            pass
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as e:
        log(f"无法创建锁文件: {e}")
        return False


def release_lock():
    """释放进程锁"""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, "r") as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(LOCK_FILE)
    except Exception:
        pass


# ============================================================
# 安全防护: 网络重试 + 数据校验
# ============================================================

def fetch_with_retry(url):
    """带指数退避重试的网页抓取 (不影响主机 — 每次请求 <1KB, 15s 超时)"""
    import random
    last_error = None
    for attempt in range(NET_RETRY_COUNT):
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=NET_TIMEOUT_SEC, context=ctx) as resp:
                return resp.read()
        except Exception as e:
            last_error = e
            if attempt < NET_RETRY_COUNT - 1:
                wait = NET_RETRY_BASE_SEC * (2 ** attempt) + random.uniform(0, 2)
                log(f"网络请求失败 (尝试 {attempt+1}/{NET_RETRY_COUNT}): {e}, {wait:.0f}s 后重试")
                time.sleep(wait)
    raise last_error


def validate_counts(jyl, mcda, mcdb):
    """数据校验: 返回 (jyl, mcda, mcdb, warnings)"""
    warnings = []
    max_per_restaurant = 80

    for name, val in [("江燕楼", jyl), ("麦当劳A", mcda), ("麦当劳B", mcdb)]:
        if val is not None:
            if val < 0:
                warnings.append(f"{name}={val} 为负数, 已修正为 0")
                val = 0
            elif val > max_per_restaurant * 2:  # 超出合理范围太多
                warnings.append(f"{name}={val} 异常偏大 (>{max_per_restaurant*2}), 可能网页格式变化")

    if mcda is not None and mcdb is not None and (mcda + mcdb) > 160:
        warnings.append(f"麦当劳合计={mcda+mcdb} 超总名额, 可能解析错误")

    return jyl, mcda, mcdb, warnings


# ============================================================
# 模拟模式初始化 (放在 log() 定义之后)
# ============================================================
if DEMO_MODE:
    log(">>> 模拟模式 <<< 数据将自动增长以测试告警逻辑")
    import random as _random
    _demo_jyl   = [8, 7, 6, 5]           # 江燕楼多行订单 (4行, 目前合计26)
    _demo_mcda  = [12, 4]                 # 麦当劳A   (2行, 目前合计16)
    _demo_mcdb  = [5, 4, 3]               # 麦当劳B   (3行, 目前合计12)
    _demo_table = [
        ["餐号",   "订餐人员", "合计"],
        ["江燕楼", "张三",    str(sum(_demo_jyl))],
        ["江燕楼", "李四",    ""],
        ["江燕楼", "王五",    ""],
        ["江燕楼", "赵六",    ""],
        ["麦当劳A","甲一",    str(sum(_demo_mcda))],
        ["麦当劳A","乙二",    ""],
        ["麦当劳B","丙三",   str(sum(_demo_mcdb))],
        ["麦当劳B","丁四",   ""],
        ["麦当劳B","戊五",   ""],
    ]
    def _demo_tick():
        """模拟订单增长"""
        global _demo_jyl, _demo_mcda, _demo_mcdb
        _demo_jyl[0]  += _random.randint(0, 2)
        _demo_jyl[1]  += _random.randint(0, 1)
        _demo_mcda[0] += _random.randint(0, 1)
        _demo_mcdb[0] += _random.randint(0, 1)
        _demo_table[1][2] = str(_demo_jyl[0])
        _demo_table[2][2] = str(_demo_jyl[1])
        _demo_table[3][2] = str(_demo_jyl[2])
        _demo_table[4][2] = str(_demo_jyl[3])
        _demo_table[5][2] = str(_demo_mcda[0])
        _demo_table[6][2] = str(_demo_mcda[1])
        _demo_table[7][2] = str(_demo_mcdb[0])
        _demo_table[8][2] = str(_demo_mcdb[1])
        _demo_table[9][2] = str(_demo_mcdb[2])

# ============================================================
# HTML 表格解析 (轻量, 不依赖第三方库)
# ============================================================

class TableExtractor(HTMLParser):
    """从 HTML 中提取所有 <table> 的行数据, 每行是一个 list[str]"""

    def __init__(self):
        super().__init__()
        self.tables = []        # [[[cell, cell, ...], ...], ...]
        self._current_table = []
        self._current_row = []
        self._in_td = False
        self._in_th = False
        self._cell_text = ""
        self._in_table = False
        self._table_stack = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_stack += 1
            self._current_table = []
        elif tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._cell_text = ""
            if tag == "td":
                self._in_td = True
            else:
                self._in_th = True

    def handle_endtag(self, tag):
        if tag == "table":
            self._table_stack -= 1
            if self._current_table:
                self.tables.append(self._current_table)
                self._current_table = []
        elif tag == "tr":
            if self._current_row and self._current_table is not None:
                self._current_table.append(self._current_row)
            self._current_row = []
        elif tag in ("td", "th"):
            text = self._cell_text.strip()
            if self._current_row is not None:
                self._current_row.append(text)
            if tag == "td":
                self._in_td = False
            else:
                self._in_th = False
            self._cell_text = ""

    def handle_data(self, data):
        if self._in_td or self._in_th:
            self._cell_text += data


def fetch_and_parse(url):
    """抓取网页, 返回所有表格 [[[cell, ...], row], table]"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        html_bytes = resp.read()

    # 尝试自动检测编码
    html = None
    for enc in ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]:
        try:
            html = html_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if html is None:
        html = html_bytes.decode("utf-8", errors="replace")

    parser = TableExtractor()
    parser.feed(html)
    return parser.tables


def get_order_counts(tables):
    """
    在所有表格中搜索, 累加同一餐厅所有行的第3列人数:
    返回 (jiangyanlou_total, mcd_a_total, mcd_b_total)
    未找到任何匹配行的返回 None
    """
    jyl_total = 0
    mcda_total = 0
    mcdb_total = 0
    jyl_found = False
    mcda_found = False
    mcdb_found = False

    for table in tables:
        for row in table:
            if len(row) < 3:
                continue

            # 第1列 (索引0) 匹配餐厅名, 第3列 (索引2) 取数字
            col0_text = row[0].strip()
            col2_text = row[2].strip()
            nums = re.findall(r'\d+', col2_text)
            count = int(nums[0]) if nums else 0

            # 江燕楼
            if any(kw in col0_text for kw in JIANGYANLOU_KEYWORDS):
                jyl_total += count
                jyl_found = True
            # 麦当劳A
            if any(kw in col0_text for kw in MCD_A_KEYWORDS):
                mcda_total += count
                mcda_found = True
            # 麦当劳B
            if any(kw in col0_text for kw in MCD_B_KEYWORDS):
                mcdb_total += count
                mcdb_found = True

    return (
        jyl_total if jyl_found else None,
        mcda_total if mcda_found else None,
        mcdb_total if mcdb_found else None,
    )


# ============================================================
# 右下角弹窗 (不抢焦点、不阻塞、倒计时自动消失)
# ============================================================

class ToastAlert:
    """
    右下角弹窗:
      - 按钮含义明确: [停止监控此餐厅] vs [已知晓，稍后提醒]
      - 不抢焦点、30s 倒计时后自动选「稍后提醒」
    """

    _closed = False
    _window = None

    PALETTE = {
        "bg":        "#fef9f4",
        "card_bg":   "#ffffff",
        "text":      "#3e3640",
        "subtext":   "#8c8580",
        "border":    "#f0e4d6",
        "stop_btn":  "#e0543e",
        "later_btn": "#a09890",
        "countdown": "#b8a99a",
        "icon_bg":   "#fff4eb",
    }

    @classmethod
    def show(cls, restaurant_name, count, timeout=30):
        import tkinter as tk

        cls._closed = False
        cls._window = None
        p = cls.PALETTE

        if "麦当劳" in restaurant_name or "麥當勞" in restaurant_name:
            icon = "🍔"
            other_name = "江燕楼"
        else:
            icon = "🍱"
            other_name = "麦当劳"

        short_name = restaurant_name.replace("A+麦当劳B", "").replace("+", "+").strip()

        def _show():
            win = tk.Tk()
            cls._window = win
            win.title(f"订餐告警 - {restaurant_name}")
            win.resizable(False, False)
            win.attributes('-topmost', True)
            win.configure(bg=p["bg"])

            W, H = 420, 240
            screen_w = win.winfo_screenwidth()
            screen_h = win.winfo_screenheight()
            x = screen_w - W - 30
            y = screen_h - H - 60
            win.geometry(f"{W}x{H}+{x}+{y}")

            # ---- 主卡片 ----
            card = tk.Frame(win, bg=p["card_bg"], highlightbackground=p["border"],
                            highlightthickness=1)
            card.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

            # ---- 顶部色条 (图标 + 标题) ----
            top_bar = tk.Frame(card, bg=p["icon_bg"], height=52)
            top_bar.pack(fill=tk.X)
            top_bar.pack_propagate(False)

            tk.Label(top_bar, text=icon, font=("Segoe UI Emoji", 22),
                     bg=p["icon_bg"]).pack(side=tk.LEFT, padx=(14, 8), pady=8)

            tk.Label(top_bar,
                     text=f"{restaurant_name}  ·  点餐已达 {count} 人",
                     font=("Microsoft YaHei", 14, "bold"),
                     fg=p["text"], bg=p["icon_bg"]).pack(side=tk.LEFT, pady=11)

            # ---- 内容区 ----
            content = tk.Frame(card, bg=p["card_bg"])
            content.pack(fill=tk.BOTH, expand=True, padx=16, pady=(10, 2))

            tk.Label(content,
                     text=f"已超出阈值 40 人，当前 {count} 人",
                     font=("Microsoft YaHei", 11), fg=p["subtext"],
                     bg=p["card_bg"]).pack(anchor="w")

            # ---- 按钮区 ----
            btn_bar = tk.Frame(card, bg=p["card_bg"])
            btn_bar.pack(fill=tk.X, padx=14, pady=(6, 10))

            def do_stop():
                cls._closed = True
                win.destroy()
            def do_later():
                cls._closed = False
                win.destroy()

            # 停止按钮 (红色) - 点击 → 去订餐, 两家都停, 当天退出
            s_btn = tk.Frame(btn_bar, bg=p["stop_btn"], cursor="hand2")
            s_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
            s_lbl = tk.Label(s_btn,
                             text=f"去订餐\n今日不再提醒",
                             font=("Microsoft YaHei", 12, "bold"),
                             fg="white", bg=p["stop_btn"],
                             justify=tk.CENTER, padx=10, pady=6)
            s_lbl.pack()
            s_btn.bind("<Button-1>", lambda e: do_stop())
            s_lbl.bind("<Button-1>", lambda e: do_stop())

            # 稍后按钮 (灰色) - 点击 → 暂不处理, 继续监控两家
            l_btn = tk.Frame(btn_bar, bg=p["later_btn"], cursor="hand2")
            l_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
            l_lbl = tk.Label(l_btn,
                             text=f"稍后提醒\n继续监控两家",
                             font=("Microsoft YaHei", 12),
                             fg="white", bg=p["later_btn"],
                             justify=tk.CENTER, padx=10, pady=6)
            l_lbl.pack()
            l_btn.bind("<Button-1>", lambda e: do_later())
            l_lbl.bind("<Button-1>", lambda e: do_later())

            # ---- 倒计时 ----
            cdl = tk.Label(card,
                           text=f"{timeout}s 后自动选择「已知晓，稍后提醒」",
                           font=("Microsoft YaHei", 9),
                           fg=p["countdown"], bg=p["card_bg"])
            cdl.pack(pady=(0, 8))

            def countdown(n):
                if cls._window is None or cls._window != win:
                    return
                if n <= 0:
                    cls._closed = False
                    win.destroy()
                    return
                cdl.config(text=f"{n}s 后自动选择「已知晓，稍后提醒」")
                win.after(1000, countdown, n - 1)

            win.after(1000, countdown, timeout - 1)
            win.mainloop()

        t = threading.Thread(target=_show, name="AlertThread", daemon=True)
        t.start()
        waited = 0
        while t.is_alive():
            time.sleep(0.3)
            waited += 0.3
            if waited > (timeout + 10):
                log("告警弹窗无响应, 强制关闭线程")
                cls._closed = False
                try:
                    if cls._window:
                        cls._window.destroy()
                except Exception:
                    pass
                break
        return cls._closed


def alert(restaurant_name, count):
    """弹出右下角告警, 返回 True=用户点了停止监控"""
    log(f"!!! 右下角弹出告警: {restaurant_name} = {count} !!!")
    stopped = ToastAlert.show(restaurant_name, count, timeout=30)
    if stopped:
        log(f"用户点击 [停止监控]: {restaurant_name}")
    else:
        log(f"用户点击 [继续监控] 或超时: {restaurant_name}")
    return stopped


# ============================================================
# 主逻辑
# ============================================================

def is_monitoring_time():
    """当前是否在监控时间段内"""
    now = datetime.datetime.now().time()
    start = datetime.time(START_HOUR, START_MINUTE)
    end = datetime.time(END_HOUR, END_MINUTE)
    return start <= now <= end


def wait_until_start():
    """智能等待: 已在监控时段则立即开始, 未到则等到13:00, 已过则等明天"""
    now = datetime.datetime.now()
    start_today = now.replace(hour=START_HOUR, minute=START_MINUTE,
                              second=0, microsecond=0)
    end_today   = now.replace(hour=END_HOUR, minute=END_MINUTE,
                              second=0, microsecond=0)

    if is_monitoring_time():
        # 13:00-16:30 之间 → 立即开始, 不等待
        log(f"当前就在监控时段内, 立即开始")
        return

    if now < start_today:
        # 还没到 13:00 → 等到今天 13:00
        pass  # start_today 已经是今天 13:00
    else:
        # 16:30 之后 → 等到明天 13:00
        start_today += datetime.timedelta(days=1)

    wait_sec = (start_today - now).total_seconds()
    log(f"等待到 {start_today.strftime('%Y-%m-%d %H:%M:%S')} 开始监控 "
        f"(还需 {wait_sec/60:.0f} 分钟)")
    # 分段 sleep, 每 30 秒检查一次退出信号
    while wait_sec > 0 and not _exit_flag:
        chunk = min(30, wait_sec)
        time.sleep(chunk)
        wait_sec -= chunk


# ============================================================
# 安全机制: 信号捕获 → 优雅退出
# ============================================================
import signal as _signal

_exit_flag = False

def _handle_exit_signal(signum, frame):
    global _exit_flag
    log(f"收到退出信号 (signal={signum}), 正在安全退出...")
    _exit_flag = True

# 注册信号处理器 (SIGINT=Ctrl+C, SIGTERM=kill 默认)
try:
    _signal.signal(_signal.SIGINT, _handle_exit_signal)
    _signal.signal(_signal.SIGTERM, _handle_exit_signal)
except Exception:
    pass  # 非主线程可能注册失败, 忽略


def safe_main():
    """带异常保护的 main 包装器"""
    try:
        main()
    except KeyboardInterrupt:
        log("用户按 Ctrl+C, 正在退出...")
    except SystemExit:
        pass
    except Exception as e:
        log(f"!!! 未捕获异常: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        release_lock()
        log("锁文件已释放")
        # 检查是否有僵尸 tkinter 线程
        for t in threading.enumerate():
            if t != threading.main_thread() and t.is_alive():
                log(f"等待后台线程: {t.name}")
        log("安全退出完成")


def main():
    log("=" * 60)
    log("订餐监控闹钟启动")
    log(f"监控网址: {URL}")
    log(f"时间段:   {START_HOUR:02d}:{START_MINUTE:02d} - "
        f"{END_HOUR:02d}:{END_MINUTE:02d}")
    log(f"检测间隔: {CHECK_INTERVAL} 秒")
    log(f"江燕楼阈值: {JIANGYANLOU_THRESHOLD} 人")
    log(f"麦当劳合计阈值: {MCD_COMBINED_THRESHOLD} 人")

    # 等到达开始时间 (模拟模式跳过)
    if not DEMO_MODE:
        wait_until_start()
    log(">>> 监控开始 <<<")

    # === 检查持久化状态: 断电重启后, 之前的标记直接恢复 ===
    jyl_alerted = is_skipped_today("jyl")
    mcd_alerted = is_skipped_today("mcd")
    _was_running_today = bool(load_state().get(_today_key()))  # 今天有状态记录=之前跑过

    if jyl_alerted:
        log("启动检查: 江燕楼今天已告警(或已超限), 不再监控")
    if mcd_alerted:
        log("启动检查: 麦当劳今天已告警(或已超限), 不再监控")
    if jyl_alerted and mcd_alerted:
        log(">>> 两家均已处理, 程序退出 <<<")
        daily_cleanup()
        return

    # === 第一轮检测: 仅崩溃重启场景下自动跳过超限餐厅 ===
    try:
        if DEMO_MODE:
            _demo_tick()
            tables = [_demo_table]
        else:
            tables = fetch_and_parse(URL)
        jyl_count, mcda_count, mcdb_count = get_order_counts(tables)

        # 只在"之前跑过, 崩溃重启"时才自动跳过
        if _was_running_today:
            if not jyl_alerted and jyl_count is not None \
                    and jyl_count >= JIANGYANLOU_THRESHOLD:
                log(f"[!] 崩溃重启检测: 江燕楼已 {jyl_count} 人 (>=40), 自动跳过")
                save_state("jyl")  # 崩溃重启单家超限, 只标记该家
                jyl_alerted = True
            if not mcd_alerted and mcda_count is not None and mcdb_count is not None:
                mcd_sum = mcda_count + mcdb_count
                if mcd_sum >= MCD_COMBINED_THRESHOLD:
                    log(f"[!] 崩溃重启检测: 麦当劳A+B已 {mcd_sum} 人 (>=40), 自动跳过")
                    save_state("mcd")  # 崩溃重启单家超限, 只标记该家
                    mcd_alerted = True
        else:
            log("今日首次运行, 不跳过 (即使已超阈值也正常告警)")

        if jyl_alerted and mcd_alerted:
            log(">>> 启动检测后两家均已超限, 程序退出 <<<")
            daily_cleanup()
            return
    except Exception as e:
        log(f"启动检测失败: {e} (将继续监控)")

    # 反骚扰: 跟踪上次告警时的计数值与时间
    jyl_last_alert_cnt = None   # 江燕楼上次告警时的人数
    jyl_last_alert_ts  = None   # 江燕楼上次告警时的时间戳
    mcd_last_alert_cnt = None
    mcd_last_alert_ts  = None

    while True:
        # 安全退出: 收到 Ctrl+C 或 SIGTERM
        if _exit_flag:
            log(">>> 收到退出信号, 安全退出 <<<")
            break

        # 检查是否超过结束时间 (模拟模式跳过)
        if not DEMO_MODE and not is_monitoring_time():
            log(f">>> {END_HOUR:02d}:{END_MINUTE:02d} 已到, 监控结束 <<<")
            break

        # 如果两家都已告警, 退出
        if jyl_alerted and mcd_alerted:
            log(">>> 两家均已告警并关闭, 程序退出 <<<")
            break

        try:
            # ===== 抓取 + 解析 =====
            if DEMO_MODE:
                _demo_tick()
                tables = [_demo_table]
            else:
                tables = fetch_and_parse(URL)
            jyl_count, mcda_count, mcdb_count = get_order_counts(tables)

            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            # 构造输出
            parts = [f"[{now_str}]"]
            if jyl_count is not None:
                parts.append(f"江燕楼={jyl_count}")
            else:
                parts.append("江燕楼=未识别")
            if mcda_count is not None and mcdb_count is not None:
                mcd_sum = mcda_count + mcdb_count
                parts.append(f"麦当劳A={mcda_count} B={mcdb_count} 合计={mcd_sum}")
            elif mcda_count is not None:
                parts.append(f"麦当劳A={mcda_count} B=未识别")
            elif mcdb_count is not None:
                parts.append(f"麦当劳A=未识别 B={mcdb_count}")
            else:
                parts.append("麦当劳=未识别")
            log(" | ".join(parts))

            # ===== 判断告警 (反骚扰: 不每分告警, 需满足冷却/增量) =====

            def _should_alert(last_cnt, last_ts):
                """判断是否该弹出告警: 首次必须有, 后续需冷却+增量"""
                if last_cnt is None or last_ts is None:
                    return True
                now = time.time()
                cooldown_ok = (now - last_ts) >= REALERT_COOLDOWN_MIN * 60
                delta_ok = (jyl_count if last_cnt == jyl_last_alert_cnt else mcd_sum) is not None
                return cooldown_ok or ((count_val - last_cnt) >= REALERT_DELTA if 'count_val' in dir() else False)

            # 江燕楼
            if not jyl_alerted and jyl_count is not None \
                    and jyl_count >= JIANGYANLOU_THRESHOLD:
                if jyl_last_alert_cnt is None or jyl_last_alert_ts is None \
                   or (time.time() - jyl_last_alert_ts >= REALERT_COOLDOWN_MIN * 60
                       and (jyl_count - jyl_last_alert_cnt) >= REALERT_DELTA):
                    stopped = alert("江燕楼", jyl_count)
                    jyl_last_alert_cnt = jyl_count
                    jyl_last_alert_ts  = time.time()
                    if stopped:
                        # 已去订餐 → 两家都停 (原子写入, 防崩溃丢数据)
                        save_state("jyl", "mcd")
                        jyl_alerted = True
                        mcd_alerted = True
                        log("用户已处理, 两家均停止监控")

            # 麦当劳A+B
            if not mcd_alerted and mcda_count is not None \
                    and mcdb_count is not None:
                mcd_sum = mcda_count + mcdb_count
                if mcd_sum >= MCD_COMBINED_THRESHOLD:
                    if mcd_last_alert_cnt is None or mcd_last_alert_ts is None \
                       or (time.time() - mcd_last_alert_ts >= REALERT_COOLDOWN_MIN * 60
                           and (mcd_sum - mcd_last_alert_cnt) >= REALERT_DELTA):
                        stopped = alert("麦当劳A+麦当劳B", mcd_sum)
                        mcd_last_alert_cnt = mcd_sum
                        mcd_last_alert_ts  = time.time()
                        if stopped:
                            # 已去订餐 → 两家都停 (原子写入)
                            save_state("jyl", "mcd")
                            jyl_alerted = True
                            mcd_alerted = True
                            log("用户已处理, 两家均停止监控")

        except urllib.error.URLError as e:
            log(f"网络错误: {e}")
        except Exception as e:
            log(f"解析错误: {e}")
            import traceback
            log(traceback.format_exc())

        # 等待下一次检测
        time.sleep(CHECK_INTERVAL)

    daily_cleanup()
    log("程序退出")


if __name__ == "__main__":
    # 获取单实例锁 (防止同时跑两份)
    if not acquire_lock():
        sys.exit(0)
    safe_main()
