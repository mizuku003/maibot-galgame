"""galgame领域大神 —— 「最新 gal 情报」的取数与归一化。

这个模块只做三件事：把 RSS / 网页里的资讯抓下来、把不同源的条目压成同一种
结构、按日期排好序。渲染在 gl_render.py，命令与工具在 plugin.py。

几个**实测出来的**事实（换源或改解析器前先看这里，别再踩一遍）：

1. bugbug 的 RSS **不带正文图**：<description> 10/10 条都没有 <img>，
   图片全在 <content:encoded> 里（15~47KB 全文）。想配图只能从 encoded
   抠第一张；<enclosure> 更不能用 —— 那是 PV 的 mp4 视频，不是封面。
2. /bugbug/feed/ 和 /b_game/feed/ 内容**几乎重复**（同一条新闻两边都发），
   所以默认只取前者。
3. 月幕的资讯列表**没有日期**：/co/article 上日期模式命中 0 次。
   中文条目因此排在日文条目后面，不要伪造日期。
4. co-article-item 是 Vue 模板（{{topicId}} 这种占位符），**不能用**；
   能用的只有 swiper-slide（轮播）和 co-column-info（文章卡片）。
5. RSS 的 pubDate 是 RFC822（Mon, 28 Sep 2026 07:00:26 +0000），
   用 email.utils.parsedate_to_datetime 解，别自己写正则。

安全性上只做「读」：抓取失败一律降级（少一个源，不是整个功能不可用），
翻译失败退原文，任何解析异常都不往上抛给命中处理器。
"""

from __future__ import annotations

import asyncio
import html as _html_mod
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

from gl_http import HttpClient, normalize_name

# --- 源 -------------------------------------------------------------------

BUGBUG_FEEDS: dict[str, str] = {
    "bugbug": "https://www.bugbug.news/bugbug/feed/",
    # b_game 与 bugbug 高度重合，留着是为了「想两个都要」时能配置上
    "b_game": "https://www.bugbug.news/b_game/feed/",
    "dgame": "https://www.bugbug.news/dgame/feed/",
}
YMGAL_ARTICLE_URL = "https://www.ymgal.games/co/article"
DEFAULT_SOURCES: tuple[str, ...] = ("bugbug", "dgame", "ymgal")
NEWS_SOURCE_LABELS: dict[str, str] = {
    "bugbug": "BugBug.NEWS",
    "b_game": "BugBug（美少女ゲーム）",
    "dgame": "BugBug（同人）",
    "ymgal": "月幕",
}

# --- 文本工具 -------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_WS_RE = re.compile(r"\s+")
_IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
# bugbug 每条 description 尾巴都挂版权，形状是 "Copyright &copy; 2026    All Rights Reserved."；
# 注意 strip_html 会先把实体反转义成 ©，所以两种写法都要认。
#
# 只删这两种**定形**的写法。以前多了个「或者到字符串结尾」的分支：正文里任何一处出现
# Copyright，就会从那儿一路删到文末（段落中段有这个词，后半段就没了）。
# 宁可留下一个版权尾巴，也不能吞正文。
_COPYRIGHT_RE = re.compile(
    r"Copyright\s*(?:&copy;|©)?\s*\d{0,4}[^\n]{0,120}?All Rights Reserved\.?"
    r"|Copyright\s*(?:&copy;|©)?\s*\d{0,4}\s*\Z",
    re.I,
)
_TITLE_QUOTE_RE = re.compile(r"""["']([^"']{1,200})["']""")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.I | re.S)
# 「1. 中文」「1、中文」「- 中文」「1) 中文」这类逐行写法
_LINE_ITEM_RE = re.compile(r"^\s*(?:[-*•·]\s*|\d+\s*[.、:：)]\s*)")
# 假名：判断一段文字是不是日文（中文没有假名，所以这个判据足够稳）
_KANA_RE = re.compile(r"[\u3040-\u30ff]")


def has_cjk(text: Any) -> bool:
    """标题里有没有中日文字符。

    纯罗马音/英文标题（VNDB 上一半的作品是这种）**不要**送去翻 ——
    「Sullyland Nursery Rhyme」被译成中文只会更难认。
    """
    for ch in str(text or ""):
        if "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def strip_html(text: Any) -> str:
    """把一小段 HTML 压成纯文本（摘要用，不做完整解析）。"""
    raw = str(text or "")
    if not raw:
        return ""
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = re.sub(r"<br\s*/?>", " ", raw, flags=re.I)
    raw = _TAG_RE.sub(" ", raw)
    raw = _html_mod.unescape(raw)
    return _WS_RE.sub(" ", raw).strip()


