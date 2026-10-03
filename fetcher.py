# -*- coding: utf-8 -*-
"""抓取与解析：只用标准库，不需要 pip install，Actions 里跑得快。

策略：把列表页整个拉下来，用正则筛出"文章详情页"的链接和标题。
比 RSS 靠谱——实测人民网、新华网的官方 RSS 早就停更了（人民网停在 2025-06，
教育频道甚至停在 2016 年），所以本项目直接抓网页。
"""

import calendar
import html as html_mod
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_ANCHOR_RE = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _decode(body, headers):
    """按 charset 解码，中文站点常见 gb2312/gbk，统一走 gb18030。"""
    charset = ""
    ctype = (headers.get("Content-Type") or "") if headers else ""
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', body[:3000], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    charset = (charset or "utf-8").lower()
    if charset in ("gb2312", "gbk", "gb-2312"):
        charset = "gb18030"
    try:
        return body.decode(charset, "replace")
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", "replace")


def _is_cert_error(exc):
    """部分政府站点证书链不完整，urllib 会把 SSLCertVerificationError 包进 URLError。"""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(exc).upper()


def http_get(url, timeout=None, retry=None):
    """带重试的 GET，返回 (解码后的文本, 原始字节数)。"""
    timeout = timeout or config.REQUEST_TIMEOUT
    attempts = (retry if retry is not None else config.RETRY) + 1
    last_err = None

    for i in range(attempts):
        try:
            return _once(url, timeout, verify=True)
        except Exception as exc:
            last_err = exc
            if config.SSL_FALLBACK and _is_cert_error(exc):
                logging.warning("证书校验失败，降级为不校验证书重试：%s", url)
                try:
                    return _once(url, timeout, verify=False)
                except Exception as inner:
                    last_err = inner
        if i < attempts - 1:
            time.sleep(2 + i * 3)
    raise RuntimeError(f"请求失败：{url} -> {type(last_err).__name__}: {last_err}")


def _once(url, timeout, verify):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if verify:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = resp.read()
        # headers 得在 with 里取。连接关掉之后再读 resp.headers 属于"碰巧能用"，
        # 不同版本的 http.client 不保证。
        headers = resp.headers
    return _decode(body, headers), len(body)


def extract_articles(page_url, text, pattern, limit, title_pattern=""):
    """从列表页里提取文章：(标题, 绝对URL)。

    pattern       用来筛出真正的文章链接，避免把导航、广告也算进来
    title_pattern 有些站（比如粉笔）标题不在 <a> 里，而在紧跟其后的兄弟节点里，
                  这时用这个正则从链接后面 500 字里把标题捞出来
    如果 pattern 为空，则退化为"标题里含中文且足够长"。
    """
    pat = re.compile(pattern) if pattern else None
    title_pat = re.compile(title_pattern) if title_pattern else None
    out, seen_url = [], set()

    for m in _ANCHOR_RE.finditer(text):
        href = (m.group(1) or "").strip()
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue

        if pat:
            if not pat.search(href):
                continue
        else:
            if not re.search(r"\.html?($|\?)", href):
                continue

        title = _clean(m.group(2))
        if len(title) < 6 and title_pat:
            # 标题在链接后面的兄弟节点里
            window = text[m.end(): m.end() + 500]
            tm = title_pat.search(window)
            if tm:
                # title_pattern 忘了写捕获组时退回整个匹配（group(0)）——
                # 直接 group(1) 会抛 IndexError，那是个纯配置笔误，
                # 不该把整个任务打挂。
                title = _clean(tm.group(1) if tm.lastindex else tm.group(0))

        if len(title) < 6 or not _CJK_RE.search(title):
            continue

        url = urllib.parse.urljoin(page_url, href)
        # 只去掉 #锚点。查询串**故意保留**：有些站的文章地址就靠 ?id= 区分，
        # 一刀切掉反而会把不同文章并成一条。
        url = url.split("#")[0]
        if url in seen_url:
            continue
        seen_url.add(url)
        out.append({"title": title, "url": url})
        # limit=0 表示**先不限制**：调用方要先把整页扫完、再按日期和关键词过滤、
        # 最后才按 max 截断。反过来做（先截断再过滤）会让列表页靠前的旧文
        # 把配额占满，后面真正新鲜的稿子一条都进不来。
        if limit and len(out) >= limit:
            break
    return out


