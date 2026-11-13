#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
招聘数据桥 · job_bridge.py
==========================================
给「兼职匹配工作台」提供真实招聘数据。纯标准库，不需要 pip install。

它解决两件浏览器做不到的事：
  1. 跨域限制  —— 招聘站不发 CORS 头，网页里的 fetch 一开就红
  2. 前端渲染  —— 58同城这类站的岗位是 JS 渲染的，直接抓 HTML 只有空壳
所以桥在本机跑，用你自己的 Chrome 把页面渲染出来，再把结构化数据喂回工作台。

启动：
    python job_bridge.py
    python job_bridge.py --port 8765

接口：
    GET /                                  直接托管工作台页面（同源，无跨域）
    GET /ping                              自检（Chrome 在不在、各源能不能用）
    GET /search?q=剪辑&city=杭州&sources=boss,58&limit=20
                                           多源并发抓取、合并返回；sources 可只跑选中的平台
    GET /login/boss                        在本机拉起一个 BOSS 登录窗口（页面里点「扫码登录」就调它）
    GET /render?url=<编码后的网址>           通用兜底：渲染任意页面取正文

返回统一格式：
    {"ok":true,"items":[{"source":"58","sourceName":"58同城","title":"...","company":"...",
      "city":"...","pay":"...","url":"...","content":"...","published":"..."}],
     "stats":{"58":30,"shixiseng":12},"errors":{}}

数据来源说明：
    58同城    Chrome 渲染 m.58.com/{城市缩写}/jianzhi/  → 按关键词二次筛选
    实习僧    HTTP 直取列表页 + 详情页（详情页薪资/JD 全是明文，无字体反爬）
    BOSS直聘  CDP + 页面内 fetch（需在本机窗口扫码登录一次，登录态留在专用 profile）
    电鸭社区  HTTP 直取，岗位数据在 __NEXT_DATA__ 里；远程/线上单，不用坐班