def clean_summary(text: Any, *, limit: int = 120) -> str:
    """摘要：去标签、去掉 bugbug 每条都带的版权尾巴、截断。"""
    out = strip_html(text)
    out = _COPYRIGHT_RE.sub("", out).strip()
    if limit > 0 and len(out) > limit:
        out = out[: max(1, limit - 1)].rstrip() + "…"
    return out


def first_image(html_text: Any) -> str:
    """抠出正文里的第一张图（bugbug 的封面只能这么来）。"""
    match = _IMG_SRC_RE.search(str(html_text or ""))
    if not match:
        return ""
    return _html_mod.unescape(match.group(1)).strip()


def parse_pubdate(text: Any) -> str:
    """RFC822 → YYYY-MM-DD HH:MM（转成机器本地时区）；解不出来给空串。

    空串有明确含义：**这条没有日期**（月幕的中文资讯就是这种），
    排序时沉到列表末尾，而不是当成 1970 年。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        # 超长年份会抛 OverflowError（不是 ValueError）；漏了它整个源的 RSS 都会丢，
        # 因为 parse_feed 只捕 ET.ParseError，异常会一路冒到 collect_news。
        return ""
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


# --- RSS ------------------------------------------------------------------


def _local_name(tag: Any) -> str:
    """{http://purl.org/dc/elements/1.1/}creator → creator。"""
    return str(tag).rsplit("}", 1)[-1].lower()


def parse_feed(xml_text: str, *, source: str, limit: int = 10) -> list[dict[str, Any]]:
    """解析一条 RSS：只取插件要用到的字段，顺带从正文里抠封面。"""
    raw = str(xml_text or "").strip()
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"RSS 解析失败：{exc}") from exc

    out: list[dict[str, Any]] = []
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        title = ""
        link = ""
        published = ""
        creator = ""
        description = ""
        encoded = ""
        for child in item:
            name = _local_name(child.tag)
            text = child.text or ""
            if name == "title":
                title = text.strip()
            elif name == "link":
                link = text.strip()
            elif name == "pubdate":
                published = parse_pubdate(text)
            elif name == "creator":
                creator = text.strip()
            elif name == "description":
                description = text
            elif name == "encoded":
                encoded = text
        title = strip_html(title)
        if not title:
            continue
        out.append(
            {
                "source": source,
                "title": title,
                "title_zh": "",
                "url": link,
                "published": published,
                "image": first_image(encoded) or first_image(description),
                "summary": clean_summary(description),
                # 正文节选：RSS 里就有全文（content:encoded），点开某一条时用它，
                # 不用为了看详情再跑一趟站点 —— 正文页反而可能需要登录。
                "body": clean_summary(encoded, limit=600)
                or clean_summary(description, limit=600),
                "lang": "ja",
                "creator": creator,
            }
        )
        if limit > 0 and len(out) >= limit:
            break
    return out


async def fetch_feed(
    http: HttpClient, source: str, *, limit: int = 10, cache_minutes: int = 15
) -> list[dict[str, Any]]:
    """抓一条 bugbug 的 RSS（失败抛 HttpError，由上层按源降级）。"""
    url = BUGBUG_FEEDS.get(source)
    if not url:
        raise ValueError(f"未知资讯源：{source}")
    text = await http.request(
        "GET",
        url,
        source="news",
        expect_json=False,
        cache_ttl=float(max(0, cache_minutes) * 60),
        cache_key=f"news:feed:{source}",
    )
    return parse_feed(str(text or ""), source=source, limit=limit)


# --- 月幕中文资讯 ---------------------------------------------------------

