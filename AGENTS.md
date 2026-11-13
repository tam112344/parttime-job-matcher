# AGENTS.md · 给 AI 的接入说明书

> 这是一个 Python 3.8+ 纯标准库的单文件数据桥（`job_bridge.py`）+ 单文件前端（`兼职匹配工作台.html`）。
> 新增招聘平台的做法是：在 `job_bridge.py` 里写一个 `src_xxx()` 函数并注册，**不要改动前端结构**。
> 动手前先读完本文件，需要写具体代码时再读 `docs/接入新平台.md`。

---

## 一、硬约束（违反即视为改坏）

1. **禁止编造岗位数据**。任何源拿不到数据就返回 `[]`，绝不允许用示例、模板、模型生成的内容填充。前端对空结果有专门处理。
2. **禁止引入第三方依赖**。只能用 Python 标准库，不允许 `requests`、`bs4`、`lxml`、`playwright`、`selenium`。需要 HTML 解析就用 `re` + `html` 模块。
3. **禁止改动 `/ping` 的返回结构**。前端「04 平台」卡片、状态灯、`sources=` 过滤全靠它，字段少一个页面就白。
4. **禁止改动 `src_xxx()` 的出参字段名**。数据契约见 `docs/接入新平台.md` 第二节，前端按键名取值。
5. **禁止把登录凭证写进代码或提交进仓库**。`.bridge_profile/`、`credential.json`、`*.key`、`localStorage` 导出物一律在 `.gitignore` 里。
6. **禁止提高抓取频率**。并发数、翻页数保持现状，不要为了"数据更多"去刷站。
7. **不要重写前端**。需要新平台出现在界面上，正确做法是让 `/ping` 自动带出来 —— 前端是数据驱动的，不需要改 HTML。

---

## 二、运行与自测

```bash
python3 job_bridge.py            # 默认 127.0.0.1:8765，1 秒后自动打开浏览器
python3 job_bridge.py --no-open  # 不自动开浏览器（调试用）
python3 job_bridge.py --port 9000
```

自测三连（改完必须全过）：

```bash
# 1. 新源出现在清单里，且 ready 为 true
curl -s http://127.0.0.1:8765/ping

# 2. 只跑新源，能拿到数据
curl -s "http://127.0.0.1:8765/search?q=剪辑&city=杭州&limit=5&sources=新源id"

# 3. 全源同跑不报错
curl -s "http://127.0.0.1:8765/search?q=剪辑&city=杭州&limit=10"
```

判定标准：`/ping` 里新源 `"ready": true` 且 `"why"` 为空；`/search` 的 `stats` 里新源数字 > 0；`errors` 为空。

---

## 三、架构

```
浏览器页面 (兼职匹配工作台.html)
        │  fetch /ping /search /login/boss
        ▼
本机数据桥 (job_bridge.py, 127.0.0.1:8765)
        │
        ├─ HTTP 直取      → 实习僧、电鸭社区（服务端渲染 / 页面内嵌 JSON）
        └─ Chrome CDP     → 58同城（JS 渲染）、BOSS直聘（需登录态）
                │  在用户已登录的真实页面里执行 fetch
                ▼
          招聘网站
```

关键点：

- **页面由桥托管**（`GET /` 直接返回同目录的 HTML），所以页面和接口**同源**，不存在跨域。这是刻意设计，不要改成打开 `file://`。
- **`/search` 是并发多源**：`search_all()` 开线程跑选中的源，再按 `MERGE_ORDER` **交错合并**（各源轮流取一条），保证榜单不被某一站垄断。
- **`sources` 参数**决定本轮跑哪几个源，前端勾选直接传过来；不传 = 全跑。

---

## 四、加一个源：改三处

### 1. 写取数函数

```python
# ---------------------------------------------------------------- 源：某某招聘

def src_xxx(q, city, limit):
    """一句话说明这个站适合什么类型的岗位、为什么用这种取数方式。"""
    out = []
    # ... 抓取逻辑 ...
    return out[:limit]
```

- 签名固定：`src_xxx(q: str, city: str, limit: int) -> list[dict]`
- `q` 是检索词（可能含空格，需要拆词），`city` 是中文城市名，`limit` 是本轮上限
- 拿不到就 `return []`，**不要抛异常**（异常会被 `run_source()` 吞掉记进 `errors`，但会让这一源静默归零）
- 可复用工具函数：`http_get(url, referer='')`、`clean(s, n=0)`（去标签 + 截断）、`kw_hit(text, words)`（命中词数打分）、`find_chrome()`

### 2. 注册进两张表

```python
MERGE_ORDER = ['boss', '58', 'shixiseng', 'eleduck', 'xxx']   # 交错取样顺序
SRC_FUNCS = {'58': src_58, 'shixiseng': src_shixiseng, 'boss': src_boss,
             'eleduck': src_eleduck, 'xxx': src_xxx}
```

`MERGE_ORDER` 决定轮播顺序，建议把本地岗和远程岗穿插开，别让同类连着出现。

### 3. 在 `src_status()` 里加一行

```python
{'id': 'xxx', 'name': '某某招聘', 'region': '本地',   # region: 本地 / 远程 / 本地 / 远程
 'note': '一句话卖点，会显示在平台卡片上',
 'ready': True, 'how': 'HTTP 直取', 'why': '',
 'canLogin': False},                                  # 需要扫码登录的站才为 True
```

做完这三步就结束了：前端「04 平台」卡片、状态灯、勾选过滤、交错合并**全部自动生效**，不需要碰 HTML。

---

## 五、选哪种取数方式

| 场景 | 模式 | 参考实现 | 成本 |
|---|---|---|---|
| 页面 HTML 里有 `__NEXT_DATA__` / `__INITIAL_STATE__` / 内嵌 JSON | **A. 读内嵌 JSON** | `src_eleduck()` | 最低，优先试这个 |
| 服务端渲染的列表页，或列表 HTML 里就有岗位结构 | **B. HTML 直取 + 详情页** | `src_shixiseng()` | 中 |
| JS 渲染，或必须带登录态、有一次性 token | **C. CDP 页面内 fetch** | `src_boss()` | 高，但最万能 |

判断顺序：先用 `curl` 看 `view-source` 里有没有岗位文字 → 有就是 B 或 A；没有就上 C。
**C 的具体做法（手写 WebSocket、`Runtime.evaluate`、一次性 token 的重试策略）全部在 `docs/接入新平台.md` 第四、五节。**

---

## 六、改完提交前

- [ ] `python3 -m py_compile job_bridge.py` 通过
- [ ] `/ping` 新源 `ready: true`
- [ ] `/search?sources=新源` 返回条数 > 0，字段齐全（`title/company/city/pay/url/content` 不能是空串或 `None`）
- [ ] `/search` 全源同跑，`errors` 为空
- [ ] 打开页面，新源出现在「04 平台」卡片里，勾选/取消它能改变结果
- [ ] 没有把任何 cookie、profile、Key 提交进 git
