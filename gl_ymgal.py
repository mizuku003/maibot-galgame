"""galgame领域大神 —— 月幕 Galgame（ymgal.games）客户端。

月幕是这个插件里的**中文源**，它提供三样 VNDB 给不了的东西：

1. ``chineseName``：官方认定的中文译名（不是机翻）；
2. ``haveChinese``：**有没有中文版**——中文玩家问的第一句话就是「这游戏有汉化吗」；
3. 别名检索：它的搜索能吃外号，「罚抄」能命中 Rewrite，这对中文圈的叫法太重要了。

认证走 OAuth2.0 的 client_credentials（官方给了公开凭证，查询接口无权限限制）。
官方文档特意提醒：**不要用定时器刷新 token**，而应该在「被拒绝时」再取——
所以这里只在 401 时重取，不做后台续期。
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date
from typing import Any

from gl_http import HttpClient, HttpError

BASE = "https://www.ymgal.games"
TOKEN_URL = f"{BASE}/oauth/token"
# 封面 CDN。注意月幕的接口并不统一：search-game / archive 给的是完整 URL，
# 而 random-game 只给 ``archive/main/xx/xxx.webp`` 这种相对路径，得自己补域名。
CDN_BASE = "https://cdn.ymgal.games/"


def _absolute_image(value: Any) -> str:
    """把月幕的封面字段补成绝对 URL。

    相对路径直接丢给渲染层会变成「下不下来的图」，卡片上就是一块空白 ——
    以前 ``/gal random`` 的封面一直是坏的，就是栽在这里。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "data:")):
        return text
    return CDN_BASE + text.lstrip("/")


def _attr_of(tag_html: str, name: str) -> str:
    """从一段开始标签里取属性（属性顺序不固定，所以不用一个巨型正则去抓整张卡）。"""
    match = re.search(name + r'="([^"]*)"', tag_html, re.I)
    return match.group(1) if match else ""