_SWIPER_RE = re.compile(r'<div class="swiper-slide">(.*?)</div>\s*</div>', re.S)
_SWIPER_LINK_RE = re.compile(r'<a[^>]+href="([^"]*?/co/article/\d+)"[^>]*>(.*?)</a>', re.S)
_COLUMN_RE = re.compile(
    r"""<div title="([^"]*)"\s+class="co-column-info"[^>]*onclick="[^"]*?'([^']*)'"[^>]*>(.*?)</div>\s*</div>""",
    re.S,
)
_COLUMN_INTRO_RE = re.compile(r"<p>(.*?)</p>", re.S)
_ANY_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def parse_ymgal_articles(html_text: str, *, limit: int = 6) -> list[dict[str, Any]]:
    """解析 /co/article：轮播 + 专栏卡片。

    这个页面的列表是 Vue 渲染的模板（co-article-item 里全是 {{...}} 占位符），
    真正带了内容的只有 swiper-slide 与 co-column-info 两块。
    /co/collection/ 是专栏聚合页而不是文章，跳过。
    """
    raw = str(html_text or "")
    if not raw:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(url: str, title: str, summary: str, image: str) -> None:
        url = str(url or "").strip()
        title = strip_html(title)
        if not url or not title or url in seen:
            return
        seen.add(url)
        out.append(
            {
                "source": "ymgal",
                "title": title,
                "title_zh": title,  # 月幕本来就是中文，不用翻
                "url": url,
                "published": "",  # 页面确实没有日期，不编
                "image": str(image or "").strip(),
                "summary": clean_summary(summary),
                "lang": "zh",
                "creator": "",
            }
        )

    for block in _SWIPER_RE.findall(raw):
        match = _SWIPER_LINK_RE.search(block)
        if not match:
            continue
        img = _ANY_IMG_RE.search(block)
        push(match.group(1), match.group(2), "", img.group(1) if img else "")

    for title, url, block in _COLUMN_RE.findall(raw):
        if "/co/collection/" in url:
            continue
        intro = _COLUMN_INTRO_RE.search(block)
        img = _ANY_IMG_RE.search(block)
        # title 属性是完整的文章名，比 <a class="title"> 里的更可靠
        push(url, title or "", intro.group(1) if intro else "", img.group(1) if img else "")

    return out[:limit] if limit > 0 else out


async def fetch_ymgal_articles(
    http: HttpClient, *, limit: int = 6, cache_minutes: int = 30
) -> list[dict[str, Any]]:
    """抓月幕的中文资讯页（失败抛 HttpError，由上层按源降级）。"""
    text = await http.request(
        "GET",
        YMGAL_ARTICLE_URL,
        source="news",
        expect_json=False,
        cache_ttl=float(max(0, cache_minutes) * 60),
        cache_key="news:ymgal:article",
    )
    return parse_ymgal_articles(str(text or ""), limit=limit)


# --- 正文页：只有用户点开详情时才跑这一趟 -------------------------------

# RSS 里给的正文是截断过的（bugbug 的 content:encoded 也才几百字），所以
# `/gal 详情 <序号>` 会再跑一趟原文页把整篇抓回来。做法是「粗切 + 段落」：
# 先把文档里最像正文的那一块切出来（从它的开头切到文档末尾，靠 </article> /
# </main> 收口 —— 正则数不清嵌套的 div，右开区间最稳），再把 <p> 一段段抠出来。
# 抠失败就返回空串，调用方退回 RSS 那段摘要：详情宁可短一点，也不能空着。
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_DROP_TAG_RE = re.compile(
    r"<(script|style|noscript|iframe|svg|form|nav|header|footer|aside)\b[^>]*>.*?</\1>",
    re.S | re.I,
)
_PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
_ARTICLE_START_RES = (
    re.compile(r"<article\b[^>]*>", re.I),
    re.compile(r"<main\b[^>]*>", re.I),
    re.compile(
        r"""<div\b[^>]*class=["'][^"']*(?:entry-content|post-content|article-content"""
        r"""|article-body|articleBody|news-content|rich_media_content|main-content"""
        r"""|single-content|content-inner)[^"']*["'][^>]*>""",
        re.I,
    ),
)
_ARTICLE_END_RE = re.compile(r"</(?:article|main)>", re.I)
# 段落级噪声：相关文章、分享按钮、版权行、栏目名之类
_NOISE_RE = re.compile(
    r"^(?:関連記事|関連する記事|この記事をシェア|シェアする|ツイート|コメント|"
    r"※?この記事(?:は|を)|前の記事|次の記事|一覧へ|トップへ|All Rights Reserved|"
    r"Copyright|タグ[:：]|カテゴリ[:：]|【?PR】?|広告|スポンサー)",
    re.I,
)
_MIN_PARA_LEN = 12
_ARTICLE_CACHE_MINUTES = 60