def _clean(raw):
    title = html_mod.unescape(_TAG_RE.sub("", raw or ""))
    title = re.sub(r"\s+", " ", title).strip()
    # 有些站会在标题前面挂"推荐/置顶"之类的标签，去掉更清爽
    return re.sub(r"^(推荐|置顶|热门|头条|最新|图集|视频)\s*[:：]?\s*", "", title)


# URL 里常见的日期写法，按优先级匹配
_DATE_RES = [
    re.compile(r"/t(20\d{2})(\d{2})(\d{2})_"),             # 政府站常见：/t20260907_10215581.shtml
    re.compile(r"/(20\d{2})[/-](\d{2})[/-](\d{2})[/.]"),   # /2026/09/20/  /2026-09-20/
    re.compile(r"/(20\d{2})/(\d{2})-(\d{2})/"),            # /2026/09-20/
    re.compile(r"/(20\d{2})/(\d{2})(\d{2})/"),             # /2026/0920/
    re.compile(r"/(20\d{2})(\d{2})(\d{2})/"),              # /20260920/
]


def url_date(url):
    """从 URL 里猜发布日期，猜不出返回 None。"""
    for rex in _DATE_RES:
        m = rex.search(url)
        if not m:
            continue
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        except ValueError:
            continue
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        try:
            return time.struct_time((y, mo, d, 0, 0, 0, 0, 0, -1))
        except ValueError:
            continue
    return None


def too_old(url, days):
    """URL 里的日期超过 days 天就算旧闻。days 为 0 表示不判。"""
    if not days:
        return False
    d = url_date(url)
    if d is None:
        return False          # 读不出日期就不拦，宁可多推也别漏
    # 必须用 calendar.timegm 按 UTC 算：time.mktime 是按**本机时区**解释的，
    # 同一份数据在本地（北京）和 Actions（UTC）会算出差 8 小时的年龄，
    # 卡在边界上的条目两边结论不一致。
    age = (time.time() - calendar.timegm(d)) / 86400
    return age > days


def _filter_by_age(name, items, src):
    days = src.get("max_age_days", config.MAX_AGE_DAYS)
    if not days:
        return items
    kept = [it for it in items if not too_old(it["url"], days)]
    if len(kept) != len(items):
        logging.info("源【%s】按日期过滤（%d 天内）：%d -> %d 条", name, days, len(items), len(kept))
    return kept


