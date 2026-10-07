"""galgame领域大神 —— 百度贴吧抓取。

只为一件事：给模型攒「贴吧怎么说话」的语料 —— 不签到、不回帖、不读消息。

七件实测出来的事决定了这个模块的样子（2026-09 实测）：

1. **PC 吧页已经废了**。https://tieba.baidu.com/f?kw=<吧名> 现在返回的是
   「贴吧小程序」引导页（实测 7081 字节，<title>贴吧小程序</title>，
   经典选择器 a.j_th_tit 一个都没有）。服务端还在渲染的入口是**移动 m 站**：
   /mo/q/m?kw=<吧名>&pn=<偏移>，实测每页 30 条
   <li class="tl_top ..." data-tid="...">，标题在 div.ti_title 里。
2. **pn 是偏移量，步长必须等于 30**。实测 pn=0 出第 1~30 条、pn=25 出第 26~55 条
   （与 pn=0 重叠 5 条）、pn=30 出第 31~60 条（与 pn=0 零重叠）。
   步长一旦大于 30（比如 50）就会整段漏掉第 31~50 条。
3. **裸请求会被拦**。不带任何 cookie 打贴吧任意页面 → HTTP 403 +「百度安全验证」。
   它拦的是「一个 cookie 都没有」，不是「没登录」—— 先去 https://www.baidu.com/
   走一趟拿到访客 cookie（BAIDUID 等）再回来就是 200。所以客户端第一次请求前先播种。
4. **正文不需要登录**（这处最容易搞错）。帖子页 /p/<tid> 免登录确实只给空壳：
   桌面版是 11148 字节的「百度贴吧」SPA 壳，移动版是 7081 字节的「贴吧小程序」引导页，
   d_post_content / l_post 计数全是 0。但把参数从 tid= 换成**老式的 kz=**，
   走 /mo/q/m?kz=<tid>，服务端会把首页楼层整个渲染好：实测 8/8 个帖子都能免登录
   拿到正文，每个帖子 20+ 层。所以这个模块**不需要 BDUSS 之类的任何登录票据**，
   只要一个访客 cookie。
5. **kz= 页面只有首页**。pn= 对它无效（pn=1/2/3/99 返回同一批 28~30 层）。
   想要更多语料就多抓几个帖子，而不是翻页 —— 这也是 dump_forum 的默认策略。
6. **没有服务端热门排序**。sort / st / hot / order / tab=good 五组参数实测全被忽略，
   返回的第一条帖子完全相同（服务端只按最后回复时间排）。所以「热门」只能在本模块
   里自己做：回复数就印在列表页每条帖子上（div.ti_zan_reply > div.btn_reply >
   span.btn_icon），与帖子页里的 threadInfo.reply_num 逐条一致
   （水楼列表页 234185 / 帖子页 234186，差 1 是列表快照稍旧）。
   dump_forum 因此是「抓几页 → 本地按回复数降序 → 取前 N」。
7. **楼层既没有点赞数、也没有热门排序**。帖子页里「赞」字出现 0 次，
   threadInfo 的 zan / agree 都是空数组，operate_statistic_wrapper 里只有
   btn_reply 和 btn_collect 两个按钮；sort=hot 与 see_lz 参数无效，楼层永远是
   fn=1,2,3... 的固定顺序，首页最多 30 层。所以「评论按热度排」做不到，
   替代办法是按文本形态筛：话术在短回复里、不在长文里。parse_posts 因此对楼主层
   保留全文（截断 max_chars），对回复层只留 reply_min_chars ~ reply_max_chars 的
   短句（默认 4~200 字），整条留或整条丢、绝不截半句。

拿不到正文时这里**不抛异常**，只返回空列表 + 一条日志：学习器会安静地退化成
「只学标题」，而不是让整轮学习失败。galgame 吧的标题本身就很有梗，退化也不亏。
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

TIEBA_ORIGIN = "https://tieba.baidu.com"
BAIDU_HOME = "https://www.baidu.com/"
LIST_PATH = "/mo/q/m"

# m 站的 UA。用桌面 UA 打 m 站会被跳回「贴吧小程序」引导页。
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

# 列表页每页 30 条，pn 是**偏移量**而不是页码，所以步长必须等于 30。
# 实测 pn=0 → 第 1~30 条；pn=25 → 第 26~55 条（与 pn=0 重叠 5 条）；
# pn=30 → 第 31~60 条（与 pn=0 零重叠）。写 50 会整段漏掉第 31~50 条。
# 注意：这个偏移只对列表页有效，帖子页（kz=）的 pn 是无效参数。
LIST_PAGE_STEP = 30

# 反爬：403 有两种，长得完全不一样，处理方式也相反。
#   1. 真·403（cookie 没播种上）—— 重试有意义
#   2. 403 +「百度安全验证」—— 是限流，重试只会把封禁加深
# 以前两句都写成「多半是访客 cookie 没播种成功」，把请求间隔调小之后，
# 一次真实封禁被误诊成 cookie 问题，白白多打了两轮请求。
_BLOCK_MARKERS = ("百度安全验证", "请开启JavaScript", "security_verify")
# 触发反爬后多久不再请求（秒）。宁可这一轮不学，也不要一直敲门。
BLOCK_COOLDOWN_SECONDS = 900


class TiebaBlockedError(RuntimeError):
    """被贴吧反爬拦住了（403 +「百度安全验证」）。

    这是「暂时不给我看」，不是「没有内容」—— 调用方必须停下并保留旧语料，
    绝不能把它当成「这个吧是空的」。
    """

# --- HTML 解析 -------------------------------------------------------------
#
# 这里全是正则而不是 bs4：插件只带 httpx 一个依赖（_manifest.json 里写死的），
# 引入 bs4 会让用户装插件时多一步；而贴吧这两处结构十几年没变过，正则够用，
# 也没有「bs4 没装 → 静默 0 结果」那种坑
# （真有插件栽在这上面：没装 bs4 时只记一条 debug，结果静默 0）。

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{2,}")

# 列表项：<li class="tl_top ..." data-tid="9857572690"> … </li>
# class 前缀 tl_ 是实测出来的（tl_top 是置顶，普通帖是 tl_shadow）
_LIST_ITEM_RE = re.compile(
    r'<li[^>]*class="[^"]*\btl_[^"]*"[^>]*data-tid="(?P<tid>\d+)"[^>]*>(?P<body>.*?)</li>',
    re.S,
)
_TITLE_RE = re.compile(
    r'<div[^>]*class="[^"]*\bti_title\b[^"]*"[^>]*>(?P<body>.*?)</div>', re.S
)
_SPAN_RE = re.compile(r"<span[^>]*>(?P<text>.*?)</span>", re.S)

# 回复数（这就是「热门排序」和「质量门槛」唯一的量化依据）：
#
#   <div class="ti_zan_reply clearfix">
#     <div class="ti_func_btn btn_reply"><span class="btn_icon">238</span></div>
#   </div>
#
# 置顶帖（吧规 / 工作楼）没有这个节点 → 回复数记 0，配合 exclude 天然被排除。
# 实测该数字与帖子页里 threadInfo.reply_num 一致，所以不用为「查热度」多花一次请求。
_REPLY_BLOCK_RE = re.compile(
    r'class="[^"]*\bti_zan_reply\b[^"]*"[^>]*>(?P<block>.{0,600}?)</a>', re.S
)
_REPLY_NUM_RE = re.compile(
    r'class="[^"]*\bbtn_reply\b[^"]*"[^>]*>.{0,200}?class="btn_icon"[^>]*>(?P<num>\d+)',
    re.S,
)

# 楼层（帖子页）。实测结构，2026-09::
#
#   <ul id="pblist" class="list ">
#     <li tid="153971846150" data-tid="153971846150" fn="1"
#         data-info="{&quot;un&quot;:&quot;&quot;,&quot;is_annoy&quot;:0,&quot;pid&quot;:153971846150,
#                     &quot;floor_num&quot;:1,&quot;name_show&quot;:&quot;贴吧用户_Qe9S8PX&quot;,...}"
#         class="list_item post_list_item default_feedback j_post_list_item no_border" is_inner_floor=''>
#       <div class="list_item_wrapper"><div class="list_main">
#         <div class="post_title_embed">标题</div>
#         <div class="list_item_top clearfix">
#           <div class="list_item_user_wrap">…<span class="user_name"><a …>作者</a></span>
#             …<span class="list_item_time">今天 16:04</span></div>
#           <div class="content" lz="1">正文</div>       ← lz="1" 表示楼主层
#         </div>
#       </div></div>
#     </li>
#
# 注意外层的 <li> 可能自带缩进/换行，属性顺序也可能变，所以先切 <li> 块再单独读属性。
_FLOOR_LI_RE = re.compile(r"<li(?P<attrs>[^>]*)>(?P<body>.*?)</li>", re.S)
_ATTR_RE = re.compile(r'(?P<key>[a-zA-Z_:-]+)="(?P<value>[^"]*)"')
_CONTENT_OPEN_RE = re.compile(r'<div[^>]*class="[^"]*\bcontent\b[^"]*"[^>]*>')
_DIV_TAG_RE = re.compile(r"<(?P<close>/?)div\b[^>]*>")
_FLOOR_USER_RE = re.compile(r'class="user_name"[^>]*>(?P<name>.*?)</a>', re.S)
_FLOOR_TIME_RE = re.compile(r'class="list_item_time">(?P<time>.*?)</span>', re.S)

# 移动页会往正文里塞导流提示，学话术没用，顺手删掉
_NAG_RE = re.compile(
    r"^.*(?:打开贴吧\s*App|看高清大图|下载贴吧\s*App|打开手百\s*App).*$", re.M | re.I
)

# 贴吧经典的引流广告。实测楼层里混着「复制到别的帖子在点开 maqne...」这类东西，
# 学进去只会教坏模型，整条丢。
_SPAM_RE = re.compile(r"复制到别的?帖子?在点开|加我(?:微信|QQ)|私聊我", re.I)


def _text_of(raw: str) -> str:
    """把一段 HTML 片段变成可读文本：去标签、还原实体、压空白。"""
    if not raw:
        return ""
    out = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    out = re.sub(r"</p>", "\n", out, flags=re.I)
    out = _TAG_RE.sub("", out)
    out = _html.unescape(out)
    out = _WS_RE.sub(" ", out)
    return _BLANK_RE.sub("\n", out).strip()


def _clean_content(text: str) -> str:
    """去掉正文里的导流提示，再压一遍空行。"""
    if not text:
        return ""
    return _BLANK_RE.sub("\n", _NAG_RE.sub("", text)).strip()


def _attrs_of(raw: str) -> dict[str, str]:
    return {m.group("key"): m.group("value") for m in _ATTR_RE.finditer(raw or "")}


def _data_info(raw: str) -> dict[str, Any]:
    """读楼层上的 data-info（HTML 转义过的 JSON）。读不出来就返回空 dict。"""
    if not raw:
        return {}
    try:
        obj = json.loads(_html.unescape(raw))
    except Exception:  # noqa: BLE001
        return {}
    return obj if isinstance(obj, dict) else {}


def _reply_count_of(body: str) -> int:
    """从列表项里读回复数。读不到返回 0（置顶帖就是这种情况）。"""
    block_m = _REPLY_BLOCK_RE.search(body or "")
    if not block_m:
        return 0
    num_m = _REPLY_NUM_RE.search(block_m.group("block"))
    if not num_m:
        return 0
    try:
        return int(num_m.group("num"))
    except (TypeError, ValueError):
        return 0


def _extract_content(body: str) -> tuple[str, bool]:
    """从楼层片段里取出正文 div 的内容，返回（文本, 是否楼主层）。

    用配平扫描而不是非贪婪正则：正文里可能嵌 div（引用、图片、楼中楼），
    非贪婪会在一半就截断。
    """
    open_m = _CONTENT_OPEN_RE.search(body)
    if not open_m:
        return "", False
    start = open_m.end()
    depth = 1
    seg = body[start:]
    for tag_m in _DIV_TAG_RE.finditer(body, start):
        depth += -1 if tag_m.group("close") else 1
        if depth == 0:
            seg = body[start : tag_m.start()]
            break
    is_op = 'lz="1"' in open_m.group(0)
    return _clean_content(_text_of(seg)), is_op


@dataclass
class TiebaThread:
    """列表页里的一个帖子。

    replies 是列表页上印着的回复数 —— 它决定了这个帖子排第几、进不进门槛，
    也顺带告诉学习器「这个话题有多少人在说话」。
    """

    tid: str
    title: str
    forum: str = ""
    is_top: bool = False
    replies: int = 0

    @property
    def url(self) -> str:
        return f"{TIEBA_ORIGIN}/p/{self.tid}"

    @property
    def mobile_url(self) -> str:
        """真正能拿到正文的那个地址（老式 kz= 参数）。"""
        return f"{TIEBA_ORIGIN}{LIST_PATH}?kz={self.tid}"


@dataclass
class TiebaPost:
    """帖子里的一个楼层。"""

    author: str = ""
    content: str = ""
    floor: int = 0
    time: str = ""
    is_op: bool = False


@dataclass
class TiebaForumDump:
    """一次抓取的产物，交给学习器去提炼。"""

    forum: str
    threads: list[TiebaThread] = field(default_factory=list)
    posts: list[tuple[TiebaThread, list[TiebaPost]]] = field(default_factory=list)
    fetched_threads: int = 0
    failed_threads: int = 0
    # 门槛统计：让「为什么这次只学到几条」能在日志里看见，
    # 而不是像 EGS 索引那次一样静默截断
    seen_threads: int = 0
    skipped_low: int = 0
    skipped_high: int = 0
    # 因为「正文已经读过」而跳过的帖子数
    skipped_known: int = 0
    # 被反爬拦住时置 True：这一轮的数字全是「没抓到」而不是「没有」。
    # 调用方必须区别对待，别把它当成「这个吧是空的」。
    blocked: bool = False

    def titles(self) -> list[str]:
        return [t.title for t in self.threads if t.title]

    def texts(self) -> list[str]:
        """标题 + 所有楼层正文，学习器真正要吃的东西。"""
        out = list(self.titles())
        for _thread, posts in self.posts:
            out.extend(p.content for p in posts if p.content)
        return out


def _looks_blocked(status_code: int, text: str) -> bool:
    """这一页到底是反爬拦截页，还是正常内容？

    只看状态码不够：百度有时候也用 200 回验证页。
    """
    if status_code == 403:
        return True
    if status_code >= 400:
        return False
    head = (text or "")[:2000]
    return any(marker in head for marker in _BLOCK_MARKERS)


class TiebaClient:
    """贴吧抓取客户端。

    生命周期跟插件绑在一起（on_unload 里 aclose()）。内部自己维护 cookie：
    只有访客 cookie，**不需要登录**。每次请求之间 sleep delay_seconds（带抖动），
    因为贴吧对高频访问很敏感 —— 列表页很便宜，帖子正文页才是触发限流的地方。
    """

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        proxy: str = "",
        delay_seconds: float = 3.0,
        retries: int = 2,
    ) -> None:
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.delay_seconds = max(0.0, float(delay_seconds))
        self._blocked_at = 0.0
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            # 跟 gl_http 一个理由：别让宿主环境里的 HTTP_PROXY / NO_PROXY 干扰，
            # 代理只走插件自己的配置项
            "trust_env": False,
            "headers": {
                "User-Agent": MOBILE_UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            },
        }
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)
        self._seeded = False
        self._seeded_ok = False      # 播种是否真的成功过：失败时的 403 = 缺 cookie，不是反爬
        self._seed_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- 底层 ---------------------------------------------------------------

    async def _sleep(self) -> None:
        """请求之间歇一下。加抖动是因为固定间隔反而更像脚本。"""
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds * random.uniform(0.8, 1.3))

    def _blocked_remaining(self) -> float:
        """还在冷却期里就返回剩余秒数，否则 0。"""
        if not self._blocked_at:
            return 0.0
        left = BLOCK_COOLDOWN_SECONDS - (time.monotonic() - self._blocked_at)
        return left if left > 0 else 0.0

    def _mark_blocked(self) -> None:
        self._blocked_at = time.monotonic()

    async def _seed_guest_cookie(self) -> None:
        """先访问一次百度首页，把访客 cookie 拿回来。

        没有这一步，贴吧任意页面都是 403 +「百度安全验证」——
        实测拦的是「一个 cookie 都没有」，不是「没登录」。
        """
        if self._seeded:
            return
        async with self._seed_lock:
            if self._seeded:
                return
            try:
                await self._client.get(
                    BAIDU_HOME, headers={"User-Agent": MOBILE_UA, "Accept": "text/html,*/*"}
                )
                self._seeded_ok = True
            except Exception as exc:  # noqa: BLE001
                # 播种失败也放行：有些网络环境下百度首页不可达但 tieba 本身可达
                logger.debug("[Galgame/贴吧] 访客 cookie 播种失败（继续尝试）：%s", exc)
            self._seeded = True

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        """取一页 HTML。失败返回 None（调用方决定是重试还是放弃）。

        唯独反爬拦截会抛 TiebaBlockedError —— 那是「暂时不给我看」，
        重试只会把封禁加深，必须让整轮抓取立刻停下来。
        """
        left = self._blocked_remaining()
        if left > 0:
            raise TiebaBlockedError(
                f"还在反爬冷却期（剩 {int(left)} 秒），本轮不再请求贴吧"
            )
        await self._seed_guest_cookie()
        merged = dict(headers or {})
        last: Exception | None = None
        reseeded = False
        for attempt in range(self.retries + 1):
            try:
                resp = await self._client.get(url, params=params, headers=merged)
                # 访客 cookie 没拿到时贴吧对**任意**页面都回 403（它自己的 docstring 就这么写）。
                # 这跟反爬拦截是两回事：先重新播种一次再重试，别直接判成封禁把整轮停掉。
                if resp.status_code == 403 and not self._seeded_ok and not reseeded:
                    reseeded = True
                    self._seeded = False
                    await self._seed_guest_cookie()
                    raise httpx.HTTPError("HTTP 403（缺访客 cookie，已重新播种）")
                if _looks_blocked(resp.status_code, resp.text):
                    self._mark_blocked()
                    logger.warning(
                        "[Galgame/贴吧] 触发反爬拦截（%s + 百度安全验证）：%s —— "
                        "本轮停止，%s 秒内不再请求贴吧（旧语料保留）",
                        resp.status_code,
                        url,
                        BLOCK_COOLDOWN_SECONDS,
                    )
                    raise TiebaBlockedError("贴吧返回「百度安全验证」，已停止本轮抓取")
                if resp.status_code >= 400:
                    raise httpx.HTTPError(f"HTTP {resp.status_code}")
                text = resp.text
                if not text.strip():
                    raise httpx.HTTPError("空响应")
                return text
            except TiebaBlockedError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.8 * (attempt + 1))
        logger.warning("[Galgame/贴吧] 请求失败 %s：%s", url, last)
        return None

    # -- 列表 ---------------------------------------------------------------

    async def fetch_thread_list(self, forum: str, *, pages: int = 1) -> list[TiebaThread]:
        """抓某吧的帖子列表（标题 + 回复数）。

        Args:
            forum: 吧名，不带「吧」字（galgame / galgame笑话）。
            pages: 翻几页，每页 30 条，按 LIST_PAGE_STEP 递增偏移。
        """
        forum = (forum or "").strip()
        if not forum:
            return []
        out: list[TiebaThread] = []
        seen: set[str] = set()
        for page in range(max(1, int(pages))):
            offset = page * LIST_PAGE_STEP
            raw = await self._get(
                f"{TIEBA_ORIGIN}{LIST_PATH}",
                params={"kw": forum, "pn": offset},
                headers={"User-Agent": MOBILE_UA},
            )
            await self._sleep()
            if raw is None:
                # 单页失败就跳过去继续翻，别把「没取到」当成「没有更多」
                # （EGS 索引那次就是这么静默截断到 300 条的）
                continue
            page_items = parse_thread_list(raw, forum=forum)
            fresh = 0
            for item in page_items:
                if item.tid in seen:
                    continue
                seen.add(item.tid)
                out.append(item)
                fresh += 1
            logger.debug(
                "[Galgame/贴吧] %s 第 %s 页（偏移 %s）：解析 %s 条（新增 %s）",
                forum,
                page + 1,
                offset,
                len(page_items),
                fresh,
            )
            if not page_items:
                # 空页才是真正的「到头了」
                break
        return out

    # -- 正文 ---------------------------------------------------------------

    async def fetch_thread_posts(
        self,
        thread: TiebaThread,
        *,
        max_posts: int = 30,
        max_chars: int = 800,
        reply_min_chars: int = 4,
        reply_max_chars: int = 200,
    ) -> list[TiebaPost]:
        """抓一个帖子的首页楼层。免登录，不需要任何票据。"""
        referer = f"{TIEBA_ORIGIN}{LIST_PATH}?kw={quote(thread.forum)}" if thread.forum else ""
        headers = {"User-Agent": MOBILE_UA}
        if referer:
            headers["Referer"] = referer
        raw = await self._get(
            f"{TIEBA_ORIGIN}{LIST_PATH}",
            # 老式 kz=（帖子 id）参数才会给服务端渲染的正文；
            # 换成 tid= 会拿到 /p/ 那套空壳。pn 对 kz= 无效，只出首页。
            params={"kz": thread.tid, "pn": 1},
            headers=headers,
        )
        await self._sleep()
        if raw is None:
            return []
        posts = parse_posts(
            raw,
            max_posts=max_posts,
            max_chars=max_chars,
            reply_min_chars=reply_min_chars,
            reply_max_chars=reply_max_chars,
        )
        if not posts:
            logger.debug("[Galgame/贴吧] 帖子 %s 没解析出正文（可能被删/结构变了）", thread.tid)
        return posts

    async def dump_forum(
        self,
        forum: str,
        *,
        list_pages: int = 4,
        max_threads: int = 50,
        min_replies: int = 10,
        max_replies: int = 2000,
        hot: bool = True,
        max_posts: int = 30,
        max_chars: int = 800,
        reply_min_chars: int = 4,
        reply_max_chars: int = 200,
        skip_tids: set[str] | None = None,
    ) -> TiebaForumDump:
        """抓一个吧：抓几页列表 → 本地挑帖 → 逐个帖子抓正文。

        「热门排序」是本模块自己做的：服务端没有排序参数（实测 sort / st / hot /
        order / tab 全被忽略），但回复数就印在列表页每条帖子上，所以抓几页回来自己排。
        回复数门槛同时挡两头 —— 太低（< min_replies）样本量不够、话术不典型；
        太高（> max_replies）基本是水楼 / 直播楼，里面全是「+1」「顶」。

        正文抓不到不算失败 —— threads 里的标题照样是有用的语料
        （galgame 吧 / galgame笑话吧的标题本身就很有梗）。
        """
        dump = TiebaForumDump(forum=forum)
        try:
            threads = await self.fetch_thread_list(forum, pages=list_pages)
        except TiebaBlockedError:
            # 连列表都没拿到 —— 什么都没学到，但原因必须写清楚
            dump.blocked = True
            return dump
        # 置顶帖一般是啊规 / 导航，且列表页不给回复数，天然被排除
        threads = [t for t in threads if not t.is_top]
        dump.seen_threads = len(threads)

        lo, hi = int(min_replies), int(max_replies)
        dump.skipped_low = sum(1 for t in threads if t.replies < lo)
        dump.skipped_high = sum(1 for t in threads if t.replies > hi)
        kept = [t for t in threads if lo <= t.replies <= hi]
        if skip_tids:
            # 读过正文的帖子不再读第二遍：正文页是触发反爬的地方，
            # 而重复语料在入库那头本来就会被去重挡掉
            before = len(kept)
            kept = [t for t in kept if t.tid not in skip_tids]
            dump.skipped_known = before - len(kept)
            logger.debug(
                "[Galgame/贴吧] %s 跳过 %s 条已读过的帖子，剩 %s 条新帖",
                forum,
                dump.skipped_known,
                len(kept),
            )
        if hot:
            # 回复数降序；同数保持列表顺序（也就是最后回复时间新的在前）
            kept.sort(key=lambda t: -t.replies)
        logger.debug(
            "[Galgame/贴吧] %s 候选 %s 条 → 门槛 %s~%s 后剩 %s 条（低于下限 %s，高于上限 %s）",
            forum,
            dump.seen_threads,
            lo,
            hi,
            len(kept),
            dump.skipped_low,
            dump.skipped_high,
        )
        if not kept and dump.seen_threads:
            logger.warning(
                "[Galgame/贴吧] %s 的 %s 条帖子全部落在回复数门槛 %s~%s 之外，本次不学这个吧",
                forum,
                dump.seen_threads,
                lo,
                hi,
            )

        dump.threads = kept[: max(0, int(max_threads))]
        try:
            for thread in dump.threads:
                posts = await self.fetch_thread_posts(
                    thread,
                    max_posts=max_posts,
                    max_chars=max_chars,
                    reply_min_chars=reply_min_chars,
                    reply_max_chars=reply_max_chars,
                )
                if posts:
                    dump.posts.append((thread, posts))
                    dump.fetched_threads += 1
                else:
                    dump.failed_threads += 1
        except TiebaBlockedError:
            # 剩下的帖子下次再说。已经拿到的标题/正文照常交给学习器，
            # 但 blocked 会一路传到日志里，免得「没抓到」被看成「这个吧是空的」。
            dump.blocked = True
            dump.failed_threads += max(0, len(dump.threads) - dump.fetched_threads - dump.failed_threads)
        return dump


# --- 纯函数：解析（拆出来是为了能脱离网络单测） ----------------------------


def parse_thread_list(raw: str, *, forum: str = "") -> list[TiebaThread]:
    """解析 m 站列表页。

    结构（实测，2026-09）::

        <li class="tl_shadow tl_shadow_new" data-tid="11041271660" data-floor="28">
          <div class="ti_infos clearfix">…作者 / 时间…</div>
          <a href="/p/11041271660?lp=5028&mo_device=1" class="j_common ti_item">
            <div class="ti_title"><span>真正的标题</span></div>
            <div class="ti_zan_reply clearfix">
              <div class="ti_func_btn btn_reply"><span class="btn_icon">238</span></div>
            </div>
          </a>
        </li>

    标题取 div.ti_title 里**最后一个** span —— 第一个是「置顶 / 精」图标。
    回复数取 div.ti_zan_reply 里 btn_reply 的 btn_icon。
    顺带一提：li 上的 data-floor 是**列表序号**（置顶为 0），不是楼层数，别拿来当热度。
    """
    out: list[TiebaThread] = []
    for m in _LIST_ITEM_RE.finditer(raw or ""):
        tid = m.group("tid")
        body = m.group("body")
        title = ""
        title_m = _TITLE_RE.search(body)
        if title_m:
            spans = list(_SPAN_RE.finditer(title_m.group("body")))
            if spans:
                title = _text_of(spans[-1].group("text"))
            else:
                title = _text_of(title_m.group("body"))
        if not title:
            title = _text_of(body)
        if not title:
            continue
        is_top = "ti_icon_zhiding" in body
        out.append(
            TiebaThread(
                tid=tid,
                title=title,
                forum=forum,
                is_top=is_top,
                replies=0 if is_top else _reply_count_of(body),
            )
        )
    return out


def parse_posts(
    raw: str,
    *,
    max_posts: int = 30,
    max_chars: int = 800,
    reply_min_chars: int = 4,
    reply_max_chars: int = 200,
) -> list[TiebaPost]:
    """解析帖子页的楼层。

    一个楼层一个 li 元素，class 里带 post_list_item、fn 属性是楼层号；作者、时间、
    正文都在**同一个 li 里**（这点比老式桌面页好，桌面页要跨节点配对，容易张冠李戴）。
    楼中楼回复是嵌在父楼层正文里的纯文本（形如「某某: 内容」），不单独成层。

    楼层取舍是这个模块的「伪热门排序」：贴吧的楼层既没有点赞数也没有热度参数
    （实测 sort=hot / see_lz 无效、zan 数组为空、页面上「赞」字出现 0 次），
    但话术本来就不在长文里 —— 短回复才是金矿。所以：

    * 楼主层（lz=1）：保留全文，长了按 max_chars 截断（主楼往往是一整段感想）。
    * 回复层：只保留 reply_min_chars ~ reply_max_chars 长度的，**整条留或整条丢**，
      不截半句（半截句子学出来只会是坏语料）。
    """
    if not raw:
        return []
    posts: list[TiebaPost] = []
    limit = max(0, int(max_posts))
    lo, hi = int(reply_min_chars), int(reply_max_chars)
    for li_m in _FLOOR_LI_RE.finditer(raw):
        attrs = _attrs_of(li_m.group("attrs"))
        if "post_list_item" not in attrs.get("class", ""):
            continue
        body = li_m.group("body")
        content, is_op = _extract_content(body)
        if not content:
            continue
        if _SPAM_RE.search(content):
            continue
        if is_op:
            if max_chars > 0 and len(content) > max_chars:
                content = content[:max_chars]
        elif not (lo <= len(content) <= hi):
            continue
        info = _data_info(attrs.get("data-info", ""))
        name_m = _FLOOR_USER_RE.search(body)
        if name_m:
            author = _text_of(name_m.group("name"))
        else:
            # name_show 可能是显式 null（.get 的默认值只对「没有这个 key」生效），
            # str(None) 会写出一个 "None" 当作者名。
            _name_raw = info.get("name_show")
            author = _text_of(_name_raw) if isinstance(_name_raw, str) else ""
        time_m = _FLOOR_TIME_RE.search(body)
        try:
            floor = int(attrs.get("fn") or info.get("floor_num") or 0)
        except (TypeError, ValueError):
            floor = 0
        posts.append(
            TiebaPost(
                author=author,
                content=content,
                floor=floor,
                time=_text_of(time_m.group("time")) if time_m else "",
                is_op=is_op,
            )
        )
        if limit and len(posts) >= limit:
            break
    return posts