def _article_region(html_text: str) -> str:
    """粗切正文区域：从最像正文的那块开始，一直切到文档末尾。

    几个候选按「越具体越优先」排：<article> → <main> → 带 entry-content
    这类类名的 div。都不命中就整篇当正文，让段落过滤自己去筛。
    """
    for pattern in _ARTICLE_START_RES:
        match = pattern.search(html_text)
        if not match:
            continue
        region = html_text[match.end() :]
        cut = _ARTICLE_END_RE.search(region)
        if cut:
            region = region[: cut.start()]
        # 切出来的东西太短说明这块不是正文（比如只有一个标题），换下一个候选
        if len(strip_html(region)) >= 200:
            return region
    return html_text


def extract_article(html_text: Any, *, limit: int = 0) -> str:
    """从正文页 HTML 里抠出整篇正文，段落之间空一行。

    `limit` 是字数上限（0 = 不限制），超了截断并补省略号。
    """
    raw = str(html_text or "")
    if not raw.strip():
        return ""
    raw = _COMMENT_RE.sub(" ", raw)
    raw = _DROP_TAG_RE.sub(" ", raw)
    region = _article_region(raw)

    paragraphs: list[str] = []
    for piece in _PARA_RE.findall(region):
        text = clean_summary(piece, limit=0)
        if len(text) < _MIN_PARA_LEN or _NOISE_RE.match(text):
            continue
        if paragraphs and text == paragraphs[-1]:
            continue
        paragraphs.append(text)
    if not paragraphs:
        whole = clean_summary(region, limit=0)
        paragraphs = [whole] if len(whole) >= _MIN_PARA_LEN else []

    out = "\n\n".join(paragraphs).strip()
    if limit > 0 and len(out) > limit:
        out = out[: max(1, limit - 1)].rstrip() + "…"
    return out


async def fetch_article(
    http: HttpClient,
    url: str,
    *,
    limit: int = 0,
    cache_minutes: int = _ARTICLE_CACHE_MINUTES,
) -> str:
    """按需抓一篇文章的正文页（只有看详情时才走这条路）。

    抓不到就返回空串（不抛）：调用方退回 RSS 里那段摘要，详情照发。
    """
    target = str(url or "").strip()
    if not target:
        return ""
    try:
        text = await http.request(
            "GET",
            target,
            source="news",
            expect_json=False,
            cache_ttl=float(max(0, cache_minutes) * 60),
            cache_key=f"news:article:{target}",
        )
    except Exception:  # 抓不到就退回 RSS 里那段，命令不能因为一个网页挂掉
        return ""
    return extract_article(str(text or ""), limit=limit)


# --- 汇总 -----------------------------------------------------------------


def dedupe_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按链接与标题去重（bugbug 三个 feed 之间会重复同一条新闻）。"""
    out: list[dict[str, Any]] = []
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").split("#")[0].strip()
        key = normalize_name(str(item.get("title") or "")).replace(" ", "")
        if url and url in seen_url:
            continue
        if key and key in seen_title:
            continue
        if url:
            seen_url.add(url)
        if key:
            seen_title.add(key)
        out.append(item)
    return out


def sort_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """有日期的按时间倒序在前，没日期的保持原顺序沉底。"""
    dated = [it for it in items if str(it.get("published") or "")]
    undated = [it for it in items if not str(it.get("published") or "")]
    dated.sort(key=lambda it: str(it.get("published") or ""), reverse=True)
    return dated + undated


def apply_quota(items: list[dict[str, Any]], per_source: int) -> list[dict[str, Any]]:
    """每源限流：否则一个源刷 30 条会把别家的都挤掉。"""
    if per_source <= 0:
        return list(items)
    counts: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for item in items:
        source = str(item.get("source") or "")
        if counts.get(source, 0) >= per_source:
            continue
        counts[source] = counts.get(source, 0) + 1
        out.append(item)
    return out


async def collect_news(
    http: HttpClient,
    *,
    sources: list[str] | tuple[str, ...] = DEFAULT_SOURCES,
    per_source: int = 6,
    max_items: int = 12,
    cache_minutes: int = 15,
) -> tuple[list[dict[str, Any]], list[str]]:
    """并发抓所有资讯源。

    返回 (条目, 警告)：**单源失败只记一条警告**，其余源照常出结果 ——
    新闻这种东西，一个站挂了不该让整条命令失败。
    """
    picked = [str(s).strip() for s in sources if str(s).strip()]
    if not picked:
        return [], ["没有配置任何资讯源"]

    async def one(source: str) -> list[dict[str, Any]]:
        if source == "ymgal":
            return await fetch_ymgal_articles(
                http, limit=max(per_source, 4), cache_minutes=max(cache_minutes, 30)
            )
        return await fetch_feed(
            http, source, limit=max(per_source, 4), cache_minutes=cache_minutes
        )

    results = await asyncio.gather(*[one(s) for s in picked], return_exceptions=True)
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for source, result in zip(picked, results):
        if isinstance(result, BaseException):
            warnings.append(f"{NEWS_SOURCE_LABELS.get(source, source)} 抓取失败：{result}")
            continue
        items.extend(result)

    items = apply_quota(dedupe_news(items), per_source)
    items = sort_news(items)
    return items[:max_items], warnings


# --- 翻译（日文标题 → 中文） ----------------------------------------------

_TRANSLATE_TEMPLATE = """下面是 galgame 资讯站近期文章的日文标题。请把它们翻译成简洁的中文标题。

