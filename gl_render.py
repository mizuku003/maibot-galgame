"""galgame领域大神 —— 图卡渲染。

卡片用**内联 CSS 的自包含 HTML**，封面图以 base64 内嵌：宿主的 ``render.html2png``
默认不让页面访问外网（``allow_network`` 为假），直接写图片 URL 会渲染成空白。

样式上的两个取舍：
- 不依赖任何外部字体，只用系统字体栈，中文优先「微软雅黑 / 苹方」；
- 评分不合并成一个数——批评空间的中央值和 VNDB 的贝叶斯分口径不同，
  并排展示、各自一条进度条，不做加权合并。
"""

from __future__ import annotations

import asyncio
import base64
import html as _html
from typing import Any

from gl_http import HttpClient
from gl_merge import GameInfo

FONT_STACK = (
    '"Microsoft YaHei","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC",'
    '"Source Han Sans SC",sans-serif'
)

# 必须自己声明编码：宿主若是把 HTML 落文件再打开、或走非 UTF-8 的默认解析，
# 中文会整片变乱码（本地用无头浏览器截图时就踩到了）。
HEAD = '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'


def _esc(text: Any) -> str:
    return _html.escape(str(text or ""), quote=True)


def _bar(value: float, maximum: float, color: str) -> str:
    """进度条。value/maximum 都当百分制处理。"""
    pct = 0.0 if maximum <= 0 else max(0.0, min(value / maximum * 100.0, 100.0))
    return (
        f'<div style="background:#eceff3;border-radius:6px;height:10px;overflow:hidden;margin-top:4px;">'
        f'<div style="width:{pct:.1f}%;height:100%;background:{color};"></div></div>'
    )


# 卡片里封面最大显示到 150×214（信息卡），按 2 倍图留点余量。
# 列表卡只用 62×88，压到这个尺寸照样清晰。
COVER_MAX_SIZE = (320, 460)
# 超过这个体积就先压一压。月幕的封面大多 10~33KB，但偶尔会甩几百 KB 的原图 ——
# 一张几百 KB 的原图转成 base64 后体积还要涨三成，能把整个 HTML 顶到近 1MB。
COVER_SHRINK_THRESHOLD = 60 * 1024


