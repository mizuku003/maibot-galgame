"""galgame领域大神 —— VNDB（Visual Novel Database）客户端。

只走 Kana API（v2）：``POST https://api.vndb.org/kana/<endpoint>``，读接口免认证。
一般可以直连，不需要代理。

VNDB 是这个插件里**唯一能给出「通关时长」和「内容标签树」**的源，所以它是推荐功能的骨架：

- ``length_minutes``：通关时长（分钟），分档判断短篇/中篇/长篇全靠它；
- ``tags``：标签带 ``rating``（该标签对该作品的权重）与 ``spoiler``（剧透等级），
  按权重排序后取前几个才是有信息量的标签，原始顺序没意义；
- ``titles``：多语言标题，含 ``zh-Hans`` / ``zh-Hant``，是中文名的重要补充。

注意 VNDB 的使用条款要求 UA 能识别出调用方（配置项里已带项目地址），
另外它有限流（约 200 次 / 5 分钟），所以这里做了缓存和最小字段拉取。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from gl_http import HttpClient, HttpError

API = "https://api.vndb.org/kana"

# 列表页只取必要字段：字段越多 VNDB 那边越慢，也越容易撞限流
LIST_FIELDS = (
    "id,title,alttitle,released,rating,votecount,length_minutes,"
    "image.url,developers.name,platforms,titles.lang,titles.title,titles.main"
)
DETAIL_FIELDS = (
    LIST_FIELDS + ",description,aliases,olang,tags.id,tags.name,tags.rating,tags.spoiler,tags.category"
)

# 常见标签名 → VNDB 的英文标签名。
#
# VNDB 的标签**只有英文名**，直接传「百合」「泣系」在 /tag 里搜不到，
# 而搜不到时的后果（条件被静默丢弃、返回未筛选结果）比报错更糟。
# 所以这里先把最常见的一批中文/日文叫法折成英文 —— 都是实测能在 VNDB 命中的名字。
TAG_ALIASES: dict[str, str] = {
    # 情感基调
    "泣系": "Nakige", "泣きゲー": "Nakige", "催泪": "Nakige",
    "郁系": "Utsuge", "鬱ゲー": "Utsuge", "致郁": "Utsuge",
    # 题材
    "百合": "Yuri", "gl": "Yuri",
    "耽美": "Boys Love", "bl": "Boys Love",
    "推理": "Mystery", "悬疑": "Suspense", "ミステリ": "Mystery",
    "恐怖": "Horror", "ホラー": "Horror",
    "科幻": "Science Fiction", "sf": "Science Fiction",
    "奇幻": "Fantasy", "ファンタジー": "Fantasy",
    "恋爱": "Romance", "纯爱": "Romance",
    "搞笑": "Comedy", "コメディ": "Comedy",
    "战斗": "Battle", "バトル": "Battle",
    "日常": "Slice of Life",
    "后宫": "Harem", "ハーレム": "Harem",
    # 设定与角色
    "校园": "School", "学园": "School",
    "妹妹": "Imouto", "实妹": "Imouto",
    "女装": "Cross-dressing", "伪娘": "Cross-dressing",
    # 形式
    "短篇": "Short",
    "音乐": "Music",
}
# 英文名本身也要能大小写不敏感地命中（"yuri" -> "Yuri"）
TAG_ALIASES.update({v.lower(): v for v in TAG_ALIASES.values()})

# 这条提示会被带进给用户的回复里，说清楚为什么标签没筛成
TAG_HINT = (
    "VNDB 的标签只有英文名。常见标签可以直接用中文"
    "（百合 / 泣系 / 郁系 / 推理 / 纯爱 / 战斗 / 日常 / 后宫 / 校园 / 短篇…），"
    "其余请用英文名（如 Nakige、Utsuge、Mystery）"
)


def resolve_tag_alias(name: str) -> str:
    """把常见中文标签名折成 VNDB 的英文名；没有别名就原样返回。"""
    key = (name or "").strip().lower()
    return TAG_ALIASES.get(key, (name or "").strip())


class VndbClient:
    """VNDB Kana API 的最小封装。"""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    # -- 内部 --------------------------------------------------------------

    async def _post(
        self, endpoint: str, body: dict[str, Any], *, ttl: float = 300.0
    ) -> dict[str, Any]:
        return await self._http.request(
            "POST",
            f"{API}/{endpoint}",
            source="vndb",
            json_body=body,
            cache_ttl=ttl,
            cache_key=f"vndb:{endpoint}:{body}",
        )

    # -- 查询 --------------------------------------------------------------

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """全文搜索（中/日/英标题都能搜到，VNDB 自己会处理）。"""
        data = await self._post(
            "vn",
            {
                "filters": ["search", "=", query],
                "fields": LIST_FIELDS,
                "results": max(1, min(limit, 25)),
                "sort": "searchrank",
            },
            ttl=600.0,
        )
        return [self._normalize(vn) for vn in data.get("results", [])]

    async def get(self, vn_id: str) -> dict[str, Any] | None:
        """按 id 取详情（v12849 这种）。"""
        if not vn_id:
            return None
        data = await self._post(
            "vn",
            {"filters": ["id", "=", vn_id], "fields": DETAIL_FIELDS, "results": 1},
            ttl=1800.0,
        )
        results = data.get("results", [])
        return self._normalize(results[0]) if results else None

    async def query_page(
        self,
        *,
        filters: list[Any],
        sort: str = "rating",
        reverse: bool = True,
        limit: int = 10,
        page: int = 1,
        want_count: bool = False,
    ) -> dict[str, Any]:
        """按条件筛选，保留 ``more`` / ``count`` 这些分页元信息。

        和别处只取结果的查法不同：它把游标信息一起带回来 —— 「按评分找作品」
        要能翻到中坚作品而不是永远停在榜首，就靠这个。

        Args:
            limit: 单页条数。VNDB 硬上限是 **100**（传 101 直接 400），
                别照搬直觉里的「随便取多少」。
            page: 第几页，从 1 开始；``more`` 为假表示后面没了。
            want_count: 是否让 VNDB 统计符合条件的总数（多花一点开销）。

        两个语法坑记在这：
        - 降序**不是** ``sort="-"`` 前缀，而是单独一个 ``reverse: true``；
        - 排序只认 id / title / released / rating / votecount / searchrank，
          想按长短排只能取回来自己在本地排。
        """
        body: dict[str, Any] = {
            "filters": filters,
            "fields": LIST_FIELDS,
            "results": max(1, min(limit, 100)),
            "sort": sort,
            "reverse": reverse,
        }
        if page > 1:
            body["page"] = int(page)
        if want_count:
            body["count"] = True
        data = await self._post("vn", body, ttl=600.0)
        return {
            "results": [self._normalize(vn) for vn in data.get("results", [])],
            "more": bool(data.get("more")),
            "count": int(data.get("count") or 0),
        }

    async def upcoming(self, *, days: int = 90, limit: int = 20) -> list[dict[str, Any]]:
        """未来 N 天要发售的日文作品。

        这是补月幕的盲区用的一条路：月幕的日历只收录它自己收了的作品，
        而 VNDB 的 released 覆盖更全，还带评分与票数。注意 VNDB 的 released
        精度可能只到月（例如 2026-09 这种），排序时按字符串比就行。
        """
        today = date.today()
        span = max(1, min(int(days), 365))
        page = await self.query_page(
            filters=[
                "and",
                ["released", ">=", today.isoformat()],
                ["released", "<=", (today + timedelta(days=span)).isoformat()],
                ["olang", "=", "ja"],
            ],
            sort="released",
            reverse=False,
            limit=max(1, min(int(limit), 100)),
            want_count=True,
        )
        return list(page.get("results") or [])

    async def latest_added(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """最近被 VNDB 收录的作品（sort=id 倒序）。

        注意语义：这是「刚进数据库」，不是「刚公布」—— 里面有 TBA，也有补录的
        老作品（实测第一页混着 released 为 2025 的条目）。所以它只适合当
        「数据库最近更新了什么」的边角料，不要拿它当新作榜。
        """
        page = await self.query_page(
            filters=[],
            sort="id",
            reverse=True,
            limit=max(1, min(int(limit), 100)),
        )
        return list(page.get("results") or [])

    async def tag_id(self, name: str) -> str | None:
        """标签名 → 标签 id（g833 这种）。

        VNDB 的筛选只认 id，所以「泣系」「推理」这类关键词得先解析成 id。
        传中文别名（百合 → Yuri）也能解析，见 :data:`TAG_ALIASES`。

        解析失败返回 None。**调用方不能因此把标签条件丢掉** ——
        那会让用户以为筛过了，实际拿到的是未筛选结果。
        """
        name = resolve_tag_alias(name)
        if not name:
            return None
        try:
            # 注意：/tag 的合法字段里**没有 rating**（带了会 400），别照搬 /vn 的字段列表
            data = await self._post(
                "tag",
                {"filters": ["search", "=", name], "fields": "id,name,vn_count", "results": 10},
                ttl=86400.0,
            )
        except HttpError:
            return None
        results = data.get("results", [])
        if not results:
            return None
        # 优先同名精确匹配（"Mystery" 要命中 Mystery 而不是 Mystery Elements），
        # 没有同名的再退而求其次，取作品数最多的那个
        target = name.strip().lower()
        for tag in results:
            if str(tag.get("name") or "").strip().lower() == target:
                return str(tag.get("id") or "") or None
        results.sort(key=lambda t: int(t.get("vn_count") or 0), reverse=True)
        return str(results[0].get("id") or "") or None

    async def characters(self, vn_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """出场角色 + 声优。

        要打两次接口，因为 VNDB 把这两半数据放的地方不一样：
        - 角色本体在 ``/character``（字段是 id/name/original/image/gender）；
        - 声优挂在 ``/vn`` 的 ``va`` 字段上（``va.character.id`` + ``va.staff.name``），
          ``/character`` 上**没有** va 字段，写成 ``fields="va"` 会直接 400。
        拿回来按 character.id 本地 join。
        """
        if not vn_id:
            return []
        data = await self._post(
            "character",
            {
                "filters": ["vn", "=", ["id", "=", vn_id]],
                "fields": "id,name,original,image.url,gender",
                "results": max(1, min(limit, 25)),
            },
            ttl=3600.0,
        )
        results = data.get("results", [])

        va_map: dict[str, dict[str, Any]] = {}
        try:
            vn_data = await self._post(
                "vn",
                {
                    "filters": ["id", "=", vn_id],
                    "fields": "va.character.id,va.staff.name,va.staff.original",
                    "results": 1,
                },
                ttl=3600.0,
            )
            for rel in (vn_data.get("results") or [{}])[0].get("va") or []:
                cid = str((rel.get("character") or {}).get("id") or "")
                if cid:
                    va_map[cid] = rel.get("staff") or {}
        except HttpError:
            va_map = {}  # 声优拿不到不影响角色列表

        out: list[dict[str, Any]] = []
        for ch in results:
            cid = str(ch.get("id") or "")
            staff = va_map.get(cid) or {}
            gender = ch.get("gender")
            out.append(
                {
                    "id": cid,
                    "name": str(ch.get("name") or ""),
                    "name_original": str(ch.get("original") or ""),
                    "gender": (gender[0] if isinstance(gender, list) and gender else "") or "",
                    "va": str(staff.get("name") or ""),
                    "va_original": str(staff.get("original") or ""),
                    "image": (ch.get("image") or {}).get("url") or "",
                }
            )
        return out

    # -- 归一化 ------------------------------------------------------------

    @staticmethod
    def _normalize(vn: dict[str, Any]) -> dict[str, Any]:
        """把 VNDB 的返回压成插件自己的扁平结构。"""
        titles = vn.get("titles") or []
        by_lang: dict[str, str] = {}
        for item in titles:
            lang = str(item.get("lang") or "")
            title = str(item.get("title") or "")
            # main 表示该语言的主标题，优先用它
            if title and (lang not in by_lang or item.get("main")):
                by_lang[lang] = title

        tags: list[dict[str, Any]] = []
        for tag in vn.get("tags") or []:
            if tag.get("spoiler", 0) and float(tag.get("spoiler") or 0) >= 1:
                continue  # 剧透标签不上卡片
            tags.append(
                {
                    "id": str(tag.get("id") or ""),
                    "name": str(tag.get("name") or ""),
                    "rating": float(tag.get("rating") or 0.0),
                    "category": str(tag.get("category") or "cont"),
                }
            )
        tags.sort(key=lambda t: t["rating"], reverse=True)

        return {
            "source": "vndb",
            "id": str(vn.get("id") or ""),
            "title": str(vn.get("title") or ""),
            "title_ja": by_lang.get("ja", ""),
            "title_zh": by_lang.get("zh-Hans") or by_lang.get("zh-Hant") or "",
            "title_en": by_lang.get("en", "") or str(vn.get("alttitle") or ""),
            "released": str(vn.get("released") or ""),
            "rating": float(vn["rating"]) if vn.get("rating") is not None else None,
            "votecount": int(vn.get("votecount") or 0),
            "length_minutes": int(vn.get("length_minutes") or 0),
            "cover": (vn.get("image") or {}).get("url") or "",
            "developers": [str(d.get("name") or "") for d in (vn.get("developers") or [])],
            "platforms": [str(p) for p in (vn.get("platforms") or [])],
            "tags": tags,
            "description": str(vn.get("description") or ""),
            "aliases": [str(a) for a in (vn.get("aliases") or [])],
        }