要求：
- 只输出一个 JSON 数组，元素是字符串，顺序和数量与输入**完全一致**；
- 不要输出解释、Markdown 代码块或多余字段；
- 专有名词（作品名、厂商名）保留原文或音译，不要意译成不相关的东西；
- 标题里的【】、!!、《》等符号保留，数字与日期原样保留。

输入（共 {count} 条）：
{items}
"""

_BODY_TEMPLATE = """下面是一段 galgame 资讯的正文（日文）。请用简洁的中文把它的要点讲清楚。

要求：
- 只输出中文正文，不要标题、不要寒暄、不要 Markdown 代码块；
- 2~4 句话、200 字以内，保留作品名 / 厂商 / 发售日这些关键信息；
- 作品名与厂商名保留原文写法，不要意译。

正文：
{text}
"""


def build_translate_prompt(titles: list[str]) -> str:
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
    return _TRANSLATE_TEMPLATE.format(count=len(titles), items=lines)


def _pick_translation_array(text: str) -> list[Any] | None:
    """从一段文本里找出翻译结果数组：裸数组、{"translations": [...]} 都认。"""
    for body in (text, *_JSON_FENCE_RE.findall(text)):
        start, end = body.find("["), body.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(body[start : end + 1])
            except ValueError:
                data = None
            if isinstance(data, list):
                return data
        brace_start, brace_end = body.find("{"), body.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                obj = json.loads(body[brace_start : brace_end + 1])
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                for value in obj.values():
                    if isinstance(value, list):
                        return value
    return None


def parse_translate_reply(raw: str, count: int) -> list[str]:
    """从模型回复里抠出翻译结果；抠不出来就给空列表（调用方退原文）。

    宁可多认几种形状：实测里模型有时包一层 ```json、有时写成
    {"translations": [...]}、有时用单引号、有时干脆一行一条「1. 中文」。
    只认最标准那一种的代价是**整批标题静悄悄留在日文**，用户只会觉得
    「说好的翻译呢」。
    """
    text = str(raw or "").strip()
    if not text or count <= 0:
        return []
    fence = _JSON_FENCE_RE.search(text)
    body = fence.group(1).strip() if fence else text

    data = _pick_translation_array(body)
    if data is not None:
        out = [strip_html(x) for x in data if isinstance(x, (str, int, float))]
        out = [x for x in out if x]
        if out:
            return out[:count]

    # 单引号/漏引号的类 JSON（`['标题一', '标题二']`）
    quoted = [strip_html(m.group(1)) for m in _TITLE_QUOTE_RE.finditer(body)]
    quoted = [x for x in quoted if x]
    if len(quoted) >= 2:
        return quoted[:count]

    # 最后一条路：逐行取（`1. 中文标题`）。行数必须和输入条数一致 ——
    # 模型有时候只回一句「没找到」「抱歉，我无法翻译」，按行取就会把这种话
    # 当成标题贴出去；条数对不上就宁可留着日文原文。
    lines: list[str] = []
    for line in body.splitlines():
        item = _LINE_ITEM_RE.sub("", line.strip()).strip()
        item = item.strip("\"'“”「」").strip().rstrip(",，").strip()
        if not item or item in ("[", "]", "```", "```json"):
            continue
        if item[0] in "{}" or item.startswith("json"):
            continue
        if item.endswith(("：", ":")):  # 「翻译结果：」这种小标题
            continue
        lines.append(item)
    if len(lines) != count:
        return []
    return lines


async def translate_indexes(
    items: list[dict[str, Any]],
    indexes: list[int],
    generate: Callable[[str], Awaitable[str]] | None,
    *,
    batch: int = 20,
) -> list[str]:
    """按给定下标批量翻标题，返回与 indexes 等长的中文表（翻不动的位置给空串）。"""
    # 返回长度必须与 indexes 等长（调用方拿它跟下标 zip）：越界位置给空串占位，
    # 而不是把它们从列表里挤掉、让后面的译文整体错位。
    titles = [
        str(items[i].get("title") or "") if 0 <= i < len(items) else "" for i in indexes
    ]
    if not titles or generate is None:
        return [""] * len(titles)
    out: list[str] = []
    step = max(1, batch)
    for start in range(0, len(titles), step):
        chunk = titles[start : start + step]
        try:
            raw = await generate(build_translate_prompt(chunk))
        except Exception:  # noqa: BLE001
            out.extend([""] * len(chunk))
            continue
        got = parse_translate_reply(raw, len(chunk))
        if len(got) < len(chunk):
            got = got + [""] * (len(chunk) - len(got))
        out.extend(got[: len(chunk)])
    return out


async def translate_titles(
    items: list[dict[str, Any]],
    generate: Callable[[str], Awaitable[str]] | None,
    *,
    batch: int = 20,
) -> list[str]:
    """把新闻里的日文标题批量翻成中文。

    generate 是宿主模型的一次调用（插件里包着 ctx.llm.generate）。
    翻不动就返回空列表，调用方保留原文 —— 新闻里混几个日文标题远好过整条命令失败。
    返回值的顺序与 `lang == "ja"` 的条目一一对应（apply_translations 依赖这个）。
    """
    idxs = [
        i
        for i, it in enumerate(items)
        if isinstance(it, dict) and str(it.get("lang") or "") == "ja"
    ]
    return await translate_indexes(items, idxs, generate, batch=batch)


async def translate_missing(
    items: list[dict[str, Any]],
    generate: Callable[[str], Awaitable[str]] | None,
    *,
    batch: int = 20,
) -> int:
    """把「还没有中文名 + 标题是中日文」的条目翻出来，直接写回 title_zh。

    给**预定列表**用：月幕发售日历与 VNDB 给的都是日文原名，卡片上直接显示
    原名的话中文用户看不懂（用户实测反馈就是「说好的翻译呢」）。
    返回真正补上的条数。
    """
    idxs = [
        i
        for i, it in enumerate(items)
        if isinstance(it, dict)
        and not str(it.get("title_zh") or "").strip()
        and has_cjk(it.get("title"))
    ]
    if not idxs:
        return 0
    got = await translate_indexes(items, idxs, generate, batch=batch)
    filled = 0
    for index, zh in zip(idxs, got):
        zh = str(zh or "").strip()
        if zh and zh != str(items[index].get("title") or "").strip():
            items[index]["title_zh"] = zh
            filled += 1
    return filled


def looks_japanese(text: Any) -> bool:
    """有没有假名 —— 有就是日文（中文里不会出现假名）。"""
    return bool(_KANA_RE.search(str(text or "")))


async def translate_text(
    text: Any,
    generate: Callable[[str], Awaitable[str]] | None,
    *,
    limit: int = 900,
) -> str:
    """把一段日文正文翻成中文摘要（详情页用）。

    翻不动就给空串，调用方保留日文原文 —— 详情页宁可显示原文，也不能空着。
    """
    body = clean_summary(text, limit=limit)
    if not body or generate is None or not looks_japanese(body):
        return ""
    try:
        raw = await generate(_BODY_TEMPLATE.format(text=body))
    except Exception:  # noqa: BLE001
        return ""
    out = str(raw or "").strip()
    fence = _JSON_FENCE_RE.search(out)
    if fence:
        out = fence.group(1).strip()
    return out.strip("`").strip()