def _guess_image_mime(raw: bytes) -> str:
    """按**字节魔数**判断图片类型。

    不能按 URL 后缀猜：月幕的封面几乎全是 ``.webp``，而原来的写法是
    「不是 .png 就当 jpeg」，等于给 webp 字节贴上 ``image/jpeg`` 的标签。
    浏览器处理 ``data:`` URI 时以声明的 MIME 为准，贴错就可能开天窗。
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"GIF8"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"<svg") or raw.startswith(b"<?xml"):
        return "image/svg+xml"
    return "image/jpeg"


def _shrink_image(raw: bytes) -> bytes | None:
    """把过大的封面压成缩略图；压不了就返回 None（调用方原样用原图）。

    Pillow 是宿主既有的依赖（``requirements.txt`` 里的 ``pillow>=12.3.0``），
    正常不会缺。这里仍然 try 一下：**压缩失败绝不能导致丢图**，宁可发大图。
    """
    try:
        import io

        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            converted = image.convert("RGB")
            converted.thumbnail(COVER_MAX_SIZE, Image.LANCZOS)
            buffer = io.BytesIO()
            converted.save(buffer, format="JPEG", quality=85, optimize=True)
            shrunk = buffer.getvalue()
    except Exception:  # noqa: BLE001
        return None
    # 小图重编码反而会膨胀，那就别换
    return shrunk if len(shrunk) < len(raw) else None


async def cover_data_uri(http: HttpClient | None, url: str) -> str:
    """把封面下下来做成 data URI；失败就是没图，不抛。

    两件事必须做对：

    1. **MIME 按字节判断**，不按 URL 后缀（见 :func:`_guess_image_mime`）；
    2. **大图先压**。不压的话偶尔一张几百 KB 的原图就能把 HTML 顶到 1MB，
       渲染和 RPC 都跟着变慢 —— 而卡片上它只占 62×88 像素。
    """
    if not url or http is None:
        return ""
    try:
        raw = await http.fetch_bytes(url, source="cover")
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    mime = _guess_image_mime(raw)
    if len(raw) > COVER_SHRINK_THRESHOLD:
        shrunk = _shrink_image(raw)
        if shrunk is not None:
            raw, mime = shrunk, "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


ADULT_POV_CATEGORIES = {"エロシーン", "エロ", "Hシーン", "陵辱", "成人向け"}
ADULT_POV_WORDS = ("陵辱", "輪姦", "中出し", "おかず", "抜き", "Hシーン")


def _eval_strip(info: GameInfo, accent: str) -> str:
    """评价强度一行：好评率 / 积压率 / 多久开始好看 / 玩家实测时长。

    这几个数字比总分更能说明"这游戏到底怎么样"：
    好评率（80 分以上占比）看口碑稳不稳，积压率看有多少人买了没打，
    玩家实测时长还能和 VNDB 的估算互相对照。
    """
    chips: list[tuple[str, str]] = []
    if info.egs_praise_rate is not None:
        chips.append(("好评率", f"{info.egs_praise_rate}%"))
    if info.egs_stacked:
        chips.append(("积压率", f"{info.egs_stacked.get('percent')}%"))
    if info.egs_giveup:
        chips.append(("弃坑率", f"{info.egs_giveup.get('percent')}%"))
    if info.egs_interesting_minutes:
        chips.append(("开始好看", f"{info.egs_interesting_minutes / 60:.0f} 小时"))
    if info.egs_play_minutes:
        chips.append(("实测时长", f"{info.egs_play_minutes / 60:.0f} 小时"))
    if not chips:
        return ""
    cells = "".join(
        f'<div style="flex:1;min-width:96px;background:#f7f9fc;border-radius:8px;'
        f'padding:7px 9px;"><div style="color:#9aa3b0;font-size:11px;">{_esc(k)}</div>'
        f'<div style="font-weight:700;font-size:15px;color:{accent};">{_esc(v)}</div></div>'
        for k, v in chips
    )
    return f'<div style="display:flex;gap:8px;margin-top:12px;">{cells}</div>'


def _pov_block(info: GameInfo, *, hide_adult: bool, limit: int = 6) -> str:
    """评价维度：好在哪 / 差在哪。这是 EGS 里信息量最大的一块。"""
    pov = dict(info.egs_pov or {})
    if not pov:
        return ""
    if hide_adult:
        pov = {k: v for k, v in pov.items() if k not in ADULT_POV_CATEGORIES}
    rows: list[str] = []
    # 优先展示"好在哪"和"差在哪"，这两类比零散标签有用
    order = ["ここがいい", "傾向", "ネガティブ", "シナリオ", "グラフィック", "音", "キャラクター",
             "ゲーム性", "主人公", "背景", "ジャンル", "システム"]
    for key in order:
        if key not in pov or not pov[key]:
            continue
        entries = pov[key]
        if hide_adult:
            entries = [e for e in entries if not any(w in e["name"] for w in ADULT_POV_WORDS)]
        if not entries:
            continue
        title = {"ここがいい": "好评点", "ネガティブ": "吐槽点"}.get(key, key)
        names = "、".join(f"{e['name']}({e['votes']})" for e in entries[:limit])
        color = "#c0392b" if key == "ネガティブ" else "#4a5361"
        rows.append(
            f'<div style="font-size:12.5px;margin:3px 0;color:{color};">'
            f'<span style="color:#8b95a3;">{_esc(title)}：</span>{_esc(names)}</div>'
        )
        if len(rows) >= 4:
            break
    if not rows:
        return ""
    return (
        '<div style="margin-top:12px;padding:9px 11px;background:#fbfcfe;'
        'border:1px solid #eef1f6;border-radius:8px;">'
        '<div style="font-size:11.5px;color:#9aa3b0;margin-bottom:2px;">'
        '批评空间用户评价维度（括号内为投票数）</div>' + "".join(rows) + "</div>"
    )


def _reviews_block(info: GameInfo, accent: str, limit: int = 2) -> str:
    """短评两条：得分 + 正文。给人看的东西，字不用多。"""
    reviews = (info.egs_reviews or [])[:limit]
    if not reviews:
        return ""
    rows = "".join(
        f'<div style="margin:5px 0;font-size:12px;line-height:1.55;color:#4a5361;">'
        f'<span style="color:{accent};font-weight:700;">{r.get("score")} 点</span> '
        f'{_esc(r.get("text") or "")[:110]}'
        f'<span style="color:#b3bac6;">'
        f'{"　" + str(r.get("play_hours")) + "h" if r.get("play_hours") else ""}</span></div>'
        for r in reviews
    )
    return (
        '<div style="margin-top:12px;">'
        '<div style="font-size:11.5px;color:#9aa3b0;margin-bottom:2px;">批评空间短评</div>'
        f"{rows}</div>"
    )


def _characters_block(info: GameInfo, limit: int = 10) -> str:
    """角色 / 声优一行。中文圈问「谁配的」要的就是这块。"""
    lines = info.character_lines(limit=limit)
    if not lines:
        return ""
    return (
        '<div style="margin-top:12px;">'
        '<div style="font-size:11.5px;color:#9aa3b0;margin-bottom:2px;">角色 / 声优</div>'
        f'<div style="font-size:12.5px;line-height:1.85;color:#4a5361;">{_esc("、".join(lines))}</div>'
        "</div>"
    )


def build_info_html(
    info: GameInfo,
    *,
    cover_uri: str = "",
    accent: str = "#c2355f",
    width: int = 760,
    max_tags: int = 12,
    show_egs_distribution: bool = False,
    show_reviews: bool = True,
    hide_adult_pov: bool = True,
) -> str:
    """资料卡：封面 + 标题 + 元信息 + 三源评分 + 标签 + 简介。"""
    title = _esc(info.display_title())
    sub_parts = [p for p in [info.title_ja, info.title_main] if p and p != info.display_title()]
    subtitle = _esc(" / ".join(dict.fromkeys(sub_parts)))
    aliases = "、".join(info.aliases[:5])

    cover_html = (
        f'<img src="{cover_uri}" style="width:150px;height:214px;object-fit:cover;'
        f'border-radius:10px;box-shadow:0 3px 10px rgba(0,0,0,.18);flex:none;" />'
        if cover_uri
        else '<div style="width:150px;height:214px;border-radius:10px;background:#e9ecf1;'
        'display:flex;align-items:center;justify-content:center;color:#9aa3b0;'
        'font-size:13px;flex:none;">无封面</div>'
    )

    # 元信息表
    meta_rows: list[tuple[str, str]] = [
        ("发售", info.released or "未知"),
        ("厂商", info.developer or "未知"),
        ("时长", info.length_text()),
        ("中文", info.chinese_text()),
    ]
    if info.platforms:
        meta_rows.append(("平台", " / ".join(info.platforms)))
    if info.restricted is not None:
        meta_rows.append(("分级", "18 禁" if info.restricted else "全年龄"))
    meta_html = "".join(
        f'<div style="display:flex;gap:8px;margin:3px 0;">'
        f'<span style="color:#8b95a3;flex:none;width:42px;">{_esc(k)}</span>'
        f'<span style="color:#2b3440;">{_esc(v)}</span></div>'
        for k, v in meta_rows
    )

    # 评分区：三源并列
    rating_blocks: list[str] = []
    if info.egs_median:
        extra = f"平均 {info.egs_mean:.1f} · {info.egs_votes} 人" if info.egs_mean else ""
        rating_blocks.append(
            f'<div style="flex:1;min-width:180px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:13px;">'
            f'<span style="color:#5b6472;">批评空间 中央値</span>'
            f'<span style="font-weight:700;color:{accent};">{info.egs_median}</span></div>'
            f'{_bar(float(info.egs_median), 100.0, accent)}'
            f'<div style="color:#9aa3b0;font-size:11px;margin-top:3px;">{_esc(extra)}</div></div>'
        )
    if info.vndb_rating is not None:
        rating_blocks.append(
            f'<div style="flex:1;min-width:180px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:13px;">'
            f'<span style="color:#5b6472;">VNDB</span>'
            f'<span style="font-weight:700;color:#2e6fd6;">{info.vndb_rating:.1f}</span></div>'
            f'{_bar(float(info.vndb_rating), 100.0, "#2e6fd6")}'
            f'<div style="color:#9aa3b0;font-size:11px;margin-top:3px;">'
            f'{info.vndb_votes} 票</div></div>'
        )
    ratings_html = (
        f'<div style="display:flex;gap:18px;margin-top:10px;">{"".join(rating_blocks)}</div>'
        if rating_blocks
        else '<div style="margin-top:10px;color:#9aa3b0;font-size:13px;">暂无评分数据</div>'
    )

    # 批评空间得点分布（可选）
    dist_html = ""
    if show_egs_distribution and info.egs_detail:
        buckets = info.egs_detail.get("distribution") or []
        if buckets:
            peak = max(int(b.get("count") or 0) for b in buckets) or 1
            bars = "".join(
                f'<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;">'
                f'<div style="width:100%;height:{max(2, int(int(b["count"]) / peak * 46))}px;'
                f'background:{accent};opacity:.75;border-radius:2px 2px 0 0;"></div>'
                f'<div style="font-size:9px;color:#9aa3b0;">{_esc(b["bucket"])}</div></div>'
                for b in reversed(buckets)
            )
            dist_html = (
                f'<div style="margin-top:12px;"><div style="font-size:12px;color:#8b95a3;'
                f'margin-bottom:4px;">批评空间得点分布</div>'
                f'<div style="display:flex;align-items:flex-end;gap:3px;height:64px;">{bars}</div></div>'
            )

    tags_html = ""
    tag_names = [str(t.get("name") or "") for t in info.tags[:max_tags]]
    if tag_names:
        tags_html = (
            '<div style="margin-top:12px;display:flex;flex-wrap:wrap;gap:6px;">'
            + "".join(
                f'<span style="background:#f1f3f7;color:#4a5361;border-radius:20px;'
                f'padding:2px 10px;font-size:12px;">{_esc(t)}</span>'
                for t in tag_names
            )
            + "</div>"
        )

    intro = (info.intro or "").strip()
    intro_html = ""
    if intro:
        intro_html = (
            f'<div style="margin-top:12px;color:#5b6472;font-size:12.5px;line-height:1.6;'
            f'display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden;">'
            f"{_esc(intro[:600])}</div>"
        )

    source_note = " · ".join(
        {"ymgal": "月幕", "vndb": "VNDB", "egs": "批评空间"}.get(s, s) for s in info.sources
    )

    eval_html = _eval_strip(info, accent)
    pov_html = _pov_block(info, hide_adult=hide_adult_pov)
    reviews_html = _reviews_block(info, accent) if show_reviews else ""
    characters_html = _characters_block(info)

    return f"""{HEAD}<body style="margin:0;padding:0;background:transparent;">
