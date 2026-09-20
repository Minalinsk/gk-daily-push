# -*- coding: utf-8 -*-
r"""
每日时政 + 公考公告推送 —— 配置文件

要改的东西基本都在这个文件里，改完直接提交就行。

【源的两大类】
  公告类（放在前面）：省考 / 事业单位招考公告，全国范围
  时政类（放在后面）：新闻联播级别的时政要闻

  SOURCES 的顺序是有用的：推送时按源轮转挑选，消息超长会从尾巴截断，
  所以把"最不想漏"的公告放在前面。

【想加一个源？】
复制下面 SOURCES 里的一条，改三样：
  name    —— 显示名
  url     —— 列表页地址（在浏览器打开，确认能看到文章标题列表）
  pattern —— 用来从页面里"筛出文章链接"的正则，最省事的办法：
             在列表页随便点开一篇文章，看地址栏的 URL 长什么样
             例如 http://www.news.cn/politics/20260920/e6f43.../c.html
             规律是 news.cn/politics/年月日/一长串/c.html
             正则就写：news\.cn/politics/\d{8}/\w+/c\.html
             （正则里 . 要写成 \. ，数字写 \d ，字母数字串写 \w+）
  拿不准就把 url 发我，我帮你写。

【一条源支持的可选字段】
  pattern       —— 必填，筛文章链接的正则
  title_pattern —— 可选，标题不在 <a> 标签里时（比如粉笔），用它从链接后面 500 字里捞标题
  include       —— 可选，本源专用白名单，标题不含这些词就不推
  exclude       —— 可选，本源专用黑名单
  max           —— 可选，本源最多解析几条。经验值：直接写"这张页面一共有多少条"，
                   宁可开大——真正压量的是下面的 max_age_days 和去重索引。
                   开小了的话，分栏排版的页面（比如华图）只能吃到最前面一两栏。
  max_age_days  —— 可选，覆盖全局的 MAX_AGE_DAYS。公告类内容稀疏，可以放宽到几十天

【实测结论，省得再踩】
  公考雷达（gongkaoleida）—— 阿里云 WAF 的 JS 挑战，纯标准库抓不了，放弃。
  国家公务员局 / 人社部官网 —— JS 挑战或 403，抓不动；国考消息靠媒体和培训机构转载覆盖。
  中公的公务员频道（offcn.com/gwy）—— 大部分是"公告什么时候出""行测每日一练"
    这种流量文，不是公告本身，所以没放进来。
  华图 / 中公的页面大多是 GBK 编码，fetcher 里已经做了自动识别，不用管。
"""

import os

# ============================== 备考噪音黑名单 ==============================
# 培训机构（华图 / 中公 / 粉笔）的页面里混着大量备考类"内容农场"文章，
# 标题里带下面这些词的都不是真的招考公告，公告类源统一套用，把噪音挡掉。
# 想放宽就删词；想更严就继续加词。
# （注意：别放"图书""教材"这种词——"公开招聘图书管理员公告"会被误杀）
STUDY_NOISE = [
    "估分", "考情", "考点", "真题", "解析", "答案", "题库", "模拟题",
    "每日一练", "练习题", "网课", "课程", "直播", "备考", "预测",
    "大纲", "冷知识", "血泪", "考什么", "几月份", "什么时候",
    "题型", "经验分享", "上岸经验",
]

