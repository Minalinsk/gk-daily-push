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
  kind          —— "announce"=招考公告源 / "news"=时政源。
                   公告源的东西进「日程提醒」（抽报名/考试时间），
                   时政源的东西进「每日资讯清单」
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
        "kind": "announce",   # announce=招考公告源（要过时间检查）/ news=时政源
        "url": "https://rst.shanxi.gov.cn/rsks/gwyks/",
        "pattern": r"t\d{8}_\d+\.shtml",
        "max": 30,
        "max_age_days": 120,
    },
    {
        "name": "山西省考·事业单位",
        "kind": "announce",   # announce=招考公告源（要过时间检查）/ news=时政源
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
        "kind": "announce",   # announce=招考公告源（要过时间检查）/ news=时政源
        "url": "https://www.huatu.com/gwy/zhaokao/",
        "pattern": r"huatu\.com/20\d{2}/\d{4}/\d+\.html",
        "exclude": STUDY_NOISE,
        "max": 320,
        "max_age_days": 45,
    },
    {
        # ★ 华图事业单位频道，全国事业单位招考公告（实测 283 条，45 天内 103 条）
        "name": "华图·全国事业单位",
        "kind": "announce",   # announce=招考公告源（要过时间检查）/ news=时政源
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
        "kind": "announce",   # announce=招考公告源（要过时间检查）/ news=时政源
        "url": "https://www.offcn.com/sydw/",
        "pattern": r"offcn\.com/sydw/20\d{2}/\d{4}/\d+\.html",
        "exclude": STUDY_NOISE,
        "max": 180,
        "max_age_days": 30,
    },
    {
        # 山西华图的公务员频道，按日期倒序、专盯山西，作为全国页的兜底
        "name": "华图·山西公务员",
        "kind": "announce",   # announce=招考公告源（要过时间检查）/ news=时政源
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
        "kind": "announce",   # announce=招考公告源（要过时间检查）/ news=时政源
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
        "kind": "news",   # announce=招考公告源（要过时间检查）/ news=时政源
        "url": "http://www.news.cn/politics/",
        "pattern": r"news\.cn/politics/\d{8}/\w+/c\.html",
        "max": 30,
    },
    {
        "name": "新华网·法治",
        "kind": "news",   # announce=招考公告源（要过时间检查）/ news=时政源
        "url": "http://www.news.cn/legal/",
        "pattern": r"news\.cn/legal/\d{8}/\w+/c\.html",
        "max": 20,
        "max_age_days": 7,
    },
    {
        "name": "央视网·要闻",
        "kind": "news",   # announce=招考公告源（要过时间检查）/ news=时政源
        "url": "https://news.cctv.com/",
        "pattern": r"news\.cctv\.com/20\d{2}/\d{2}/\d{2}/ARTI\w+\.shtml",
        "max": 24,
    },
    {
        "name": "人民网·观点",
        "kind": "news",   # announce=招考公告源（要过时间检查）/ news=时政源
        "url": "http://opinion.people.com.cn/",
        "pattern": r"/n1/\d{4}/\d{4}/c\d+-\d+\.html",
        "max": 24,
    },
    {
        "name": "中国新闻网·要闻",
        "kind": "news",   # announce=招考公告源（要过时间检查）/ news=时政源
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
# 每日资讯清单 = **只放时政**（前一天的热门时政，公告不在这里）。
# 考试公告统一走"日程提醒"那条线，见下面的 SCHEDULE_ENABLED。
MAX_TOTAL = 24          # 清单最多几条（防止超出企业微信长度限制）
MAX_PER_SOURCE = 12     # 每个时政源最多取几条
TITLE_MAX = 38          # 标题超过多少字截断

# 时政只取「前一天」发布的（按 URL 里的日期判断）。
# 某个源前一晚没更新时，退而取它最新的几条，免得整个源缺席。
NEWS_FALLBACK_MAX = 3   # 兜底时每个源最多取几条

# 首次运行（本地还没索引）时：True = 照常推送，可能一次推一大串
#                          False = 只建索引不推送，避免被刷屏
FIRST_RUN_PUSH = False

# ============================== 去重索引 ==============================
STATE_FILE = "state/seen.json"   # 记录已推送过的链接，靠它实现"只推新的"
STATE_MAX = 3000                 # 索引最多保留多少条（先进先出）

# ============================== 考试日程提醒 ==============================
# 公告源（kind="announce"）里**只要写明了报名时间或考试时间**的公告，
# 都会进这条提醒（公告维度，不是"只看未来 3 天"）：
#   ① 报名进行中   ② 最近几天要动的事   ③ 其它已经把时间定下来了的公告
# 时间点存在日历（state/exams.json）里，累积着用。嫌吵就把 SCHEDULE_ENABLED 改成 False。
SCHEDULE_ENABLED = True

# 每轮最多抓几篇公告正文来解析时间点。
# 正文页常有 100~250KB，但这个值不能太小——只有抓过正文的公告才进得了提醒，
# 已经解析过的会走缓存，所以日常只是把"新公告"补齐。
DETAIL_FETCH = 40

# 只对标题带这些词的公告去抓正文（公示、名单、备考文不用抓）
DETAIL_HINTS = ["公告", "简章", "招录", "招聘", "考试录用", "遴选", "选调"]
DETAIL_SKIP = ["公示", "拟录用", "拟聘用", "成绩", "名单", "职位表",
               "汇总", "入口", "通知单", "资格复审",
               "取消", "终止", "延期", "推迟", "补充公告"]

# 只解析最近这些天发布的公告（按 URL 里的日期判断）
DETAIL_MAX_AGE_DAYS = 25

# 提前几天开始提醒（报名截止 / 准考证打印 / 笔试 / 面试）
REMIND_DAYS = 3

# 一条提醒里最多列几条（超出的折叠成"另有 N 条"）
REMIND_ONGOING_MAX = 8           # 「报名进行中」最多几条
REMIND_UPCOMING_MAX = 10         # 「最近 N 天」最多几条
REMIND_OTHER_MAX = 20            # 「其它已定时间的公告」最多几条

# 一条消息正文的字节预算（企业微信 markdown 上限 4096 字节，留点余量）
MSG_BUDGET = 3400

# 「报名进行中」只列这些天内截止的（太远的先不占地方）
ONGOING_WINDOW_DAYS = 30

SCHEDULE_FILE = "state/exams.json"
SCHEDULE_MAX = 400               # 日历里最多保留多少条

# 手动兜底：机器抓不到的固定日期，自己填（取消注释即生效）
# 格式：(日期, 事件, 名称)
# MANUAL_EVENTS = [
#     ("2026-10-15", "报名开始", "2027年度国家公务员考试"),
#     ("2026-11-29", "笔试", "2027年度国家公务员考试"),
# ]
MANUAL_EVENTS = []

# ============================== 网络 ==============================
REQUEST_TIMEOUT = 15    # 单次请求超时（秒）
RETRY = 2               # 每个源失败重试次数
SSL_FALLBACK = True     # 证书校验失败时降级重试（部分政府站点证书链有问题）

# ============================== 推送 ==============================
# 优先读环境变量（GitHub Secrets），没配就读这里的默认值
PUSH_METHOD = os.getenv("PUSH_METHOD") or ""            # 填 wework 走企业微信
WEWORK_WEBHOOK = os.getenv("WEWORK_WEBHOOK") or ""
WEWORK_MSG_TYPE = os.getenv("WEWORK_MSG_TYPE") or "markdown"   # markdown / text

# 消息标题（这条是"每日资讯清单"用的；公告那条的标题在 exam_dates.py 里写死为
# 「⏰ 考试日程提醒」）
REPORT_TITLE = "每日时政"

# 调试用：置 1 则只打印不真发
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# 调试用：置 1 则只发一条测试消息，验证企业微信通道通不通
# （不抓取、不动索引。Actions 页面手动 Run workflow 时勾选「只发测试消息」即可）
TEST_PUSH = os.getenv("TEST_PUSH", "0") == "1"