def strip_tags(text: Any) -> str:
    """极简去标签（只给标题用，不引入依赖）。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()


def _parse_iso_date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


# 发售日历的卡片开始标签。class 可能带别的类名，所以只认这一个词。
_CARD_START_RE = re.compile(r'<a[^>]*class="[^"]*\bgame-view-card\b[^"]*"[^>]*>', re.I)
_CARD_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_CARD_TAG_RE = re.compile(r'<span class="ant-tag[^"]*">([^<]*)</span>', re.I)
_CARD_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_CARD_GID_RE = re.compile(r"/ga(\d+)")


def parse_release_calendar(html_text: str) -> list[dict[str, Any]]:
    """解析月幕的发售日历页（/release-list/<年>/<月>）。

    卡片长这样（属性顺序以线上为准，代码里不假设）：

        <a class="game-view-card" title="日文原名" href="/ga123">
          <img class="lazy" data-original="封面">
          <h3>日文原名</h3>
          <div class="tag-info-list">
            <span class="ant-tag ant-tag-green">厂商</span>
            <span class="ant-tag ant-tag-green">2026-09-25发行</span>
            <span class="ant-tag ant-tag-green">无中文版</span>   ← 也可能整条没有
          </div>
        </a>

    两个必须记住的点：

    1. **没有作品的月份，页面上根本没有 release-list 容器**，正文只写
       「暂未发现游戏」。所以「找不到卡片」是正常结果，不是解析失败，
       更不能抛异常 —— 否则 12 月这种空档会把整条「预定」命令带崩。
    2. 汉化标签可能整条缺省（既没有「有中文版」也没有「无中文版」），
       这时按「不确定」处理，不要硬说没有。
    """
    raw = str(html_text or "")
    if not raw:
        return []

    out: list[dict[str, Any]] = []
    for match in _CARD_START_RE.finditer(raw):
        start_tag = match.group(0)
        end = raw.find("</a>", match.end())
        block = raw[match.end() : end if end > 0 else len(raw)]

        h3 = _CARD_H3_RE.search(block)
        title = _attr_of(start_tag, "title").strip() or strip_tags(h3.group(1) if h3 else "")
        if not title:
            continue
        gid_match = _CARD_GID_RE.search(_attr_of(start_tag, "href"))
        cover = _attr_of(block, "data-original").strip() or _attr_of(block, "src").strip()

        tags = [t.strip() for t in _CARD_TAG_RE.findall(block) if t.strip()]
        developer = tags[0] if tags else ""
        released = ""
        have_chinese = False
        restricted: bool | None = None
        for tag in tags[1:]:
            date_match = _CARD_DATE_RE.search(tag)
            if date_match and not released:
                released = date_match.group(1)
                continue
            if "有中文版" in tag or tag.strip() == "中文版":
                have_chinese = True
            elif "无中文版" in tag:
                have_chinese = False
            elif "全年龄" in tag:
                restricted = False

        out.append(
            {
                "source": "ymgal",
                "id": gid_match.group(1) if gid_match else "",
                "title": title,
                "title_zh": "",  # 日历页只有日文原名，别假装有中文名
                "cover": _absolute_image(cover),
                "released": released,
                "have_chinese": have_chinese,
                "restricted": restricted,
                "developer": developer,
                "score": "",
            }
        )
    return out


class YmgalClient:
    """月幕开放 API 封装（含 token 自管理）。"""

    def __init__(self, http: HttpClient, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = ""
        self._token_expire_at = 0.0

    # -- 认证 --------------------------------------------------------------

    async def _ensure_token(self, *, force: bool = False) -> str:
        """拿一个可用的 access_token（有效期内复用）。"""
        now = time.monotonic()
        if not force and self._token and now < self._token_expire_at:
            return self._token

        url = (
            f"{TOKEN_URL}?grant_type=client_credentials"
            f"&client_id={self._client_id}&client_secret={self._client_secret}&scope=public"
        )
        data = await self._http.request(
            "GET", url, source="ymgal", cache_ttl=0.0, cache_key=f"ymgal:token:{now // 60:.0f}"
        )
        token = str((data or {}).get("access_token") or "")
        if not token:
            raise HttpError("ymgal", f"取 token 失败：{data}")
        expires_in = float((data or {}).get("expires_in") or 3599)
        self._token = token
        # 留 60 秒余量，避免边界上刚好过期
        self._token_expire_at = time.monotonic() + max(expires_in - 60, 30)
        return token

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, ttl: float = 600.0) -> Any:
        """带 token 的 GET，401 时重取一次 token 再试（官方推荐的「拒绝策略」）。"""
        for attempt in range(2):
            token = await self._ensure_token(force=attempt > 0)
            headers = {
                "Accept": "application/json;charset=utf-8",
                "Authorization": f"Bearer {token}",
                "version": "1",
            }
            try:
                return await self._http.request(
                    "GET",
                    f"{BASE}{path}",
                    source="ymgal",
                    headers=headers,
                    params=params,
                    cache_ttl=ttl,
                    cache_key=f"ymgal:{path}:{params}",
                )
            except HttpError as exc:
                # token 失效才重取；其它错误（比如 614 找不到档案）直接抛
                if attempt == 0 and ("401" in exc.detail or "UNAUTHORIZED" in exc.detail):
                    self._token = ""
                    continue
                raise
        raise HttpError("ymgal", "token 重取后仍然失败")

    # -- 查询 --------------------------------------------------------------

    async def search_list(self, keyword: str, limit: int = 5) -> list[dict[str, Any]]:
        """列表搜索：支持别名与外号，返回多条。"""
        data = await self._get(
            "/open/archive/search-game",
            {"mode": "list", "keyword": keyword, "pageNum": 1, "pageSize": max(1, min(limit, 20))},
        )
        payload = (data or {}).get("data") or {}
        return [self._normalize_list_item(item) for item in (payload.get("result") or [])]

    async def search_accurate(self, keyword: str, similarity: int = 70) -> dict[str, Any] | None:
        """精确搜索：命中名称最像的那一部，直接带出详情。"""
        try:
            data = await self._get(
                "/open/archive/search-game",
                {"mode": "accurate", "keyword": keyword, "similarity": str(similarity)},
            )
        except HttpError:
            # 找不到时月幕给的是 code=614，当作「没匹配」而不是错误
            return None
        payload = (data or {}).get("data") or {}
        game = payload.get("game")
        if not game:
            return None
        return self._normalize_detail(game, payload.get("pidMapping") or {})

    async def get(self, gid: int | str) -> dict[str, Any] | None:
        """按 gid 取详情。"""
        if not gid:
            return None
        try:
            data = await self._get("/open/archive", {"gid": str(gid)}, ttl=1800.0)
        except HttpError:
            return None
        payload = (data or {}).get("data") or {}
        game = payload.get("game")
        if not game:
            return None
        return self._normalize_detail(game, payload.get("pidMapping") or {})

    async def new_releases(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """查某段时间内发售的作品（区间不超过 50 天，官方限制）。"""
        data = await self._get(
            "/open/archive/game",
            {"releaseStartDate": start_date, "releaseEndDate": end_date},
            ttl=3600.0,
        )
        items = (data or {}).get("data") or []
        if isinstance(items, dict):  # 有的接口把这个包了一层
            items = items.get("result") or []
        return [self._normalize_list_item(item) for item in items if isinstance(item, dict)]

    async def release_calendar(self, year: int, month: int) -> list[dict[str, Any]]:
        """取某一个月的发售日历页（HTML 页面，不是 JSON 接口）。"""
        url = f"{BASE}/release-list/{int(year)}/{int(month):02d}"
        text = await self._http.request(
            "GET",
            url,
            source="ymgal",
            expect_json=False,
            cache_ttl=6 * 3600.0,
            cache_key=f"ymgal:calendar:{int(year)}-{int(month):02d}",
        )
        return parse_release_calendar(str(text or ""))

    async def release_calendar_range(self, start: str, end: str) -> list[dict[str, Any]]:
        """取 [start, end] 之间要发售的作品，走发售日历 HTML。

        为什么不复用 new_releases：那个接口有 **50 天上限**，而「预定」想一次
        看 90 天。日历页按自然月分页，最多三个月，并发取回来按日期夹一下。

        空月份（月幕没有收录的月份）返回空列表，不报错；只有**一个月都取不到**
        才抛 HttpError，让上层按源降级。
        """
        first = _parse_iso_date(start)
        last = _parse_iso_date(end)
        if first is None or last is None or first > last:
            return []

        months: list[tuple[int, int]] = []
        year, month = first.year, first.month
        while (year, month) <= (last.year, last.month) and len(months) < 24:
            months.append((year, month))
            month += 1
            if month > 12:
                month = 1
                year += 1

        pages = await asyncio.gather(
            *[self.release_calendar(y, m) for y, m in months],
            return_exceptions=True,
        )

        out: list[dict[str, Any]] = []
        errors: list[str] = []
        for (y, m), page in zip(months, pages):
            if isinstance(page, BaseException):
                errors.append(f"{y}-{m:02d}: {page}")
                continue
            out.extend(page)
        if errors and not out:
            raise HttpError("ymgal", "发售日历抓取失败：" + "；".join(errors[:3]))

        picked: list[dict[str, Any]] = []
        for item in out:
            released = _parse_iso_date(item.get("released"))
            if released is None or not (first <= released <= last):
                continue
            picked.append(item)
        picked.sort(key=lambda it: (str(it.get("released") or ""), str(it.get("title") or "")))
        return picked

    async def random_games(self, count: int = 5) -> list[dict[str, Any]]:
        """随机几部（官方已过滤掉过于冷门的条目）。"""
        data = await self._get(
            "/open/archive/random-game", {"num": max(1, min(count, 10))}, ttl=0.0
        )
        items = (data or {}).get("data") or []
        return [self._normalize_list_item(item) for item in items if isinstance(item, dict)]

    async def developer_name(self, developer_id: int | str) -> str:
        """机构 id → 机构名（月幕的列表接口只给 id）。"""
        if not developer_id:
            return ""
        try:
            data = await self._get("/open/archive", {"orgId": str(developer_id)}, ttl=86400.0)
        except HttpError:
            return ""
        org = ((data or {}).get("data") or {}).get("org") or {}
        return str(org.get("chineseName") or org.get("name") or "")

    # -- 归一化 ------------------------------------------------------------

    @staticmethod
    def _aliases(game: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for item in game.get("extensionName") or []:
            name = str((item or {}).get("name") or "").strip()
            if name and name not in out:
                out.append(name)
        return out

    @classmethod
    def _normalize_list_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        """列表接口的返回比详情少很多，只取有的字段。"""
        return {
            "source": "ymgal",
            "id": str(item.get("gid") or item.get("id") or ""),
            "title": str(item.get("name") or ""),
            "title_zh": str(item.get("chineseName") or ""),
            "cover": _absolute_image(item.get("mainImg")),
            "released": str(item.get("releaseDate") or ""),
            "have_chinese": bool(item.get("haveChinese")),
            "restricted": bool(item.get("restricted")) if item.get("restricted") is not None else None,
            "developer": str(item.get("orgName") or ""),
            "score": str(item.get("score") or ""),
        }

    @classmethod
    def _normalize_detail(cls, game: dict[str, Any], pid_mapping: dict[str, Any]) -> dict[str, Any]:
        """详情接口：把角色 + CV 组合成人能看的形式。"""
        characters: list[dict[str, Any]] = []
        for rel in game.get("characters") or []:
            cid = str((rel or {}).get("cid") or "")
            cv_id = str((rel or {}).get("cvId") or "")
            cv = (pid_mapping.get(cv_id) or {}).get("name") if cv_id else ""
            characters.append(
                {
                    "cid": cid,
                    "cv": str(cv or ""),
                    "position": int((rel or {}).get("characterPosition") or 2),
                }
            )

        releases: list[dict[str, Any]] = []
        for rel in game.get("releases") or []:
            releases.append(
                {
                    "name": str((rel or {}).get("releaseName") or (rel or {}).get("release_name") or ""),
                    "platform": str((rel or {}).get("platform") or ""),
                    "language": str(
                        (rel or {}).get("releaseLanguage") or (rel or {}).get("release_language") or ""
                    ),
                    "date": str((rel or {}).get("releaseDate") or (rel or {}).get("release_date") or ""),
                }
            )

        staff: list[dict[str, Any]] = []
        for s in game.get("staff") or []:
            staff.append(
                {
                    "name": str((s or {}).get("empName") or (s or {}).get("emp_name") or ""),
                    "job": str((s or {}).get("jobName") or (s or {}).get("job_name") or ""),
                }
            )

        more: dict[str, str] = {}
        for entry in game.get("moreEntry") or []:
            key = str((entry or {}).get("key") or "").strip()
            val = str((entry or {}).get("value") or "").strip()
            if key:
                more[key] = val

        return {
            "source": "ymgal",
            "id": str(game.get("gid") or ""),
            "title": str(game.get("name") or ""),
            "title_zh": str(game.get("chineseName") or ""),
            "aliases": cls._aliases(game),
            "released": str(game.get("releaseDate") or ""),
            "cover": _absolute_image(game.get("mainImg")),
            "intro": str(game.get("introduction") or ""),
            "have_chinese": bool(game.get("haveChinese")),
            "restricted": bool(game.get("restricted")),
            "developer_id": str(game.get("developerId") or ""),
            "type_desc": str(game.get("typeDesc") or ""),
            "characters": characters,
            "releases": releases,
            "staff": staff,
            "more": more,
        }