def fetch_source(src):
    """抓一个源，返回 (条目列表, 失败原因)。成功时原因是 None。

    把原因带出来是给 main 用的：源连续几天抓不到，得有机会冒到推送消息里，
    不然像"山西两个源全挂、中公一直 403"这种事只会躺在 Actions 日志里，
    而日志是没人天天去看的。
    """
    name = src["name"]
    url = src["url"]
    limit = int(src.get("max") or 10)
    try:
        text, size = http_get(url)
    except Exception as exc:
        logging.error("源【%s】抓取失败：%s", name, exc)
        return [], "抓取失败：%s" % exc

    try:
        # ⚠️ limit 传 0 = 先把整页扫完。过滤放在后面做，最后才按 max 截断。
        #    原来是「先取前 max 条、再按日期过滤」，于是列表页靠前的旧文会把
        #    配额占满，真正新鲜的稿子（常在页面后半段的分栏/专题区）反而被挡在外面。
        #    实测新华网·时政：整页 58 条链接、前 30 条只覆盖到页面中部，
        #    而它的稿子日期本来就散（09-21~09-30 都有），先截后滤会随排版变化随机漏稿。
        items = extract_articles(
            url, text,
            src.get("pattern", ""),
            0,
            src.get("title_pattern", ""),
        )
    except re.error as exc:
        logging.error("源【%s】的 pattern 正则写错了：%s", name, exc)
        return [], "pattern 正则写错：%s" % exc
    except Exception as exc:
        # 兜底：一个源的配置写错（或页面结构怪）不该拖垮整个任务。
        # 以前这里只接 re.error，别的异常会一路冒到 main，当天全部源都不推。
        logging.error("源【%s】解析出错，已跳过本源：%s: %s",
                      name, type(exc).__name__, exc)
        return [], "解析出错：%s: %s" % (type(exc).__name__, exc)

    if not items:
        logging.warning("源【%s】没解析出条目（页面可能改版或 pattern 需要更新）", name)
        return [], "页面没解析出条目（pattern 可能失效）"

    items = _filter_by_age(name, items, src)
    items = _filter_by_source_keywords(name, items, src)
    if len(items) > limit:
        logging.info("源【%s】按 max=%d 截断：%d -> %d 条",
                     name, limit, len(items), limit)
        items = items[:limit]

    for it in items:
        it["source"] = name
        # announce=招考公告源 / news=时政源（决定要不要过"有没有写报名/考试时间"的检查）
        it["kind"] = src.get("kind", "news")
    logging.info("源【%s】解析到 %d 条（页面 %.0f KB）", name, len(items), size / 1024)
    # 抓到了、也解析出了条目，只是全被日期/关键词过滤掉 —— 源本身是好的，
    # 不算失败（别让它进 failures，否则"今天确实没新公告"会被误报成故障）。
    return items, None


def _filter_by_source_keywords(name, items, src):
    """按源单独的关键词规则过滤（关键词为空则不筛）。"""
    inc = src.get("include") or []
    exc = src.get("exclude") or []
    if not inc and not exc:
        return items
    kept = []
    for it in items:
        title = it["title"]
        if inc and not any(w in title for w in inc):
            continue
        if any(w in title for w in exc):
            continue
        kept.append(it)
    if len(kept) != len(items):
        logging.info("源【%s】按关键词过滤：%d -> %d 条", name, len(items), len(kept))
    return kept


_IPV4_FORCED = False


def force_ipv4():
    """把 DNS 解析限制成只返回 IPv4 地址。

    GitHub 的托管 runner **没有 IPv6 出口**，而国内不少政府站的域名同时挂着
    AAAA 记录（rst.shanxi.gov.cn 就是）。Python 会挨个试 getaddrinfo 返回的地址，
    撞上 IPv6 就是一条 `[Errno 101] Network is unreachable` —— 白烧一轮重试，
    而且报错信息完全看不出是"地址族"的问题（日志里只显示域名请求失败）。
    只保留 A 记录之后，剩下的报错才是站点本身的真实状态（403 / 超时 / 连接被拒），
    排查时不用再猜。
    """
    global _IPV4_FORCED
    if _IPV4_FORCED:
        return
    orig = socket.getaddrinfo

    def v4_only(host, port, family=0, type=0, proto=0, flags=0):
        return orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = v4_only
    _IPV4_FORCED = True
    logging.info("DNS 解析已限制为 IPv4（托管 runner 没有 IPv6 出口）")


def fetch_all(sources):
    """依次抓全部源；单个源出错不影响其他源。

    返回 (全部条目, {源名: 失败原因})。第二个值是给 main 判断"源健康度"用的，
    所以必须把**原因**带出去，不能只给个名字。
    """
    if config.FORCE_IPV4:
        force_ipv4()
    socket.setdefaulttimeout(config.REQUEST_TIMEOUT)
    all_items, failures = [], {}
    for src in sources:
        items, reason = fetch_source(src)
        if items:
            all_items.extend(items)
        if reason:
            failures[src["name"]] = reason
    if failures:
        logging.warning("以下源本次没拿到数据：%s", "、".join(failures))
        for name, reason in failures.items():
            logging.warning("    %s -> %s", name, reason)
    return all_items, failures
