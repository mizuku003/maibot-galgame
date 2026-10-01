"""galgame领域大神 —— 批评空间（ErogameScape）客户端。

这块是三个源里最麻烦的，因为它**没有 API**，而且站内搜索接口对非浏览器请求
直接返回 200 + 空 body（反爬）。目前能走通的只有两条路：

1. **榜单页可翻页**：``toukei_median.php?offset=N&year=1900&count=<最低数据数>``
   每页 100 条，字段是「名称 / 品牌 / 中央値 / 平均値 / 標準偏差 / データ数」。
   → 用它翻出一个本地索引，解决「游戏名 → EGS id」这个最大的难题。
2. **详情页可直抓**：``game.php?game=<id>``，能拿到中央値、平均値、データ数、
   標準偏差、最高/最低点、giveup 率、積んでる率、得点分布、公式ジャンル、属性、タグ。

另外两个坑记在这：
- 页面是 **UTF-8**（这站很老，直觉会以为是 Shift_JIS，我一开始就栽了）；
- 未发售/无人评价的作品页面上**没有中央値**，解析要能接受"这作没人评"而不是报错。
"""

from __future__ import annotations

import asyncio
import gzip
import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from gl_http import (
    HttpClient,
    HttpError,
    normalize_name,
    similarity,
    similarity_of_normalized,
    strip_version_suffix,
)

BASE = "https://erogamescape.dyndns.org/~ap2/ero/toukei_kaiseki"
INDEX_VERSION = 1

logger = logging.getLogger(__name__)


def _flatten(raw_html: str) -> str:
    """HTML → 连续文本。够用，不引 bs4（少一个依赖少一份折腾）。"""
    text = re.sub(r"<script.*?</script>", " ", raw_html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t　]+", " ", text)


def _memo_text(raw: str) -> str:
    """长文感想正文 → 纯文本。

    和 :func:`_flatten` 的区别是**保留 ``<br />`` 的换行** ——
    长文动辄几千字，挤成一行交给模型很难读，分段的批评也看不出结构。
    """
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _unquote_js(value: str) -> str:
    """用户名在链接里是百分号编码的（中文 ID 很常见），还原成人能看的字。"""
    from urllib.parse import unquote

    try:
        return unquote(value)
    except Exception:  # noqa: BLE001
        return value