# ============================== 抓取源 ==============================
SOURCES = [
    # ---------- 一、省考 / 事业单位招考公告（全国）----------

    {
        # 山西省人事考试网的公务员栏目，最权威的山西省考来源
        "name": "山西省考·公务员",
        "url": "https://rst.shanxi.gov.cn/rsks/gwyks/",
        "pattern": r"t\d{8}_\d+\.shtml",
        "max": 30,
        "max_age_days": 120,
    },
    {
        "name": "山西省考·事业单位",
        "url": "https://rst.shanxi.gov.cn/rsks/sydwks/",
        "pattern": r"t\d{8}_\d+\.shtml",
        "max": 30,
        "max_age_days": 120,
    },
    {
        # ★ 覆盖面最广的一条：华图的"招考公告"页，把 37 个省市（含山西）的
        #   公务员公告按省份分段堆在一页里，实测 276 条，全是真公告
        #   （拟录用公示、招录公告、面试公告、体检通知……）
        #   max 必须开大：页面按省份分段，山西排在很后面，取少了翻不到；
        #   真正压量的是 max_age_days（45 天实测留 64 条）
        "name": "华图·全国公务员",
        "url": "https://www.huatu.com/gwy/zhaokao/",
        "pattern": r"huatu\.com/20\d{2}/\d{4}/\d+\.html",
        "exclude": STUDY_NOISE,
        "max": 320,
        "max_age_days": 45,
    },
    {
        # ★ 华图事业单位频道，全国事业单位招考公告（实测 283 条，45 天内 103 条）
        "name": "华图·全国事业单位",
        "url": "https://sydw.huatu.com/",
        "pattern": r"sydw\.huatu\.com/20\d{2}/\d{4}/\d+\.html",
        "exclude": STUDY_NOISE,
        "max": 320,
        "max_age_days": 45,
    },
    {
        # ★ 中公事业单位频道，按日期倒序排（0919 / 0918 / 0917…），
        #   天然适合"只推新的"，实测每天都有"全国事业单位招聘公告汇总"
        "name": "中公·全国事业单位",
        "url": "https://www.offcn.com/sydw/",
        "pattern": r"offcn\.com/sydw/20\d{2}/\d{4}/\d+\.html",
        "exclude": STUDY_NOISE,
        "max": 180,
        "max_age_days": 30,
    },
    {
        # 山西华图的公务员频道，按日期倒序、专盯山西，作为全国页的兜底
        "name": "华图·山西公务员",
        "url": "https://sx.huatu.com/gwy/",
        "pattern": r"huatu\.com/20\d{2}/\d{4}/\d+\.html",
        "exclude": STUDY_NOISE,
        "max": 80,
        "max_age_days": 90,
    },
    {
        # 粉笔首页是服务端渲染的，能直接拿到招考公告标题（实测 84 条）
        # 它的资讯列表页是前端渲染的（抓不到），所以这里用首页
        # 注意：首页里的链接是相对路径，所以 pattern 不要带域名
        # 原来是只留山西（include: ["山西"]），既然要全国就去掉了；
        # 想收回来就在这条里加一行： "include": ["山西"],
        "name": "粉笔·招考公告",
        "url": "https://www.fenbi.com/",
        "pattern": r"exam-information-detail/\d+",
        "title_pattern": r'class="[^"]*article-title[^"]*">([^<]{6,80})<',
        "exclude": STUDY_NOISE,
        "max": 40,
        "max_age_days": 60,
    },

    # ---------- 二、时政要闻 ----------

    {
        "name": "新华网·时政",
        "url": "http://www.news.cn/politics/",
        "pattern": r"news\.cn/politics/\d{8}/\w+/c\.html",
        "max": 30,
    },
    {
        "name": "新华网·法治",
        "url": "http://www.news.cn/legal/",
        "pattern": r"news\.cn/legal/\d{8}/\w+/c\.html",
        "max": 20,
        "max_age_days": 7,
    },
    {
        "name": "央视网·要闻",
        "url": "https://news.cctv.com/",
        "pattern": r"news\.cctv\.com/20\d{2}/\d{2}/\d{2}/ARTI\w+\.shtml",
        "max": 24,
    },
    {
        "name": "人民网·观点",
        "url": "http://opinion.people.com.cn/",
        "pattern": r"/n1/\d{4}/\d{4}/c\d+-\d+\.html",
        "max": 24,
    },
    {
        "name": "中国新闻网·要闻",
        "url": "https://www.chinanews.com.cn/",
        "pattern": r"chinanews\.com\.cn/\w+/20\d{2}/\d{2}-\d{2}/\d+\.shtml",
        "max": 24,
    },
]

# ============================== 筛选规则 ==============================
# 标题里包含任一关键词才推送；留空 [] = 不筛选，源里抓到什么推什么
# 例：只想看考公相关 -> ["公务员", "招录", "选调", "事业单位", "考试", "公告", "面试"]
INCLUDE_KEYWORDS = []

# 标题里出现这些词直接丢弃（过滤广告、招标等噪音）
EXCLUDE_KEYWORDS = ["招标", "中标", "采购公告", "招聘信息", "招商", "征婚"]

# 标题最少多少个字才算"像个文章"（过滤导航链接，比如华图页面里的"每日公告汇总"）
MIN_TITLE_LEN = 8

# 只保留最近几天内的文章：从 URL 里读日期，太旧的直接丢掉
# （新闻列表页底部经常挂着几个月前的旧链接，不挡会混进来）
# 填 0 表示不按日期过滤；URL 里读不出日期的条目一律保留
MAX_AGE_DAYS = 3

# ============================== 输出控制 ==============================
MAX_TOTAL = 24          # 整条消息最多几条（防止超出企业微信长度限制）
MAX_PER_SOURCE = 12     # 每个源最多取几条
TITLE_MAX = 38          # 标题超过多少字截断

# 首次运行（本地还没索引）时：True = 照常推送，可能一次推一大串
#                          False = 只建索引不推送，避免被刷屏
FIRST_RUN_PUSH = False

# ============================== 去重索引 ==============================
STATE_FILE = "state/seen.json"   # 记录已推送过的链接，靠它实现"只推新的"
STATE_MAX = 3000                 # 索引最多保留多少条（先进先出）

# ============================== 网络 ==============================
REQUEST_TIMEOUT = 15    # 单次请求超时（秒）
RETRY = 2               # 每个源失败重试次数
SSL_FALLBACK = True     # 证书校验失败时降级重试（部分政府站点证书链有问题）

# ============================== 推送 ==============================
# 优先读环境变量（GitHub Secrets），没配就读这里的默认值
PUSH_METHOD = os.getenv("PUSH_METHOD") or ""            # 填 wework 走企业微信
WEWORK_WEBHOOK = os.getenv("WEWORK_WEBHOOK") or ""
WEWORK_MSG_TYPE = os.getenv("WEWORK_MSG_TYPE") or "markdown"   # markdown / text

# 消息标题
REPORT_TITLE = "每日时政 + 公考公告"

# 调试用：置 1 则只打印不真发
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# 调试用：置 1 则只发一条测试消息，验证企业微信通道通不通
# （不抓取、不动索引。Actions 页面手动 Run workflow 时勾选「只发测试消息」即可）
TEST_PUSH = os.getenv("TEST_PUSH", "0") == "1"