<div id="card" style="width:{width}px;box-sizing:border-box;padding:20px;
     background:linear-gradient(180deg,#ffffff 0%,#fbfcfe 100%);
     border:1px solid #e6e9ef;border-radius:14px;font-family:{FONT_STACK};color:#2b3440;">
  <div style="display:flex;gap:16px;">
    {cover_html}
    <div style="flex:1;min-width:0;">
      <div style="font-size:22px;font-weight:700;line-height:1.25;">{title}</div>
      <div style="color:#8b95a3;font-size:12.5px;margin-top:4px;">{subtitle}</div>
      {f'<div style="color:#a6aebb;font-size:11.5px;margin-top:3px;">别名：{_esc(aliases)}</div>' if aliases else ''}
      <div style="margin-top:10px;font-size:13px;">{meta_html}</div>
      {ratings_html}
    </div>
  </div>
  {eval_html}
  {pov_html}
  {characters_html}
  {reviews_html}
  {dist_html}
  {tags_html}
  {intro_html}
  <div style="margin-top:14px;padding-top:8px;border-top:1px dashed #e6e9ef;
       color:#b3bac6;font-size:11px;display:flex;justify-content:space-between;">
    <span>数据来源：{_esc(source_note) or '—'}</span>
    <span>{_esc(info.egs_id and f'EGS #{info.egs_id}' or '')}</span>
  </div>
</div>
</body>"""


async def build_recommend_html(
    items: list[GameInfo],
    *,
    http: HttpClient | None = None,
    accent: str = "#c2355f",
    width: int = 760,
    condition_text: str = "",
) -> str:
    """推荐卡：一次列几部，每行带封面缩略图和评分。"""
    rows: list[str] = []
    for idx, info in enumerate(items, 1):
        cover = await cover_data_uri(http, info.cover)
        thumb = (
            f'<img src="{cover}" style="width:62px;height:88px;object-fit:cover;'
            f'border-radius:6px;flex:none;" />'
            if cover
            else '<div style="width:62px;height:88px;border-radius:6px;background:#e9ecf1;'
            'flex:none;"></div>'
        )
        ratings: list[str] = []
        if info.egs_median:
            ratings.append(
                f'<span style="color:{accent};font-weight:700;">中央値 {info.egs_median}</span>'
            )
        if info.vndb_rating is not None:
            ratings.append(
                f'<span style="color:#2e6fd6;font-weight:700;">VNDB {info.vndb_rating:.1f}</span>'
            )
        meta = " · ".join(
            p
            for p in [
                info.released,
                info.developer,
                info.length_text(),
                info.chinese_text(),
            ]
            if p
        )
        rows.append(
            f'<div style="display:flex;gap:12px;padding:12px 0;border-bottom:1px dashed #eceff3;">'
            f"{thumb}"
            f'<div style="flex:1;min-width:0;">'
            f'<div style="font-size:16px;font-weight:700;">{idx}. {_esc(info.display_title())}</div>'
            f'<div style="color:#8b95a3;font-size:12px;margin-top:3px;">{_esc(meta)}</div>'
            f'<div style="margin-top:6px;font-size:12.5px;display:flex;gap:14px;">'
            f'{" ".join(ratings)}</div>'
            f'</div></div>'
        )

    return f"""{HEAD}<body style="margin:0;padding:0;background:transparent;">
<div id="card" style="width:{width}px;box-sizing:border-box;padding:20px;
     background:#ffffff;border:1px solid #e6e9ef;border-radius:14px;
     font-family:{FONT_STACK};color:#2b3440;">
  <div style="font-size:18px;font-weight:700;color:{accent};">Galgame 推荐</div>
  {f'<div style="color:#8b95a3;font-size:12.5px;margin-top:4px;">筛选条件：{_esc(condition_text)}</div>' if condition_text else ''}
  <div style="margin-top:8px;">{"".join(rows)}</div>
  <div style="margin-top:10px;color:#b3bac6;font-size:11px;">
    评分口径：批评空间为中央值，VNDB 为贝叶斯分，两者不可直接比较
  </div>
</div>
</body>"""


async def _fetch_covers(
    http: HttpClient | None, urls: list[str], *, concurrency: int = 6
) -> list[str]:
    """并发把一批封面做成 data URI。

    必须并发：串行取 15 张大概要四五秒，用户按一下 `/gal new` 会以为机器人卡死了。
    限流到 6 路是因为封面都在同一个 CDN 上，打太猛容易被打回来。
    单张失败返回空串（卡片退化成灰色占位块），不抛异常。
    """
    if http is None:
        return ["" for _ in urls]
    semaphore = asyncio.Semaphore(concurrency)

    async def one(url: str) -> str:
        if not url:
            return ""
        async with semaphore:
            try:
                return await cover_data_uri(http, url)
            except Exception:  # noqa: BLE001
                return ""

    return list(await asyncio.gather(*[one(url) for url in urls]))


async def build_release_html(
    items: list[dict[str, Any]],
    *,
    http: HttpClient | None = None,
    accent: str = "#c2355f",
    width: int = 760,
    heading: str = "新作",
    subtitle: str = "",
) -> str:
    """发售列表卡：一批作品，每行封面缩略图 + 名称 + 发售日 + 厂商。

    和推荐卡的区别是这里的数据来自月幕的列表接口，字段少（没有评分、没有时长），
    所以只摆它真有的东西 —— 与其画一列「—」，不如留白。
    """
    covers = await _fetch_covers(http, [str(it.get("cover") or "") if isinstance(it, dict) else "" for it in items])

    rows: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        cover = covers[index] if index < len(covers) else ""
        thumb = (
            f'<img src="{cover}" style="width:62px;height:88px;object-fit:cover;'
            f'border-radius:6px;flex:none;" />'
            if cover
            else '<div style="width:62px;height:88px;border-radius:6px;background:#e9ecf1;flex:none;"></div>'
        )
        title = _esc(str(item.get("title_zh") or item.get("title") or "（未知）"))
        original = str(item.get("title") or "")
        original_html = (
            f'<span style="color:#a6aebb;font-size:11.5px;">{_esc(original)}</span>'
            if original and original != str(item.get("title_zh") or "")
            else ""
        )
        meta = " · ".join(
            part
            for part in [str(item.get("released") or ""), str(item.get("developer") or "")]
            if part
        )
        chips: list[str] = []
        if item.get("have_chinese"):
            chips.append(
                f'<span style="background:#e8f5ec;color:#2e7d5b;border-radius:4px;'
                f'padding:1px 6px;font-size:11px;">有中文版</span>'
            )
        if item.get("restricted"):
            chips.append(
                '<span style="background:#f7eeee;color:#a15050;border-radius:4px;'
                'padding:1px 6px;font-size:11px;">18禁</span>'
            )
        # 这几块单独拼好再塞进模板：把引号和转义写进 f-string 表达式里
        # 在 3.11 及更早版本是语法错误，没必要为了省两行赌宿主用哪个 Python。
        original_block = (
            f'<div style="margin-top:2px;">{original_html}</div>' if original_html else ""
        )
        chips_block = (
            '<div style="margin-top:5px;display:flex;gap:6px;">' + "".join(chips) + "</div>"
            if chips
            else ""
        )
        rows.append(
            '<div style="display:flex;gap:12px;padding:11px 0;border-bottom:1px dashed #eceff3;">'
            + thumb
            + '<div style="flex:1;min-width:0;">'
            + f'<div style="font-size:15.5px;font-weight:700;">{index + 1}. {title}</div>'
            + original_block
            + f'<div style="color:#8b95a3;font-size:12px;margin-top:3px;">{_esc(meta)}</div>'
            + chips_block
            + "</div></div>"
        )

    return f"""{HEAD}<body style="margin:0;padding:0;background:transparent;">
<div id="card" style="width:{width}px;box-sizing:border-box;padding:20px;
     background:#ffffff;border:1px solid #e6e9ef;border-radius:14px;
     font-family:{FONT_STACK};color:#2b3440;">
  <div style="font-size:18px;font-weight:700;color:{accent};">{_esc(heading)}</div>
  {f'<div style="color:#8b95a3;font-size:12.5px;margin-top:4px;">{_esc(subtitle)}</div>' if subtitle else ''}
  <div style="margin-top:8px;">{"".join(rows)}</div>
  <div style="margin-top:10px;color:#b3bac6;font-size:11px;">数据来源：月幕 Galgame</div>
</div>
</body>"""


def build_search_lines(items: list[GameInfo]) -> list[str]:
    """搜索结果的纯文本行（图卡之外的兜底/摘要）。"""
    lines: list[str] = []
    for idx, info in enumerate(items, 1):
        bits = [f"{idx}. {info.display_title()}"]
        if info.title_ja and info.title_ja != info.display_title():
            bits.append(f"（{info.title_ja}）")
        if info.released:
            bits.append(f"[{info.released[:4]}]")
        if info.length_minutes:
            bits.append(info.length_text())
        if info.have_chinese:
            bits.append("有汉化")
        if info.vndb_rating is not None:
            bits.append(f"VNDB {info.vndb_rating:.1f}")
        lines.append(" ".join(bits))
    return lines


# 资讯条的来源标签。文本源与发售源混在一张卡上，所以这里要认两种来源。
NEWS_SOURCE_LABELS = {
    "bugbug": "BugBug",
    "b_game": "BugBug",
    "dgame": "BugBug（同人）",
    "ymgal": "月幕",
    "vndb": "VNDB",
}


async def build_news_html(
    items: list[dict[str, Any]],
    *,
    http: HttpClient | None = None,
    accent: str = "#c2355f",
    width: int = 760,
    heading: str = "最新 gal 情报",
    subtitle: str = "",
) -> str:
    """资讯 / 预定列表卡：一行一条（缩略图 + 标题 + 来源与时间 + 摘要）。

    和发售卡的区别只在「一行里摆什么」：新闻有摘要没有厂商，预定有厂商没有摘要。
    两边的字段名不一样（news 用 image/published，upcoming 用 cover/released），
    所以取值时两套都认一遍，别指望调用方先归一化 —— 日历条目就是发售结构。
    """
    covers = await _fetch_covers(
        http,
        [
            str(it.get("cover") or it.get("image") or "") if isinstance(it, dict) else ""
            for it in items
        ],
        concurrency=4,
    )

    labels: set[str] = set()
    rows: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        cover = covers[index] if index < len(covers) else ""
        thumb = (
            '<img src="' + cover + '" style="width:78px;height:58px;'
            'object-fit:cover;border-radius:6px;flex:none;" />'
            if cover
            else '<div style="width:78px;height:58px;border-radius:6px;'
            'background:#e9ecf1;flex:none;"></div>'
        )

        source = str(item.get("source") or "")
        label = NEWS_SOURCE_LABELS.get(source, source or "网络")
        labels.add(label)

        title = _esc(str(item.get("title_zh") or item.get("title") or "（未知）"))
        original = str(item.get("title") or "")
        original_html = (
            f'<span style="color:#a6aebb;font-size:11.5px;">{_esc(original)}</span>'
            if original and original != str(item.get("title_zh") or "")
            else ""
        )
        original_block = (
            f'<div style="margin-top:2px;">{original_html}</div>' if original_html else ""
        )

        when = str(item.get("published") or item.get("released") or "")
        meta = " · ".join(part for part in [label, when] if part)

        summary = _esc(str(item.get("summary") or ""))
        summary_block = (
            '<div style="color:#6b7685;font-size:12px;margin-top:4px;'
            'line-height:1.5;">' + summary + "</div>"
            if summary
            else ""
        )

        chips: list[str] = []
        if item.get("have_chinese"):
            chips.append(
                '<span style="background:#e8f5ec;color:#2e7d5b;border-radius:4px;'
                'padding:1px 6px;font-size:11px;">有中文版</span>'
            )
        rating = item.get("rating")
        if isinstance(rating, (int, float)) and float(rating) > 0:
            chips.append(
                f'<span style="background:#eef2fb;color:#3b5b9a;border-radius:4px;'
                f'padding:1px 6px;font-size:11px;">VNDB {float(rating):.1f}</span>'
            )
        chips_block = (
            '<div style="margin-top:5px;display:flex;gap:6px;flex-wrap:wrap;">'
            + "".join(chips)
            + "</div>"
            if chips
            else ""
        )

        rows.append(
            '<div style="display:flex;gap:12px;padding:11px 0;'
            'border-bottom:1px dashed #eceff3;">'
            + thumb
            + '<div style="flex:1;min-width:0;">'
            + f'<div style="font-size:15.5px;font-weight:700;">{index + 1}. {title}</div>'
            + original_block
            + f'<div style="color:#8b95a3;font-size:12px;margin-top:3px;">{_esc(meta)}</div>'
            + summary_block
            + chips_block
            + "</div></div>"
        )

    footer = "、".join(sorted(labels)) or "网络"
    return f"""{HEAD}<body style="margin:0;padding:0;background:transparent;">
<div id="card" style="width:{width}px;box-sizing:border-box;padding:20px;
     background:#ffffff;border:1px solid #e6e9ef;border-radius:14px;
     font-family:{FONT_STACK};color:#2b3440;">
  <div style="font-size:18px;font-weight:700;color:{accent};">{_esc(heading)}</div>
  {f'<div style="color:#8b95a3;font-size:12.5px;margin-top:4px;">{_esc(subtitle)}</div>' if subtitle else ''}
  <div style="margin-top:8px;">{"".join(rows)}</div>
  <div style="margin-top:10px;color:#b3bac6;font-size:11px;">数据来源：{_esc(footer)}</div>
</div>
</body>"""


async def build_detail_html(
    item: dict[str, Any],
    *,
    http: HttpClient | None = None,
    accent: str = "#c2355f",
    width: int = 760,
    heading: str = "详情",
    subtitle: str = "",
) -> str:
    """一条新闻 / 一部的详情卡：大图 + 标题 + 正文整篇。

    列表卡那套「78×58 缩略图 + 一行摘要」装不下详情 —— 正文动辄几千字，
    挤在 right-hand 的小灰字里没法读。所以详情单独一版：封面横过来占满整幅，
    标题单独一行，正文用 13.5px / 行高 1.85 铺开，段落之间靠 body 里的空行
    （``white-space:pre-wrap``）自然分开。
    """
    data = item if isinstance(item, dict) else {}
    cover_url = str(data.get("image") or data.get("cover") or "")
    covers = await _fetch_covers(http, [cover_url], concurrency=1)
    cover = covers[0] if covers else ""
    cover_block = (
        f'<img src="{cover}" style="width:100%;height:260px;object-fit:cover;'
        'object-position:center 30%;border-radius:10px;margin-top:12px;display:block;" />'
        if cover
        else ""
    )

    title = _esc(str(data.get("title_zh") or data.get("title") or "（未知）"))
    original = str(data.get("title") or "")
    original_block = (
        f'<div style="color:#8b95a3;font-size:12px;margin-top:3px;">{_esc(original)}</div>'
        if original and original != str(data.get("title_zh") or "")
        else ""
    )

    when = str(data.get("published") or data.get("released") or "")
    label = NEWS_SOURCE_LABELS.get(str(data.get("source") or ""), "")
    meta = " · ".join(part for part in [label, when, str(data.get("developer") or "")] if part)
    meta_block = (
        f'<div style="color:#8b95a3;font-size:12.5px;margin-top:6px;">{_esc(meta)}</div>'
        if meta
        else ""
    )

    chips: list[str] = []
    if data.get("have_chinese"):
        chips.append(
            '<span style="background:#e8f5ec;color:#2e7d5b;border-radius:4px;'
            'padding:1px 6px;font-size:11px;">有中文版</span>'
        )
    rating = data.get("rating")
    if isinstance(rating, (int, float)) and float(rating) > 0:
        chips.append(
            f'<span style="background:#eef2fb;color:#3b5b9a;border-radius:4px;'
            f'padding:1px 6px;font-size:11px;">VNDB {float(rating):.1f}</span>'
        )
    chips_block = (
        '<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;">'
        + "".join(chips)
        + "</div>"
        if chips
        else ""
    )

    body = _esc(str(data.get("body") or data.get("summary") or "").strip())
    body_block = (
        '<div style="margin-top:14px;font-size:13.5px;line-height:1.85;'
        'color:#333c48;white-space:pre-wrap;word-break:break-word;">' + body + "</div>"
        if body
        else '<div style="margin-top:14px;color:#b3bac6;font-size:12.5px;">这条没抓到正文。</div>'
    )

    url = str(data.get("url") or "").strip()
    url_block = (
        f'<div style="margin-top:14px;padding-top:10px;border-top:1px solid #eef1f5;'
        f'color:#8b95a3;font-size:11.5px;word-break:break-all;">{_esc(url)}</div>'
        if url
        else ""
    )

    footer = label or "网络"
    return f"""{HEAD}<body style="margin:0;padding:0;background:transparent;">
<div id="card" style="width:{width}px;box-sizing:border-box;padding:20px;
     background:#ffffff;border:1px solid #e6e9ef;border-radius:14px;
     font-family:{FONT_STACK};color:#2b3440;">
  <div style="font-size:12px;font-weight:600;letter-spacing:3px;
       color:{accent};">{_esc(heading)}</div>
  {f'<div style="color:#8b95a3;font-size:12.5px;margin-top:4px;">{_esc(subtitle)}</div>' if subtitle else ''}
  {cover_block}
  <div style="margin-top:14px;font-size:20px;font-weight:700;line-height:1.35;">{title}</div>
  {original_block}
  {meta_block}
  {chips_block}
  {body_block}
  {url_block}
  <div style="margin-top:10px;color:#b3bac6;font-size:11px;">数据来源：{_esc(footer)}</div>
</div>
</body>"""


# ------------------------------------------------------------- /gal help
#
# ---------------------------------------------------------------------------
# 用法卡（/gal help）
#
# 版式是「galgame 启动画面 + 信息面板」：上半部分整幅主视觉（hero），标题压在画面
# 左下角；下半部分是深色信息面板，把 5 组指令按「编号 + 组名 + 发丝线 + 命令行 +
# 说明」排出来。命令行用等宽字体、暖金色，说明用低对比灰 —— 扫一眼就能找到要的。
#
# hero 的图从插件目录的 assets/help/ 里读（按文件名排序）：
#   放一张   → 当主视觉，全幅盖满；标题下方压了从透明到面板色的渐变，压字不糊；
#   放多张   → 第一张当主视觉，其余的在面板底部排成插图条（最多 4 张，见 HELP_ASSET_LIMIT）；
#   一张不放 → 用内置的自绘夜空 SVG（见 :func:`help_sky_svg`）：不联网、不依赖
#              第三方素材、没有版权问题。
# 注意：插件目录**自带**一张 assets/help/zz_builtin_poster.jpg 当默认头图；
# 文件名 zz_ 前缀排最后，所以用户自己放图就会顶替它，把目录清空才走夜空那条路。
#
# 两个踩过的坑，别改回去：
# 1. 字体栈带双引号（``"Microsoft YaHei"…``），**绝不能**直接塞进 ``style="…"``
#    属性里 —— HTML 解析器在第一个引号处就把属性截断了，后面的 position / color
#    全丢（本地踩过：卡片根元素掉回 ``static``，绝对定位的子元素改以视口为包含块、
#    右侧溢出 70px）。所以字体一律走 <style> 块里的 class，行内样式只写没有引号的值。
# 2. 夜空 hero 的随机数固定种子：同一张卡每次渲染必须长得一样，不然用户两次
#    ``/gal help`` 会拿到两张不同的星空。

HELP_ASSET_LIMIT = 4
_HELP_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 带引号，只能出现在 <style> 块里（见上面第 1 条）
_HELP_MONO = '"Cascadia Mono","Consolas","DejaVu Sans Mono","Microsoft YaHei",monospace'

# 用法卡自己的主色：这张卡是深色海报，暖金色最配画面；不用 output.accent_color
# （那个是给白底资料卡用的深色高亮，压到黑底上会看不见）。
_HELP_ACCENT = "#f0a860"
_HELP_HERO_H = 480  # 有图时的 hero 高度
_HELP_SKY_H = 470  # 自绘夜空的高度
_HELP_NAV = ("SEARCH", "NEWS", "REVIEW", "STYLE")
_HELP_SKY_SEED = 20240617

# 指令菜单正文。放这里而不是 plugin.py：改文案不用动命令分发逻辑
HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "找游戏",
        [
            ("/gal 85+ 10h", "中央值 ≥85、通关时长 ≤10 小时（最常用的组合）"),
            ("/gal 85-90 6-20h", "区间写法：分数 85~90、时长 6~20 小时；60- 表示 ≤60"),
            ("/gal v80+ tSLG y2015", "VNDB 分 ≥80 / 玩法 SLG / 2015 年起（含），可以叠着写"),
            ("/gal 类型 SLG", "按玩法类型挑（ADV、RPG、SLG、乙女…）"),
            ("/gal 年份 2015", "某年的排行（按批评空间中央值）"),
            ("/gal 人气", "按批评空间票数（数据数）排行"),
            ("/gal random 5", "随手来 5 部，抽签用"),
        ],
    ),
    (
        "最新情报",
        [
            ("/gal 情报 [条数]", "只要圈内新闻，不带发售列表"),
            ("/gal 预定 [天数]", "只要未来准备发售的（月幕日历 + VNDB，带厂商和汉化状态）"),
            ("/gal 新作 [天数]", "只要最近已经发售的，也可以写 /gal new（最多回看 50 天）"),
            ("/gal 详情 <序号>", "看第 N 条的整篇正文（也可以直接打 /gal 3）"),
        ],
    ),
    (
        "锐评与维护",
        [
            ("/锐评 <作品名>", "依据批评空间的评分与长文感想写一段（以聊天记录形式发出）"),
            ("/gal 状态", "数据源、本地索引、体检（哪张表写了多少行）· 管理员"),
            ("/gal update", "增量重抓榜单前几页（新作与分数变动）· 管理员"),
            ("/gal sync", "全量重建本地索引（第一次装或索引坏了再用）· 管理员"),
        ],
    ),
    (
        "贴吧话术",
        [
            ("/gal 话术 状态", "看看学到多少表达方式和黑话 · 管理员"),
            ("/gal 话术 刷新", "立刻去贴吧采一轮语料 · 管理员"),
            ("/gal 话术 打标 · 恢复 · 清理", "给行加/去吧名标签 · 把留档的语料写回 · 撤掉插件写过的行 · 管理员"),
            ("/gal 话术 体检", "语料到底写进哪张表、审核到哪一步 · 管理员"),
        ],
    ),
    (
        "也可以直接问",
        [
            ("「有没有 10 小时以内的催泪 gal」", "说个大概就行，不用记语法"),
            ("「最近 gal 圈有什么新闻」", "照样发一张卡片，不用记命令"),
        ],
    ),
]

HELP_NOTES = [
    "时长、分数都能用简写：85+ / 85-90 / 60- / 10h / 10h+ / h5-20 / v80+ / t标签 / y2015 / n5",
    "批评空间（EGS）是可选增量源：连不上或没开，插件只用月幕 + VNDB 照常工作",
    "贴吧话术默认关闭（配置 → 贴吧话术），要用再打开",
    "卡片发不出来会自动退回纯文本，功能不受影响",
]

# 用法卡上的机器人名字由宿主配置提供（bot.nickname）；拿不到就用不点名的写法
_HELP_TAGLINE = "报名字查资料，报条件挑游戏"
_HELP_INTRO = "下面 5 组是常用指令；懒得记也行，直接用大白话提问就行"
_HELP_INTRO_NAMED = "下面 5 组是常用指令；懒得记也行，直接用大白话提问，{name}会自己挑工具"


def local_image_data_uri(path: Any) -> str:
    """本地图片 → data URI。读不到就返回空串（卡片少一张图，不算错）。"""
    try:
        from pathlib import Path

        raw = Path(str(path)).read_bytes()
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    return f"data:{_guess_image_mime(raw)};base64,{base64.b64encode(raw).decode('ascii')}"


def help_asset_data_uris(folder: Any, *, limit: int = HELP_ASSET_LIMIT) -> list[str]:
    """把 ``assets/help`` 下的图片按文件名排序转成 data URI。

    宿主渲染 HTML 时不允许页面访问外网，所以只能是内嵌的 data URI；
    目录不存在、文件不是图片、读失败一律当「没放图」。
    """
    try:
        from pathlib import Path

        files = sorted(
            path
            for path in Path(str(folder)).iterdir()
            if path.is_file() and path.suffix.lower() in _HELP_ASSET_SUFFIXES
        )
    except Exception:  # noqa: BLE001
        return []
    uris: list[str] = []
    for path in files:
        if len(uris) >= limit:
            break
        uri = local_image_data_uri(path)
        if uri:
            uris.append(uri)
    return uris


def help_sky_svg(*, width: int = 830, height: int = _HELP_SKY_H) -> str:
    """自绘夜空 hero：渐变天幕 + 星星 + 四芒星 + 落日辉光 + 云带 + 远山 + 花瓣。

    纯 SVG 图元拼的：不联网、不带任何第三方素材、也没有版权问题，所以能随插件
    一起分发。随机数固定种子，保证每次渲染结果一致。
    """
    import random

    rnd = random.Random(_HELP_SKY_SEED)
    view_w = 830
    stars = "".join(
        f'<circle cx="{rnd.uniform(0, view_w):.1f}" cy="{rnd.uniform(0, 300):.1f}" '
        f'r="{rnd.uniform(.6, 1.6):.2f}" fill="#fff" opacity="{rnd.uniform(.15, .8):.2f}"/>'
        for _ in range(120)
    )
    sparks = ""
    for _ in range(7):
        x, y, s = rnd.uniform(30, view_w - 30), rnd.uniform(20, 260), rnd.uniform(5, 11)
        sparks += (
            f'<path d="M{x:.1f} {y - s:.1f} L{x + s * .22:.1f} {y:.1f} '
            f'L{x + s:.1f} {y:.1f} L{x + s * .22:.1f} {y + s:.1f} '
            f'L{x:.1f} {y + s:.1f} L{x - s * .22:.1f} {y:.1f} '
            f'L{x - s:.1f} {y:.1f} L{x - s * .22:.1f} {y - s:.1f} Z" '
            f'fill="#fff" opacity=".55"/>'
        )
    petals = ""
    for _ in range(38):
        px, py = rnd.uniform(0, view_w), rnd.uniform(120, 470)
        rx, ry = rnd.uniform(2, 4.6), rnd.uniform(1.4, 3)
        petals += (
            f'<ellipse cx="{px:.1f}" cy="{py:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" '
            f'transform="rotate({rnd.randint(0, 180)} {px:.1f} {py:.1f})" '
            f'fill="#ffe9ef" opacity="{rnd.uniform(.18, .5):.2f}"/>'
        )
    return (
        '<svg width="100%" height="100%" viewBox="0 0 830 470" '
        'preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg" '
        'style="position:absolute;inset:0;">'
        "<defs>"
        '<linearGradient id="hsky" x1="0" y1="0" x2="0.35" y2="1">'
        '<stop offset="0" stop-color="#080c1e"/><stop offset="0.34" stop-color="#22204e"/>'
        '<stop offset="0.62" stop-color="#5c3663"/><stop offset="0.82" stop-color="#b05a5c"/>'
        '<stop offset="1" stop-color="#f0a860"/></linearGradient>'
        '<radialGradient id="hsun" cx="0.5" cy="0.5" r="0.5">'
        '<stop offset="0" stop-color="#fff3d0" stop-opacity=".95"/>'
        '<stop offset="0.35" stop-color="#ffca7a" stop-opacity=".55"/>'
        '<stop offset="1" stop-color="#ff9a4a" stop-opacity="0"/></radialGradient>'
        '<linearGradient id="hhill" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#2a1c34"/><stop offset="1" stop-color="#120c1c"/>'
        "</linearGradient>"
        "</defs>"
        f'<rect width="830" height="470" fill="url(#hsky)"/>'
        + stars
        + sparks
        + '<ellipse cx="620" cy="392" rx="230" ry="150" fill="url(#hsun)"/>'
        '<circle cx="620" cy="392" r="34" fill="#fff0cc" opacity=".92"/>'
        + "".join(
            f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="{c}" opacity="{op}"/>'
            for cx, cy, rx, ry, c, op in (
                (180, 300, 260, 20, "#e7a9a0", .28),
                (520, 336, 330, 24, "#f3c08a", .30),
                (760, 288, 210, 16, "#9f7fb5", .26),
                (330, 396, 420, 26, "#c07a86", .22),
            )
        )
        + '<path d="M0 470 L0 424 L120 386 L250 428 L360 398 L520 440 L650 404 '
        'L790 442 L830 412 L830 470 Z" fill="url(#hhill)" opacity=".92"/>'
        + petals
        + "</svg>"
    )


def _help_css(width: int, accent: str, hero_h: int) -> str:
    """用法卡的 <style> 块。字体栈（带引号）只能放这里，见文件顶部第 1 条。"""
    cmd_w = max(190, int(width * 0.335))
    return (
        "<style>"
        "*{box-sizing:border-box;}"
        "body{margin:0;padding:0;background:transparent;}"
        f"#card{{width:{width}px;background:#0b0d14;color:#fff;"
        f"font-family:{FONT_STACK};overflow:hidden;}}"
        f".hc-hero{{position:relative;height:{hero_h}px;overflow:hidden;}}"
        ".hc-fill{position:absolute;inset:0;background-size:cover;background-position:50% 42%;}"
        ".hc-scrim{position:absolute;inset:0;background:linear-gradient(180deg,"
        "rgba(9,11,17,.60) 0%,rgba(9,11,17,.16) 26%,rgba(9,11,17,.52) 60%,"
        "rgba(11,13,20,.94) 88%,#0b0d14 100%);}"
        # 标题区再压一层局部暗角：海报可能很亮，光靠全局暗罩不一定压得住字
        ".hc-veil{position:absolute;left:0;right:0;bottom:0;height:220px;"
        "background:linear-gradient(0deg,rgba(6,8,13,.80),rgba(6,8,13,0));}"
        ".hc-top{position:absolute;top:22px;left:28px;right:28px;display:flex;"
        "justify-content:space-between;align-items:center;}"
        f".hc-brand{{font-family:{_HELP_MONO};font-size:10.5px;letter-spacing:2.6px;"
        "color:rgba(255,255,255,.85);border:1px solid rgba(255,255,255,.34);padding:3px 9px;}"
        f".hc-nav{{font-family:{_HELP_MONO};font-size:10.5px;letter-spacing:2px;"
        "color:rgba(255,255,255,.66);}"
        ".hc-nav i{font-style:normal;opacity:.35;}"
        ".hc-brand,.hc-nav{text-shadow:0 1px 6px rgba(0,0,0,.85);}"
        ".hc-titles{position:absolute;left:28px;right:28px;bottom:28px;}"
        ".hc-kicker,.hc-title,.hc-tag,.hc-ver{text-shadow:0 1px 7px rgba(0,0,0,.8);}"
        f".hc-kicker{{font-family:{_HELP_MONO};font-size:11px;letter-spacing:4.5px;"
        "color:rgba(255,236,210,.85);}"
        ".hc-title{margin-top:11px;font-size:39px;font-weight:800;letter-spacing:1px;"
        "line-height:1.12;text-shadow:0 3px 20px rgba(0,0,0,.72);}"
        ".hc-tagline{margin-top:13px;display:flex;align-items:center;gap:12px;}"
        f".hc-dash{{flex:none;width:52px;height:2px;background:{accent};}}"
        ".hc-tag{font-size:12.5px;color:rgba(255,255,255,.78);}"
        f".hc-ver{{margin-left:auto;font-family:{_HELP_MONO};font-size:11px;"
        "color:rgba(255,255,255,.62);}"
        f".hc-hair{{position:absolute;left:0;right:0;bottom:0;height:2px;"
        f"background:linear-gradient(90deg,{accent},rgba(240,168,96,.05));}}"
        ".hc-panel{padding:26px 30px 24px;}"
        ".hc-intro{font-size:12.5px;line-height:1.7;color:rgba(255,255,255,.52);"
        "margin-bottom:22px;}"
        ".hc-sec{display:flex;align-items:center;gap:11px;margin:0 0 6px;}"
        f".hc-num{{font-family:{_HELP_MONO};font-size:11px;letter-spacing:1.5px;color:{accent};}}"
        ".hc-secname{font-size:14.5px;font-weight:700;color:#fff;letter-spacing:.6px;}"
        f".hc-rule{{flex:1;height:1px;background:linear-gradient(90deg,{accent},"
        "rgba(255,255,255,.05));}"
        ".hc-row{display:flex;gap:20px;align-items:baseline;padding:8px 0;"
        "border-top:1px solid rgba(255,255,255,.07);}"
        f".hc-cmd{{flex:none;width:{cmd_w}px;font-family:{_HELP_MONO};font-size:13.5px;"
        "font-weight:700;color:#ffe6c2;}"
        ".hc-desc{flex:1;font-size:12.5px;line-height:1.6;color:rgba(255,255,255,.58);}"
        ".hc-gap{height:20px;}"
        ".hc-foot{margin-top:6px;padding-top:14px;border-top:1px solid rgba(255,255,255,.12);}"
        ".hc-note{margin-top:6px;font-size:11.5px;line-height:1.6;color:rgba(255,255,255,.5);}"
        f".hc-src{{margin-top:12px;display:flex;justify-content:space-between;"
        f"align-items:flex-end;font-family:{_HELP_MONO};font-size:10.5px;"
        "letter-spacing:.6px;color:rgba(255,255,255,.34);}"
        ".hc-strip{margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;}"
        ".hc-strip img{width:172px;height:112px;object-fit:cover;border-radius:10px;"
        "border:1px solid rgba(255,255,255,.22);}"
        ".hc-cap{margin-top:8px;font-size:11px;color:rgba(255,255,255,.42);}"
        "</style>"
    )


def _help_hero_html(
    *,
    width: int,
    height: int,
    image_uri: str,
    title: str,
    greeting: str,
    version: str,
) -> str:
    """上半幅主视觉：背景图（或自绘夜空）+ 暗角 + 顶部导航 + 左下标题 + 底部发丝线。"""
    if image_uri:
        # base64 里不会有引号，塞进 url() 是安全的
        background = f'<div class="hc-fill" style="background-image:url({image_uri});"></div>'
    else:
        background = help_sky_svg(width=width, height=height)
    nav = '<div class="hc-nav">' + " <i>·</i> ".join(_HELP_NAV) + "</div>"
    version_html = f'<span class="hc-ver">v{_esc(version)}</span>' if version else ""
    return (
        f'<div class="hc-hero" style="height:{height}px;">'
        + background
        + '<div class="hc-scrim"></div><div class="hc-veil"></div>'
        + '<div class="hc-top"><span class="hc-brand">USAGE</span>' + nav + "</div>"
        + '<div class="hc-titles">'
        '<div class="hc-kicker">GALGAME FIELD MASTER</div>'
        f'<div class="hc-title">{_esc(title)}</div>'
        '<div class="hc-tagline"><span class="hc-dash"></span>'
        f'<span class="hc-tag">{_esc(greeting)}</span>'
        + version_html
        + "</div></div>"
        '<div class="hc-hair"></div>'
        "</div>"
    )


def _help_panel_html(
    *,
    intro: str,
    notes: list[str],
    gallery: list[str] | None = None,
) -> str:
    """下半幅信息面板：5 组指令 + 插图条（有的话）+ 脚注 + 来源行。"""
    parts: list[str] = [f'<div class="hc-panel"><div class="hc-intro">{_esc(intro)}</div>']
    for index, (name, items) in enumerate(HELP_SECTIONS, 1):
        parts.append(
            '<div class="hc-sec">'
            f'<span class="hc-num">{index:02d}</span>'
            f'<span class="hc-secname">{_esc(name)}</span>'
            '<span class="hc-rule"></span></div>'
        )
        for cmd, desc in items:
            parts.append(
                '<div class="hc-row">'
                f'<div class="hc-cmd">{_esc(cmd)}</div>'
                f'<div class="hc-desc">{_esc(desc)}</div></div>'
            )
        parts.append('<div class="hc-gap"></div>')
    if gallery:
        parts.append(
            '<div class="hc-strip">'
            + "".join(f'<img src="{uri}" />' for uri in gallery)
            + "</div>"
            '<div class="hc-cap">插图位：往插件目录 assets/help/ 丢图片（png/jpg/gif/webp），'
            "第一张当主视觉，后面的排在这里</div>"
        )
    parts.append(
        '<div class="hc-foot">'
        + "".join(f'<div class="hc-note">· {_esc(line)}</div>' for line in notes)
        + '<div class="hc-src"><span>SOURCE: YMGal · VNDB · BugBug · EROGAME</span>'
        "<span>THE STORY CONTINUES</span></div></div></div>"
    )
    return "".join(parts)


def build_help_html(
    *,
    version: str = "",
    accent: str = _HELP_ACCENT,
    width: int = 830,
    assets: list[str] | None = None,
    notes: list[str] | None = None,
    title: str = "galgame 领域大神",
    greeting: str = "",
    intro: str = "",
    bot_name: str = "",
) -> str:
    """``/gal help`` 的用法卡（galgame 启动画面风）。取图不联网：图都是 data URI。

    ``assets`` 是已经转好的 data URI 列表（见 help_asset_data_uris）：
    第一张当 hero 主视觉，其余的排在面板底部；传空就用内置的自绘夜空。

    ``bot_name`` 是部署方自己的机器人昵称（调用方从宿主配置读）：有就写进副标题
    和开头一句，没有就用不点名的文案 —— 卡片里不写死任何人的机器人名字。
    """
    name = str(bot_name or "").strip()
    if not greeting:
        greeting = f"{name} · {_HELP_TAGLINE}" if name else _HELP_TAGLINE
    if not intro:
        intro = _HELP_INTRO_NAMED.format(name=name) if name else _HELP_INTRO
    assets = [uri for uri in (assets or []) if uri]
    hero_uri = assets[0] if assets else ""
    gallery = assets[1:]
    hero_h = _HELP_HERO_H if hero_uri else _HELP_SKY_H
    note_lines = list(notes if notes is not None else HELP_NOTES)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        + _help_css(width, accent, hero_h)
        + "</head>"
        '<body><div id="card">'
        + _help_hero_html(
            width=width,
            height=hero_h,
            image_uri=hero_uri,
            title=title,
            greeting=greeting,
            version=version,
        )
        + _help_panel_html(intro=intro, notes=note_lines, gallery=gallery)
        + "</div></body></html>"
    )