class EgsIndex:
    """榜单索引：名称 → id / 中央値 / 平均値 / 数据数。落一份 JSON 在插件数据目录。"""

    def __init__(self, path: Path, *, seed_path: Path | None = None) -> None:
        self.path = path
        # 随插件发布的内置索引（gzip）。用户第一次加载时铺过去，
        # 这样就不必自己等一次全量构建（那是上万次请求、好几分钟的事）。
        self.seed_path = seed_path
        self.items: list[dict[str, Any]] = []
        self.built_at: float = 0.0
        self.min_votes: int = 0
        # 下面这些结构都在 _build_lookup 里一次性建好，供 match() 快速匹配。
        # 索引上万条时「每次查询都把全表归一化一遍再算 difflib」是撑不住的。
        self._normalized: list[tuple[str, dict[str, Any]]] = []
        self._charsets: list[frozenset[str]] = []
        self._norm_by_id: dict[str, str] = {}
        self._base_norm_by_id: dict[str, str] = {}
        self._exact: dict[str, dict[str, Any]] = {}

    # -- 持久化 ------------------------------------------------------------

    def load(self) -> bool:
        """从磁盘装索引；没有就先用**内置种子**铺一份。

        内置种子是随插件发布的完整索引（gzip 压缩过），所以用户装完插件
        立刻就能按分数检索，不用先等一次全量构建。
        """
        if not self.path.exists() and not self._install_seed():
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 索引坏了就当没有，重建即可
            return False
        if int(data.get("version") or 0) != INDEX_VERSION:
            return False
        self.items = list(data.get("items") or [])
        self.built_at = float(data.get("built_at") or 0.0)
        self.min_votes = int(data.get("min_votes") or 0)
        self._build_lookup()
        return bool(self.items)

    def _install_seed(self) -> bool:
        """把内置种子解压到数据目录；失败就当没有（退回联网构建）。"""
        if self.seed_path is None or not self.seed_path.exists():
            return False
        try:
            with gzip.open(self.seed_path, "rb") as handle:
                payload = json.loads(handle.read().decode("utf-8"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[EGS] 内置索引不可用，将退回联网构建：%s", exc)
            return False
        logger.info(
            "[EGS] 已铺好内置索引：%s 条（首次加载不用等全量构建）",
            len(payload.get("items") or []),
        )
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "built_at": self.built_at,
            "min_votes": self.min_votes,
            "count": len(self.items),
            "items": self.items,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def merge(self, incoming: list[dict[str, Any]]) -> dict[str, int]:
        """把一批新抓的条目并进索引：同 id 覆盖（分数会变），新 id 追加。

        这是增量更新的落地动作 —— 只动抓回来的那一批，没抓到的原样留着。
        返回 ``{"added": 新增, "updated": 更新}``。
        """
        by_id: dict[str, dict[str, Any]] = {
            str(it.get("id") or ""): it for it in self.items if it.get("id")
        }
        added = updated = 0
        for item in incoming:
            gid = str(item.get("id") or "")
            if not gid:
                continue
            old = by_id.get(gid)
            if old is None:
                by_id[gid] = dict(item)
                added += 1
            else:
                old.update(item)
                updated += 1
        if added or updated:
            self.items = list(by_id.values())
            self.built_at = time.time()
            self._build_lookup()
            self.save()
        return {"added": added, "updated": updated}

    def age_days(self) -> float:
        if not self.built_at:
            return 9999.0
        return (time.time() - self.built_at) / 86400.0

    def _build_lookup(self) -> None:
        """一次性建好匹配用的辅助结构。

        - ``_normalized`` / ``_charsets``：归一化名与它的字符集，模糊匹配用；
        - ``_exact``：归一化名 → 条目，精确命中走它，O(1)；
        - ``_norm_by_id`` / ``_base_norm_by_id``：给 gl_merge 复用，
          省得它在一万多个候选上再归一化一遍。
        """
        self._normalized = []
        self._charsets = []
        self._norm_by_id = {}
        self._base_norm_by_id = {}
        self._exact = {}
        for item in self.items:
            raw_name = str(item.get("name") or "")
            norm = normalize_name(raw_name)
            self._normalized.append((norm, item))
            self._charsets.append(frozenset(norm))
            item_id = str(item.get("id") or "")
            if item_id:
                self._norm_by_id[item_id] = norm
                self._base_norm_by_id[item_id] = normalize_name(strip_version_suffix(raw_name))
            if not norm:
                continue
            # 同名多版本（PS3 / 全年齢版）时保留票数高的，更可能是本体
            old = self._exact.get(norm)
            if old is None or int(item.get("votes") or 0) > int(old.get("votes") or 0):
                self._exact[norm] = item

    def norm_of(self, item: dict[str, Any]) -> str:
        """条目名的归一化形式（建索引时已算好，这里直接取）。"""
        return self._norm_by_id.get(str(item.get("id") or ""), "")

    def base_norm_of(self, item: dict[str, Any]) -> str:
        """条目名剥掉载体/版本后缀后的归一化形式（建索引时已算好）。"""
        return self._base_norm_by_id.get(str(item.get("id") or ""), "")

    # -- 匹配 --------------------------------------------------------------

    def match(self, *names: str, threshold: int = 72) -> dict[str, Any] | None:
        """拿一堆候选名去匹配（中文名、日文名、别名轮流试），返回最像的那条。

        这里刻意用『取最高分』而不是『第一个过线的』——因为调用方给的名字顺序
        不代表可信度顺序（中文名可能是错译，日文名才是正主）。

        索引上万条，所以匹配分两步走，不能让每次查询都全表跑一遍 difflib：

        1. **精确命中**走 ``_exact`` 字典，O(1)。日文原名（EGS 自己的名字、
           VNDB 给的 ``title_ja``）基本都在这一步就结束了；
        2. 没命中才做模糊匹配，且先用**字符集重叠**预筛 —— 相似度要过阈值，
           两边至少得共享三分之一的字符；这一步把绝大多数无关条目挡在 difflib 之外。
        """
        if not self._normalized:
            return None
        best: tuple[int, int, dict[str, Any]] | None = None
        for name in names:
            if not name:
                continue
            query_norm = normalize_name(name)
            if not query_norm:
                continue
            # ① 精确命中
            exact = self._exact.get(query_norm)
            if exact is not None:
                votes = int(exact.get("votes") or 0)
                if best is None or (100, votes) > (best[0], best[1]):
                    best = (100, votes, exact)
                continue
            # ② 模糊匹配：字符集预筛 → 算相似度 → 版本后缀扣分
            #
            # 但**已经拿到满分命中时直接跳过**：相似度 100 只在两边归一化后完全相等时
            # 才会给出（也就是上面那一支），模糊匹配最高只能到 99，不可能翻盘。
            # 这一步让「日文原名精确命中」这个最常见的情形从
            # O(名字数 × 全表) 降到 O(名字数) —— 索引一万九千条时这就是 200ms 和 0ms 的区别。
            if best is not None and best[0] >= 100:
                continue
            query_chars = frozenset(query_norm)
            query_has_paren = ("(" in name) or ("（" in name)
            for (item_norm, item), item_chars in zip(self._normalized, self._charsets):
                if len(query_chars & item_chars) * 3 < len(query_chars):
                    continue
                # 传 min_score 让它在算 difflib 之前先用长度挡一道 —— 这是热点
                score = similarity_of_normalized(query_norm, item_norm, min_score=threshold)
                if score < threshold:
                    continue
                item_name = str(item.get("name") or "")
                # 查询没带载体/版本后缀时，给带后缀的条目（PS3 版、全年齢版…）扣一点分，
                # 否则「WHITE ALBUM2」会匹到「WHITE ALBUM2 幸せの向こう側(PS3)」而不是本体
                if not query_has_paren and ("(" in item_name or "（" in item_name):
                    score -= 3
                if score < threshold:
                    continue
                votes = int(item.get("votes") or 0)
                # 同分时投票数多的优先（更可能是本体而非移植版）
                if best is None or (score, votes) > (best[0], best[1]):
                    best = (score, votes, item)
        return best[2] if best else None

    def median_range(self) -> tuple[int, int]:
        """索引实际覆盖的中央值区间。

        这个数字很关键：EGS 榜单页是按中央值**降序**给的，所以「只翻前 N 页」
        的后果是索引**天生只覆盖高分**。不把这个范围说出来，用户问
        「30 分左右的作品」时只会看到一句「没找到」，根本猜不到是索引里
        压根没有那几档的数据，而不是自己的条件写错了。
        """
        medians = [int(item.get("median") or 0) for item in self.items if item.get("median")]
        if not medians:
            return (0, 0)
        return (min(medians), max(medians))

    def stats(self) -> dict[str, Any]:
        low, high = self.median_range()
        return {
            "count": len(self.items),
            "age_days": round(self.age_days(), 1),
            "min_votes": self.min_votes,
            "median_low": low,
            "median_high": high,
        }


class EgsClient:
    """批评空间：建索引 + 抓详情。"""

    def __init__(
        self,
        http: HttpClient,
        index_path: Path,
        *,
        min_votes: int = 5,
        max_pages: int = 120,
        delay_seconds: float = 1.2,
        page_retries: int = 3,
        max_consecutive_failures: int = 5,
        seed_path: Path | None = None,
    ) -> None:
        self._http = http
        self.index = EgsIndex(index_path, seed_path=seed_path)
        self.min_votes = min_votes
        self.max_pages = max_pages
        self.delay = delay_seconds
        # 单页请求失败时的重试次数，以及「连续多少页失败就认定站点/代理挂了」
        self.page_retries = max(1, page_retries)
        self.max_consecutive_failures = max(1, max_consecutive_failures)
        self._building = False
        # 最近一次构建的结果，供 /gal status 与日志说明「到底翻到哪、为什么停」
        self.last_build: dict[str, Any] = {}

    @property
    def building(self) -> bool:
        return self._building

    # -- 索引构建 ----------------------------------------------------------

    async def _fetch_ranking_page(self, offset: int) -> str | None:
        """取一页榜单。网络失败时按退避重试；最终仍失败返回 None。

        返回 None 表示「这一页没拿到」，**不等于**「榜单到头了」。
        这两件事以前被合并成同一个 ``break`` —— 后果就是直连抖动一次，
        整个索引被静默截断在第 300 条，而且没有任何日志。
        """
        url = (
            f"{BASE}/toukei_median.php?offset={offset}"
            f"&year=1900&count={self.min_votes}"
        )
        last_error: Exception | None = None
        for attempt in range(self.page_retries):
            try:
                body = await self._http.request(
                    "GET",
                    url,
                    source="egs",
                    expect_json=False,
                    cache_ttl=0.0,
                    headers={"Referer": f"{BASE}/"},
                )
                return str(body)
            except HttpError as exc:
                last_error = exc
                if attempt < self.page_retries - 1:
                    # 退避重试：EGS 是个人站，别打太急
                    await asyncio.sleep(self.delay * (attempt + 2))
        logger.warning(
            "[EGS] offset=%s 重试 %s 次仍失败，跳过该页：%s",
            offset,
            self.page_retries,
            last_error,
        )
        return None

    async def build_index(self, *, progress=None) -> int:
        """翻完榜单页，重建本地索引。返回收录条数。

        每页 100 条，翻页间隔默认 1.2 秒 —— 这是个人站，别当 CDN 使。

        两个必须分清的情况（以前混成一个 ``break``，正是索引只有 300 条的真凶）：

        - **榜单到头了**：页面正常取回，但里面一条 ``game.php`` 链接都没有。
          这是正常终止。
        - **这一页没取回来**：超时 / 代理抽风。**绝不能**当成到头了 ——
          单页会先重试，仍失败则跳过该页继续翻，只有**连续**失败到阈值才中止，
          并把原因写进日志与 :attr:`last_build`。
        """
        if self._building:
            return len(self.index.items)
        self._building = True
        items: list[dict[str, Any]] = []
        pages_ok = 0
        failed_offsets: list[int] = []
        consecutive_failures = 0
        stop_reason = f"翻满 max_pages={self.max_pages} 页"
        started_at = time.monotonic()
        try:
            for page in range(self.max_pages):
                offset = page * 100
                body = await self._fetch_ranking_page(offset)
                if body is None:
                    failed_offsets.append(offset)
                    consecutive_failures += 1
                    if consecutive_failures >= self.max_consecutive_failures:
                        stop_reason = (
                            f"连续 {consecutive_failures} 页请求失败，已中止"
                            f"（多半是代理/网络问题，检查「批评空间专用代理」配置）"
                        )
                        break
                    continue
                rows = self._parse_ranking_rows(body)
                if not rows:
                    stop_reason = f"翻到 offset={offset} 榜单已无数据（正常到底）"
                    break
                consecutive_failures = 0
                items.extend(rows)
                pages_ok += 1
                if progress is not None:
                    progress(len(items), page + 1)
                await asyncio.sleep(self.delay)
        finally:
            self._building = False

        if items:
            # 同一部作品在榜单里可能重复出现（不同版本），按 id 去重保留数据数高的
            dedup: dict[str, dict[str, Any]] = {}
            for it in items:
                old = dedup.get(it["id"])
                if old is None or int(it.get("votes") or 0) > int(old.get("votes") or 0):
                    dedup[it["id"]] = it
            self.index.items = sorted(
                dedup.values(), key=lambda x: int(x.get("votes") or 0), reverse=True
            )
            self.index.min_votes = self.min_votes
            self.index.built_at = time.time()
            self.index._build_lookup()  # noqa: SLF001 —— 同类内部，直接调
            self.index.save()

        self.last_build = {
            "count": len(self.index.items),
            "pages_ok": pages_ok,
            "failed_pages": len(failed_offsets),
            "failed_offsets": failed_offsets[:20],
            "stop_reason": stop_reason,
            "duration": round(time.monotonic() - started_at, 1),
        }
        logger.info(
            "[EGS] 索引构建结束：%s 条 / 成功 %s 页 / 失败 %s 页 / 耗时 %s 秒；%s",
            self.last_build["count"],
            pages_ok,
            len(failed_offsets),
            self.last_build["duration"],
            stop_reason,
        )
        return len(self.index.items)

    # -- 增量更新与二分定位 ------------------------------------------------

    async def update_recent(self, *, pages: int = 30) -> dict[str, Any]:
        """**增量更新**：只重抓榜单前面若干页，合并进已有索引。

        榜单是按中央值**降序**的，所以前面几页就是高分作品。新作只要口碑不错，
        很快会出现在这一段；已有作品的分数变化也主要发生在这里。
        深处分段（低分、冷门）变化慢得多，不值得每次重抓 —— 那是全量重建才干的事。

        所以「第一次慢、之后只补新增」是成立的：增量只抓前面几十页，
        相对全量的整份榜单（上百页）只是零头。
        """
        if self._building:
            return {"added": 0, "updated": 0, "pages": 0, "skipped": "已有构建在进行"}
        self._building = True
        collected: list[dict[str, Any]] = []
        pages_ok = 0
        try:
            for page in range(max(1, pages)):
                body = await self._fetch_ranking_page(page * 100)
                if body is None:
                    # 一页失败不中断整次增量；但一页都没成功就说明站点/代理有问题
                    if pages_ok == 0 and page > 2:
                        break
                    continue
                rows = self._parse_ranking_rows(body)
                if not rows:
                    break
                collected.extend(rows)
                pages_ok += 1
                await asyncio.sleep(self.delay)
        finally:
            self._building = False

        stats = self.index.merge(collected) if collected else {"added": 0, "updated": 0}
        stats["pages"] = pages_ok
        logger.info(
            "[EGS] 增量更新结束：%s 页 / 新增 %s 条 / 更新 %s 条 / 索引共 %s 条",
            pages_ok,
            stats["added"],
            stats["updated"],
            len(self.index.items),
        )
        return stats

    async def _page_lowest_median(self, offset: int) -> int | None:
        """取某页的**最低**中央值；空页返回 None。二分定位全靠它。"""
        body = await self._fetch_ranking_page(offset)
        if body is None:
            return None
        medians = [
            int(r.get("median") or 0)
            for r in self._parse_ranking_rows(body)
            if r.get("median")
        ]
        return min(medians) if medians else None

    async def boundary_offset(self, threshold: int, *, max_offset: int = 40000) -> int:
        """二分找出**最小的**那个 offset，使该页的最低中央值 < ``threshold``。

        也就是「中央值高于 threshold 的区段」到此为止。
        榜单降序，本该能按分数随机访问 —— 没有升序参数也无所谓，**二分就够了**：
        找低分档（低分作品全在榜单末尾）只要十几次请求，而不是翻完整份榜单。
        """
        lo, hi = 0, max_offset
        while lo < hi:
            mid = ((lo + hi) // 2) // 100 * 100
            lowest = await self._page_lowest_median(mid)
            if lowest is None or lowest < threshold:
                hi = mid
            else:
                lo = mid + 100
            if lo >= hi:
                break
            await asyncio.sleep(self.delay * 0.5)   # 二分请求密，间隔减半
        return lo

    async def scan_median_band(
        self, *, min_egs: int = 0, max_egs: int = 0, pages: int = 5
    ) -> list[dict[str, Any]]:
        """不靠本地索引，直接在榜单上取一段中央值区间。

        低分作品在榜单**末尾**、高分在**开头**，所以：

        - 只给 ``max_egs``（找烂作）→ 二分跳到区间起点，再往后翻；
        - 只给 ``min_egs``（找高分）→ 二分找到下界，往前取几页；
        - 都不给 → 从榜单开头翻。
        """
        if max_egs > 0:
            start = await self.boundary_offset(int(max_egs) + 1)
        elif min_egs > 0:
            start = max(0, await self.boundary_offset(int(min_egs)) - max(1, pages) * 100)
        else:
            start = 0

        out: list[dict[str, Any]] = []
        for index in range(max(1, pages)):
            body = await self._fetch_ranking_page(start + index * 100)
            if body is None:
                continue
            rows = self._parse_ranking_rows(body)
            if not rows:
                break
            for row in rows:
                median = int(row.get("median") or 0)
                if min_egs and median < int(min_egs):
                    continue
                if max_egs and median > int(max_egs):
                    continue
                out.append(row)
            await asyncio.sleep(self.delay)
        return out

    # -- 其它榜单视角 -------------------------------------------------------

    async def year_ranking(self, year: int, *, offset: int = 0) -> list[dict[str, Any]]:
        """某一年的中央值排行（``toukei_year_median.php``），每页 100 条、降序。

        **不能和全局榜单共用解析**：这个页面的表是五列
        （名称 / 品牌 / 中央値 / 標準偏差 / データ数），**没有「平均値」**；
        按六列去判断会把整页跳过 —— 这正是之前误判「按年份筛不可用」的原因。

        另外它的 ``offset`` 是可用的，所以年份内的分数档同样能翻到。
        """
        try:
            body = await self._http.request(
                "GET",
                f"{BASE}/toukei_year_median.php?year={int(year)}&offset={max(0, int(offset))}",
                source="egs",
                expect_json=False,
                cache_ttl=1800.0,
                headers={"Referer": f"{BASE}/"},
            )
        except HttpError:
            return []
        return self._parse_year_rows(str(body))

    @staticmethod
    def _parse_year_rows(raw_html: str) -> list[dict[str, Any]]:
        """解析**按年份**的排行页：五列，没有「平均値」。"""
        out: list[dict[str, Any]] = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw_html, flags=re.S):
            link = re.search(r"game\.php\?game=(\d+)", row)
            if not link:
                continue
            cells = [
                re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S)
            ]
            if len(cells) < 5:
                continue
            name = re.sub(r"\s*OHP\s*$", "", cells[0]).strip()
            try:
                out.append(
                    {
                        "id": link.group(1),
                        "name": name,
                        "brand": cells[1],
                        "median": int(float(cells[2])) if cells[2] else 0,
                        "sd": float(cells[3]) if cells[3] else 0.0,
                        "votes": int(float(cells[4])) if cells[4] else 0,
                    }
                )
            except (ValueError, IndexError):
                continue
        return out

    async def datacount_ranking(
        self, *, offset: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        """按「数据数」（票数）排行的榜单，每页 200 条。

        和按中央值排是**两个视角**：中央值告诉你「评价最高」，
        数据数告诉你「玩的人最多 / 最有名」—— 后者更适合找入坑作。
        """
        try:
            body = await self._http.request(
                "GET",
                f"{BASE}/toukei_datacount.php?offset={max(0, offset)}&count=5",
                source="egs",
                expect_json=False,
                cache_ttl=1800.0,
                headers={"Referer": f"{BASE}/"},
            )
        except HttpError:
            return []
        return self._parse_ranking_rows(str(body))[:limit]

    async def attlist_games(
        self, att_id: int, *, limit: int = 300
    ) -> list[dict[str, Any]]:
        """按 EGS 的「属性 / 类型」筛作品（RPG / SLG / ACT / STG / 3D …）。

        ``attlist.php?att[N]=on`` 是 EGS 独有的分类维度 —— VNDB 的标签体系里
        没有这一套。一次能返回上千部，非常划算。

        注意：这个列表**不带分数**（它只是作品清单），要分数还得按 id 去补。
        """
        try:
            body = await self._http.request(
                "GET",
                f"{BASE}/attlist.php?att[{int(att_id)}]=on",
                source="egs",
                expect_json=False,
                cache_ttl=3600.0,
                headers={"Referer": f"{BASE}/"},
            )
        except HttpError:
            return []
        rows = self._parse_attlist_rows(str(body))[:limit]
        for row in rows:
            row["att_id"] = int(att_id)
        return rows

    @staticmethod
    def _parse_attlist_rows(raw_html: str) -> list[dict[str, Any]]:
        """解析 attlist 的作品行（和榜单页的表结构不同，得单独解）。"""
        out: list[dict[str, Any]] = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw_html, flags=re.S):
            link = re.search(r"game\.php\?game=(\d+)", row)
            if not link:
                continue
            cells = [
                re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S)
            ]
            name = ""
            for cell in cells:
                if cell and len(cell) > 1 and not cell.isdigit():
                    name = re.sub(r"\s*OHP\s*$", "", cell).strip()
                    break
            if not name:
                continue
            out.append({"id": link.group(1), "name": name, "att_id": 0})
        return out

    @staticmethod
    def _parse_ranking_rows(raw_html: str) -> list[dict[str, Any]]:
        """解析榜单页的一行：名称 / 品牌 / 中央値 / 平均値 / 標準偏差 / データ数。"""
        out: list[dict[str, Any]] = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw_html, flags=re.S):
            if "game.php?game=" not in row:
                continue
            link = re.search(r"game\.php\?game=(\d+)", row)
            if not link:
                continue
            cells = [
                re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S)
            ]
            if len(cells) < 6:
                continue
            name = re.sub(r"\s*OHP\s*$", "", cells[0]).strip()
            try:
                out.append(
                    {
                        "id": link.group(1),
                        "name": name,
                        "brand": cells[1],
                        "median": int(float(cells[2])) if cells[2] else 0,
                        "mean": float(cells[3]) if cells[3] else 0.0,
                        "sd": float(cells[4]) if cells[4] else 0.0,
                        "votes": int(float(cells[5])) if cells[5] else 0,
                    }
                )
            except (ValueError, IndexError):
                continue
        return out

    # -- 按名字直接搜索 ------------------------------------------------------

    async def search_by_name(self, keyword: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """按名字直接搜 EGS（``kensaku.php``）。

        这条路以前被判过死刑 —— 早先的结论是「站内搜索接口对非浏览器请求返回
        ``200 + 空响应``（反爬）」，于是整个方案退化成「翻榜单建本地索引」。
        **那个结论是错的**：真正的原因是少传了一个必填参数 ``word_category=name``。
        补上之后，插件自己的 UA 也能正常拿到结果，域名用 ``.dyndns.org`` 或
        ``.org`` 都一样。

        而且搜索结果比榜单页**字段更全**：

        - ``id``：详情页 ``game.php?game=<id>`` 要的就是它；
        - ``median`` / ``sd`` / ``votes``：中央値 / 標準偏差 / データ数；
        - ``released``：**发售日** —— 榜单索引里根本没有这一列。

        所以「名字 → id」这一步不必依赖本地索引：搜索更新、更准，
        索引还没建好时也能用。这正是「批评空间只是附加项」的关键。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        url = (
            f"{BASE}/kensaku.php?category=game&word_category=name"
            f"&word={quote(keyword)}&mode=normal"
        )
        try:
            body = await self._http.request(
                "GET",
                url,
                source="egs",
                expect_json=False,
                cache_ttl=1800.0,
                cache_key=f"egs:search:{keyword}",
                headers={"Referer": f"{BASE}/"},
            )
        except HttpError:
            return []
        return self._parse_search_rows(str(body))[:limit]

    async def search_best(
        self, *names: str, threshold: int = 72
    ) -> dict[str, Any] | None:
        """拿一堆候选名依次去搜，返回第一个够像的结果。

        候选名按调用方给的顺序试，**命中即止** —— 每个名字都是一次网络请求，
        不能八个名字全打一遍。日文原名（``title_ja``）排在最前，通常第一个就中。

        挑选逻辑和 VNDB 那边一致：相似度优先，同分看票数（票数多的更可能是本体
        而不是某个移植版）。
        """
        for name in names:
            if not name:
                continue
            results = await self.search_by_name(name)
            if not results:
                continue
            scored = [
                (similarity(name, str(item.get("name") or "")), item) for item in results
            ]
            scored = [(score, item) for score, item in scored if score >= threshold]
            if not scored:
                continue
            scored.sort(key=lambda pair: (pair[0], int(pair[1].get("votes") or 0)), reverse=True)
            return scored[0][1]
        return None

    @staticmethod
    def _parse_search_rows(raw_html: str) -> list[dict[str, Any]]:
        """解析搜索结果的一行：名称 / 品牌 / **发售日** / 中央値 / 標準偏差 / データ数。

        注意列序和榜单页（``toukei_median.php``）**不一样**：这里多了发售日，
        所以不能和 :meth:`_parse_ranking_rows` 共用一份解析 —— 硬套会把发售日
        当成中央值。
        """
        out: list[dict[str, Any]] = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw_html, flags=re.S):
            if "game.php?game=" not in row:
                continue
            link = re.search(r"game\.php\?game=(\d+)", row)
            if not link:
                continue
            cells = [
                re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S)
            ]
            if len(cells) < 6:
                continue
            name = re.sub(r"\s*OHP\s*$", "", cells[0]).strip()
            try:
                out.append(
                    {
                        "id": link.group(1),
                        "name": name,
                        "brand": cells[1],
                        # 未发售的作品这一列是空的，别当成日期
                        "released": cells[2] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[2]) else "",
                        "median": int(float(cells[3])) if cells[3] else 0,
                        "sd": float(cells[4]) if cells[4] else 0.0,
                        "votes": int(float(cells[5])) if cells[5] else 0,
                    }
                )
            except (ValueError, IndexError):
                continue
        return out

    # -- 详情 --------------------------------------------------------------

    async def get_detail(
        self, game_id: str | int, *, max_reviews: int = 3
    ) -> dict[str, Any] | None:
        """抓详情页：评分 + 得点分布 + 评价维度 + 短评。

        抓一次缓存一小时，所以同一部作品反复问不会反复打人家服务器。
        """
        if not game_id:
            return None
        try:
            raw = await self._http.request(
                "GET",
                f"{BASE}/game.php?game={game_id}",
                source="egs",
                expect_json=False,
                cache_ttl=3600.0,
                cache_key=f"egs:detail:{game_id}",
                headers={"Referer": f"{BASE}/"},
            )
        except HttpError:
            return None
        return self.parse_detail(str(raw), str(game_id), max_reviews=max_reviews)

    async def get_long_review(self, game_id: str | int, uid: str) -> dict[str, Any] | None:
        """抓一篇「长文感想」—— 批评空间里真正算得上锐评的东西。

        短评只有一两句话，长文才是完整的批评与吐槽（动辄几千字）。
        入口就嵌在游戏页的短评里（``memo.php?game=X&uid=Y``），
        所以「挑哪几篇」不花请求，只有抓正文才花。

        缓存一小时；抓不到返回 None —— 锐评是加分项，缺了不该影响主流程。
        """
        if not game_id or not uid:
            return None
        try:
            raw = await self._http.request(
                "GET",
                f"{BASE}/memo.php?game={game_id}&uid={quote(str(uid), safe='')}",
                source="egs",
                expect_json=False,
                cache_ttl=3600.0,
                cache_key=f"egs:memo:{game_id}:{uid}",
                headers={"Referer": f"{BASE}/"},
            )
        except HttpError:
            return None
        review = self.parse_long_review(str(raw), str(uid))
        return review if review.get("text") else None

    @staticmethod
    def parse_long_review(raw_html: str, uid: str = "") -> dict[str, Any]:
        """解析长文感想页。页面结构很干净，三块：

        - 得分：``<span class="red bold">100</span>点``
        - 一句话短评：``<div id="hitokoto">``
        - 长文正文：``<div id="memo">``（用 ``<br />`` 分行）

        正文里会有 ``&hellip;`` 这类实体，交给 :func:`_memo_text` 还原。
        """
        out: dict[str, Any] = {"uid": uid, "text": "", "hitokoto": ""}
        # 得分的 class 在两种页面上不一样：游戏页是 `red bold`，memo 页是 `red`
        score = re.search(
            r'<span class="red(?:\s+bold)?">\s*(\d+)\s*</span>\s*点', raw_html
        )
        if score:
            out["score"] = int(score.group(1))
        hitokoto = re.search(
            r'<div id="hitokoto"[^>]*>(.*?)</div>', raw_html, flags=re.S | re.I
        )
        if hitokoto:
            out["hitokoto"] = _memo_text(hitokoto.group(1))
        memo = re.search(r'<div id="memo"[^>]*>(.*?)</div>', raw_html, flags=re.S | re.I)
        if memo:
            out["text"] = _memo_text(memo.group(1))
        return out

    @staticmethod
    def parse_detail(raw_html: str, game_id: str, *, max_reviews: int = 3) -> dict[str, Any]:
        """从详情页抠数据。

        除了评分，还尽量把「评价如何」的料挖出来 —— 这些才是玩家真正想知道的：

        - ``praise_rate``：好评率（80 分以上占比），比中央值更能反映"口碑稳不稳"；
        - ``stacked`` / ``giveup``：积压率与弃坑率，中文圈口中的「積んでる」；
        - ``interesting_minutes``：**面白くなってきた時間**（多久开始好看），EGS 独有；
        - ``play_minutes``：玩家实测通关时长中位数，可用来跟 VNDB 的时长互相对照；
        - ``pov``：用户评价维度（シナリオ「シナリオがいい(137)」这类带票数的标签）；
        - ``reviews``：最新几条短评（得分 + 正文），注意跳过带剧透标记的长文。

        未发售或零评价的作品页面上没有中央値那一段，这里返回 ``median=None``，
        调用方应当显示「暂无评分」而不是当成抓取失败。
        """
        text = _flatten(raw_html)
        detail: dict[str, Any] = {"source": "egs", "id": game_id}

        title = re.search(r"<title>(.*?)</title>", raw_html, flags=re.S)
        if title:
            # 页面标题长这样：「ランス10    ErogameScape -エロゲー批評空間-」，站点名要切掉
            raw_title = html.unescape(title.group(1)).strip().split("\n")[0].strip()
            for suffix in ["ErogameScape", "エロゲー批評空間"]:
                if suffix in raw_title:
                    raw_title = raw_title.split(suffix)[0].strip()
            detail["title"] = raw_title

        stats = re.search(
            r"中央値\s*(\d+)\s*平均値\s*([\d.]+)\s*データ数\s*(\d+)\s*"
            r"標準偏差\s*([\d.]+)\s*最高点\s*(\d+)\s*最低点\s*(\d+)",
            text,
        )
        if stats:
            detail.update(
                {
                    "median": int(stats.group(1)),
                    "mean": float(stats.group(2)),
                    "votes": int(stats.group(3)),
                    "sd": float(stats.group(4)),
                    "max": int(stats.group(5)),
                    "min": int(stats.group(6)),
                }
            )
        else:
            detail.update({"median": None, "mean": None, "votes": 0})

        giveup = re.search(r"giveupした人\s*(\d+)\((\d+)%\)", text)
        stacked = re.search(r"積んでる人\s*(\d+)\((\d+)%\)", text)
        if giveup:
            detail["giveup"] = {"count": int(giveup.group(1)), "percent": int(giveup.group(2))}
        if stacked:
            detail["stacked"] = {"count": int(stacked.group(1)), "percent": int(stacked.group(2))}

        # 时间维度的两个中位数（EGS 独有）：多久开始好看 / 玩家实测通关时长
        interesting = re.search(r"面白くなってきた時間中央値\s*(\d+)\s*時間", text)
        play = re.search(r"プレイ時間中央値\s*(\d+)\s*時間", text)
        if interesting:
            detail["interesting_minutes"] = int(interesting.group(1)) * 60
        if play:
            detail["play_minutes"] = int(play.group(1)) * 60

        released = re.search(r"発売日\s*(\d{4}-\d{2}-\d{2})", text)
        if released:
            detail["released"] = released.group(1)

        # 属性 / 标签 / POV 都在这张表里，优先按表格解析；表不在时退回文本启发式
        table = EgsClient._parse_attribute_table(raw_html)
        detail["genre"] = table.get("genre") or ""
        if not detail["genre"]:
            genre = re.search(r"公式ジャンル\s*(.+?)\s*属性", text)
            if genre:
                detail["genre"] = genre.group(1).strip()[:60]
        detail["attributes"] = table.get("attributes") or EgsClient._parse_marked_list(
            text, "属性", stop_marks=("タグ",)
        )
        detail["tags"] = table.get("tags") or EgsClient._parse_marked_list(
            text, "タグ", stop_marks=("シナリオ", "背景", "傾向")
        )
        detail["pov"] = table.get("pov") or {}

        distribution = EgsClient._parse_distribution(text)
        detail["distribution"] = distribution
        if distribution:
            total = sum(int(b["count"]) for b in distribution) or 0
            if total:
                high = sum(
                    int(b["count"])
                    for b in distribution
                    if EgsClient._bucket_floor(b["bucket"]) >= 80
                )
                detail["praise_rate"] = round(high / total * 100)

        detail["reviews"] = EgsClient._parse_reviews(raw_html, limit=max_reviews)
        return detail

    @staticmethod
    def _bucket_floor(bucket: str) -> int:
        """把得点分布的档位标签转成下界分值：「100」→100、「90～99」→90、「0～9」→0。"""
        match = re.match(r"(\d+)", str(bucket))
        return int(match.group(1)) if match else 0

    @staticmethod
    def _parse_marked_list(
        text: str, label: str, *, stop_marks: tuple[str, ...] = ()
    ) -> list[str]:
        """解析「属性 A , B , C」这类逗号分隔列表。取第一处看起来最像列表的候选。"""
        best: list[str] = []
        for match in re.finditer(re.escape(label), text):
            window = text[match.end() : match.end() + 400]
            line = window.split("\n")[0].strip().lstrip("：: ").strip()
            if not line:
                continue
            for stop in stop_marks or ("対応OS", "ブランド", "発売日", "公式ジャンル"):
                if stop in line:
                    line = line.split(stop)[0]
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) > len(best):
                best = parts
        # 过滤纯符号项
        return [p for p in best if len(p) >= 1 and not re.fullmatch(r"[/、,，\s]+", p)][:20]

    @staticmethod
    def _parse_attribute_table(raw_html: str) -> dict[str, Any]:
        """解析 ``<table id="att_pov_table">`` —— 属性/标签/POV 评价维度都在这一张表里。

        表结构是每行 ``<th>分类</th><td>条目1(票数) , 条目2(票数) …</td>``，
        剧透 POV 会被包在 ``<span class="netabare">`` 里。按表格解析比按纯文本猜准得多：

        - ``公式ジャンル`` → 官方类型
        - ``属性`` → 游戏属性（RPG / 声なし / 修正ファイルあり …）
        - ``タグ`` → 用户标签
        - 其余行（シナリオ / 背景 / 傾向 / 絵 / 音楽 …）→ **用户评价维度**，
          形如「シナリオがいい(137)」——这才是"大家觉得它好在哪"。
        """
        out: dict[str, Any] = {"attributes": [], "tags": [], "genre": "", "pov": {}}
        table = re.search(
            r'<table[^>]*id="att_pov_table"[^>]*>(.*?)</table>', raw_html, flags=re.S | re.I
        )
        if not table:
            return out
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), flags=re.S | re.I):
            head = re.search(r"<th[^>]*>(.*?)</th>", row, flags=re.S | re.I)
            body = re.search(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)
            if not head or not body:
                continue
            label = _flatten(head.group(1)).strip()
            cell = body.group(1)

            if label == "公式ジャンル":
                out["genre"] = _flatten(cell).strip()[:60]
                continue

            if label in ("属性", "タグ"):
                items = [
                    _flatten(m).strip()
                    for m in re.findall(r"<a[^>]*>(.*?)</a>", cell, flags=re.S | re.I)
                ]
                items = [i for i in items if i]
                if not items:
                    items = [p.strip() for p in _flatten(cell).split(",") if p.strip()]
                out["attributes" if label == "属性" else "tags"] = items[:20]
                continue

            # 其余行都是 POV 评价维度；剧透条目单独摘出来丢掉，别把剧透写进卡片
            entries: list[dict[str, Any]] = []
            for match in re.finditer(
                r'(<span class="netabare">)?\s*<a[^>]*>(.*?)</a>\s*[\(（](\d+)[\)）]',
                cell,
                flags=re.S | re.I,
            ):
                name = _flatten(match.group(2)).strip()
                votes = int(match.group(3))
                if name and len(name) >= 2 and not match.group(1):
                    entries.append({"name": name, "votes": votes})
            if entries and label and len(label) <= 6:
                entries.sort(key=lambda e: e["votes"], reverse=True)
                out["pov"][label] = entries[:6]
        return out

    @staticmethod
    def _parse_reviews(raw_html: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """解析短评：``<span class="red bold">100</span>点 <br /> 正文 …``

        只取页面上直接可见的短评（不带剧透标记）。同时把每条短评里嵌着的
        **长文感想入口**一并捞出来：``長文感想(字数)(ネタバレ注意)`` 这种链接上
        带着 uid、字数与剧透标记，所以「挑哪几篇长文」不需要额外请求，
        只有真去抓正文才要（见 :meth:`get_long_review`）。
        """
        reviews: list[dict[str, Any]] = []
        # 每条评论是一个 uid_xxx 容器
        for block in re.split(r'<div class="uid_', raw_html)[1:]:
            block = block[:2500]
            score = re.search(r'<span class="red bold">\s*(\d+)\s*</span>', block)
            if not score:
                continue
            # 长文感想入口：href="memo.php?game=X&amp;uid=Y">長文感想(185)(ネタバレ注意)</a>
            memo = re.search(
                r'href="memo\.php\?game=\d+&(?:amp;)?uid=([^"&]+)">'
                r"長文感想\((\d+)\)(\(ネタバレ注意\))?",
                block,
            )
            body = block.split("<br />", 1)
            body_text = body[1] if len(body) > 1 else ""
            # 正文到「→ 長文感想」或 play_time 为止
            body_text = re.split(r"→|<div", body_text)[0]
            body_text = _flatten(body_text).strip()
            if not body_text or len(body_text) < 4:
                continue
            play = re.search(r"総プレイ時間\s*:\s*(\d+)\s*h", block)
            interesting = re.search(r"面白くなってきた時間\s*:\s*(\d+)\s*h", block)
            user = re.search(r"user_infomation\.php\?user=([^\"&]+)", block)
            # 短评在页面上是公开可见的；带星号的「長文感想(…)(ネタバレ注意)」只是长文的入口，
            # 不代表这条短评本身剧透 —— 所以只有正文被 netabare 包裹时才算剧透
            spoiler = bool(re.search(r'<span class="netabare">', block[:600]))
            if spoiler:
                continue
            reviews.append(
                {
                    "score": int(score.group(1)),
                    "text": body_text[:220],
                    "user": _unquote_js(user.group(1)) if user else "",
                    "play_hours": int(play.group(1)) if play else 0,
                    "interesting_hours": int(interesting.group(1)) if interesting else 0,
                    # 长文感想入口（可能没有）
                    "memo_uid": _unquote_js(memo.group(1)) if memo else "",
                    "memo_length": int(memo.group(2)) if memo else 0,
                    "memo_spoiler": bool(memo.group(3)) if memo else False,
                }
            )
            if len(reviews) >= limit:
                break
        return reviews

    @staticmethod
    def _parse_distribution(text: str) -> list[dict[str, Any]]:
        """得点分布：解析「100 / 90～99 / … / 0～9」这些档位的度数。"""
        start = text.find("得点分布")
        if start < 0:
            return []
        chunk = text[start : start + 1200]
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        # 形如「100 519」「90～99 455」「0～9 0」，中间可能夹着「状況 度数 グラフ」表头
        for match in re.finditer(r"(?:^|\s)(\d{1,3})(?:～(\d{1,3}))?\s+(\d{1,6})(?=\s|$)", chunk):
            low = match.group(1)
            high = match.group(2) or low
            label = f"{high}" if not match.group(2) else f"{low}～{high}"
            if label in seen:
                continue
            seen.add(label)
            out.append({"bucket": label, "count": int(match.group(3))})
            if len(out) >= 11:
                break
        return out