"""

import os
import re
import sys
import json
import base64
import socket
import struct
import html
import time
import shutil
import threading
import subprocess
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '2026.11.13-plat'
PORT = 8765
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
TIMEOUT_HTTP = 20
TIMEOUT_RENDER = 50

# 城市 → 58 子域缩写（58 只认缩写，全拼会 404）
CITY_58 = {
    '北京': 'bj', '上海': 'sh', '广州': 'gz', '深圳': 'sz', '杭州': 'hz',
    '成都': 'cd', '南京': 'nj', '武汉': 'wh', '西安': 'xa', '重庆': 'cq',
    '苏州': 'su', '天津': 'tj', '长沙': 'cs', '郑州': 'zz', '青岛': 'qd',
    '合肥': 'hf', '宁波': 'nb', '无锡': 'wx', '佛山': 'fs', '东莞': 'dg',
    '厦门': 'xm', '福州': 'fz', '济南': 'jn', '沈阳': 'sy', '大连': 'dl',
    '昆明': 'km', '南昌': 'nc', '贵阳': 'gy', '太原': 'ty', '石家庄': 'sjz',
    '哈尔滨': 'hrb', '长春': 'cc', '温州': 'wz', '常州': 'cz', '泉州': 'qz',
    '南宁': 'nn', '珠海': 'zh', '中山': 'zs', '惠州': 'huizhou', '金华': 'jh',
    '嘉兴': 'jx', '南通': 'nt', '台州': 'tz', '烟台': 'yt', '徐州': 'xz',
    '海口': 'hk', '兰州': 'lz', '绍兴': 'sx', '淄博': 'zb', '洛阳': 'ly',
}

PY = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 基础工具

def find_chrome():
    """找一个能跑 headless 的浏览器。优先 Chrome，其次 Edge。"""
    cands = []
    if sys.platform.startswith('win'):
        for base in (os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                     os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'),
                     os.environ.get('LOCALAPPDATA', '')):
            if not base:
                continue
            cands += [
                os.path.join(base, r'Google\Chrome\Application\chrome.exe'),
                os.path.join(base, r'Microsoft\Edge\Application\msedge.exe'),
            ]
    elif sys.platform == 'darwin':
        cands = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                 '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                 '/Applications/Chromium.app/Contents/MacOS/Chromium']
    else:
        for n in ('google-chrome', 'google-chrome-stable', 'chromium',
                  'chromium-browser', 'microsoft-edge'):
            p = shutil.which(n)
            if p:
                cands.append(p)
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return ''


CHROME = find_chrome()


def _profile_dir():
    d = os.path.join(PY, '.bridge_profile')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(os.path.expanduser('~'), '.job_bridge_profile')
        os.makedirs(d, exist_ok=True)
    return d


def render(url, wait_ms=5000):
    """用本机 Chrome 渲染页面，返回渲染后的完整 HTML。失败返回空串。"""
    if not CHROME:
        return ''
    args = [CHROME, '--headless=new', '--disable-gpu', '--no-sandbox',
            '--disable-dev-shm-usage', '--mute-audio',
            '--disable-blink-features=AutomationControlled',
            '--user-data-dir=' + _profile_dir(),
            '--user-agent=' + UA,
            '--virtual-time-budget=%d' % wait_ms,
            '--dump-dom', url]
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=TIMEOUT_RENDER)
        return p.stdout.decode('utf-8', 'ignore')
    except Exception:
        return ''


def http_get(url, referer=''):
    """标准库 GET，带浏览器 UA。失败返回空串。"""
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': referer or url,
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_HTTP) as r:
            raw = r.read()
        for enc in ('utf-8', 'gbk', 'gb18030'):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode('utf-8', 'ignore')
    except Exception:
        return ''


def to_text(h):
    """HTML → 纯文本（去脚本样式，压空白）。"""
    h = re.sub(r'<script[\s\S]*?</script>', ' ', h, flags=re.I)
    h = re.sub(r'<style[\s\S]*?</style>', ' ', h, flags=re.I)
    h = re.sub(r'<[^>]+>', ' ', h)
    h = html.unescape(h)
    return re.sub(r'\s+', ' ', h).strip()


def clean(s, n=0):
    s = html.unescape(str(s or ''))
    # 招聘站把 emoji/图标编成字体私有区实体，常常不带结尾分号（&#xf59f），
    # 落在标题里就是乱码，必须连无分号形式一起清掉
    s = re.sub(r'&#x?[0-9a-fA-F]{2,6};?', '', s)
    s = re.sub(r'[\ue000-\uf8ff\u200b-\u200f\ufeff\ufffd]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = s.strip('|-·　')
    return s[:n] if n else s


def kw_hit(text, words):
    """粗略相关度：标题或正文里出现了几个技能词。"""
    t = text or ''
    return sum(1 for w in words if w and w in t)


# ---------------------------------------------------------------- 源：58同城

def src_58(q, city, limit):
    """58同城的岗位靠 JS 渲染，必须用 Chrome 过一遍。城市只认缩写子域。"""
    city = re.sub(r'市$', '', (city or '').strip())
    sub = CITY_58.get(city, '')
    if not sub:
        sub = 'hz'

    # q 可以是多个词（页面端会带上扩展词），第一个做主搜索，全部做命中判断
    words = [w for w in re.split(r'[\s,、/｜|]+', q or '') if len(w) >= 2]
    main = words[0] if words else ''
    urls = []
    if main:
        urls.append('https://m.58.com/%s/jianzhi/?key=%s' % (sub, urllib.parse.quote(main)))
    urls.append('https://m.58.com/%s/jianzhi/' % sub)
    out, seen = [], set()
    for u in urls:
        h = render(u, 5500)
        if not h:
            continue
        for m in re.finditer(r'<li[^>]*infoid="(\d+)"[^>]*>([\s\S]{20,4000}?)</li>', h):
            infoid, body = m.group(1), m.group(2)
            if infoid in seen:
                continue
            t = re.search(r'class="company_name"[^>]*>([^<]{2,60})', body)
            if not t:
                continue
            title = clean(t.group(1))
            price = re.search(r'class="price"[^>]*>\s*([\d._\-]+)\s*<em[^>]*>([^<]+)</em>', body)
            pay = ''
            if price:
                lo = price.group(1).replace('_', '-')
                pay = '%s %s' % (lo, clean(price.group(2)))
            pos = re.search(r'class="pos"[^>]*>([^<]+)</span>', body)
            area = clean(pos.group(1)) if pos else ''
            # 第二个 span 才是公司名：<span class="pos">区域</span><span>公司</span>
            comp = re.search(r'<span class="pos"[^>]*>[^<]*</span>\s*<span[^>]*>([^<]{2,40})</span>', body)
            company = clean(comp.group(1)) if comp else ''
            href = re.search(r'href="(https://m\.58\.com/[^"]+?\.shtml)', body)
            url_ = href.group(1) if href else ''
            if not url_:
                continue
            seen.add(infoid)
            # 列表项里通常还有一行「经验/学历/结算」的小字，拿不到就留空，不猜
            attr = re.search(r'class="(?:attr2?|attr)"[^>]*>([\s\S]{0,120}?)</div>', body)
            tags = ''
            if attr:
                tags = clean(re.sub(r'<[^>]+>', '·', attr.group(1)), 46).strip('·')
            area_txt = area if (area and area != city) else ''
            city_field = (city + (' ' + area_txt if area_txt else '')).strip()
            content = '｜'.join(x for x in [
                '岗位：' + title,
                '薪资：' + (pay or '未标注'),
                '城市：' + (city_field or '未标注'),
                '公司：' + (company or '未标注'),
                '来源：58同城兼职频道（站点直接列出）',
            ] if x)
            out.append({
                'source': '58', 'sourceName': '58同城',
                'title': title, 'company': company,
                'city': city_field,
                'pay': pay, 'url': url_, 'content': content, 'published': '',
                'tags': tags, 'exp': '', 'degree': '', 'poster': '', 'hrTitle': '',
                'jd': '', '_hit': kw_hit(title + content, words),
            })
            if len(out) >= limit * 3:
                break
        if len(out) >= limit * 3:
            break

    # 有关键词时只保留命中的 —— 宁可少而准，也不要拿一堆保安/服务员凑数
    if words:
        hit = [x for x in out if x['_hit'] > 0]
        out = hit if hit else []
    out.sort(key=lambda x: -x['_hit'])
    for x in out:
        x.pop('_hit', None)
    return out[:limit]


# ---------------------------------------------------------------- 源：实习僧

def _sxs_detail(url):
    """实习僧详情页：薪资、时长、发布时间、完整 JD 都是明文。"""
    h = http_get(url)
    if not h:
        return {}
    t = to_text(h)
    d = {}
    m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', t)
    if m:
        d['published'] = m.group(1)[:10]
    m = re.search(r'(\d+)\s*-\s*(\d+)\s*/\s*天', t)
    if m:
        d['pay'] = '%s-%s/天' % (m.group(1), m.group(2))
    if not d.get('pay'):
        m = re.search(r'(\d+)\s*元\s*/\s*(天|小时|月)', t)
        if m:
            d['pay'] = '%s元/%s' % (m.group(1), m.group(2))
    bits = []
    m = re.search(r'(\d)\s*天\s*[／/]\s*周', t)
    if m:
        bits.append('每周%s天' % m.group(1))
    m = re.search(r'实习\s*(\d+)\s*个月', t)
    if m:
        bits.append('实习%s个月' % m.group(1))
    for tag in ('远程实习', '寒假实习', '暑假实习', '可转正实习', '零基础实习'):
        if tag in t:
            bits.append(tag)
    d['tags'] = '·'.join(bits)
    # 正文：从「职位描述」/「工作内容」起，到「投递要求」/「工作地点」止
    body = ''
    s = t.find('职位描述')
    if s < 0:
        s = t.find('工作内容')
    if s >= 0:
        e = len(t)
        for stop in ('投递要求', '工作地点', '收起地图'):
            p = t.find(stop, s + 10)
            if 0 < p < e:
                e = p
        body = t[s:e]
    d['jd'] = clean(body, 900)
    return d


def src_shixiseng(q, city, limit):
    """实习僧：列表页 HTTP 直取，详情页补薪资与 JD。城市用中文名直接传参有效。"""
    words = [w for w in re.split(r'[\s,、/｜|]+', q or '') if len(w) >= 2]
    main = words[0] if words else ''
    if not main:
        return []
    url = 'https://www.shixiseng.com/interns?keyword=%s' % urllib.parse.quote(main)
    c = re.sub(r'市$', '', (city or '').strip())
    if c:
        url += '&city=' + urllib.parse.quote(c)
    h = http_get(url, referer='https://www.shixiseng.com/')
    if not h:
        return []
    items, seen = [], set()
    for m in re.finditer(
            r'<a href="(https://www\.shixiseng\.com/intern/inn_[^"?]+)[^"]*"[^>]*title="([^"]{2,60})"', h):
        u, title = m.group(1), clean(m.group(2))
        if u in seen:
            continue
        seen.add(u)
        seg = h[m.start():m.start() + 2600]
        cm = re.search(r'class="city ellipsis"[^>]*>([^<]+)', seg)
        c = clean(cm.group(1)) if cm else ''
        # 公司名：href="javascript:;" 的那个 title 链接（岗位名那条指向 intern 详情页）
        cm2 = re.search(r'<a[^>]*href="javascript:;"[^>]*title="([^"]{2,40})"', seg) \
              or re.search(r'<a[^>]*title="([^"]{2,40})"[^>]*href="javascript:;"', seg)
        comp = clean(cm2.group(1)) if cm2 else ''
        items.append({'url': u, 'title': title, 'city': c, 'company': comp})
        if len(items) >= max(limit, 10):
            break

    # 详情页并发补全（限 6 并发，礼貌且够快）
    lock = threading.Lock()
    idx = [0]

    def worker():
        while True:
            with lock:
                i = idx[0]
                idx[0] += 1
            if i >= len(items):
                return
            if i >= limit:
                return
            try:
                items[i].update(_sxs_detail(items[i]['url']))
            except Exception:
                pass

    ts = [threading.Thread(target=worker) for _ in range(min(6, max(1, len(items))))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    out = []
    for it in items[:limit]:
        pay = it.get('pay', '')
        tags = it.get('tags', '')
        content = '｜'.join(x for x in [
            '岗位：' + it['title'],
            '薪资：' + (pay or '未标注（列表页字体加密，详情页未取到）'),
            '城市：' + (it['city'] or city or '未标注'),
            '公司：' + (it['company'] or '未标注'),
            '时段：' + tags if tags else '',
            'JD：' + (it.get('jd') or '（未取到正文）'),
        ] if x)
        out.append({
            'source': 'shixiseng', 'sourceName': '实习僧',
            'title': it['title'], 'company': it['company'],
            'city': it['city'] or city, 'pay': pay,
            'url': it['url'], 'content': content,
            'published': it.get('published', ''),
            'tags': tags, 'exp': '', 'degree': '', 'poster': '', 'hrTitle': '',
            'jd': it.get('jd', ''),
        })
    return out


# ---------------------------------------------------------------- 源：电鸭社区（远程）

def src_eleduck(q, city, limit):
    """电鸭社区：远程 / 线上兼职与外包单的聚集地，正好补上「不想坐班」这块。

    站点是 Next.js，首页那 25 条最新帖整段塞在 __NEXT_DATA__ 里，一次 HTTP 就能取到，
    不登录、不消耗 token、不触发滑块。版块页和站内搜索页走的是验证页（302 到 /verification），
    所以这里只认首页信息流，再按 category.kind == 'work' 只留招聘帖 —— 宁可少，不掺职场闲聊。
    """
    words = [w for w in re.split(r'[\s,、/｜|]+', q or '') if len(w) >= 2]
    if not words:
        words = ['远程']

    h = http_get('https://eleduck.com/')
    if not h:
        return []
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">([\s\S]*?)</script>', h)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    props = ((data.get('props') or {}).get('initialProps') or {}).get('pageProps') or {}
    posts = ((props.get('postList') or {}).get('posts')) or []

    def pack(p, hit):
        pid = str(p.get('id') or '')
        title = clean(p.get('full_title') or p.get('title') or '', 90)
        summary = clean(p.get('summary') or '', 300)
        who = clean((p.get('user') or {}).get('nickname') or '', 24)
        grp = clean((p.get('group') or {}).get('name') or '', 30)
        cat = clean((p.get('category') or {}).get('name') or '', 20)
        tags = '/'.join(clean(t.get('name') or '', 12) for t in (p.get('tags') or [])[:4]
                        if isinstance(t, dict))
        day = str(p.get('published_at') or '')[:10]
        content = '｜'.join(x for x in [
            '岗位：' + title,
            '摘要：' + (summary[:200] or '未标注'),
            '发布：' + (day or '未标注'),
            '发布人：' + (who or '未标注'),
            '版块：' + (cat or grp or '电鸭社区'),
            '来源：电鸭社区（远程/线上岗，站点直接列出）',
        ] if x)
        return {
            'source': 'eleduck', 'sourceName': '电鸭社区',
            'title': title, 'company': (grp or who),
            'city': '远程', 'pay': '',
            'url': 'https://eleduck.com/posts/' + pid,
            'content': content, 'published': day,
            'tags': tags, 'exp': '', 'degree': '', 'poster': who, 'hrTitle': '',
            'jd': summary, '_hit': hit,
        }

    hits, loose = [], []
    for p in posts:
        if not isinstance(p, dict):
            continue
        if p.get('pinned') or p.get('deleted') or p.get('status') != 'published':
            continue                      # 置顶那条是社区规则，不是岗位
        cat = p.get('category') or {}
        if str(cat.get('kind') or '') != 'work':
            continue                      # 只要招聘类，职场讨论/访谈故事一律不要
        title = clean(p.get('full_title') or p.get('title') or '', 90)
        if len(title) < 4:
            continue
        tags = ' '.join(clean(t.get('name') or '', 12) for t in (p.get('tags') or [])
                        if isinstance(t, dict))
        hit = kw_hit(title + ' ' + clean(p.get('summary') or '') + ' ' + tags, words)
        (hits if hit > 0 else loose).append(pack(p, hit))

    out = hits + loose[:3]                # 一个都没命中时，兜 3 条最新招聘帖，不留空窗
    return out[:max(limit, len(hits))]


# ---------------------------------------------------------------- 源：BOSS直聘

_boss_state = {'ts': 0.0, 'ok': False}


# ---------------------------------------------------------------- BOSS 直聘
# 为什么不再让 boss-cli 发请求：
#   BOSS 的 joblist / detail 接口对 __zp_stoken__ 做一次性校验。同一个 token
#   只用一次就失效，第二次请求会返回 code=37「您的环境存在异常」，并在响应里
#   下发 {name, seed, ts} 让人用 JS 算出新 token。这套算法只在真实页面里跑得
#   动，纯 HTTP 客户端拿着导出的 cookie 必然第二次就被拦。
#
#   所以改成：用 Chrome 调试协议，在**真实页面上下文**里 fetch。
#   浏览器自己带 cookie、自己算 token、自己过挑战，我们只把 JSON 取回来。
#   顺带绕开了 Chrome 127+ 的 App-Bound cookie 加密 —— 根本不碰磁盘上的 cookie。

BOSS_CITY = {
    '北京': '101010100', '上海': '101020100', '广州': '101280100', '深圳': '101280600',
    '杭州': '101210100', '成都': '101270100', '南京': '101190100', '武汉': '101200100',
    '西安': '101110100', '苏州': '101190400', '长沙': '101250100', '天津': '101030100',
    '重庆': '101040100', '郑州': '101180100', '东莞': '101281600', '佛山': '101280800',
    '合肥': '101220100', '青岛': '101120200', '宁波': '101210400', '沈阳': '101070100',
    '昆明': '101290100', '大连': '101070200', '厦门': '101230200', '珠海': '101280700',
    '无锡': '101190200', '福州': '101230100', '济南': '101120100', '哈尔滨': '101050100',
    '长春': '101060100', '南昌': '101240100', '贵阳': '101260100', '南宁': '101300100',
    '石家庄': '101090100', '太原': '101100100', '兰州': '101160100', '海口': '101310100',
}


def _zp_city(city):
    c = re.sub(r'(市|地区|特别行政区)$', '', (city or '').strip())
    if c in BOSS_CITY:
        return BOSS_CITY[c]
    for k, v in BOSS_CITY.items():
        if k and (k in c or c in k):
            return v
    return '100010000'          # 全国


class _WS:
    """够用就好的 WebSocket 实现，只为读 Chrome 调试协议。"""

    def __init__(self, url, timeout=20):
        if not url.startswith('ws://'):
            raise RuntimeError('调试地址不对：' + url[:40])
        rest = url[5:]
        hostport, _, path = rest.partition('/')
        host, _, port = hostport.partition(':')
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            'GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n'
            'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
            'Sec-WebSocket-Version: 13\r\n\r\n' % (path, hostport, key)).encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            d = self.sock.recv(4096)
            if not d:
                raise RuntimeError('调试端口握手失败')
            buf += d
        head, _, self.buf = buf.partition(b'\r\n\r\n')
        if b'101' not in head.split(b'\r\n')[0]:
            raise RuntimeError('调试端口拒绝握手')

    def _read(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(65536)
            if not d:
                raise RuntimeError('调试连接已断开')
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, payload):
        n = len(payload)
        hdr = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack('>H', n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack('>Q', n)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._frame(0x1, text.encode('utf-8'))

    def recv_msg(self):
        chunks = []
        while True:
            b0, b1 = self._read(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack('>H', self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack('>Q', self._read(8))[0]
            payload = self._read(ln) if ln else b''
            if op == 0x9:
                self._frame(0xA, payload)
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise RuntimeError('页面关掉了调试连接')
            if op in (0x1, 0x2, 0x0):
                chunks.append(payload)
                if fin:
                    return b''.join(chunks).decode('utf-8', 'replace')

    def close(self):
        try:
            self._frame(0x8, b'')
            self.sock.close()
        except Exception:
            pass


class _CDP:
    def __init__(self, ws_url):
        self.ws = _WS(ws_url)
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(self.ws.recv_msg())
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise RuntimeError('%s -> %s' % (method, msg['error'].get('message', '')))
                return msg.get('result', {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


_BOSS_LAST = {'port': 0}


def _boss_profile():
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or os.path.expanduser('~')
    d = os.path.join(base, 'boss-bridge-profile')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _boss_page_ws(timeout=0.4):
    """找一个开着、且停在 zhipin.com 的页面，返回 (端口, 调试地址)。"""
    ports = []
    if _BOSS_LAST['port']:
        ports.append(_BOSS_LAST['port'])
    ports += [p for p in range(9223, 9253) if p != _BOSS_LAST['port']]
    for port in ports:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/json/list' % port,
                                        timeout=timeout) as r:
                ts = json.loads(r.read().decode('utf-8', 'ignore'))
        except Exception:
            continue
        for t in ts:
            if t.get('type') == 'page' and 'zhipin.com' in str(t.get('url', '')):
                _BOSS_LAST['port'] = port
                return port, t.get('webSocketDebuggerUrl') or ''
    return None, ''


def _boss_launch(target=None):
    """没有可用窗口就自己拉一个：用专用 profile（登录态在里面），
    窗口挪到屏幕外，不打扰用户。

    target 传登录页时窗口要留在屏幕内 —— 用户得看得见二维码才扫得了。"""
    target = target or 'https://www.zhipin.com/web/geek/job'
    login = 'login' in target
    chrome = CHROME or find_chrome()
    if not chrome:
        return None, ''
    port = 0
    for p in range(9223, 9253):
        sk = socket.socket()
        try:
            sk.bind(('127.0.0.1', p))
            port = p
            break
        except OSError:
            continue
        finally:
            sk.close()
    if not port:
        return None, ''
    try:
        subprocess.Popen(
            [chrome,
             '--user-data-dir=' + _boss_profile(),
             '--remote-debugging-port=%d' % port,
             '--no-first-run', '--no-default-browser-check', '--disable-sync',
             '--window-size=%s' % ('1000,760' if login else '1100,820'),
             '--window-position=%s' % ('120,80' if login else '-2400,-2400'),
             target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None, ''
    for _ in range(60):
        time.sleep(0.3)
        pp, ws = _boss_page_ws()
        if ws:
            time.sleep(3.5)      # 等搜索页自己把风控挑战跑完
            return pp, ws
    return None, ''


def _boss_eval(c, expr):
    res = c.call('Runtime.evaluate', {'expression': expr, 'awaitPromise': True,
                                      'returnByValue': True, 'timeout': 30000})
    r = res.get('result') or {}
    if r.get('subtype') == 'error':
        raise RuntimeError('页面里执行失败：' + str(r.get('description', ''))[:120])
    return r.get('value') or ''


def _boss_js_fetch(path):
    """在页面里 fetch 同源接口 —— 请求是浏览器自己发的，风控按真人算。"""
    return ("(async()=>{const r=await fetch(%s,{credentials:'include',"
            "headers:{'accept':'application/json, text/plain, */*'}});"
            "return await r.text();})()"
            % json.dumps(path, ensure_ascii=False))


def _boss_json(c, path):
    txt = _boss_eval(c, _boss_js_fetch(path))
    try:
        return json.loads(txt)
    except Exception:
        raise RuntimeError('BOSS 返回的不是 JSON：' + str(txt)[:100])


def _boss_jd(d):
    info = (d.get('zpData') or {}).get('jobInfo') or {}
    for k in ('postDescription', 'jobDescription', 'jobDesc', 'description'):
        v = info.get(k)
        if isinstance(v, str) and len(v.strip()) > 20:
            return clean(v, 900)
    return ''


def _boss_collect(q, city, limit):
    """搜索 + 前三条补正文。返回 (jobList, {securityId: 正文})"""
    words = [w for w in re.split(r'[\s,、/｜|]+', q or '') if len(w) >= 2]
    kw = words[0] if words else (q or '')
    cc = _zp_city(city)

    port, ws = _boss_page_ws()
    if not ws:
        port, ws = _boss_launch()
    if not ws:
        raise RuntimeError('没有可用的 BOSS 浏览器窗口')

    c = _CDP(ws)
    try:
        path = ('/wapi/zpgeek/search/joblist.json?query=%s&city=%s&page=1&pageSize=15&jobType=1903'
                % (urllib.parse.quote(kw), cc))
        o = _boss_json(c, path)
        if o.get('code') == 37:
            # token 用过了：让页面正常加载一次搜索页，浏览器自己算出新 token
            url = ('https://www.zhipin.com/web/geek/job?query=%s&city=%s&jobType=1903'
                   % (urllib.parse.quote(kw), cc))
            c.call('Page.navigate', {'url': url})
            time.sleep(5.5)
            o = _boss_json(c, path)
        if o.get('code') != 0:
            raise RuntimeError('BOSS %s：%s' % (o.get('code'), o.get('message')))
        lst = (o.get('zpData') or {}).get('jobList') or []

        if not lst:
            # 兼职类目筛出 0 条时退回不限，别让整个源空跑
            o = _boss_json(c, path.replace('&jobType=1903', ''))
            if o.get('code') == 0:
                lst = (o.get('zpData') or {}).get('jobList') or []

        # 正文只补第 1 条。token 的剩余次数很紧，补 3 条会让第 2、3 条
        # 直接把会话打成"环境异常"，得不偿失；其余岗位点开原帖看全文即可。
        details = {}
        for x in lst[:1]:
            sid = (x.get('securityId') or '').strip()
            if not sid:
                continue
            try:
                d = _boss_json(c, '/wapi/zpgeek/job/detail.json?securityId='
                               + urllib.parse.quote(sid, safe=''))
            except Exception:
                break
            if d.get('code') != 0:
                break
            jd = _boss_jd(d)
            if jd:
                details[sid] = jd
        return lst, details
    finally:
        c.close()


def _boss_warmup():
    """桥一启动就把 BOSS 窗口拉起来。

    Chrome 冷启动 + 页面过挑战要好几秒。放在启动时做，用户填表单的
    那几十秒里就热好了，点「开始匹配」时不用再等。"""
    try:
        port, ws = _boss_page_ws()
        if not ws:
            _boss_launch()
        _boss_ready(ttl=0)
    except Exception:
        pass


def _boss_ready(ttl=120):
    """判断能不能用。注意：这里**故意不发业务请求**。

    BOSS 的 __zp_stoken__ 只够用两三次请求，自检要是一次、搜索一次、
    详情再一次，真正要数据的时候 token 就没了。所以自检只读浏览器的
    cookie（Storage.getCookies 不消耗 token），看到登录态就算可用。
    """
    now = time.time()
    if _boss_state.get('ts') and now - _boss_state['ts'] < ttl:
        return _boss_state.get('ok', False), _boss_state.get('why', '')
    ok, why = False, ''
    port, ws = _boss_page_ws()
    if not ws:
        why = '没有打开的 BOSS 窗口，双击「连接BOSS直聘.bat」'
    else:
        try:
            c = _CDP(ws)
            try:
                ck = c.call('Storage.getCookies').get('cookies', [])
            finally:
                c.close()
            names = {x.get('name') for x in ck if 'zhipin.com' in str(x.get('domain', ''))}
            ok = bool({'zp_at', 'wt2'} & names)
            if not ok:
                why = '窗口里的 BOSS 还没登录'
        except Exception as e:
            why = '窗口连着但读不到登录态：' + str(e)[:60]
    _boss_state['ts'] = now
    _boss_state['ok'] = ok
    _boss_state['why'] = why
    return ok, why


def _boss_url(title, eid):
    """BOSS 网页详情页只认 encryptJobId（28 位短 token）。
    securityId 是一长串带 / + = 的签名，拼进路径必然「页面不存在」。
    没有 encryptJobId 时不要瞎拼，退回站内搜索页。"""
    eid = (eid or '').strip()
    if eid and re.match(r'^[A-Za-z0-9_~.\-]{16,64}$', eid):
        return 'https://www.zhipin.com/job_detail/%s.html' % eid, 'job'
    q = urllib.parse.quote((title or '').strip()[:40])
    return 'https://www.zhipin.com/web/geek/job?query=' + q, 'search'


def src_boss(q, city, limit):
    """BOSS 直聘：让本机 Chrome 代发请求（一次性 token 在页面里才过得去）。"""
    lst, details = _boss_collect(q, city, limit)
    out = []
    for x in lst[:limit]:
        title = clean(x.get('jobName') or x.get('positionName') or '')
        if not title:
            continue
        # BOSS 的两个 id 分工不同：
        #   securityId    —— 300 多字符的签名，只给接口用
        #   encryptJobId  —— 28 位短 token，网页详情页路径只认这个
        sid = (x.get('securityId') or '').strip()
        eid = (x.get('encryptJobId') or x.get('encryptId') or '').strip()
        url, link_kind = _boss_url(title, eid)
        company = clean(x.get('brandName') or x.get('companyName') or '')
        pay = clean(x.get('salaryDesc') or '')
        area = '·'.join([y for y in [x.get('cityName'), x.get('areaDistrict'),
                                     x.get('businessDistrict')] if y])
        exp = clean(x.get('experienceName') or x.get('jobExperience') or '')
        degree = clean(x.get('degreeName') or x.get('jobDegree') or '')
        poster = clean(x.get('bossName') or '')
        hr_title = clean(x.get('bossTitle') or x.get('bossTitleName') or '')
        welfare = '·'.join(clean(w) for w in (x.get('welfareList') or []) if w)[:60]
        labels = '·'.join(clean(l) for l in (x.get('jobLabels') or x.get('skills') or []) if l)[:60]
        # 福利标签里经常混着「经验不限·学历不限」，和经验/学历字段重复，
        # 拆开去重，卡片上不留两遍同样的字
        parts = []
        for seg in (welfare + '·' + labels).split('·'):
            seg = seg.strip()
            if seg and seg not in (exp, degree) and seg not in parts:
                parts.append(seg)
        tags = '·'.join(parts)[:90]
        jd = details.get(sid, '')
        content = '｜'.join(z for z in [
            '岗位：' + title, '薪资：' + (pay or '未标注'),
            '城市：' + (area or city or '未标注'),
            '经验：' + (exp or '未标注'),
            '学历：' + (degree or '未标注'),
            '公司：' + (company or '未标注'),
            '发布人：' + (poster + ('/' + hr_title if hr_title else '') if poster else '未标注'),
            '福利：' + welfare if welfare else '',
            '来源：BOSS直聘（浏览器直连）',
        ] if z)
        if jd:
            content += '｜JD：' + jd[:400]
        out.append({
            'source': 'boss', 'sourceName': 'BOSS直聘',
            'title': title, 'company': company, 'city': area or city,
            'pay': pay, 'url': url, 'content': content, 'published': '',
            'exp': exp, 'degree': degree, 'poster': poster, 'hrTitle': hr_title,
            'companyTag': '·'.join(y for y in [
                clean(x.get('brandIndustry') or ''),
                clean(x.get('brandScaleName') or ''),
                clean(x.get('brandStageName') or '')] if y),
            'tags': tags, 'jd': jd,
            'linkKind': link_kind,
        })
    return out


def _find_joblist(o):
    """在返回体里递归找岗位数组，兼容 boss-cli 换信封。"""
    if isinstance(o, list):
        if o and isinstance(o[0], dict) and ('jobName' in o[0] or 'positionName' in o[0]):
            return o
        for x in o:
            r = _find_joblist(x)
            if r:
                return r
        return []
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ('jobList', 'jobs', 'list', 'items') and isinstance(v, list):
                r = _find_joblist(v)
                if r:
                    return r
        for v in o.values():
            if isinstance(v, (dict, list)):
                r = _find_joblist(v)
                if r:
                    return r
    return []


# ---------------------------------------------------------------- 平台清单 / 聚合

def src_status():
    """平台清单：谁可用、怎么接的、为什么不可用。
    页面上的「平台中心」就是照这份清单画的，加源只改这里 + 一个 src_xxx。"""
    ok58 = bool(CHROME)
    ok_boss, why_boss = _boss_ready()
    return [
        {'id': 'boss', 'name': 'BOSS直聘', 'region': '本地 / 远程',
         'note': '岗位多、更新快，需在本机窗口扫码登录一次',
         'ready': ok_boss, 'how': '本机 Chrome 代发请求' if ok_boss else '',
         'why': why_boss, 'canLogin': True},
        {'id': '58', 'name': '58同城', 'region': '本地',
         'note': '到店/线下兼职主力，按城市抓整页在架岗位',
         'ready': ok58, 'how': '本机 Chrome 渲染' if ok58 else '',
         'why': '' if ok58 else '没找到 Chrome/Edge'},
        {'id': 'shixiseng', 'name': '实习僧', 'region': '本地 / 线上',
         'note': '实习与线上兼职，详情页薪资、每周天数、JD 全是明文',
         'ready': True, 'how': 'HTTP 直取 + 详情页', 'why': ''},
        {'id': 'eleduck', 'name': '电鸭社区', 'region': '远程',
         'note': '远程/线上外包单，不用坐班；发布人多为团队负责人',
         'ready': True, 'how': 'HTTP 直取（页面内嵌数据）', 'why': ''},
    ]


def run_source(fn, q, city, limit, results, errors, key):
    try:
        items = fn(q, city, limit) or []
        results[key] = items
    except Exception as e:
        errors[key] = str(e)[:160]


# 交错时的取样顺序：本地与远程穿插，避免榜单被某一类占满
MERGE_ORDER = ['boss', '58', 'shixiseng', 'eleduck']
SRC_FUNCS = {'58': src_58, 'shixiseng': src_shixiseng, 'boss': src_boss, 'eleduck': src_eleduck}


def search_all(q, city, limit=18, sources=None):
    """sources 为空表示「全跑」；传了就只跑选中的平台。
    选了几个平台，统计里就只出现这几个 —— 页面的「数据来源」条要能对上。"""
    want = [s for s in MERGE_ORDER if (not sources or s in sources)]
    if not want:
        want = list(MERGE_ORDER)
    results, errors = {}, {}
    ts = []
    for key in want:
        fn = SRC_FUNCS.get(key)
        if not fn:
            continue
        t = threading.Thread(target=run_source, args=(fn, q, city, limit, results, errors, key))
        t.start()
        ts.append(t)
    for t in ts:
        t.join(timeout=120)

    items, stats = [], {}
    # 交错合并：各源均匀取样，避免某一源垄断榜单 —— 这是「分析公正」的前提
    buckets = {k: list(results.get(k, [])) for k in want}
    for k in want:
        stats[k] = len(buckets.get(k, []))
    while any(buckets.get(k) for k in want):
        for k in want:
            if buckets.get(k):
                items.append(buckets[k].pop(0))
                if len(items) >= limit * 2:
                    break
        if len(items) >= limit * 2:
            break
    stats['_on'] = want
    return items, stats, errors


APP_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_NAME = '兼职匹配工作台.html'


def page_path():
    """工作台 HTML 的位置：和桥同一个目录。页面由桥自己托管，
    浏览器打开 http://127.0.0.1:8765/ 就是「一个程序搞定」——
    既没有跨域，也不用另外双击 bat 打开本地文件。"""
    p = os.path.join(APP_DIR, PAGE_NAME)
    if os.path.isfile(p):
        return p
    try:
        for n in os.listdir(APP_DIR):
            if n.endswith('.html') and '工作台' in n:
                return os.path.join(APP_DIR, n)
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = 'JobBridge/' + VERSION

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,OPTIONS')
        # file:// 页面访问 127.0.0.1 会被 Chrome 的 Private Network 策略拦，
        # 必须带这个头，否则网页端只会看到一句 Failed to fetch。
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_html(self, body):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        # 页面就在本机，永远读最新的那份，不要浏览器缓存旧版本
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,OPTIONS')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        g = lambda k, d='': (qs.get(k, [d])[0] or d)

        if u.path == '/ping':
            self._send({
                'ok': True, 'version': VERSION, 'chrome': CHROME or '',
                'platform': sys.platform,
                'sources': src_status(),
                'cities': len(CITY_58),
            })
            return

        if u.path == '/login/boss':
            # 页面里点「扫码登录」就会打到这里：桥拉起一个看得见的 Chrome 窗口，
            # 用户拿手机扫完，登录态存进专用 profile，之后抓数据直接复用。
            ok, why = _boss_ready(ttl=0)
            if ok:
                self._send({'ok': True, 'already': True, 'why': ''})
                return
            pp, ws = _boss_launch('https://www.zhipin.com/web/user/?ka=header-login')
            if ws:
                self._send({'ok': True, 'launched': True, 'port': pp})
            else:
                self._send({'ok': False, 'error': '没能拉起浏览器：' + (why or 'Chrome 不可用')}, 502)
            return

        if u.path == '/search':
            q = clean(g('q'))[:120]
            city = clean(g('city'))[:20]
            raw_src = g('sources', '')
            sources = [s.strip() for s in raw_src.split(',') if s.strip()] if raw_src else None
            try:
                limit = max(3, min(40, int(g('limit', '18'))))
            except Exception:
                limit = 18
            t0 = time.time()
            items, stats, errors = search_all(q, city, limit, sources)
            self._send({'ok': True, 'items': items, 'stats': stats, 'errors': errors,
                        'query': q, 'city': city,
                        'ms': int((time.time() - t0) * 1000)})
            return

        if u.path == '/render':
            target = g('url')
            if not target.startswith('http'):
                self._send({'ok': False, 'error': 'url 必须是 http(s)'}, 400)
                return
            h = render(target, int(g('wait', '5000') or 5000))
            if not h:
                self._send({'ok': False, 'error': '渲染失败（Chrome 不可用或页面打不开）'}, 502)
                return
            self._send({'ok': True, 'url': target, 'text': to_text(h)[:6000],
                        'html_len': len(h)})
            return

        # 桥自己托管工作台页面：浏览器只开一个 http://127.0.0.1:PORT 就够了，
        # 同源之后页面调接口不再需要任何跨域豁免，也不会出现 file:// 被拦的问题。
        if u.path in ('/', '/index.html', '/app', '/workbench'):
            p = page_path()
            if not p:
                self._send({'ok': False,
                            'error': '没找到工作台 HTML：把「%s」和 job_bridge.py 放在同一个目录' % PAGE_NAME}, 404)
                return
            try:
                with open(p, 'rb') as f:
                    self._send_html(f.read())
            except Exception as e:
                self._send({'ok': False, 'error': '读页面失败：' + str(e)[:80]}, 500)
            return

        self._send({'ok': False, 'error': '未知路径，可用：/ /ping /search /login/boss /render'}, 404)


def main():
    global PORT, CHROME
    args = sys.argv[1:]
    if '--port' in args:
        try:
            PORT = int(args[args.index('--port') + 1])
        except Exception:
            pass
    if '--chrome' in args:
        CHROME = args[args.index('--chrome') + 1]

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    print('=' * 58)
    print(' 招聘数据桥 v%s  ·  端口 %d' % (VERSION, PORT))
    print('=' * 58)
    print(' 浏览器 :', CHROME or '未找到（58同城源将不可用）')
    print(' 平台   : BOSS直聘 / 58同城 / 实习僧 / 电鸭社区')
    print(' BOSS   : 后台预热中（首次约需几秒；没登录就在页面里点「扫码登录」）')
    print(' 城市表 : %d 个' % len(CITY_58))
    print('-' * 58)
    print(' 打开工作台 : http://127.0.0.1:%d/' % PORT)
    print(' 自检接口   : http://127.0.0.1:%d/ping' % PORT)
    print(' 停止       : Ctrl + C')
    print('=' * 58)
    print(' 提示：页面由本程序托管，浏览器只开这一个地址就够，不用再双击别的文件。')
    print()

    threading.Thread(target=_boss_warmup, daemon=True).start()

    try:
        open_page = '--no-open' not in args
    except Exception:
        open_page = True
    if open_page:
        def _open():
            time.sleep(0.8)
            try:
                import webbrowser
                webbrowser.open('http://127.0.0.1:%d/' % PORT)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
        srv.shutdown()


if __name__ == '__main__':
    main()