def apply_translations(items: list[dict[str, Any]], titles: list[str]) -> int:
    """把翻译结果安回条目上（只改日文源，中文源的 title_zh 已经是原文）。"""
    index = 0
    filled = 0
    for item in items:
        if str(item.get("lang") or "") != "ja":
            continue
        if index >= len(titles):
            break
        zh = str(titles[index] or "").strip()
        index += 1
        if zh:
            item["title_zh"] = zh
            filled += 1
    return filled


# --- 即将发售（月幕日历 + VNDB） ------------------------------------------


def _safe_int(value: Any, default: int = 0) -> int:
    """能转 int 就转，转不了给默认值（数据源偶尔给 null 或非数字）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _vndb_to_release(vn: dict[str, Any]) -> dict[str, Any]:
    """VNDB 条目 → 发售列表条目（键名与月幕的 _normalize_list_item 对齐）。"""
    developers = [str(x) for x in (vn.get("developers") or []) if str(x).strip()]
    return {
        "source": "vndb",
        "id": str(vn.get("id") or ""),
        "title": str(vn.get("title_ja") or vn.get("title") or ""),
        "title_zh": str(vn.get("title_zh") or ""),
        "cover": str(vn.get("cover") or ""),
        "released": str(vn.get("released") or ""),
        "have_chinese": bool(str(vn.get("title_zh") or "").strip()),
        "restricted": None,
        "developer": developers[0] if developers else "",
        "rating": vn.get("rating"),
        # 以前直接 int()：votecount 是 null / 字符串时整批「新作」都会失败
        "votecount": _safe_int(vn.get("votecount")),
        "vndb_id": str(vn.get("id") or ""),
    }


def merge_upcoming(
    ymgal_items: list[dict[str, Any]],
    vndb_items: list[dict[str, Any]],
    *,
    max_items: int = 20,
) -> list[dict[str, Any]]:
    """把月幕日历与 VNDB 的未来发售合成一张表。

    月幕优先：它带厂商与汉化状态，而且标题就是日文原名；VNDB 补上评分、
    票数与月幕还没有的条目。两边标题都尽量取日文原名，用 normalize_name
    归一化后比对去重；对不齐的名（罗马音 vs 假名）会各自留一条，
    这比误删一条真作品要好。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ymgal_items:
        key = normalize_name(str(item.get("title") or "")).replace(" ", "")
        if key:
            seen.add(key)
        out.append(item)
    for vn in vndb_items:
        converted = _vndb_to_release(vn)
        keys = {
            normalize_name(str(converted.get("title") or "")).replace(" ", ""),
            normalize_name(str(vn.get("title") or "")).replace(" ", ""),
        }
        keys.discard("")
        if keys and keys & seen:
            continue
        seen |= keys
        out.append(converted)
    out.sort(key=lambda it: (str(it.get("released") or "9999"), str(it.get("title") or "")))
    return out[:max_items] if max_items > 0 else out


