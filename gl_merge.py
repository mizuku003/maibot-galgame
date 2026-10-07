"""galgame领域大神 —— 三源合并与推荐引擎。

三个源各说各话，这里负责把它们拼成一份「资料卡」：

- **月幕**给中文名、别名、汉化状态、中文简介、角色/CV、发售版本；
- **VNDB**给通关时长、标签树、多语言标题、开发商、平台；
- **批评空间**给中央値 / 平均値 / 数据数 / 得点分布。

合并的两条原则：

1. **不覆盖，只并列**。三个源对同一部作品的评分口径不同（VNDB 是贝叶斯分，
   EGS 是中央值），硬合成一个数只会误导人 —— 所以卡片上分开列，让用户自己看。
2. **缺数据不报错**。任何一源挂了或没收录，都只是卡片上少一块，
   绝不能因为 EGS 没索引就整条查询失败。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from gl_egs import EgsClient
from gl_http import format_length, normalize_name, similarity, strip_version_suffix
from gl_vndb import TAG_HINT, VndbClient
from gl_ymgal import YmgalClient

# VNDB 的 length 过滤器只有 1~5 五个档位（不是分钟），做时长筛选时先用它粗筛
LENGTH_BUCKETS = {
    1: (0, 120),        # Very short  <2h
    2: (120, 600),      # Short       2~10h
    3: (600, 1800),     # Intermediate 10~30h
    4: (1800, 3000),    # Long        30~50h
    5: (3000, 10**9),   # Very long   >50h
}

# 同一组条件最多能往后翻到第几条。翻页是把 offset 折进候选量、再整体去重富化，
# 越深越慢（每部一次网络请求），所以给一个明确的天花板：到顶之后 find() 直接返回空，
# 由调用方明确告诉用户「翻到底了」，而不是静默给一页空结果。
MAX_OFFSET = 60
# 候选池上限：比翻页上限多留 10 条，给「补完信息后被时长/年份/评分条件筛掉」的部分。
_CANDIDATE_POOL = MAX_OFFSET + 10


@dataclass
class GameInfo:
    """一部作品的合并视图。字段全部可空——缺数据是常态，不是异常。"""

    title_zh: str = ""
    title_ja: str = ""
    title_en: str = ""
    title_main: str = ""
    aliases: list[str] = field(default_factory=list)
    released: str = ""
    developer: str = ""
    length_minutes: int = 0
    have_chinese: bool | None = None
    restricted: bool | None = None
    intro: str = ""
    cover: str = ""
    tags: list[dict[str, Any]] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    staff: list[dict[str, Any]] = field(default_factory=list)
    characters: list[dict[str, Any]] = field(default_factory=list)
    releases: list[dict[str, Any]] = field(default_factory=list)
    vndb_id: str = ""
    ymgal_id: str = ""
    egs_id: str = ""
    egs_detail: dict[str, Any] | None = None
    vndb_rating: float | None = None
    vndb_votes: int = 0
    egs_median: int | None = None
    egs_mean: float | None = None
    egs_votes: int = 0
    sources: list[str] = field(default_factory=list)
    # 批评空间的「评价强度」数据（来自详情页，比一个总分有信息量）
    egs_praise_rate: int | None = None
    egs_stacked: dict[str, Any] | None = None
    egs_giveup: dict[str, Any] | None = None
    egs_play_minutes: int = 0
    egs_interesting_minutes: int = 0
    egs_genre: str = ""
    egs_attributes: list[str] = field(default_factory=list)
    egs_tags: list[str] = field(default_factory=list)
    egs_pov: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    egs_reviews: list[dict[str, Any]] = field(default_factory=list)
    # 长文感想（真正的锐评）：短评只有一两句，长文才是完整的批评与吐槽
    egs_long_reviews: list[dict[str, Any]] = field(default_factory=list)

    def display_title(self) -> str:
        """优先中文名，其次日文原名，最后 VNDB 主标题。"""
        return self.title_zh or self.title_ja or self.title_main or self.title_en or "（未知作品）"

    def length_text(self) -> str:
        return format_length(self.length_minutes)

    def chinese_text(self) -> str:
        if self.have_chinese is None:
            return "未知"
        return "有中文版" if self.have_chinese else "暂无中文版"

    def character_lines(self, limit: int = 12) -> list[str]:
        """角色 + 声优的展示行。

        名字优先用**日文原名**（``name_original``）：中文圈讨论 gal 声优时用的就是
        日文名，VNDB 给的罗马音反而不认得。没有声优的角色（比如男主）只留名字。

        月幕那条链路的角色只有 cid 没有名字，会被这里过滤掉 —— 角色数据以 VNDB 为准。
        """
        out: list[str] = []
        for item in self.characters[:limit]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name_original") or item.get("name") or "").strip()
            if not name:
                continue
            voice = str(item.get("va_original") or item.get("va") or "").strip()
            out.append(f"{name}（CV：{voice}）" if voice else name)
        return out

    def rating_lines(self) -> list[tuple[str, str]]:
        """三源评分并列展示（口径不同，不合并）。"""
        out: list[tuple[str, str]] = []
        if self.egs_median:
            detail = f"（平均 {self.egs_mean:.1f} · {self.egs_votes} 人评分）" if self.egs_mean else ""
            out.append(("批评空间 中央値", f"{self.egs_median} {detail}"))
        if self.vndb_rating is not None:
            out.append(("VNDB", f"{self.vndb_rating:.1f} / 100（{self.vndb_votes} 票）"))
        return out


class GalgameService:
    """把三个源缝在一起的门面。"""

    def __init__(
        self,
        vndb: VndbClient | None,
        ymgal: YmgalClient | None,
        egs: EgsClient | None,
        *,
        fetch_egs_detail: bool = True,
        max_reviews: int = 3,
        match_threshold: int = 72,
        fetch_long_reviews: bool = True,
        max_long_reviews: int = 2,
        scan_pages: int = 5,
    ) -> None:
        self.vndb = vndb
        self.ymgal = ymgal
        self.egs = egs
        # 详情页多抓一次请求，但"评价如何"全靠它；1 小时缓存，重复问不会重复打
        self.fetch_egs_detail = fetch_egs_detail
        self.max_reviews = max_reviews
        self.match_threshold = match_threshold
        # 长文感想（锐评）：每篇一次请求，所以只抓前几篇
        self.fetch_long_reviews = fetch_long_reviews
        self.max_long_reviews = max(0, max_long_reviews)
        # 索引不可用时，二分扫榜取几页候选
        self.scan_pages = max(1, scan_pages)
        # 本轮查询里「被忽略掉的用户条件」（目前是解析不出来的标签）。
        # 调用方取走并写进回复 —— 静默丢条件比报错更糟：用户会以为筛过了。
        self.last_warnings: list[str] = []

    # -- 查询 --------------------------------------------------------------

    async def search(self, keyword: str, limit: int = 5) -> list[GameInfo]:
        """跨源搜索：中文/日文/英文/外号都能吃。

        月幕优先（它认识「罚抄」这种外号），VNDB 兜底（它认识日文原名和英文名）。
        两边结果按名称相似度去重后合并。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        tasks = []
        if self.ymgal:
            tasks.append(self.ymgal.search_list(keyword, limit=limit))
        if self.vndb:
            tasks.append(self.vndb.search(keyword, limit=limit))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: list[GameInfo] = []
        for chunk in results:
            if isinstance(chunk, Exception) or not chunk:
                continue
            for raw in chunk:  # type: ignore[union-attr]
                self._merge_into_list(merged, raw)
        # 名称和关键词越像排越前，其次看票数（知名作品优先）
        merged.sort(
            key=lambda g: (
                max(
                    (
                        similarity(keyword, x)
                        for x in [g.title_zh, g.title_ja, g.title_main, g.title_en, *g.aliases[:5]]
                        if x
                    ),
                    default=0.0,   # 名称全空的条目不该让整次搜索崩掉
                ),
                g.vndb_votes,
            ),
            reverse=True,
        )
        return merged[:limit]

    async def lookup(self, keyword: str, *, with_egs_detail: bool = False) -> GameInfo | None:
        """查一部作品的完整资料：三源各取一次，最后合并。"""
        self.last_warnings = []
        keyword = (keyword or "").strip()
        if not keyword:
            return None

        ymgal_hit: dict[str, Any] | None = None
        vndb_hit: dict[str, Any] | None = None

        # 1) 月幕精确搜索（吃外号）+ VNDB 搜索，并发。
        #    VNDB 这里取多条而不是 limit=1 —— 它的 searchrank 只按文本相关性排，
        #    会把「名字长得像但很冷门」的作品排在正主前面（见 _pick_vndb_hit）。
        #    反正是一次请求，多取几条不额外花时间，挑哪条由后面的相似度+票数决定。
        tasks: list[Any] = []
        if self.ymgal:
            tasks.append(self.ymgal.search_accurate(keyword))
        if self.vndb:
            tasks.append(self.vndb.search(keyword, limit=8))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        idx = 0
        if self.ymgal:
            ymgal_hit = None if isinstance(results[idx], Exception) else results[idx]
            idx += 1
        if self.vndb:
            chunk = None if isinstance(results[idx], Exception) else results[idx]
            vndb_hit = self._pick_vndb_hit(keyword, ymgal_hit, list(chunk or []))

        # 2) 月幕没精确命中时用列表搜兜底，然后取第一名详情
        if not ymgal_hit and self.ymgal:
            try:
                candidates = await self.ymgal.search_list(keyword, limit=1)
                if candidates:
                    ymgal_hit = await self.ymgal.get(candidates[0]["id"])
            except Exception:  # noqa: BLE001
                ymgal_hit = None

        # 3) VNDB 那条线空手而归、但月幕已经确定了是哪部 → 拿月幕给的原名/中文名再搜一次。
        #    用户不会规规矩矩只发作品名（「苍之彼方的四重奏的声优是谁」这种整句很常见），
        #    整句丢给 VNDB 的全文检索是搜不出东西的；而月幕吃的是模糊匹配，
        #    它给的原名才是 VNDB 认得的键。这就是 _enrich_from_title 里那套「月幕当桥」。
        if vndb_hit is None and ymgal_hit and self.vndb:
            for bridge_name in (ymgal_hit.get("title"), ymgal_hit.get("title_zh")):
                if not bridge_name:
                    continue
                try:
                    hits = await self.vndb.search(str(bridge_name), limit=8)
                except Exception:  # noqa: BLE001
                    hits = []
                vndb_hit = self._pick_vndb_hit(str(bridge_name), ymgal_hit, hits)
                if vndb_hit:
                    break

        # 4) 搜索返回的是「列表字段」，没有标签和简介 —— 卡片要这两个，所以补一次详情。
        #    只补一条，VNDB 有限流，别贪。
        if vndb_hit and vndb_hit.get("id") and self.vndb:
            try:
                full = await self.vndb.get(str(vndb_hit["id"]))
                if full:
                    vndb_hit = full
            except Exception:  # noqa: BLE001 —— 补不到就用列表字段，卡片少显示点而已
                pass

        if not ymgal_hit and not vndb_hit:
            # 只开着批评空间时也要能查作品 —— 直接搜 EGS。
            # 拿得到原名、中央値、票数、发售日与厂商，比直接回一句「没找到」有用得多；
            # 这也让「关掉某两个源」不至于把插件变成废件。
            egs_hit = await self.egs.search_best(keyword) if self.egs else None
            if not egs_hit:
                return None
            info = GameInfo(title_ja=str(egs_hit.get("name") or ""))
            self._apply_egs_hit(info, egs_hit)
            if with_egs_detail or self.fetch_egs_detail:
                await self._load_egs_detail(info)
            return info

        info = self._build_info(vndb_hit, ymgal_hit)

        # 4) 批评空间：先本地索引匹配，没命中再直接搜 EGS
        await self._attach_egs(info, with_detail=with_egs_detail or self.fetch_egs_detail)
        return info

    async def _attach_egs(self, info: GameInfo, *, with_detail: bool) -> None:
        """给作品挂上批评空间的评分；with_detail 时再抓一次详情页补"评价如何"。

        详情页给的东西才是玩家真正想知道的：好评率、積んでる率（积压）、
        面白くなってきた時間（多久开始好看）、用户评价维度（好在哪 / 差在哪）、短评。

        找 EGS id 有两条路，**先本地后联网**：

        1. **本地索引匹配** —— 零网络开销，榜单收录的作品基本都在里面；
        2. 索引没命中、或者索引压根还没建好时，**直接搜 EGS**（``kensaku.php``）。

        第 2 条是「批评空间只是附加项」的兜底：索引缺失 / 过期 / 构建失败时，
        查具体作品照样能拿到评分，而不是整块功能直接消失。
        """
        if not self.egs or info.egs_id:
            return
        names = (
            info.title_ja,
            info.title_zh,
            info.title_main,
            info.title_en,
            *info.aliases[:4],
        )
        hit: dict[str, Any] | None = None
        if self.egs.index.items:
            hit = self.egs.index.match(*names, threshold=self.match_threshold)
        if not hit:
            hit = await self.egs.search_best(*names, threshold=self.match_threshold)
        if not hit:
            return
        self._apply_egs_hit(info, hit)
        if with_detail:
            await self._load_egs_detail(info)

    @staticmethod
    def _apply_egs_hit(info: GameInfo, hit: dict[str, Any]) -> None:
        """把一条 EGS 命中（本地索引行 / 搜索结果）落到 ``GameInfo`` 上。

        两种命中的字段略有差别：**索引行**有 ``mean`` 但没有 ``released``，
        **搜索结果**反过来 —— 有 ``released`` 与 ``brand``，没有 ``mean``。
        所以这里一律按「有就填」处理，缺的那个自然留空。
        """
        info.egs_id = str(hit.get("id") or "")
        info.egs_median = int(hit.get("median") or 0) or None
        info.egs_mean = float(hit.get("mean") or 0.0) or None
        info.egs_votes = int(hit.get("votes") or 0)
        # 搜索结果带发售日与品牌 —— 别的源缺失时正好补上
        if not info.released:
            info.released = str(hit.get("released") or "")
        if not info.developer:
            info.developer = str(hit.get("brand") or "")
        if "egs" not in info.sources:
            info.sources.append("egs")

    async def _load_egs_detail(self, info: GameInfo) -> None:
        """按 ``info.egs_id`` 抓详情页，补上「评价如何」。

        详情页给的东西才是玩家真正想知道的：好评率、積んでる率（积压）、
        面白くなってきた時間（多久开始好看）、用户评价维度（好在哪 / 差在哪）、短评。
        """
        if not self.egs or not info.egs_id:
            return
        detail = await self.egs.get_detail(info.egs_id, max_reviews=self.max_reviews)
        if not detail:
            return
        info.egs_detail = detail
        # 详情页比榜单行更准，覆盖之
        if detail.get("median") is not None:
            info.egs_median = int(detail["median"])
        if detail.get("mean") is not None:
            info.egs_mean = float(detail["mean"])
        if detail.get("votes"):
            info.egs_votes = int(detail["votes"])
        info.egs_praise_rate = detail.get("praise_rate")
        info.egs_stacked = detail.get("stacked")
        info.egs_giveup = detail.get("giveup")
        info.egs_play_minutes = int(detail.get("play_minutes") or 0)
        info.egs_interesting_minutes = int(detail.get("interesting_minutes") or 0)
        info.egs_genre = str(detail.get("genre") or "")
        info.egs_attributes = list(detail.get("attributes") or [])
        info.egs_tags = list(detail.get("tags") or [])
        info.egs_pov = dict(detail.get("pov") or {})
        info.egs_reviews = list(detail.get("reviews") or [])
        # 锐评素材：短评里已经带好了长文入口，这里按「不剧透优先、字数多优先」挑几篇抓正文
        if self.fetch_long_reviews and self.max_long_reviews > 0:
            info.egs_long_reviews = await self._fetch_long_reviews(info)

    async def _fetch_long_reviews(self, info: GameInfo) -> list[dict[str, Any]]:
        """抓「长文感想」—— 批评空间里唯一称得上锐评的东西。

        短评只有一两句话（`最高のゲーム`），长文才是完整的批评、吐槽与拆解
        （动辄几千字）。入口和字数、剧透标记都嵌在短评里，所以挑选不花请求。

        挑选顺序：**不剧透优先，其次字数多优先**。剧透长文对「锐评」没帮助，
        反而会毁掉用户的游戏体验，所以永远排在后面。
        """
        if not self.egs or not info.egs_id:
            return []
        candidates = [r for r in info.egs_reviews if r.get("memo_uid")]
        candidates.sort(
            key=lambda r: (not r.get("memo_spoiler", False), int(r.get("memo_length") or 0)),
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        for review in candidates[: self.max_long_reviews]:
            got = await self.egs.get_long_review(info.egs_id, str(review["memo_uid"]))
            if not got:
                continue
            got.setdefault("score", review.get("score"))
            got["length"] = int(review.get("memo_length") or 0)
            got["spoiler"] = bool(review.get("memo_spoiler"))
            got["user"] = str(review.get("user") or "")
            out.append(got)
        return out

    async def characters_for(self, info: GameInfo, limit: int = 12) -> list[dict[str, Any]]:
        """给一部**已经查好的**作品补角色与声优。

        单独开一个方法而不是让调用方走 :meth:`characters`，是因为那个内部还要再
        ``lookup`` 一次 —— 调用方手上已经有 ``GameInfo`` 了，没必要把三家接口再打一遍。

        VNDB 的角色数据最全（它是唯一带声优映射的源），拿不到就返回空列表，
        不影响主流程：查不到声优是「少一块」，不该让整个查询失败。
        """
        if not self.vndb or not info.vndb_id:
            return []
        try:
            return await self.vndb.characters(info.vndb_id, limit=limit)
        except Exception:  # noqa: BLE001
            return []

    async def characters(self, keyword: str, limit: int = 10) -> tuple[GameInfo | None, list[dict[str, Any]]]:
        """按名字查角色 + 声优（自行 lookup 的便捷入口）。"""
        info = await self.lookup(keyword)
        if info is None:
            return None, []
        return info, await self.characters_for(info, limit=limit)

    def egs_ready(self) -> bool:
        """批评空间这条路现在能不能用（有本地索引才算）。

        调用方拿它决定文案：索引不可用时 ``min_egs`` 是**降级成 VNDB 分**跑的，
        结果对得上"高分"这个意图，但口径不是批评空间，得跟用户说清楚。
        """
        return bool(self.egs and self.egs.index.items)

    async def find(
        self,
        *,
        keyword: str = "",
        length_max_minutes: int = 0,
        length_min_minutes: int = 0,
        tags: list[str] | None = None,
        min_rating: float = 0.0,
        max_rating: float = 0.0,
        min_egs: float = 0.0,
        max_egs: float = 0.0,
        min_votes: int = 0,
        year_from: int = 0,
        exclude_restricted: bool = False,
        prefer_chinese: bool = True,
        sort: str = "votes",
        count: int = 5,
        offset: int = 0,
        att_id: int = 0,
    ) -> list[GameInfo]:
        """按条件找作品 —— 「按评分检索」和「推荐」是同一件事。

        两条路径，按条件自动选：

        - **批评空间优先**（``sort="egs"``，或给了 ``min_egs``/``max_egs`` 且本地索引可用）：
          批评空间的中文圈口碑最权威，而它的数据就在本地索引里 —— 直接在索引上按
          中央值筛+排序，零 API 开销，再挑前几部去 VNDB 补时长/标签。
        - **VNDB 优先**（其余情况，含所有带标签/时长/年份这类结构化条件的查询）：
          VNDB 的过滤能力最强，先服务端粗筛，取回来再本地按真实分钟数精筛，
          并挂上批评空间分数。

        关于「按评分检索」的两个关键点：

        1. **上下界都要有**。默认按人气排序时，只给下限会返回「符合条件里最有人气的
           那几部」，分数不是主要因素；要真正锁定「75~80 分那段」得同时给
           ``min_egs`` 与 ``max_egs``（``sort="egs"`` 时这个要求更严格：
           排序恒为分数降序，只给下限时设 75 和设 95 会拿到同一批榜首）。
        2. **``offset`` 用来翻页**。合格集合可能很大，默认返回的永远是头部；
           想看到中坚作品就得往后翻。

        注意 VNDB 的 ``length`` 过滤器只有 1~5 档（不是分钟），所以时长永远是
        「服务端档位粗筛 + 本地分钟精筛」两步走。
        """
        self.last_warnings = []
        # offset 是「第几页 × 每页条数」。候选池有上限，越界时不可能再翻出一页 ——
        # 早点返回空，让调用方给出「已到翻页上限」的提示。
        if offset >= MAX_OFFSET:
            return []
        tags = list(tags or [])
        want_minutes = length_max_minutes > 0 or length_min_minutes > 0
        want_egs = min_egs > 0 or max_egs > 0
        # 批评空间的榜单前排几乎都是大长篇，所以「同时要求时长/标签」时不能从 EGS 起步 ——
        # 那些结构化条件 VNDB 能服务端筛，让它当主路径，EGS 分数只用来本地过滤。
        egs_index = bool(self.egs and self.egs.index.items)
        egs_first = egs_index and not want_minutes and not tags and (
            sort == "egs" or want_egs or att_id > 0
        )

        # 索引不可用时两条路，**优先走真的那条**：
        #   ① 榜单是降序的，二分就能定位到任意分数档 —— 拿回来的就是真中央值；
        #   ② 二分也失败（站点连不上）才降级成 VNDB 分近似，由调用方注明。
        # 早先只有 ②：用户要批评空间分，却拿到 VNDB 分，口径是错的。
        degraded_egs = want_egs and not egs_index
        scanned_items: list[dict[str, Any]] = []
        if degraded_egs and self.egs:
            try:
                scanned_items = await self.egs.scan_median_band(
                    min_egs=int(min_egs or 0),
                    max_egs=int(max_egs or 0),
                    pages=self.scan_pages,
                )
            except Exception:  # noqa: BLE001
                scanned_items = []
        if degraded_egs and not scanned_items:
            min_rating = max(min_rating, min_egs)
            if max_egs > 0:
                max_rating = max_egs if max_rating <= 0 else min(max_rating, max_egs)

        # offset 的语义是「跳过前 N 条**最终结果**」，所以只能在最后一步切片。
        # 曾经把它切在候选池上，结果富化/过滤会吃掉一部分候选，「翻一页」实际只挪动了
        # 寥寥几条，用户看到的两页大面积重叠。这里改成让下游多产出 offset+count 条。
        want = max(1, offset + count)

        vndb_args = dict(
            keyword=keyword,
            tags=tags,
            min_rating=min_rating,
            max_rating=max_rating,
            min_votes=min_votes,
            year_from=year_from,
            length_max_minutes=length_max_minutes,
            length_min_minutes=length_min_minutes,
            sort=sort,
            count=want,
        )
        egs_args = dict(
            keyword=keyword,
            min_egs=min_egs,
            max_egs=max_egs,
            length_max_minutes=length_max_minutes,
            length_min_minutes=length_min_minutes,
            min_rating=min_rating,
            max_rating=max_rating,
            min_votes=min_votes,
            year_from=year_from,
            sort=sort,
            count=want,
        )

        # 类型过滤（`/gal 类型 SLG`）：attlist 给的是「该类型有哪些 id」，
        # 和本地索引取交集就有分数了 —— 全程只多一次请求。
        allow_ids: set[str] | None = None
        if att_id > 0 and self.egs:
            allow_ids = await self.attlist_ids(int(att_id))
            if not allow_ids:
                return []

        if scanned_items:
            # 索引不可用，但二分扫榜拿到了真数据 —— 走和索引一样的后续处理
            picked = await self._find_by_egs(
                **egs_args, source_items=scanned_items, allow_ids=allow_ids
            )
        elif egs_first:
            picked = await self._find_by_egs(**egs_args, allow_ids=allow_ids)
        else:
            # 降级时 EGS 分已经被折算成 VNDB 分的区间条件，这里就不要再按
            # egs_median 过滤一遍 —— 索引不可用时 egs_median 恒为空，会全被筛掉。
            picked = await self._find_by_vndb(
                **vndb_args,
                min_egs=0.0 if degraded_egs else min_egs,
                max_egs=0.0 if degraded_egs else max_egs,
                allow_ids=allow_ids,
            )
            if not picked and egs_index and want_egs:
                picked = await self._find_by_egs(**egs_args, allow_ids=allow_ids)

        if exclude_restricted:
            picked = [g for g in picked if g.restricted is not True]

        # 最后一道去重：补完 VNDB 信息后，不同版本会塌缩成同一部作品
        picked = self._dedupe_by_work(picked)

        def score(game: GameInfo) -> float:
            base = float(game.vndb_rating or 0.0)
            if prefer_chinese and game.have_chinese:
                base += 6.0  # 有汉化对中文玩家是硬加分
            if game.egs_median:
                base += (float(game.egs_median) - 80.0) * 0.3  # 批评空间中央值小幅参与
            return base

        if sort in ("relevance", "score"):
            picked.sort(key=score, reverse=True)
        return picked[offset : offset + count]

    @staticmethod
    def _row_to_info(item: dict[str, Any]) -> GameInfo:
        """EGS 榜单行 → ``GameInfo``（只有名字与分数，其余靠后续富化补）。"""
        return GameInfo(
            title_ja=str(item.get("name") or ""),
            developer=str(item.get("brand") or ""),
            egs_id=str(item.get("id") or ""),
            egs_median=int(item.get("median") or 0) or None,
            egs_mean=float(item.get("mean") or 0.0) or None,
            egs_votes=int(item.get("votes") or 0),
            sources=["egs"],
        )

    async def build_from_rows(
        self, rows: list[dict[str, Any]], *, count: int = 5
    ) -> list[GameInfo]:
        """把一批 EGS 榜单行直接变成可展示的 ``GameInfo``（补 VNDB 与月幕的信息）。

        给 `/gal 人气` 这类「本来就已经是榜单行」的场景用 ——
        它们不需要再按条件筛，只需要富化 + 去重。
        """
        picked = [self._row_to_info(row) for row in rows[: min(count + 10, 45)]]
        if not picked:
            return []
        semaphore = asyncio.Semaphore(4)

        async def guarded(game: GameInfo) -> None:
            async with semaphore:
                # 这里的候选是**原始 EGS 榜单行**，只有名字与中央值，所以必须走
                # 完整富化（月幕当桥 → 再查 VNDB 补时长/标签/评分）。
                # 曾经这里用的是只补月幕与 EGS 的 `_enrich`，结果 `/gal 人气`
                # 与 `/gal 年份` 出来的作品没有通关时长、没有 VNDB 评分与票数。
                await self._enrich_from_title(game)

        await asyncio.gather(*[guarded(g) for g in picked], return_exceptions=True)
        return self._dedupe_by_work(picked)[:count]

    async def attlist_ids(self, att_id: int, *, limit: int = 5000) -> set[str]:
        """取某个 EGS 类型（属性）下的全部作品 id。

        ``attlist.php`` 只给「该类型有哪些作品」、**不带分数**，
        所以拿它的 id 去和本地索引**取交集** —— 这样既有类型过滤，
        又白拿了中央值，还只花一次请求。
        """
        if not self.egs or att_id <= 0:
            return set()
        rows = await self.egs.attlist_games(att_id, limit=limit)
        return {str(r.get("id") or "") for r in rows if r.get("id")}

    async def _find_by_egs(
        self,
        *,
        keyword: str = "",
        min_egs: float = 0.0,
        max_egs: float = 0.0,
        length_max_minutes: int = 0,
        length_min_minutes: int = 0,
        min_rating: float = 0.0,
        max_rating: float = 0.0,
        min_votes: int = 0,
        year_from: int = 0,
        sort: str = "votes",
        count: int = 5,
        source_items: list[dict[str, Any]] | None = None,
        allow_ids: set[str] | None = None,
    ) -> list[GameInfo]:
        """批评空间优先：筛候选 → 排序 → 取前几部补 VNDB/月幕信息。

        Args:
            source_items: 候选来源。默认用本地索引；传了就用传进来那批 ——
                索引缺失/过期时，二分扫榜拿到的行就是这样进来的，
                两条路共用同一套后续处理（补信息、去重、按条件筛）。
            allow_ids: 只保留这些 EGS id（``/gal 类型`` 的类型过滤用它）。
        """
        assert self.egs is not None
        index = self.egs.index
        lo = min_egs if min_egs > 0 else 0
        hi = max_egs if max_egs > 0 else 100
        base_items = source_items if source_items is not None else index.items
        candidates = [
            item
            for item in base_items
            if lo <= int(item.get("median") or 0) <= hi
            and int(item.get("votes") or 0) >= max(min_votes, 0)
        ]
        if allow_ids is not None:
            candidates = [
                item for item in candidates if str(item.get("id") or "") in allow_ids
            ]
        # 索引里没有发售日字段，所以 sort="released" 不该硬凑一个假的排序键
        # （曾经用 name 顶替，结果是「按标题顺序」这种毫无意义的顺序），
        # 认不出来的一律回落到中央值降序。
        key = {
            "votes": lambda it: int(it.get("votes") or 0),
        }.get(sort, lambda it: int(it.get("median") or 0))
        candidates.sort(key=key, reverse=True)
        if keyword:
            # 关键词过滤：索引里只有日文名，所以用归一化包含 + 相似度兜底。
            # 归一化形式建索引时就算好了（index.norm_of），别在一万多个候选上重算一遍。
            keyword_norm = normalize_name(keyword)
            candidates = [
                it
                for it in candidates
                if keyword_norm
                and (
                    keyword_norm in index.norm_of(it)
                    or similarity(keyword, str(it.get("name") or "")) >= 70
                )
            ]
        # 索引里同一部作品的多个载体版本是彼此独立的条目，先按「剥掉版本后缀的名字」
        # 去重 —— 否则「CLANNAD」会以 PS3 / PSV / NS 三个身份并排出现。
        # 去重放在取短名单之前，否则重复项会白占候选名额。
        deduped: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for item in candidates:
            # 索引里的条目有预算好的归一化名；二分扫回来的行不在索引里，现算一次
            name_key = index.base_norm_of(item) or normalize_name(
                strip_version_suffix(str(item.get("name") or ""))
            )
            if name_key and name_key in seen_names:
                continue
            if name_key:
                seen_names.add(name_key)
            deduped.append(item)
        candidates = deduped
        # 多取一些，给「补完信息后被时长/年份/评分条件筛掉」留余量。
        # 翻页不在这里做 —— find() 传进来的 count 已经含了 offset，切片统一放最后一步。
        # 这里的上限是关键：富化是每部一次网络请求，取太多会直接把响应时间拖到十几秒，
        # 所以只留 10 条余量，而不是按倍数放大。
        take = min(count + 10, _CANDIDATE_POOL)
        shortlist = candidates[:take]

        picked: list[GameInfo] = [self._row_to_info(item) for item in shortlist]

        # 补 VNDB（时长/标签/评分/中文标题）+ 月幕（汉化）。
        # 这里限并发：一次十路并发去打 VNDB/月幕容易被限流，反而补不齐。
        enrich_pool = picked[:take]
        semaphore = asyncio.Semaphore(4)

        async def _guarded(game: GameInfo) -> None:
            async with semaphore:
                await self._enrich_from_title(game)

        await asyncio.gather(*[_guarded(g) for g in enrich_pool], return_exceptions=True)

        # 富化之后才能按 VNDB 分 / 时长 / 发售日排 —— 在这之前它们都还是空的。
        # 早先只在候选阶段按「票数 or 中央值」排一次，于是 sort=vndb / length / released
        # 在批评空间这条路上被**静默忽略**，结果和 sort=egs 一模一样。
        if sort == "vndb":
            picked.sort(key=lambda g: g.vndb_rating or 0.0, reverse=True)
        elif sort == "length":
            picked.sort(key=lambda g: g.length_minutes or 10**9)
        elif sort == "released":
            picked.sort(key=lambda g: g.released or "", reverse=True)

        # 时长/年份/评分这些只有在补充信息之后才有，所以过滤放在补充之后做。
        # 时长的处理原则：**已知时长的先按条件筛**；如果筛完一个不剩，
        # 说明条件太苛刻，就把「时长未知」的那些放回来（总比什么都不给强）。
        if length_max_minutes > 0 or length_min_minutes > 0:
            known: list[GameInfo] = []
            unknown: list[GameInfo] = []
            for game in picked:
                minutes = game.length_minutes
                if not minutes:
                    unknown.append(game)
                    continue
                if length_max_minutes > 0 and minutes > length_max_minutes:
                    continue
                if length_min_minutes > 0 and minutes < length_min_minutes:
                    continue
                known.append(game)
            picked = known or unknown
        if min_rating > 0 or max_rating > 0:
            rating_lo = min_rating if min_rating > 0 else 0
            rating_hi = max_rating if max_rating > 0 else 100
            # 没评分的（票数太少，VNDB 干脆不给分）在上界筛选里直接出局，
            # 但纯下限筛选时留着也无意义 —— 一并排除，避免「75 分以上」里混进无分作品
            picked = [
                g
                for g in picked
                if g.vndb_rating is not None and rating_lo <= g.vndb_rating <= rating_hi
            ]
        if year_from > 0:
            picked = [g for g in picked if g.released and g.released >= f"{year_from}-01-01"]
        return picked

    async def _find_by_vndb(
        self,
        *,
        keyword: str = "",
        min_egs: float = 0.0,
        max_egs: float = 0.0,
        tags: list[str],
        min_rating: float,
        max_rating: float = 0.0,
        min_votes: int,
        year_from: int,
        length_max_minutes: int,
        length_min_minutes: int,
        sort: str,
        count: int,
        allow_ids: set[str] | None = None,
    ) -> list[GameInfo]:
        """VNDB 优先：结构化条件交给 VNDB，服务端粗筛后本地精筛。

        评分是**区间**条件（``min_rating`` / ``max_rating`` 一起下发），
        这样才筛得动「75~80 分那一档」。注意筛选与排序是两件事：
        默认按人气（``votecount``）排，只给下限也能拿到该门槛之上**最有人气**
        的那几部，而不是永远停在榜首。
        """
        if self.vndb is None:
            return []

        conditions: list[Any] = []
        if keyword:
            conditions.append(["search", "=", keyword])
        if min_rating > 0:
            conditions.append(["rating", ">=", min_rating])
        if max_rating > 0:
            conditions.append(["rating", "<=", max_rating])
        if min_votes > 0:
            conditions.append(["votecount", ">=", min_votes])
        if year_from > 0:
            conditions.append(["released", ">=", f"{year_from}-01-01"])
        if length_max_minutes > 0 or length_min_minutes > 0:
            buckets = [
                b
                for b, (lo, hi) in LENGTH_BUCKETS.items()
                if (length_max_minutes <= 0 or lo < length_max_minutes)
                and (length_min_minutes <= 0 or hi > length_min_minutes)
            ]
            if buckets:
                conditions.append(
                    ["length", "=", buckets[0]]
                    if len(buckets) == 1
                    else ["or", *[["length", "=", b] for b in buckets]]
                )

        tag_ids: list[str] = []
        failed_tags: list[str] = []
        for tag in tags:
            tid = await self.vndb.tag_id(tag)
            if tid:
                tag_ids.append(tid)
            else:
                failed_tags.append(tag)
        if failed_tags:
            self.last_warnings.append(
                f"标签「{'、'.join(failed_tags)}」在 VNDB 里查不到，已忽略。{TAG_HINT}"
            )
        if tag_ids:
            conditions.append(
                ["tag", "=", tag_ids[0]]
                if len(tag_ids) == 1
                else ["and", *[["tag", "=", t] for t in tag_ids]]
            )
        elif tags:
            # 用户明确要了标签，却一个都没解析出来 —— **绝不能就把条件丢掉**：
            # 那等于返回未筛选的结果，还让用户以为筛过了（实测踩过这个坑）。
            # 宁可没结果，也不能给错结果。
            return []

        filters: list[Any] = ["and", *conditions] if conditions else []
        sort_field, reverse = {
            "vndb": ("rating", True),
            "votes": ("votecount", True),
            "released": ("released", True),
            "title": ("title", False),
        }.get(sort, ("rating", True))
        # VNDB 单页硬上限 100（传 101 直接 400），候选池按 count 的 3 倍取，
        # 给「本地精筛会淘汰一部分」留余量。count 里已经含了 offset 的份额。
        # 以前这里写的是 limit=max(pool_size, count*4)，但客户端把上限压到了 25，
        # 于是 pool_size=60 名存实亡 —— 候选池永远只有 25 条，这就是「只能搜到头部」的直接原因。
        try:
            resp = await self.vndb.query_page(
                filters=filters,
                sort=sort_field,
                reverse=reverse,
                limit=min(100, max(count * 3, 30)),
            )
        except Exception:  # noqa: BLE001
            return []
        candidates = resp["results"]

        picked: list[GameInfo] = []
        for raw in candidates:
            minutes = int(raw.get("length_minutes") or 0)
            if length_max_minutes > 0 and minutes and minutes > length_max_minutes:
                continue
            if length_min_minutes > 0 and minutes and minutes < length_min_minutes:
                continue
            picked.append(self._build_info(raw, None))
            if len(picked) >= min(count + 10, _CANDIDATE_POOL):
                break

        # 挂批评空间分数（本地索引，零开销），并补汉化状态
        for item in picked:
            self._attach_egs_sync(item)

        # EGS「类型」过滤（`/gal 类型 SLG` 再叠时长/标签时走的是这条路）。
        # 必须放在这里：类型靠 attlist 拿到的是 EGS id 集合，而 egs_id 是上一步
        # 刚从本地索引挂上去的。以前 `_find_by_vndb` 根本没有这个参数，
        # 于是「类型 + 时长」这类组合会把类型条件**静默丢掉**。
        if allow_ids is not None:
            picked = [g for g in picked if g.egs_id and g.egs_id in allow_ids]
        # 富化的条数就是响应时间 —— 每部一次月幕请求，翻页时别跟着 count 线性涨，
        # 只多留 10 条余量给「时长/评分条件筛掉一部分」。
        enrich_n = min(count + 10, _CANDIDATE_POOL)
        # 限并发：无上限地并发打月幕会被限流，反而补不齐
        enrich_sem = asyncio.Semaphore(4)

        async def _enrich_guarded(game: GameInfo) -> None:
            async with enrich_sem:
                await self._enrich(game, want_chinese=True)

        await asyncio.gather(
            *[_enrich_guarded(item) for item in picked[:enrich_n]],
            return_exceptions=True,
        )
        # 批评空间分数是本地挂上去的，所以在这里做「按分数筛」。
        # 注意必须要求 egs_median 真的存在：索引里没有这部作品时它是 None，
        # 用 `or 0` 兜底会让「中央值 ≤ 80」这种条件把无分作品全放进来。
        if min_egs > 0 or max_egs > 0:
            egs_lo = min_egs if min_egs > 0 else 0
            egs_hi = max_egs if max_egs > 0 else 100
            picked = [
                g
                for g in picked
                if g.egs_median is not None and egs_lo <= g.egs_median <= egs_hi
            ]
            # 只有用户明确要求按批评空间分数排时才按中央值重排。
            # 默认是按人气，别在这里被顶掉 —— 这里是「筛完」，不是「排序」。
            if sort in ("egs", "score"):
                picked.sort(key=lambda g: g.egs_median or 0, reverse=True)
        if sort == "length":
            picked.sort(key=lambda g: g.length_minutes or 10**9)
        return picked

    def _attach_egs_sync(self, info: GameInfo) -> None:
        """只用本地索引挂 EGS 分数（不抓详情页）——列表场景不值得为每部都抓一次。"""
        if info.egs_id or not self.egs or not self.egs.index.items:
            return
        hit = self.egs.index.match(
            info.title_ja,
            info.title_zh,
            info.title_main,
            info.title_en,
            *info.aliases[:4],
            threshold=self.match_threshold,
        )
        if not hit:
            return
        info.egs_id = str(hit.get("id") or "")
        info.egs_median = int(hit.get("median") or 0) or None
        info.egs_mean = float(hit.get("mean") or 0.0) or None
        info.egs_votes = int(hit.get("votes") or 0)
        if "egs" not in info.sources:
            info.sources.append("egs")

    # -- 内部 --------------------------------------------------------------

    async def _enrich(self, game: GameInfo, *, want_chinese: bool) -> None:
        """给推荐结果补月幕的汉化状态与 EGS 评分（尽力而为，失败就算了）。"""
        if want_chinese and self.ymgal and game.have_chinese is None:
            for name in [game.title_zh, game.title_ja, game.title_main]:
                if not name:
                    continue
                try:
                    hit = await self.ymgal.search_accurate(name, similarity=80)
                except Exception:  # noqa: BLE001
                    hit = None
                if hit:
                    game.have_chinese = bool(hit.get("have_chinese"))
                    game.restricted = hit.get("restricted")
                    if not game.title_zh:
                        game.title_zh = hit.get("title_zh") or ""
                    if not game.cover:
                        game.cover = hit.get("cover") or ""
                    break
        if self.egs and self.egs.index.items and not game.egs_id:
            hit = self.egs.index.match(
                game.title_ja, game.title_zh, game.title_main, game.title_en, threshold=75
            )
            if hit:
                game.egs_id = str(hit.get("id") or "")
                game.egs_median = int(hit.get("median") or 0) or None
                game.egs_mean = float(hit.get("mean") or 0.0) or None
                game.egs_votes = int(hit.get("votes") or 0)

    async def _enrich_from_title(self, game: GameInfo) -> None:
        """给「只有日文名 + 批评空间分数」的候选补上 VNDB 与月幕的信息。

        EGS 优先那条路径的候选来自本地索引，除了名字和分数什么都没有；
        用户要的时长、标签、有没有汉化得去别的源补。

        跨源匹配的真实难点是**文字系统不一样**：EGS 用片假名「ランス10」，
        VNDB 的标题是罗马音「Rance X -Kessen-」，字符串相似度直接归零 ——
        所以这里让**月幕先当桥**（它吃日文原名，还能给出官方中文名），
        再拿月幕的原名/中文名去 VNDB 搜；VNDB 只返回单条结果时也直接采信。
        匹配不上就保持原样 —— 补错信息比缺信息更糟。
        """
        title = game.title_ja
        if not title:
            return

        queries: list[str] = [title]
        cleaned = strip_version_suffix(title)
        if cleaned and cleaned != title:
            queries.append(cleaned)  # 去掉「(PS3)」「全年齢版」这类后缀再试一次

        # ① 月幕当桥
        if self.ymgal and game.have_chinese is None:
            hit = None
            for q in queries:
                try:
                    hit = await self.ymgal.search_accurate(q)
                except Exception:  # noqa: BLE001
                    hit = None
                if hit:
                    break
            if hit:
                game.have_chinese = bool(hit.get("have_chinese"))
                if hit.get("restricted") is not None:
                    game.restricted = bool(hit.get("restricted"))
                if hit.get("title_zh"):
                    game.title_zh = game.title_zh or str(hit["title_zh"])
                if hit.get("cover"):
                    game.cover = game.cover or str(hit["cover"])
                if "ymgal" not in game.sources:
                    game.sources.append("ymgal")
                for cand in (hit.get("title"), hit.get("title_zh")):
                    if cand and str(cand) not in queries:
                        queries.append(str(cand))

        # ② VNDB：按候选名依次试，命中即止
        if self.vndb and not game.vndb_id:
            for q in queries:
                try:
                    hits = await self.vndb.search(q, limit=5)
                except Exception:  # noqa: BLE001
                    hits = []
                # 和 lookup() 共用挑选逻辑：同样按「相似度 + 票数」裁决。
                # 单候选直接采信（VNDB 自己处理了跨语言匹配），多候选才要求名字够像。
                top = self._pick_vndb_hit(q, None, hits, threshold=70)
                if top is None:
                    continue
                merged = self._build_info(top, None)
                game.vndb_id = merged.vndb_id
                game.title_main = game.title_main or merged.title_main
                game.title_zh = game.title_zh or merged.title_zh
                game.title_en = game.title_en or merged.title_en
                game.length_minutes = merged.length_minutes or game.length_minutes
                game.vndb_rating = merged.vndb_rating
                game.vndb_votes = merged.vndb_votes
                game.tags = merged.tags or game.tags
                game.platforms = merged.platforms or game.platforms
                game.released = game.released or merged.released
                game.cover = game.cover or merged.cover
                game.developer = game.developer or merged.developer
                game.aliases = list(dict.fromkeys(game.aliases + merged.aliases))
                if "vndb" not in game.sources:
                    game.sources.append("vndb")
                break
        await self._enrich(game, want_chinese=True)

    @classmethod
    def _pick_vndb_hit(
        cls,
        keyword: str,
        ymgal_raw: dict[str, Any] | None,
        hits: list[dict[str, Any]],
        *,
        threshold: int = 0,
    ) -> dict[str, Any] | None:
        """从 VNDB 的搜索结果里挑出真正对得上的那一条。

        VNDB 的 ``searchrank`` 是**纯文本相关性**排序，不看作品知名度 —— 于是
        「ATRI」的第一名是只有 4 票的 ``a(t)rium``，而 3633 票的正主
        ``ATRI -My Dear Moments-`` 排第三。照搬第一条的后果不是「少信息」而是
        **挂错作品的信息**：例如 ATRI 会查出「时长未知 / 暂无评分 / 没有角色」。

        做法：先算每个候选跟「查询词 + 月幕给出的中日文名与别名」的最高相似度，
        再把与最高分接近（差 5 分以内）的候选放在一起，**取票数最高的**。
        票数是这里唯一能区分「同名噪音」和「正主」的信号。

        Args:
            threshold: 大于 0 时，多候选情况下相似度低于此值直接判为没匹配上。
                单候选时不做这个要求 —— 外号检索经常整条命中（「罚抄」→ Rewrite
                相似度是 0，但月幕已经确定了就是它）。
        """
        if not hits:
            return None

        name_pool = [str(keyword or "").strip()]
        if ymgal_raw:
            for key in ("title", "title_zh"):
                value = str(ymgal_raw.get(key) or "").strip()
                if value:
                    name_pool.append(value)
            name_pool.extend(str(alias) for alias in (ymgal_raw.get("aliases") or []) if alias)
        name_pool = [name for name in name_pool if name]
        if not name_pool:
            return hits[0]

        scored: list[tuple[int, dict[str, Any]]] = []
        for hit in hits:
            names = [
                str(hit.get("title") or ""),
                str(hit.get("title_ja") or ""),
                str(hit.get("title_zh") or ""),
                str(hit.get("title_en") or ""),
                *(str(alias) for alias in (hit.get("aliases") or [])),
            ]
            names = [name for name in names if name]
            best = max((similarity(query, name) for query in name_pool for name in names), default=0)
            scored.append((best, hit))

        top_similarity = max(score for score, _ in scored)
        if len(hits) > 1 and threshold > 0 and top_similarity < threshold:
            return None
        contenders = [hit for score, hit in scored if score >= top_similarity - 5]
        contenders.sort(key=lambda item: int(item.get("votecount") or 0), reverse=True)
        return contenders[0]

    @staticmethod
    def _base_title(title: str) -> str:
        """去掉「(PS3)」「(全年齢版)」这类载体/版本后缀，用于判重。

        正则统一放在 gl_http：索引侧（gl_egs）建 lookup 时用的是同一个函数，
        两边各写一份的话判重键会不一致，去重直接失效。
        """
        return strip_version_suffix(title)

    @classmethod
    def _dedupe_by_work(cls, games: list[GameInfo]) -> list[GameInfo]:
        """同一部作品的不同版本会被当成不同条目 —— 这里合并掉。

        两种重复来源：

        - **批评空间索引**里 PS3 / PSV / NS / 全年齢版 各占一条，名字只差一个括号后缀；
        - **补完 VNDB 信息之后**，这些版本又会各自匹配到同一部 VNDB 作品，
          于是卡片上并排出现三个「CLANNAD」。

        判重键优先用 VNDB id（补到信息后最可靠），没有就退回「剥掉版本后缀的标题」。
        保留先出现的那条 —— 调用方已经按分数排好序了。
        """
        seen: set[str] = set()
        out: list[GameInfo] = []
        for game in games:
            key = game.vndb_id or normalize_name(
                cls._base_title(game.title_ja) or game.display_title()
            )
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(game)
        return out

    @staticmethod
    def _build_info(vndb_raw: dict[str, Any] | None, ymgal_raw: dict[str, Any] | None) -> GameInfo:
        info = GameInfo()
        if vndb_raw:
            info.vndb_id = str(vndb_raw.get("id") or "")
            info.title_main = str(vndb_raw.get("title") or "")
            info.title_ja = str(vndb_raw.get("title_ja") or "")
            info.title_zh = str(vndb_raw.get("title_zh") or "")
            info.title_en = str(vndb_raw.get("title_en") or "")
            info.released = str(vndb_raw.get("released") or "")
            info.length_minutes = int(vndb_raw.get("length_minutes") or 0)
            info.cover = str(vndb_raw.get("cover") or "")
            info.tags = list(vndb_raw.get("tags") or [])
            info.platforms = [str(p) for p in (vndb_raw.get("platforms") or [])]
            info.vndb_rating = vndb_raw.get("rating")
            info.vndb_votes = int(vndb_raw.get("votecount") or 0)
            info.aliases = list(vndb_raw.get("aliases") or [])
            info.intro = str(vndb_raw.get("description") or "")
            info.sources.append("vndb")
        if ymgal_raw:
            info.ymgal_id = str(ymgal_raw.get("id") or "")
            info.title_zh = info.title_zh or str(ymgal_raw.get("title_zh") or "")
            info.title_ja = info.title_ja or str(ymgal_raw.get("title") or "")
            info.released = info.released or str(ymgal_raw.get("released") or "")
            info.cover = info.cover or str(ymgal_raw.get("cover") or "")
            info.have_chinese = ymgal_raw.get("have_chinese")
            if ymgal_raw.get("restricted") is not None:
                info.restricted = bool(ymgal_raw.get("restricted"))
            # 简介以**中文源优先**：麦麦说中文，月幕的中文简介比 VNDB 的英文描述更合适，
            # 只有月幕没写简介时才退回 VNDB 的英文
            info.intro = str(ymgal_raw.get("intro") or "") or info.intro
            info.characters = list(ymgal_raw.get("characters") or [])
            info.staff = list(ymgal_raw.get("staff") or [])
            info.releases = list(ymgal_raw.get("releases") or [])
            for alias in ymgal_raw.get("aliases") or []:
                if alias and alias not in info.aliases:
                    info.aliases.append(str(alias))
            info.sources.append("ymgal")
        if not info.developer and vndb_raw:
            devs = [str(d) for d in (vndb_raw.get("developers") or [])]
            info.developer = " / ".join(devs)
        return info

    @staticmethod
    def _merge_into_list(bucket: list[GameInfo], raw: dict[str, Any]) -> None:
        """搜索结果的跨源去重：名称足够像就当成同一部，合并来源。"""
        names = [
            str(raw.get("title_zh") or ""),
            str(raw.get("title") or ""),
            str(raw.get("title_ja") or ""),
            str(raw.get("title_en") or ""),
        ]
        names = [n for n in names if n]
        for existing in bucket:
            existing_names = [
                existing.title_zh,
                existing.title_ja,
                existing.title_main,
                existing.title_en,
            ]
            for a in names:
                for b in [n for n in existing_names if n]:
                    if normalize_name(a) == normalize_name(b) or similarity(a, b) >= 92:
                        # 同一部作品：把另一个源的字段补进去
                        if raw.get("source") == "vndb":
                            existing.vndb_id = str(raw.get("id") or "")
                            existing.length_minutes = int(raw.get("length_minutes") or 0)
                            existing.vndb_rating = raw.get("rating")
                            existing.vndb_votes = int(raw.get("votecount") or 0)
                            existing.title_ja = existing.title_ja or str(raw.get("title_ja") or "")
                        else:
                            existing.ymgal_id = str(raw.get("id") or "")
                            existing.have_chinese = raw.get("have_chinese")
                            existing.restricted = raw.get("restricted")
                            existing.title_zh = existing.title_zh or str(raw.get("title_zh") or "")
                        for src in [raw.get("source")]:
                            if src and src not in existing.sources:
                                existing.sources.append(str(src))
                        return
        bucket.append(
            GameInfo(
                title_zh=str(raw.get("title_zh") or ""),
                title_ja=str(raw.get("title_ja") or "") or (str(raw.get("title") or "") if raw.get("source") == "ymgal" else ""),
                title_main=str(raw.get("title") or ""),
                title_en=str(raw.get("title_en") or ""),
                released=str(raw.get("released") or ""),
                cover=str(raw.get("cover") or ""),
                developer=str(raw.get("developer") or ""),
                length_minutes=int(raw.get("length_minutes") or 0),
                have_chinese=raw.get("have_chinese"),
                restricted=raw.get("restricted"),
                vndb_id=str(raw.get("id") or "") if raw.get("source") == "vndb" else "",
                ymgal_id=str(raw.get("id") or "") if raw.get("source") == "ymgal" else "",
                vndb_rating=raw.get("rating"),
                vndb_votes=int(raw.get("votecount") or 0),
                sources=[str(raw.get("source") or "")],
            )
        )
