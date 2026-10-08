"""galgame领域大神 —— 共用 HTTP 层。

三个源都是 HTTP，但脾气不同：

- 月幕：要 ``Authorization: Bearer <token>`` 且 token 只有 1 小时寿命（自动续）；
- VNDB：只认 POST JSON，且 UA 必须能识别出项目（写进 User-Agent 配置项）；
- 批评空间：老 CGI 站，动不动给你 200 + 空 body（反爬），要当错误处理；页面是 **UTF-8**。

所以这里统一做四件事：超时、代理、UA、重试 + 一个进程内的短 TTL 缓存。
缓存很关键——同一次对话里「搜一下」再「看详情」会重复打同一个接口。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from typing import Any

import httpx

DEFAULT_UA = "maibot-galgame/1.0.0 (https://github.com/mizuku003/maibot-galgame)"


class HttpError(RuntimeError):
    """统一的请求失败异常，带上是哪个源出的问题。"""

    def __init__(self, source: str, detail: str) -> None:
        self.source = source
        self.detail = detail
        super().__init__(f"[{source}] {detail}")


class _TTLCache:
    """极简 TTL 缓存（进程内）。

    只用于「同一个问题短时间内别重复打接口」，不做持久化——
    真正需要跨重启保留的只有 EGS 索引，那个由 gl_egs 自己落盘。
    """

    def __init__(self, max_items: int = 512) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_items

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expire_at, value = item
        if expire_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if len(self._data) >= self._max:
            # 按「谁先过期」淘汰，而不是按插入顺序：先插进来的恰好是 TTL 最长、
            # 最该留住的那些（VNDB 详情 1800s、EGS 详情 3600s、封面 86400s），
            # 按插入顺序清会把它们第一批赶走，等于白缓存。
            victims = sorted(self._data.items(), key=lambda kv: kv[1][0])[: self._max // 4]
            for k, _ in victims:
                self._data.pop(k, None)
        self._data[key] = (time.monotonic() + ttl, value)


class HttpClient:
    """包装 httpx.AsyncClient，给三个源共用。"""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        proxy: str = "",
        user_agent: str = DEFAULT_UA,
        retries: int = 2,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.cache = _TTLCache()
        # 二进制下载的 single-flight 锁（按 URL）：见 fetch_bytes
        self._byte_locks: dict[str, asyncio.Lock] = {}
        headers = {
            "User-Agent": user_agent or DEFAULT_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        }
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "headers": headers,
            "follow_redirects": True,
            # 别读环境里的 HTTP_PROXY/NO_PROXY：代理走插件自己的配置项更可控，
            # 而且某些环境（比如宿主软件的 NO_PROXY 里带 [::1]）会让 httpx 直接构造失败
            "trust_env": False,
        }
        if proxy:
            # httpx 0.26+ 用 proxy=，老版本用 proxies=
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_bytes(self, url: str, *, source: str = "http") -> bytes | None:
        """取二进制（封面图用）。

        卡片渲染时宿主默认不允许页面访问外网（``allow_network`` 为假），
        所以封面必须**先下下来、再以 base64 内嵌**进 HTML，不能直接写 URL。
        失败返回 None，卡片会退化成无图版而不是渲染失败。
        """
        if not url:
            return None
        cache_key = f"bytes:{url}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        # single-flight：一张卡片会并发取 6 张封面，同一部作品的卡又可能被连着
        # 请求两次。没有这个锁的话，同一张图会被重复下一遍 —— 既慢又容易招限流。
        # 锁按 URL 分片，不同图之间不互相挡。
        lock = self._byte_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
            try:
                resp = await self._client.get(url)
                if resp.status_code >= 400:
                    return None
                data = resp.content
                # 图片不会变，缓存久一点
                self.cache.set(cache_key, data, 86400.0)
                return data
            except Exception as exc:  # noqa: BLE001
                raise HttpError(source, f"下载二进制失败：{exc}") from exc
            finally:
                # 失败也把锁摘掉：留着的话这张图以后永远走同一个锁对象，
                # 而它已经不占用了，摘掉才是常态。
                if not lock.locked():
                    self._byte_locks.pop(cache_key, None)

    async def request(
        self,
        method: str,
        url: str,
        *,
        source: str,
        cache_ttl: float = 0.0,
        cache_key: str = "",
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        """发一个请求。

        Args:
            source: 出问题时用来标识是哪家（ymgal / vndb / egs）。
            cache_ttl: 大于 0 时启用本进程内缓存。
            expect_json: False 时返回原始文本（批评空间是 HTML）。
        """
        # headers 也要进键：两套鉴权头打同一个 URL 时不能互相命中（当前调用方都显式传了
        # cache_key，但那是调用方的纪律，不该由这里默许）。
        headers_key = tuple(sorted((headers or {}).items()))
        key = cache_key or f"{method} {url} {json_body} {params} {headers_key}"
        if cache_ttl > 0:
            hit = self.cache.get(key)
            if hit is not None:
                return hit

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await self._client.request(
                    method, url, headers=headers, json=json_body, params=params
                )
                body = resp.text
                # 老站反爬时会给你 200 + 空 body，这种情况当失败重试
                if resp.status_code == 200 and not body.strip():
                    raise httpx.HTTPError("空响应（多半是反爬拦截）")
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                result: Any = resp.json() if expect_json else body
                if cache_ttl > 0:
                    self.cache.set(key, result, cache_ttl)
                return result
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.retries and _retryable(exc):
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                break
        raise HttpError(source, f"{type(last_error).__name__}: {last_error}") from last_error


def _retryable(exc: Exception) -> bool:
    """值得重试吗？只有限流（429）、服务端抖动（5xx）和网络异常值得。

    400/404 这类是「请求本身有问题」，重试多少次都一样，只会白等退避时间。
    解析失败（JSON 不是 JSON、字段类型不对）同理 —— 正文没变，重试三次结果一样，
    只是白白多等 1.8 秒、多打上游两下。
    """
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return False
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        code = exc.response.status_code
        return code == 429 or code >= 500
    # 空响应（反爬）和宿主网络抖动都算传输层问题，值得再试
    return isinstance(exc, (httpx.TransportError, httpx.HTTPError))


# --- 文本工具（三源共用） -------------------------------------------------


def normalize_name(text: str) -> str:
    """名称归一化，用于跨源匹配。

    规则尽量保守，只做「看起来一定是同一个东西」的折叠：
    全角转半角、去空格、统一大小写、去掉译名常见的括号后缀与波浪号、
    把日文长音符号与英文写法拉平。
    """
    if not text:
        return ""
    out = str(text)
    lowered = out.lower()
    # 全角 → 半角
    chars: list[str] = []
    for ch in lowered:
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    out = "".join(chars)
    # 括号/波浪号这类符号删掉就行，但 hd / fhd / ohp 这些字母缩写必须**按词**
    # 删：无边界 replace 会把 "HDR" 削成 "r"、"SOHO" 削成 "so"，
    # 两个本来不相干的标题就被当成同一个了。
    for symbol in ["～", "~", "（", "）", "(", ")", "【", "】", "[", "]"]:
        out = out.replace(symbol, "")
    # 缩写类后缀：右边界必须卡死（不然 "HDR" 会被削成 "r"），左边界反而要放开 ——
    # 日文标题常把缩写直接粘在词尾（"XXXHD" / "CLANNADHD"），卡左边界就删不掉了。
    # 顺序有讲究："fhd" 必须排在 "hd" 前面（先删 hd 会把 FHD 削成 "f"），
    # "remastered" 必须排在 "remaster" 前面。
    for word in [
        "ohp", "full voice", "remastered", "remaster", "fhd", "hd",
        "体验版", "体験版", "完全版", "決定版",
    ]:
        if word.isascii() and word.isalpha():
            out = re.sub(rf"{word}(?![a-z])", "", out)
        else:
            out = out.replace(word, "")
            # 「Remaster版」这种后面还挂个汉字的写法：缩写删掉后剩个孤零零的「版」
            if word in ("体验版", "体験版", "完全版", "決定版"):
                out = out.replace("版", "")
    for sep in [" ", "\t", "　", "-", "_", "：", ":", "・", "/", "!", "！", "?", "？", "「", "」"]:
        out = out.replace(sep, "")
    return out.strip()


# 前缀命中（查询词就是标题的开头）给的固定分。
# 这里**刻意不按长度比例给分**：否则「ATRI」会被「a(t)rium」这种拼写更像、
# 但根本是另一部作品的名字压过去 —— 长度比例在这类检索里是负资产。
PREFIX_MATCH_SCORE = 90

# 「(PS3)」「（全年齢版）」这类载体/版本后缀。索引与判重都要剥掉它，
# 所以放在这里给 gl_egs / gl_merge 共用，避免两边各写一份正则。
_VERSION_SUFFIX = re.compile(r"[（(][^）)]*[）)]\s*$")


def strip_version_suffix(title: str) -> str:
    """去掉「(PS3)」「（全年齢版）」这类载体/版本后缀。

    EGS 榜单里同一部作品的 PS3 / PSV / NS / 全年齢版 是彼此独立的条目，
    只差一个括号后缀；判重和匹配都要先把它剥掉。
    """
    return _VERSION_SUFFIX.sub("", title or "").strip()


def similarity_of_normalized(na: str, nb: str, *, min_score: int = 0) -> int:
    """两个**已归一化**名字之间的相似度。

    单独拆出来是因为索引里上万条的名字在建索引时就已经归一化好了，
    再走 :func:`similarity` 会为每条候选重复归一化一遍 —— 那是纯浪费。
    调用方必须保证两个参数都已经过 :func:`normalize_name`。

    三种情形分开给分，因为它们在检索里的含义完全不同：

    - **前缀命中**：查询词是标题的开头（``ATRI`` → ``ATRI -My Dear Moments-``）。
      这几乎可以确定是同一部，给固定高分，把「到底哪一部」交给调用方的
      次级排序（票数）去裁决。
    - **非前缀包含**：短词出现在标题中间，可信度低一档，按比例给分。
    - 其余交给 difflib 的编辑距离。

    Args:
        min_score: 调用方只关心「够不够这个分」。传了它就能在**算编辑距离之前**
            用长度把它挡掉：difflib 的 ``ratio`` 上界是 ``2·min/(min+max)``
            （匹配字符数不可能超过较短的那个串），够不到就直接返回 0。
            difflib 是这个插件里最贵的纯计算 —— 索引一万九千条时，
            预筛放行几千条无关条目，每条跑一次编辑距离就是几百毫秒。
            返回 0 与真实低分在调用方那里等价（都会被阈值丢掉），所以是安全的。
    """
    if not na or not nb:
        return 0
    if na == nb:
        return 100
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 2 and long_.startswith(short):
        return PREFIX_MATCH_SCORE
    if short in long_:
        ratio = len(short) / max(len(long_), 1)
        score = 68 + 22 * ratio
        # 一两个字的包含关系基本没有信息量（「月」vs「月姬」），压到阈值以下
        if len(short) < 3:
            score = min(score, 55)
        return int(score)
    if min_score > 0 and 2 * len(short) * 100 < min_score * (len(short) + len(long_)):
        return 0
    matcher = difflib.SequenceMatcher(None, na, nb)
    # quick_ratio() 是 ratio() 的**上界**，而且便宜得多（只数一遍字符，
    # 不做最长匹配块搜索）。上界都够不到阈值，就没必要算真正的编辑距离 ——
    # 这是整个 match() 里最热的一行：相当一部分条目会走到这里。
    if min_score > 0 and matcher.quick_ratio() * 100 < min_score:
        return 0
    return int(matcher.ratio() * 100)


def similarity(a: str, b: str) -> int:
    """0~100 的相似度（用 difflib，够用且无依赖）。内部自行归一化。"""
    return similarity_of_normalized(normalize_name(a), normalize_name(b))


def format_length(minutes: int) -> str:
    """把 VNDB 的通关分钟数说成人话。"""
    if not minutes or minutes <= 0:
        return "未知"
    hours = minutes / 60
    if hours < 2:
        return f"约 {int(minutes)} 分钟（短篇小品）"
    if hours < 10:
        return f"约 {hours:.1f} 小时"
    if hours < 30:
        return f"约 {hours:.0f} 小时"
    if hours < 50:
        return f"约 {hours:.0f} 小时（中长篇）"
    return f"约 {hours:.0f} 小时（长篇巨作）"