def upcoming_window(days: int, *, today: date | None = None) -> tuple[str, str]:
    """未来 N 天的 (起始, 结束) 日期字符串。"""
    start = today or date.today()
    span = max(1, min(int(days), 365))
    return start.isoformat(), (start + timedelta(days=span)).isoformat()


async def collect_upcoming(
    ymgal: Any,
    vndb: Any,
    *,
    days: int = 90,
    max_items: int = 20,
) -> tuple[list[dict[str, Any]], list[str]]:
    """并发取「未来 N 天要发售」的月幕日历与 VNDB 条目。

    两个源都可能挂（月幕改版 / VNDB 限流），所以同样按源降级。
    """
    start, end = upcoming_window(days)

    async def from_ymgal() -> list[dict[str, Any]]:
        if ymgal is None:
            return []
        return await ymgal.release_calendar_range(start, end)

    async def from_vndb() -> list[dict[str, Any]]:
        if vndb is None:
            return []
        return await vndb.upcoming(days=days, limit=max(max_items, 20))

    results = await asyncio.gather(from_ymgal(), from_vndb(), return_exceptions=True)
    warnings: list[str] = []
    batches: list[list[dict[str, Any]]] = []
    for label, result in zip(("月幕日历", "VNDB"), results):
        if isinstance(result, BaseException):
            warnings.append(f"{label} 取未来发售失败：{result}")
            batches.append([])
        else:
            batches.append(list(result))
    return merge_upcoming(batches[0], batches[1], max_items=max_items), warnings
