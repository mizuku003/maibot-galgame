"""galgame领域大神 —— 让麦麦变成懂 gal 的老登。

三个源各管一段：

- **月幕（ymgal）**：中文名、**有没有汉化**、外号检索（「罚抄」→ Rewrite）、中文简介、新作日历；
- **VNDB**：通关时长、内容标签树、多语言标题、开发商、角色与声优；
- **批评空间（EGS）**：中央値、**好评率**、積んでる率（积压率）、面白くなってきた時間、
  用户评价维度（好在哪 / 差在哪）、短评 —— "这游戏到底怎么样"全靠它。

设计取舍：

1. **工具克制到两个**，且都声明成 deferred（渐进式披露）—— 没被发现时只占 system-reminder
   一行，模型明确要找资料/找新作时才拿到完整定义。查找、筛选、推荐本质是同一件事，
   合并成 ``gal_search``：给名字就是查资料，给条件就是筛作品；``gal_news``
   管新闻与即将发售。随机这类"玩票"功能放命令里，不占模型注意力；
   而 ``/gal <条件>`` 提供的是**不经过模型**的精确控制 ——
   模型可能把你的话理解偏，想自己拿捏条件时就走命令（两者共用同一套 find()）。
2. **不合并评分**。VNDB 是贝叶斯分、EGS 是中央值，口径不同，卡片上并列显示、不做加权合并。
3. **缺源不翻车**。任一源关掉、超时或没收录，只是卡片上少一块；
   批评空间尤其脆弱（没 API、搜索被反爬、还要连得上），它挂了插件照常工作。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os as _os
import re
import sys as _sys
import time
from pathlib import Path
from typing import Any

# 问题里出现这些字眼时自动去取角色/声优，不指望模型每次都记得传 with_characters
_CHARACTER_HINT = re.compile(r"声优|聲優|配音|谁配|CV\b|キャスト|角色|人物|出演", re.IGNORECASE)

# /gal 详情 <序号> 的记忆时长与条数上限（只记最近一次列出的那一屏）
_INTEL_CACHE_TTL = 1800.0
_INTEL_CACHE_MAX = 16

# 插件调能力走的是 RPC，默认超时 30 秒；用户把 llm 任务挂在慢模型上时，
# 一次生成经常要 30~60 秒，会直接吃到 [E_TIMEOUT]。这里显式放宽。
# 这个 kwarg 会被 SDK 的 call_capability 认成 RPC 超时，不会传给宿主的能力实现。
_LLM_RPC_TIMEOUT_MS = 180_000

# --- `/gal <条件>` 的条件解析 -------------------------------------------------
#
# 宿主把命令正则的**命名捕获组**（m.groupdict()）交给插件，所以自由文本必须整体
# 塞进一个命名组里，再由这里自己切词 —— 别指望用正则去拆条件。
#
# 一个条件 token 形如「键 运算符 值」，运算符可以是 = >= <= > <。
# 键放开到「非空白且不含运算符」，这样打错的中文键（「评分=85」）会走到
# 「不认识的条件」报错，而不是被静默当成关键词 —— 静默忽略会让用户以为条件生效了。
_FILTER_TOKEN = re.compile(r"^(?P<key>[^\s=<>]+)(?P<op>>=|<=|>|<|=)(?P<value>.+)$")
# 值可以是「单个数」或「区间 a-b」
_FILTER_RANGE = re.compile(r"^(?P<low>\d+(?:\.\d+)?)\s*[-~]\s*(?P<high>\d+(?:\.\d+)?)$")


def _to_halfwidth(text: str) -> str:
    """全角 → 半角。

    中文输入法下很容易打出全角的字母、数字和符号（`ｅｇｓ＝８５`），
    不折一下的话整条条件会被当成关键词，用户却以为筛选生效了。
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            # 全角 ！ 到 ～ 与半角一一对应，直接平移
            out.append(chr(code - 0xFEE0))
        elif ch == "、":
            out.append(",")
        else:
            out.append(ch)
    return "".join(out)

# 这三类支持区间与单边，键 → (下限参数名, 上限参数名)
_FILTER_RANGED_KEYS = {
    "egs": ("min_egs", "max_egs"),
    "vndb": ("min_rating", "max_rating"),
    "hours": ("min_hours", "max_hours"),
}
# 其余是单值；键 → gal_search 的参数名
_FILTER_SIMPLE_KEYS = {
    "year": "year",
    "votes": "votes",
    "count": "count",
    "offset": "offset",
    "sort": "sort",
    "tags": "tags",
    "name": "name",
    "query": "name",
}

# 中文输入法很容易打出全角符号，解析前先用 _to_halfwidth 折成半角

# `/锐评 "作品名"` 里可能包着引号，中英文、全半角都要认
_QUOTE_CHARS = "\"'“”‘’「」『』《》"


def _strip_quotes(text: str) -> str:
    """去掉首尾的引号 —— 用户很自然会写成 `/锐评 "苍之彼方的四重奏"`。"""
    return (text or "").strip().strip(_QUOTE_CHARS).strip()


# `/锐评` 的结果包成一条「聊天记录」（合并转发）发出去：
# 几百字的评论直接刷屏，还会被别人的消息插断；塞进聊天记录里只占一个气泡，
# 想看的人点开才是完整的一段。段落怎么切见 _split_review_nodes。
_REVIEW_NODE_CHARS = 200
_REVIEW_MAX_NODES = 6
# 读不到宿主昵称时的兜底发言人，就是插件自己的显示名
_BOT_NICKNAME_FALLBACK = "galgame领域大神"
_bot_nickname_cache: str | None = None
_bot_persona_cache: str | None = None

# 详情正文翻译一次喂多少字：gl_news.translate_text 默认只吃 900 字，
# 整篇丢进去会被静默截断（后半段留日文）。这里自己切块，一块一调。
_DETAIL_CHUNK_CHARS = 900


async def _bot_nickname(ctx) -> str:
    """机器人昵称，用作聊天记录里的发言人。

    走宿主的配置能力读「bot.nickname」（官方要求：插件不要自己按宿主的目录
    结构拼路径）。读不到就退回插件显示名 —— 这只是气泡上的署名，不值得让
    锐评发不出去；读失败不写缓存，下次还能再试。
    """
    global _bot_nickname_cache
    if _bot_nickname_cache is not None:
        return _bot_nickname_cache
    name = ""
    try:
        name = str(await ctx.config.get("bot.nickname", "") or "").strip()
    except Exception:  # noqa: BLE001 —— 署名而已，读不到不值得吵
        name = ""
    if not name:
        return _BOT_NICKNAME_FALLBACK
    _bot_nickname_cache = name
    return name


async def _bot_persona(ctx) -> str:
    """宿主「人格设定」（personality.personality），给锐评当口吻用。

    和昵称一样走配置能力读，不自己拼宿主路径；人格是多行文本，压成一行再
    塞进提示词，省得把模板撑开。读不到返回空串，调用方继续用插件自己的
    「锐评口吻」；读失败不写缓存，下次还能再试。
    """
    global _bot_persona_cache
    if _bot_persona_cache is not None:
        return _bot_persona_cache
    try:
        raw = await ctx.config.get("personality.personality", "")
    except Exception:  # noqa: BLE001 —— 读不到就用自己的口吻
        return ""
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    _bot_persona_cache = text
    return text


async def _host_bot_name(ctx) -> str:
    """宿主配置里的机器人昵称；读不到返回空串（用法卡就用不点名的写法）。

    和 _bot_nickname 的区别：那个是合并转发里的**发言人署名**，读不到必须顶上
    插件显示名，否则气泡没有署名；这里只是卡片文案，宁可留白，也不要把插件名
    当成机器人名写进副标题。
    """
    name = await _bot_nickname(ctx)
    return "" if name == _BOT_NICKNAME_FALLBACK else name


# ── 维护类命令的权限 ───────────────────────────────────────────────
# SDK 里**没有权限 API**（`@Command(..., permission="operator")` 只是会被忽略的
# kwargs），宿主自己那套判定在 `src/core/local_operator.py`：主程序终端
# （local operator）直接放行，其余看 `[plugin] permission` 里的 `platform:user_id`。
# 插件能用 `config.get` 读到宿主全局配置，所以这里照抄同一份数据，
# 让「谁能维护」的答案和宿主的 `/pm` 完全一致。
_MAINTENANCE_COMMAND = "mizuku003.galgame.maintenance"
# 会动索引 / 宿主数据库 / 贴吧语料的子命令，只有管理员能用。
# 命令词和用法卡（`/gal help`）上写的一一对应，别再往这里加同义词。
_MAINTENANCE_SUBS = frozenset({"sync", "update", "状态", "话术"})
_MAINTENANCE_DENIED = (
    "这条命令只有管理员能用（维护和贴吧话术会动本地索引和宿主数据库）。\n"
    "让管理员把你的账号加进宿主配置的 [plugin] permission，"
    "或者直接用主程序终端发这条命令。"
)


def _scoped_user_id(platform: Any, user_id: Any) -> str:
    """`platform:user_id`。平台名转小写、user_id 原样（和宿主的拼法一致）。"""
    plat = str(platform or "").strip().lower()
    uid = str(user_id or "").strip()
    if not plat or not uid:
        return ""
    return f"{plat}:{uid}"


def _permission_set(values: Any) -> set[str]:
    """把配置里的一串 `platform:user_id` 规范成集合；形状不对就当空集。"""
    allowed: set[str] = set()
    if not isinstance(values, (list, tuple, set, frozenset)):
        return allowed
    for value in values:
        text = str(value or "")
        if ":" not in text:
            continue
        plat, uid = text.split(":", 1)
        scoped = _scoped_user_id(plat, uid)
        if scoped:
            allowed.add(scoped)
    return allowed


def _allowed_chat_set(values: Any) -> set[str]:
    """`allow_chats` 里的聊天流 ID 集合；形状不对就当空集。"""
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {str(value).strip() for value in values if str(value or "").strip()}


def _hard_wrap(text: str, limit: int) -> list[str]:
    """把一段长文本按句读断成若干条，尽量不断在句子中间。"""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(ch) for ch in "。！？!?；;…\n")
        if cut < limit // 2:  # 这一段几乎没有句读，只能硬切
            cut = limit - 1
        pieces.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :].lstrip()
    if rest:
        pieces.append(rest)
    return [piece for piece in pieces if piece]


def _split_review_nodes(text: str) -> list[str]:
    """把锐评切成聊天记录里的几条消息。

    模型一般按空行分段，就先顺着段落切；太碎的段落攒到一起，
    单段超过 _REVIEW_NODE_CHARS 再按句读硬断，最后把条数收敛到
    _REVIEW_MAX_NODES —— 气泡太多反而不如一段整文好看。
    """
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    if not paragraphs:
        return []
    merged: list[str] = []
    for para in paragraphs:
        if merged and len(merged[-1]) + len(para) + 1 <= _REVIEW_NODE_CHARS:
            merged[-1] = merged[-1] + "\n" + para
        else:
            merged.append(para)
    nodes: list[str] = []
    for para in merged:
        nodes.extend(_hard_wrap(para, _REVIEW_NODE_CHARS))
    if len(nodes) > _REVIEW_MAX_NODES:
        head = nodes[: _REVIEW_MAX_NODES - 1]
        head.append("\n".join(nodes[_REVIEW_MAX_NODES - 1 :]))
        nodes = head
    return nodes


def _split_text_chunks(text: str, limit: int) -> list[str]:
    """把一整篇正文切成几块，供分块翻译（每块不超过 limit 字）。

    先按空行分段，能攒进上一块的就攒；单段超长再用 ``_hard_wrap`` 按句读硬断。
    """
    if limit <= 0:
        return [text] if text else []
    chunks: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        piece = para.strip()
        if not piece:
            continue
        for part in _hard_wrap(piece, limit):
            if chunks and len(chunks[-1]) + len(part) + 2 <= limit:
                chunks[-1] = chunks[-1] + "\n\n" + part
            else:
                chunks.append(part)
    return chunks


def _score_text(value: Any) -> str:
    """EGS 评论的得分前缀，形如 ``88点``；没抓到分数就返回空串（别写出「None点」）。"""
    try:
        return f"{int(value)}点"
    except (TypeError, ValueError):
        return ""


# 素材块的标题：只是给模型看的参考，**不进配置面板**（提示词里保持干净），
# 万一被模型抄进正文，也会被 _clean_review_output 整行滤掉。
_MATERIAL_HEADER = "（下面是给你的参考资料，不要写进正文）"
_REVIEW_LEAK_RE = re.compile(r"参考资料|作品素材|核对用的数字")


def _clean_review_output(text: str) -> str:
    """删掉模型顺手复述出来的内部标记（分隔线、素材块标题、字段名）。

    这些东西只该待在提示词里；模型偶尔会把「=== 核对用的数字 ===」抄进正文，
    用户看到的就是「露馅」。只删整行的标记，不动正文里的句子。
    """
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if line.startswith("===") or line.startswith("---"):
            continue
        if len(line) <= 60 and _REVIEW_LEAK_RE.search(line):
            continue
        lines.append(raw.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _render_review_prompt(template: str, *, persona: str, max_chars: int, material: str) -> str:
    """把配置面板里的提示词模板套上人设 / 篇幅 / 素材。

    两条兜底，都是为了让「配置里写错一个字符」不至于让命令整条发不出去：
    - 模板里没写 `{material}`（默认模板就故意不写）→ 素材照样追在末尾。
      素材是这个功能的命根子，漏了它模型就只能瞎编；而且素材在面板里看不见，
      提示词里只留「怎么说话」，不摆资料骨架。
    - 出现不认识的占位符（比如手滑写了 `{title}`）→ 原样留着，不抛异常。
    """
    values = {"persona": persona, "max_chars": max_chars, "material": material}
    text = str(template or "").strip()
    try:
        text = text.format(**values)
    except (KeyError, IndexError, ValueError):
        # 有不认识的占位符 / 落单的大括号：只替换我们认识的，其余保持原样
        for key in ("persona", "max_chars", "material"):
            text = text.replace("{%s}" % key, str(values[key]))
    text = text.replace("{material}", material)
    if material and material not in text:
        text = f"{text}\n\n{_MATERIAL_HEADER}\n{material}" if text else material
    return text


def _hours_hint(params: dict[str, Any]) -> str:
    """识别「本想筛时长、却写成了分数」这个常见笔误。

    裸数字默认是**批评空间分数**（``85+`` = 85 分以上），时长要加 ``h``。
    但 ``10+ 20-`` 这种写法很像是想说「10~20 小时」——
    而批评空间 10~20 分这一段几乎没作品，用户只会看到「没筛出作品」，
    根本猜不到是自己少写了个 ``h``。所以这里主动点出来。
    """
    if params.get("min_hours") or params.get("max_hours"):
        return ""
    lo = float(params.get("min_egs") or 0)
    hi = float(params.get("max_egs") or 0)
    if not (lo or hi):
        return ""
    # 没人会按「40 分以下」去找作品，出现这种下限基本就是单位写错了
    if (lo and lo < 40) or (hi and hi < 40):
        span = f"{lo:g}~{hi:g}" if lo and hi else (f"≥{lo:g}" if lo else f"≤{hi:g}")
        return (
            f"提示：裸数字默认按**批评空间分数**理解（当前 = 中央值 {span}）。"
            "想筛通关时长请加 h，例如 10h+ 20h-"
        )
    return ""


# EGS 的「玩法类型」（attlist.php 的ジャンル组）。
# VNDB 的标签体系里**没有这一套**，所以「想找个 SRPG」「不要纯视觉小说」这类需求
# 只能靠它 —— 这也是批评空间唯一不可替代的筛选维度。
_ATT_GENRES: dict[str, int] = {
    "vn": 56, "adv": 56, "avg": 56, "ビジュアルノベル": 56, "视觉小说": 56, "ノベル": 56,
    "rpg": 6, "srpg": 6, "jrpg": 6, "角色扮演": 6,
    "slg": 5, "策略": 5, "模拟": 5, "养成模拟": 5,
    "act": 13, "action": 13, "动作": 13,
    "stg": 18, "shooting": 18, "射击": 18, "弹幕": 18,
    "3d": 15, "三维": 15,
    "tbl": 20, "table": 20, "桌面": 20, "桌上": 20,
    "麻雀": 14, "麻将": 14, "mahjong": 14,
    "育成": 74, "养成": 74, "育成モノ": 74,
    "乙女": 82, "乙女ゲーム": 82, "otome": 82, "女性向": 82,
    "bl": 22, "ボーイズラブ": 22, "耽美": 22, "boyslove": 22,
    "kinetic": 36, "キネティックノベル": 36, "动态小说": 36,
    "打字": 90, "タイピング": 90, "typing": 90,
    "etc": 19, "其它": 19, "其他": 19,
}
_ATT_GENRE_HELP = (
    "VN(视觉小说) · RPG · SLG · ACT · STG · 3D · TBL(桌面) · 麻雀 · 育成 · "
    "乙女 · BL · Kinetic(动态小说) · 打字 · ETC"
)


_FILTER_HELP = (
    "用法：/gal <条件…>（空格分隔，不用写「筛选」）\n"
    "· 方向符统一在**尾部**：+ 是以上、- 是以下（跟 85+ 对称）\n"
    "    85   或  85+     85 分以上\n"
    "    85-90            85~90 分\n"
    "    60-              60 分以下（找烂作）\n"
    "    10h  或  h10     10 小时以内（短篇）\n"
    "    10h+             10 小时以上\n"
    "· 混着用：\n"
    "    85+ 10h          85 分以上、10 小时以内\n"
    "    85+ 泣系          85 分以上 + 关键词\n"
    "    v80+             改用 VNDB 分（≥80）\n"
    "    t泣系,Mystery    标签 · y2015 年份 · n5 条数\n"
    "· 要精确控制也可以用键=值：\n"
    "    egs=85-90 hours=5-20 count=5 sort=egs offset=5\n"
    "说明：结果默认按人气（票数）排序，想按分数排加 sort=egs；"
    "要锁定某一档请写区间（如 75-80）。"
)


def _parse_bound(op: str, value: str) -> tuple[float, float]:
    """把「运算符 + 值」解成 ``(下限, 上限)``，``0`` 表示不限。"""
    rng = _FILTER_RANGE.match(value)
    if rng:
        low, high = float(rng.group("low")), float(rng.group("high"))
        if low > high:
            low, high = high, low
        return low, high
    try:
        num = float(value)
    except ValueError as exc:
        raise ValueError(f"「{value}」不是数字") from exc
    if op in (">=", ">"):
        return num, 0.0
    if op in ("<=", "<"):
        return 0.0, num
    # `=` 配单个数按**下限**理解 —— 「egs=85」就是「85 分以上」的直觉写法，
    # 要上界请显式写 `egs<=60`，要区间请写 `egs=75-80`。
    return num, 0.0


# --- 简写语法 ---------------------------------------------------------------
#
# `>=` 在手机和 QQ 上很难打，所以给常用条件配一套符号更少的写法。
# 这里只认「看起来就是条件」的 token，认不出来一律交回关键词 ——
# 简写是便利，不该把普通搜索词也吃掉（比如 `9-nine-` 必须还能当关键词）。
_SH_PLAIN = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\+?$")          # 85 / 85+
_SH_RANGE = re.compile(r"^(?P<low>\d+(?:\.\d+)?)\s*[-~]\s*(?P<high>\d+(?:\.\d+)?)$")  # 85-90
_SH_LEADING_MINUS = re.compile(r"^-\d+(?:\.\d+)?$")             # -60（旧写法，现在要报错）
_SH_LEADING_NUM = re.compile(r"^-?\d")


def _parse_shorthand(token: str) -> dict[str, Any] | None:
    """解析简写条件；**认不出来返回 None**，由调用方当关键词处理。

    支持的写法（维度默认是批评空间中央值）：

    - ``85`` / ``85+``  → 批评空间 ≥ 85（分数越高越好，裸数字自然是下限）
    - ``85-90``         → 批评空间 85~90
    - ``60-``           → 批评空间 ≤ 60（找烂作）
    - ``10h`` / ``h10`` / ``10h-`` → **时长 ≤ 10 小时**（「10 小时的短篇」的直觉）
    - ``10h+``          → 时长 ≥ 10 小时
    - ``h5-20``         → 时长 5~20 小时
    - ``v80+`` / ``v50-`` → 换成 VNDB 评分
    - ``t泣系,Mystery``  → 标签 · ``y2015`` 年份下限 · ``n5`` 条数

    方向符统一放在**尾部**：``+`` 是以上、``-`` 是以下 ——
    和 ``85+`` 对称，比 ``-60`` 那种前缀写法好打也好记。
    """
    body = token
    dim = "egs"

    # 先摘掉尾部的方向符，否则 `10h+` / `10h-` 里的 `h` 不在末尾，后缀判断看不见它
    has_plus = body.endswith("+")
    has_minus = (not has_plus) and body.endswith("-")
    core = body[:-1] if (has_plus or has_minus) else body
    lowered = core.lower()

    # 维度前缀：h=时长、v=VNDB；后面必须跟数字（可带负号），否则不算条件
    if lowered[:1] == "h" and _SH_LEADING_NUM.match(core[1:]):
        dim, core = "hours", core[1:]
    elif lowered[:1] == "v" and _SH_LEADING_NUM.match(core[1:]):
        dim, core = "vndb", core[1:]
    # 维度后缀：10h
    elif lowered[-1:] == "h" and re.fullmatch(r"\d+(?:\.\d+)?", core[:-1]):
        dim, core = "hours", core[:-1]
    elif lowered[:1] == "t" and len(core) > 1:
        # 「t + 标签」是用法卡上写着的写法（/gal v80+ tSLG y2015）。但只看首字母的话，
        # 「/gal tsukihime」这种以 t 开头的作品名会被整词当成标签吃掉（query 变空、静默搜不到）。
        # 所以要求标签部分**看起来像标签**：含逗号、或含大写字母 / 非 ASCII 字符。
        _tag_part = core[1:]
        if "," in _tag_part or any(ch.isupper() or ord(ch) > 127 for ch in _tag_part):
            return {"tags": [t.strip() for t in _tag_part.split(",") if t.strip()]}
    elif lowered[:1] == "y" and core[1:].isdigit():
        return {"year": int(core[1:])}
    elif lowered[:1] == "n" and core[1:].isdigit():
        return {"count": int(core[1:])}

    low_field, high_field = _FILTER_RANGED_KEYS[dim]

    rng = _SH_RANGE.match(core)
    if rng:
        low, high = float(rng.group("low")), float(rng.group("high"))
        if low > high:
            low, high = high, low
        return {low_field: low, high_field: high}

    # 后缀减号 = 上限
    if has_minus:
        plain = _SH_PLAIN.match(core)
        return {high_field: float(plain.group("num"))} if plain else None

    # 旧的前缀写法 `-60`：直接报错并给出新写法，别让它静默退化成关键词搜索
    if _SH_LEADING_MINUS.match(core):
        suggested = token.replace("-", "", 1)
        if not suggested.endswith("-"):
            suggested += "-"
        raise ValueError(f"「{token}」的上限请用后缀减号，写成「{suggested}」")

    plain = _SH_PLAIN.match(core)
    if plain:
        num = float(plain.group("num"))
        # 评分是 0~100，超过 100 就不可能是分数 —— 多半是个数字开头的作品名（「428」）。
        # 这种情况交回关键词，别把它变成一个必然搜空的分数条件。
        if dim in ("egs", "vndb") and num > 100:
            return None
        # 分数：裸数字是**下限**（「85 分」= 85 分以上）
        # 时长：裸数字是**上限**（「10 小时」= 10 小时以内的短篇）
        # 想反过来就显式加 `+`：`10h+` = 10 小时以上
        if dim == "hours" and not has_plus:
            return {high_field: num}
        return {low_field: num}
    return None


def parse_filter(raw: str) -> dict[str, Any]:
    """把 `/gal <条件>` 的条件串解析成 ``gal_search`` 的参数。

    解析不出来就抛 :class:`ValueError`，由调用方回一句人话 + 帮助。
    不认识的键也抛错而不是静默忽略 —— 静默忽略会让用户以为条件生效了。
    """
    params: dict[str, Any] = {}
    words: list[str] = []
    for token in _to_halfwidth(raw or "").split():
        match = _FILTER_TOKEN.match(token)
        if not match:
            # 不是「键=值」就先试简写（`85+` / `10h` / `-60`），再退成关键词
            shorthand = _parse_shorthand(token)
            if shorthand:
                params.update(shorthand)
            else:
                words.append(token)
            continue
        key = match.group("key").lower()
        op = match.group("op")
        value = match.group("value").strip()
        if key in _FILTER_RANGED_KEYS:
            low_field, high_field = _FILTER_RANGED_KEYS[key]
            low, high = _parse_bound(op, value)
            if low:
                params[low_field] = low
            if high:
                params[high_field] = high
            continue
        if key not in _FILTER_SIMPLE_KEYS:
            raise ValueError(f"不认识的条件「{key}」")
        field = _FILTER_SIMPLE_KEYS[key]
        if field == "tags":
            params["tags"] = [t.strip() for t in value.split(",") if t.strip()]
        elif field in ("sort", "name"):
            params[field] = value
        else:
            try:
                params[field] = int(float(value))
            except ValueError as exc:
                raise ValueError(f"「{key}」需要一个数字，收到「{value}」") from exc
    if words:
        merged = " ".join(words)
        params["query"] = f"{params.get('query', '')} {merged}".strip()
    return params


# 插件目录名带连字符也没关系：把自身目录塞进 sys.path，配合 gl_ 前缀的模块名导入。
_PLUGIN_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _PLUGIN_DIR not in _sys.path:
    _sys.path.insert(0, _PLUGIN_DIR)


def _read_manifest_version() -> str:
    """用法卡上要显示的版本号，直接读 _manifest.json（读不到就不显示）。"""
    try:
        with open(_os.path.join(_PLUGIN_DIR, "_manifest.json"), encoding="utf-8") as handle:
            return str(json.load(handle).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


_PLUGIN_VERSION = _read_manifest_version()

from maibot_sdk import Command, MaiBotPlugin, Tool  # noqa: E402

from gl_config import DEFAULT_REVIEW_PROMPT, GalgameConfig  # noqa: E402
from gl_egs import EgsClient  # noqa: E402
from gl_http import HttpClient, similarity  # noqa: E402
from gl_merge import MAX_OFFSET, GalgameService, GameInfo  # noqa: E402
from gl_render import (  # noqa: E402
    NEWS_SOURCE_LABELS as NEWS_LABELS,
    build_info_html,
    build_recommend_html,
    build_search_lines,
)
import gl_news  # noqa: E402
import gl_style  # noqa: E402
from gl_tieba import TiebaClient  # noqa: E402
from gl_vndb import VndbClient  # noqa: E402
from gl_ymgal import YmgalClient  # noqa: E402


class GalgamePlugin(MaiBotPlugin):
    """galgame领域大神插件。"""

    config_model = GalgameConfig

    # -- 生命周期 ---------------------------------------------------------

    async def on_load(self) -> None:
        # 宿主在 on_load 失败时**不会**调 on_unload（runner_main 直接 unregister），
        # 已经建好的 http 客户端就永远没人关了；这里兜一层，先把客户端收掉再往外抛。
        try:
            await self._on_load_inner()
        except Exception:
            await self._close_http()
            raise

    async def _on_load_inner(self) -> None:
        self._http: HttpClient | None = None
        self._egs_http: HttpClient | None = None
        self._service: GalgameService | None = None
        self._index_task: asyncio.Task | None = None
        self._tieba: TiebaClient | None = None
        self._style_ledger: gl_style.StyleLedger | None = None
        self._style_task: asyncio.Task | None = None
        self._style_sync_task: asyncio.Task | None = None
        # 后台循环与「/gal 话术 刷新」可能同时开跑：都走「查宿主现状 → 再插行」，
        # 重叠时会重复插行、重复抓贴吧。用这把锁串起来。
        self._style_learn_lock = asyncio.Lock()
        # 最近一次「新闻/预定/情报」列出的条目，供 /gal 详情 <序号> 回查：
        # {stream_id: (时间戳, [条目…])}。只活在内存里，重启就没了（列表随时能再列一次）。
        self._intel_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._setup()
        cfg = self.config
        enabled = [
            name
            for name, on in [
                ("月幕", cfg.source.ymgal_enabled),
                ("VNDB", cfg.source.vndb_enabled),
                ("批评空间", cfg.source.egs_enabled),
            ]
            if on
        ]
        self.ctx.logger.info(
            "[Galgame] 已加载：启用源=%s；批评空间索引 %s",
            " / ".join(enabled) or "（全关，工具会返回空）",
            self._index_desc(),
        )
        self._maybe_build_index()
        self._style_ledger = gl_style.StyleLedger(self._data_dir() / "style_ledger.json")
        self._maybe_sync_style_switches()
        self._maybe_start_style_loop()

    async def on_unload(self) -> None:
        await self._cancel_index_task()
        await self._cancel_style_task()
        await self._cancel_style_sync()
        # 被用户关掉（config.toml 里 [plugin] enabled=false）时把自己写进宿主表的行撤掉；
        # 重启 / 热重载 / 关机器人也会走到这里，但那三种情况下读到的是 enabled=true。
        await self._purge_style_if_disabled()
        await self._close_http()
        self.ctx.logger.info("[Galgame] 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del config_data
        if scope != "self":
            return
        await self._cancel_index_task()
        await self._cancel_style_task()
        await self._cancel_style_sync()
        await self._close_http()
        # 配置一改，上一屏的序号记忆可能指向旧数据源；机器人昵称也要重新读一次
        self._intel_cache.clear()
        global _bot_nickname_cache, _bot_persona_cache
        _bot_nickname_cache = None
        _bot_persona_cache = None
        self._setup()
        self._maybe_build_index()
        self._style_ledger = gl_style.StyleLedger(self._data_dir() / "style_ledger.json")
        self._maybe_sync_style_switches()
        self._maybe_start_style_loop()
        self.ctx.logger.info("[Galgame] 配置已更新（version=%s），运行时已重建", version)

    async def _close_http(self) -> None:
        """关掉 HTTP 客户端。

        批评空间可能有一份**独立**的客户端（走日本节点的专用代理），
        这里要一起关，而且别把同一个对象关两遍 —— 没配专用代理时
        ``_egs_http`` 就是 ``_http`` 本身。
        """
        clients = [self._http]
        if self._egs_http is not self._http:
            clients.append(self._egs_http)
        for client in clients:
            if client is not None:
                await client.aclose()
        self._http = None
        self._egs_http = None
        tieba = getattr(self, "_tieba", None)
        if tieba is not None:
            await tieba.aclose()
            self._tieba = None

    # -- 装配 -------------------------------------------------------------

    def _config_path(self) -> Path:
        """插件自己的 config.toml —— 也就是用户在 WebUI 里改的那份。"""
        return Path(_PLUGIN_DIR) / "config.toml"

    def _data_dir(self) -> Path:
        try:
            base = Path(self.ctx.paths.data_dir)
        except Exception:  # noqa: BLE001 —— 老版本 SDK 没有 paths
            base = Path(_PLUGIN_DIR) / "data"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _setup(self) -> None:
        cfg = self.config
        self._http = HttpClient(
            timeout=float(cfg.source.timeout),
            proxy=cfg.source.proxy.strip(),
            user_agent=cfg.source.user_agent.strip(),
        )
        vndb = VndbClient(self._http) if cfg.source.vndb_enabled else None
        ymgal = (
            YmgalClient(self._http, cfg.source.ymgal_client_id, cfg.source.ymgal_client_secret)
            if cfg.source.ymgal_enabled
            else None
        )
        # 批评空间单独一份客户端：它往往要走**日本节点**的代理（部分地区/线路访问不了），
        # 而 VNDB / 月幕直连就很快。共用一个 proxy 的话，代理一关那两个源也跟着挂。
        # 没配专用代理（或和共用的一样）就复用，不白开一份连接池。
        egs_proxy = cfg.egs.proxy.strip()
        self._egs_http = self._http
        if egs_proxy and egs_proxy != cfg.source.proxy.strip():
            self._egs_http = HttpClient(
                timeout=float(cfg.source.timeout),
                proxy=egs_proxy,
                user_agent=cfg.source.user_agent.strip(),
            )
        egs = None
        if cfg.source.egs_enabled:
            egs = EgsClient(
                self._egs_http,
                self._data_dir() / "egs_index.json",
                min_votes=int(cfg.egs.min_votes),
                max_pages=int(cfg.egs.max_pages),
                delay_seconds=float(cfg.egs.delay_seconds),
                # 随插件发布的内置索引：第一次加载直接铺好，用户不用等全量构建
                seed_path=Path(_PLUGIN_DIR) / "seed" / "egs_index.json.gz",
            )
            egs.index.load()
        self._service = GalgameService(
            vndb,
            ymgal,
            egs,
            fetch_egs_detail=bool(cfg.egs.fetch_detail),
            max_reviews=int(cfg.egs.max_reviews),
            match_threshold=int(cfg.egs.match_threshold),
            fetch_long_reviews=bool(cfg.egs.fetch_long_reviews),
            max_long_reviews=int(cfg.egs.max_long_reviews),
            scan_pages=int(cfg.egs.scan_pages),
        )
        # 贴吧单独一份客户端：它要先用访客 cookie 播种，抓取节奏也跟三个资料源无关
        self._tieba = TiebaClient(
            timeout=float(cfg.source.timeout),
            proxy=cfg.style.proxy.strip() or cfg.source.proxy.strip(),
            delay_seconds=float(cfg.style.delay_seconds),
        )

    def _index_desc(self) -> str:
        egs = self._service.egs if self._service else None
        if not egs:
            return "未启用"
        stats = egs.index.stats()
        if not stats["count"]:
            return "尚未建立（按名字查作品仍可用；只有「按分数检索」需要它，/gal sync 可手动建）"
        return (
            f"{stats['count']} 条（中央值覆盖 {stats['median_low']}~{stats['median_high']}），"
            f"{stats['age_days']} 天前建立"
        )

    def _maybe_build_index(self) -> None:
        """索引缺失时后台全量建；**过期时只做增量更新**。都不阻塞聊天。

        内置种子铺好之后，「缺失」基本只发生在种子被删或解压失败时，
        所以日常走的是下面那条增量分支 —— 只重抓榜单前面若干页，很快。
        """
        if not self._service or not self._service.egs:
            return
        cfg = self.config
        index = self._service.egs.index
        if not cfg.egs.auto_build:
            if not index.items:
                self.ctx.logger.info(
                    "[Galgame] 批评空间索引为空，但自动更新已关闭（/gal sync 可手动建）"
                )
            return
        if not index.items:
            self._index_task = asyncio.create_task(self._build_index_bg())
            return
        if index.age_days() > float(cfg.egs.refresh_days):
            self._index_task = asyncio.create_task(self._update_index_bg())

    async def _update_index_bg(self) -> None:
        """增量更新：只重抓榜单前面若干页（高分与新作都在那一段）。"""
        try:
            assert self._service and self._service.egs
            pages = int(self.config.egs.update_pages)
            self.ctx.logger.info("[Galgame] 开始增量更新批评空间索引（前 %s 页）…", pages)
            stats = await self._service.egs.update_recent(pages=pages)
            self.ctx.logger.info(
                "[Galgame] 增量更新完成：%s 页 / 新增 %s 条 / 更新 %s 条 / 索引共 %s 条",
                stats.get("pages"),
                stats.get("added"),
                stats.get("updated"),
                len(self._service.egs.index.items),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] 增量更新失败（不影响其它源）：%s", exc)

    async def _build_index_bg(self) -> None:
        try:
            assert self._service and self._service.egs
            self.ctx.logger.info("[Galgame] 开始后台建立批评空间索引…")
            count = await self._service.egs.build_index()
            stats = self._service.egs.last_build
            self.ctx.logger.info(
                "[Galgame] 批评空间索引完成：%s 条（成功 %s 页 / 失败 %s 页 / 耗时 %s 秒）；%s",
                count,
                stats.get("pages_ok"),
                stats.get("failed_pages"),
                stats.get("duration"),
                stats.get("stop_reason"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] 批评空间索引建立失败（不影响其它源）：%s", exc)

    async def _cancel_index_task(self) -> None:
        if self._index_task is not None and not self._index_task.done():
            self._index_task.cancel()
            try:
                await self._index_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._index_task = None

    # -- 唯一的工具 -------------------------------------------------------

    @Tool(
        "gal_search",
        # 工具描述**必须**写在这里：宿主只把 description 送给 LLM
        # （to_llm_definition() 只发 {name, description, parameters_schema}）。
        # SDK 文档现在明确「description 才是描述字段，brief_description /
        # detailed_description 已弃用，仅在没有 description 时兜底」——
        # 早先这里写的是 brief_description，靠 fallback 才生效，
        # 上游哪天去掉兜底，整个工具描述就没了。
        description=(
            "查 galgame 资料与检索。三种用法："
            "① query=作品名（中日英/外号都认）→ 完整资料+资料卡；"
            "② 只给筛选条件 → 找作品，评分口径二选一：min/max_rating 是 VNDB 分、"
            "min/max_egs 是批评空间中央值（不可换算，别混用）；"
            "③ new_within_days=N → 最近 N 天新作（独立模式，忽略其它条件）。"
            "结果默认按人气（票数）排序，所以「85 分以上」给的是符合条件里最有人气的几部；"
            "想按分数排传 sort=egs。要锁定某一档就上下界一起给（如 min_egs=75, max_egs=80）。"
            "找低分/烂作用 max_egs（如 max_egs=60）；VNDB 是贝叶斯分、低分段稀疏，"
            "只在 max_egs 查不到时才改用它。"
            "问「锐评 / 这游戏到底怎么样」时，结果里会带批评空间的**长文感想**"
            "（game.评价.长文感想），那是玩家的真实批评，直接引用它的观点，别自己编；"
            "其中「剧透=true」的那些含剧情剧透，可以引用它对游戏的评价，但别复述剧情。"
            "多看几部用 offset 翻页；问声优设 with_characters。"
        ),
        # 显式声明 deferred（渐进式披露）：宿主对插件工具的默认值本来就是 deferred
        # （component_query.py: "return deferred"），这里写出来是为了**说清楚这是刻意的**，
        # 也防止上游哪天把默认值改成 visible。
        #
        # 三档的含义（宿主 component_query.py）：
        #   visible  → 每轮都把完整定义（描述 + 参数 schema）塞进 tool_options，最贵
        #   deferred → 未发现时只在 system-reminder 里占一行「名字: 描述」，
        #              模型调 tool_search 命中后才拿到完整定义 ← 我们选这个
        #   hidden   → 直接 enabled=False，模型永远看不到
        visibility="deferred",
        # 参数刻意用 **dict 形式**而不是 ToolParameterInfo 列表：
        # 后者经 pydantic 序列化后，每条参数都会带上 enum_values / items_schema /
        # properties / required_properties / additional_properties 五个空字段 ——
        # 15 条就是约 1.8K 字符的 null，而且**全都要进 prompt**。
        # dict 形式宿主会原样采用（component_registry.py 的 parameters_raw 分支）。
        parameters={
            "query": {
                "type": "string",
                "description": "作品名或关键词（中文/日文/英文/外号都可以）",
            },
            "min_egs": {
                "type": "number",
                "description": "批评空间中央值下限（0~100），0=不限",
            },
            "max_egs": {
                "type": "number",
                "description": "批评空间中央值上限（0~100），0=不限。配合 min_egs 锁定某个分数档",
            },
            "min_rating": {
                "type": "number",
                "description": "VNDB 评分下限（0~100），0=不限。找高分用它，找低分别用",
            },
            "max_rating": {
                "type": "number",
                "description": "VNDB 评分上限（0~100），0=不限。要「低分/烂作」就用它，如 45，且不要再给 min_rating",
            },
            "min_votes": {
                "type": "integer",
                "description": "评分人数下限，用来滤掉只有几个人评过的冷门作，0=不限",
            },
            "min_hours": {"type": "number", "description": "通关时长下限（小时），0=不限"},
            "max_hours": {"type": "number", "description": "通关时长上限（小时），0=不限"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "内容标签，如 Nakige（泣系）、Utsuge（鬱系）、Mystery（推理）",
            },
            "year_from": {"type": "integer", "description": "只要这年之后发售的，0=不限"},
            "sort": {
                "type": "string",
                "description": (
                    "排序，默认 votes（人气）。"
                    "可选：votes 人气 / egs 批评空间中央值 / vndb 评分 / "
                    "length 时长 / released 发售日 / relevance 综合质量分"
                ),
            },
            "offset": {
                "type": "integer",
                "description": "跳过前 N 部（翻页用），默认 0。用户说「再来几部」时把上次的数量累加进来",
            },
            "count": {"type": "integer", "description": "返回条数，留空(0)用插件配置里的默认值"},
            "with_characters": {
                "type": "boolean",
                "description": "是否附上角色与声优（CV）。问「XX 的声优是谁」时设 true",
            },
            "new_within_days": {
                "type": "integer",
                "description": "查最近 N 天新作（独立模式，设了就忽略其它条件），0=不查。没说天数时填 30，最多 50",
            },
        },
    )
    async def handle_search(
        self,
        query: str = "",
        min_egs: float = 0,
        max_egs: float = 0,
        min_rating: float = 0,
        max_rating: float = 0,
        min_votes: int = 0,
        min_hours: float = 0,
        max_hours: float = 0,
        tags: list[str] | None = None,
        year_from: int = 0,
        sort: str = "votes",
        offset: int = 0,
        count: int = 0,
        with_characters: bool = False,
        new_within_days: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "")
        if not self._service:
            return {"success": False, "error": "插件未就绪"}
        cfg = self.config
        # 三个源全关掉时插件确实一点数据都查不到 —— 直接说清楚，
        # 别让用户对着一句「没找到」去猜是不是自己条件写错了。
        if not any([cfg.source.ymgal_enabled, cfg.source.vndb_enabled, cfg.source.egs_enabled]):
            return {
                "success": False,
                "error": (
                    "月幕 / VNDB / 批评空间三个数据源都被关掉了，没有任何可查的数据。"
                    "请到插件配置里至少开启一个源。"
                ),
            }
        query = (query or "").strip()
        tags = [t for t in (tags or []) if str(t).strip()]
        # count 留空（0）时用配置里的默认条数，这样配置项才真的有效
        count = max(1, min(int(count or 0) or int(cfg.recommend.default_count), 10))
        offset = max(0, int(offset or 0))
        if offset >= MAX_OFFSET:
            return {
                "success": False,
                "error": (
                    f"已经翻到底了：同一组条件最多只能往后翻到第 {MAX_OFFSET} 条"
                    "（翻页要重新补全前面的候选，越深越慢）。"
                    "想看更多请换一组条件，或把 offset 调小回到前面几页。"
                ),
            }
        has_filter = any(
            [min_egs, max_egs, min_rating, max_rating, min_votes, min_hours, max_hours, tags, year_from, offset]
        )
        # 配置里的「推荐评分下限」只在用户没自己给任何评分条件、**且 VNDB 开着**时兜底：
        # - 给了会跟用户的上界打架（用户要「65 分以下」，配置却卡 70 分下限 → 空结果）；
        # - VNDB 关掉时这个下限更无从谈起 —— 批评空间那条路的候选没有 VNDB 分，
        #   挂上默认下限会把它们全部筛掉，变成「关了 VNDB 就什么都搜不到」。
        if not any([min_egs, max_egs, min_rating, max_rating]) and cfg.source.vndb_enabled:
            min_rating = float(cfg.recommend.min_vndb_rating or 0)
        # 工具没指定票数门槛时回落到配置里的默认值（同理，只在 VNDB 开着时才有意义）
        votes_floor = int(min_votes or 0) or (
            int(cfg.recommend.min_vndb_votes) if cfg.source.vndb_enabled else 0
        )

        # 上下界写成同一个数（「锁定 30 分」最直译的写法）等于要求「分数正好等于 30.0」，
        # 而评分是小数 —— 这样必然搜空。用户的本意显然是「30 分上下」，所以自动放开成
        # 5 分宽的区间，并把这件事**明说**在条件文案里：不吭声地改条件就是另一回事了。
        widen_note = ""
        if min_rating and max_rating and float(min_rating) == float(max_rating):
            widened = float(min_rating) + 5
            widen_note += (
                f"（VNDB 原本写成 {float(min_rating):g}~{float(max_rating):g}，"
                f"等于只认正好 {float(min_rating):g} 分，已自动放宽到 {widened:g}）"
            )
            max_rating = widened
        if min_egs and max_egs and float(min_egs) == float(max_egs):
            widened = float(min_egs) + 5
            widen_note += (
                f"（批评空间原本写成 {float(min_egs):g}~{float(max_egs):g}，"
                f"等于只认正好 {float(min_egs):g} 分，已自动放宽到 {widened:g}）"
            )
            max_egs = widened

        try:
            # 用法③：查新作（「最近有什么新 gal」「这个月出了什么」）。
            # 走月幕的发售日历，和「按年份筛」不是一回事 —— 后者是 VNDB 的 released 过滤，
            # 只认罗马音标题、也不知道有没有汉化。
            if new_within_days:
                return await self._tool_new_releases(stream_id, new_within_days, count)

            # 用法①：纯名字 → 查资料
            if query and not has_filter:
                info = await self._service.lookup(query)
                if info is not None and self._is_confident(query, info):
                    # 角色/声优要额外打两次 VNDB，所以默认不取；除了模型主动要，
                    # 问题本身带「声优 / CV / 谁配 / 角色」这类字眼时也自动补上 ——
                    # 免得模型漏传参数导致这个功能白做。
                    if with_characters or _CHARACTER_HINT.search(query):
                        info.characters = await self._service.characters_for(info)
                    summary = self._info_summary(info)
                    if self.config.output.send_card and stream_id:
                        await self._send_info_card(info, stream_id)
                    return {"success": True, "content": self._summary_text(info), "game": summary}
                # 名字对不上具体作品 → 退化成候选列表，让模型自己决定要不要追问
                items = await self._service.search(query, limit=count)
                if not items:
                    return {"success": True, "content": f"没找到和「{query}」相关的作品。"}
                lines = build_search_lines(items)
                return {
                    "success": True,
                    "content": "没找到完全匹配的作品，这几个是候选：\n" + "\n".join(lines),
                    "candidates": [g.display_title() for g in items],
                }

            # 用法②：按条件找（含按评分检索）
            picks = await self._service.find(
                keyword=query,
                min_egs=float(min_egs or 0),
                max_egs=float(max_egs or 0),
                min_rating=float(min_rating or 0),
                max_rating=float(max_rating or 0),
                min_votes=votes_floor,
                length_min_minutes=int(float(min_hours) * 60) if min_hours else 0,
                length_max_minutes=int(float(max_hours) * 60) if max_hours else 0,
                tags=tags,
                year_from=int(year_from or 0),
                exclude_restricted=bool(cfg.recommend.exclude_restricted),
                prefer_chinese=bool(cfg.recommend.prefer_chinese),
                sort=sort or "votes",
                count=count,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] 查询失败：%s", exc)
            return {"success": False, "error": f"查询失败：{exc}"}

        # 被忽略掉的条件必须说出来 —— 否则模型会以为条件生效了，
        # 回头把它当事实讲给用户（标签解析不了时尤其容易发生）。
        warn = self._take_warnings()
        if not picks:
            return {
                "success": True,
                "content": (warn + "\n" if warn else "")
                + "按这些条件没筛出作品。"
                + self._empty_hint(
                    min_egs=min_egs, max_egs=max_egs,
                    min_rating=min_rating, max_rating=max_rating,
                    offset=offset,
                ),
            }

        condition = self._condition_text(
            query, min_egs, max_egs, min_rating, max_rating,
            votes_floor, min_hours, max_hours, tags, year_from, offset,
        ) + widen_note
        if self.config.output.send_card and stream_id and len(picks) > 1:
            try:
                html = await build_recommend_html(
                    picks,
                    http=self._http,
                    accent=self.config.output.accent_color,
                    width=int(self.config.output.card_width),
                    condition_text=condition,
                )
                await self._render_and_send(html, stream_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] 推荐卡片渲染失败：%s", exc)
        elif self.config.output.send_card and stream_id and len(picks) == 1:
            await self._send_info_card(picks[0], stream_id)

        lines = build_search_lines(picks)
        # 顺便把条件里最相关的几部的评分信息带上，省得模型再问一轮
        detail_hint = [
            {
                "作品": g.display_title(),
                "批评空间中央值": g.egs_median,
                "VNDB": g.vndb_rating,
                "时长": g.length_text(),
                "有汉化": g.have_chinese,
            }
            for g in picks[:count]
        ]
        note = ""
        if warn:
            # 被忽略掉的条件必须说出来 —— 否则模型会把「筛过」当成事实讲给用户
            note += f"\n{warn}"
        hint = _hours_hint(
            {
                "min_egs": min_egs,
                "max_egs": max_egs,
                "min_hours": min_hours,
                "max_hours": max_hours,
            }
        )
        if hint:
            note += f"\n{hint}"
        if (min_egs or max_egs) and not self._service.egs_ready():
            note += "\n（批评空间连不上，评分是按 VNDB 分近似筛的；恢复后会自动改回中央值口径）"
        # 结果刚好装满一页，说明后面很可能还有 —— 明确告诉模型「怎么要下一批」，
        # 否则它只会反复用同样的条件重问，用户看到的永远是同一批作品
        if len(picks) >= count:
            if offset + count < MAX_OFFSET:
                note += f"\n（要往后看就说「再来几部」，用同样的条件加 offset={offset + count}）"
            else:
                note += (
                    "\n（这已经是能翻到的最后一页了，再往后没有更多；"
                    "换组条件或把 offset 调小回前面看）"
                )
        return {
            "success": True,
            "content": f"按「{condition}」找到：\n" + "\n".join(lines) + note,
            "results": detail_hint,
            "next_offset": offset + count,
        }

    @Tool(
        "gal_news",
        description=(
            "galgame 最新情报。问「最近有什么 gal 新闻 / 圈内有什么新闻」"
            "「接下来要出什么」时用它。"
            "mode=news（默认）**只给圈内新闻**（日文源会翻成中文标题）；"
            "mode=upcoming **只给未来准备发售的作品**（带厂商与汉化状态，VNDB 补评分）；"
            "问「最近已经出了什么新作」应该改用 gal_search 的 new_within_days；"
            "mode=detail 配 index 看某一条的详情（序号来自上一次列出的那屏）。"
            "列出的条数会记下来，用户随后问「第 3 条是什么」就用 mode=detail、index=3。"
            "结果已经发过卡片，你只要用一两句话概括重点，不要一条条复述。"
        ),
        # 和 gal_search 一样刻意声明 deferred：没被发现时只占 system-reminder 一行。
        visibility="deferred",
        parameters={
            "mode": {
                "type": "string",
                "description": "news 新闻（默认）/ upcoming 即将发售 / detail 看某条详情",
            },
            "limit": {
                "type": "integer",
                "description": "最多几条，0 = 用插件配置里的默认值",
            },
            "days": {
                "type": "integer",
                "description": "mode=upcoming 往后看多少天，0 = 用插件配置里的默认值（默认 90 天）",
            },
            "index": {
                "type": "integer",
                "description": "mode=detail 时要看的序号（1 开始，来自上一次列出的那屏）",
            },
        },
    )
    async def handle_news(
        self,
        mode: str = "news",
        limit: int = 0,
        days: int = 0,
        index: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "")
        if not self._service:
            return {"success": False, "error": "插件未就绪"}
        want = str(mode or "news").strip().lower()
        # 看详情不依赖最新情报开关（记的东西可能来自 /gal new，那个关了也能用）
        if want in ("detail", "详情", "查看", "展开"):
            return await self._tool_detail(stream_id, index)
        news = getattr(self.config, "news", None)
        if news is None or not news.enabled:
            return {
                "success": True,
                "content": "最新情报没启用（插件配置里的「最新情报」段），查不了新闻和预定。",
            }
        if want in ("upcoming", "预定", "即将发售", "发售"):
            return await self._tool_upcoming(stream_id, limit, days)
        # 其余（news / 新闻 / 资讯 / intel）一律**只给新闻** —— 和 /gal 情报 对齐。
        # 以前 mode=intel（默认）会把新闻和即将发售拼一屏，用户分不清哪条命令给什么。
        return await self._tool_news(stream_id, limit)

    async def _tool_detail(self, stream_id: str, index: int) -> dict[str, Any]:
        """看上一次列出的第 N 条的详情（和 /gal 详情 <序号> 共用一份记忆）。"""
        try:
            number = int(str(index).strip())
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            return {"success": True, "content": "要看第几条？把序号一起给我（1 开始）。"}
        item = self._recall_intel(stream_id, number)
        if item is None:
            return {
                "success": True,
                "content": "这个序号不在最近列出的那一屏里（或者已经过了 30 分钟）。"
                "先查一次情报/预定列出来，再按编号问详情。",
            }
        detail = await self._detail_item(item)
        if stream_id:
            await self._send_detail_card(
                stream_id,
                detail,
                heading="详情",
                subtitle=NEWS_LABELS.get(str(item.get("source") or ""), ""),
                fallback="\n".join(self._detail_lines(detail)),
            )
        result = (
            self._release_result(item) if item.get("released") else self._news_result(item)
        )
        return {
            "success": True,
            "content": "\n".join(self._detail_lines(detail)),
            "results": [result],
        }

    async def _tool_news(self, stream_id: str, limit: int) -> dict[str, Any]:
        news = self.config.news
        want = int(limit) or int(news.max_items)
        try:
            items, warnings = await self._collect_news(want)
        except RuntimeError as exc:
            return {"success": True, "content": f"{exc}。"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"抓新闻失败：{exc}"}
        if not items:
            text = "这次没有抓到新闻（源可能挂了，或者站点改版了）。"
            if warnings:
                text += "\n" + "\n".join(warnings)
            return {"success": True, "content": text}
        picks = items[:want]
        lines = self._with_notes(self._news_lines(picks), warnings)
        lines.append(self._detail_hint())
        self._remember_intel(stream_id, picks)
        if stream_id:
            await self._send_news_card(
                stream_id,
                picks,
                heading="gal 圈新闻",
                subtitle=f"{self._news_source_text()}　共 {len(items)} 条",
                fallback="\n".join(lines),
            )
        return {
            "success": True,
            "content": f"圈内新闻，共 {len(items)} 条（来自 {self._news_source_text()}）：\n"
            + "\n".join(lines),
            "results": [self._news_result(item) for item in picks],
        }

    async def _tool_upcoming(self, stream_id: str, limit: int, days: int) -> dict[str, Any]:
        news = self.config.news
        want = int(limit) or max(int(news.max_items), 10)
        try:
            items, span, warnings = await self._query_upcoming_releases(days)
        except RuntimeError as exc:
            return {"success": True, "content": f"{exc}。"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"查询预定发售失败：{exc}"}
        if not items:
            text = f"未来 {span} 天没有查到预定发售。"
            if warnings:
                text += "\n" + "\n".join(warnings)
            return {"success": True, "content": text}
        picks = items[:want]
        today = datetime.date.today()
        end = (today + datetime.timedelta(days=span)).isoformat()
        subtitle = f"{today.isoformat()} ~ {end}　共 {len(items)} 部"
        if len(items) > len(picks):
            subtitle += f"，这里列出前 {len(picks)} 部"
        lines = self._with_notes(self._new_release_lines(picks), warnings)
        lines.append(self._detail_hint())
        self._remember_intel(stream_id, picks)
        if stream_id:
            await self._send_release_card(
                stream_id,
                picks,
                heading=f"未来 {span} 天预定",
                subtitle=subtitle,
                fallback="\n".join(lines),
            )
        return {
            "success": True,
            "content": f"未来 {span} 天（{today.isoformat()} ~ {end}）预定发售的 galgame，"
            f"共 {len(items)} 部：\n" + "\n".join(lines),
            "results": [self._release_result(item) for item in picks],
        }

    @staticmethod
    def _news_result(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "标题": item.get("title_zh") or item.get("title"),
            "原文标题": item.get("title"),
            "来源": NEWS_LABELS.get(str(item.get("source") or ""), ""),
            "时间": item.get("published"),
            "链接": item.get("url"),
            "摘要": item.get("summary"),
        }

    @staticmethod
    def _release_result(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "作品": item.get("title_zh") or item.get("title"),
            "原名": item.get("title"),
            "发售日": item.get("released"),
            "厂商": item.get("developer"),
            "有汉化": bool(item.get("have_chinese")),
        }

    # -- 命令（玩票功能放这，不占模型注意力） ------------------------------

    async def _may_maintain(
        self,
        platform: str,
        user_id: str,
        stream_id: str,
        local_operator: bool,
    ) -> bool:
        """维护类命令（update / sync / 状态 / 话术）能不能执行。

        判定和宿主 `/pm` 同一套、读的也是同一份宿主配置：
        主程序终端（local operator）直接放行；否则 `[plugin] permission` 里得有
        `platform:user_id`；再允许 `[plugin] command_permissions` 里按
        `mizuku003.galgame.maintenance` 单独放行某些用户或聊天流，写法照抄宿主内置命令的
        `{allow_users = [...], allow_chats = [...]}`。
        配置读不到、形状不对一律当**无权限**，宁可挡错也不放错。
        """
        if local_operator:
            return True
        scoped = _scoped_user_id(platform, user_id)
        if scoped:
            try:
                configured = await self.ctx.config.get("plugin.permission", [])
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] 读 [plugin] permission 失败：%s", exc)
                configured = []
            if scoped in _permission_set(configured):
                return True
        try:
            rules = await self.ctx.config.get("plugin.command_permissions", {})
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] 读 [plugin] command_permissions 失败：%s", exc)
            return False
        rule = rules.get(_MAINTENANCE_COMMAND) if isinstance(rules, dict) else None
        if isinstance(rule, dict):
            if scoped and scoped in _permission_set(rule.get("allow_users")):
                return True
            if str(stream_id).strip() in _allowed_chat_set(rule.get("allow_chats")):
                return True
        return False

    @Command(
        "gal",
        description=(
            "galgame领域大神：直接打 /gal 或 /gal help 看用法卡 | "
            "/gal <条件> 筛选（如 /gal 85+ 10h） | /gal 类型 SLG | /gal 年份 2015 | "
            "/gal 人气 | /gal random [n] | "
            "/gal 情报 [条数] 圈内新闻 | /gal 预定 [天数] 未来待发售 | "
            "/gal 新作 [天数]（也可以写 /gal new） | "
            "/gal 详情 <序号>（也可以直接打 /gal 3） | "
            "以下仅管理员可用：/gal 状态 | /gal update | /gal sync | "
            "/gal 话术 [状态|刷新|打标|恢复|清理|体检]"
        ),
        # rest 整体捕获剩余自由文本，条件由 parse_filter 自己切词 ——
        # 宿主只把**命名捕获组**（m.groupdict()）交给插件，用正则去拆条件拆不干净。
        pattern=r"^[/／]gal(?:\s+(?P<sub>[^\s]+))?(?:\s+(?P<rest>.*))?$",
    )
    async def handle_gal(self, **kwargs: Any) -> tuple[bool, str, int]:
        matched = kwargs.get("matched_groups") or {}
        given_sub = str(matched.get("sub") or "").strip()
        sub = given_sub or "status"
        rest = str(matched.get("rest") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return False, "缺少聊天流 ID", 1

        low = sub.lower()
        # 裸 `/gal` 以前落到状态页；状态页现在归管理员，所以裸命令改发用法卡，
        # 免得非管理员一敲就吃一句「无权限」。（`/gal 状态` 才是状态页。）
        if not given_sub:
            return await self._cmd_help(stream_id)
        if low in _MAINTENANCE_SUBS:
            if not await self._may_maintain(
                str(kwargs.get("platform") or ""),
                str(kwargs.get("user_id") or ""),
                stream_id,
                kwargs.get("is_local_operator") is True,
            ):
                await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
                return True, "维护命令需要管理员权限", 1
        # random / new 的数字参数从 rest 里取；没给就交给各自的默认值
        number = int(rest) if (rest.isascii() and rest.isdigit()) else 0

        # 命令词只认用法卡（`/gal help`）上写的那几个，外加卡片上明说的两种写法
        # （`/gal new` ≙ `/gal 新作`、`/gal 3` ≙ `/gal 详情 3`）。
        # 以前每个命令都挂了四五个同义词，看着热闹，实际上没人记得住、还容易撞车；
        # 想加新玩法就改用法卡，别在这里偷偷塞别名。
        if low == "sync":
            return await self._cmd_sync(stream_id)
        if low == "update":
            return await self._cmd_update(stream_id)
        if low == "random":
            return await self._cmd_random(stream_id, number or 3)
        if low in ("新作", "new"):
            return await self._cmd_new(stream_id, number or 30)
        if low == "预定":
            return await self._cmd_upcoming(stream_id, number or 0)
        if low == "情报":
            return await self._cmd_intel(stream_id, number or 0)
        if low == "详情":
            return await self._cmd_detail(stream_id, rest)
        if low == "help":
            return await self._cmd_help(stream_id)
        if low == "状态":
            # 「状态」是插件自己那一页（数据源、索引、体检），和 /gal help 的用法卡分开
            return await self._cmd_status(stream_id)
        if low == "类型":
            return await self._cmd_genre(stream_id, rest)
        if low == "年份":
            return await self._cmd_year(stream_id, rest)
        if low == "人气":
            return await self._cmd_datacount(stream_id, rest)
        if low == "话术":
            return await self._cmd_style(stream_id, rest)
        # 纯数字且刚列过一屏 → 当成「看第 N 条的详情」（/gal 3）。放在筛选之前，
        # 但只有记忆里真有第 N 条才走，否则照旧交给筛选（数字也可能是别的条件）。
        if low.isascii() and low.isdigit() and self._recall_intel(stream_id, int(low)) is not None:
            return await self._cmd_detail(stream_id, low)
        # **其余一律当筛选条件**（`/gal 85+ 10h`、`/gal egs>=85` 都走这条）。
        # 曾经这里去猜「看起来像不像条件」（比如判断有没有 =<>），结果 `/gal 90+`
        # 因为简写用的是 `+` 而被判成不像条件，静默掉进状态页 ——
        # 用户只会以为筛选没生效。**别猜，默认就是筛选。**
        return await self._cmd_filter(stream_id, f"{sub} {rest}".strip())

    @Command(
        "gal_review",
        description="锐评某部 galgame：/锐评 <作品名>（依据批评空间的评分与长文感想）",
        # 中文用户经常不敲空格（`/锐评「苍之彼方的四重奏」`），所以分隔符是可选的
        pattern=r"^[/／]锐评[\s:：]*(?P<keyword>.*)$",
    )
    async def handle_review(self, **kwargs: Any) -> tuple[bool, str, int]:
        matched = kwargs.get("matched_groups") or {}
        keyword = _strip_quotes(str(matched.get("keyword") or ""))
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return False, "缺少聊天流 ID", 1
        return await self._cmd_review(stream_id, keyword)

    async def _cmd_review(self, stream_id: str, keyword: str) -> tuple[bool, str, int]:
        """``/锐评 <作品名>`` —— 让模型依据批评空间的资料写一段锐评。

        和工具那条路的区别：工具把资料交给**主对话**的模型，由它决定怎么说；
        这里插件**自己再调一次模型**，把资料压成一段成品直接发出来 ——
        不依赖主对话上下文，也不会被别的话题带偏。
        """
        cfg = self.config
        if not cfg.review.enabled:
            await self.ctx.send.text(
                "锐评命令已在插件配置里关掉了。", stream_id, storage_message=False
            )
            return True, "未启用", 1
        if not self._service:
            await self.ctx.send.text("插件未就绪。", stream_id, storage_message=False)
            return False, "未就绪", 1
        if not keyword:
            await self.ctx.send.text(
                "用法：/锐评 <作品名>\n"
                "例：/锐评 ランス10 · /锐评「苍之彼方的四重奏」\n"
                "（依据批评空间的中央値、用户评价维度与长文感想，由模型综合成一段锐评）",
                stream_id,
                storage_message=False,
            )
            return True, "已输出帮助", 1

        try:
            info = await self._service.lookup(keyword)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] /锐评 查资料失败：%s", exc)
            await self.ctx.send.text(f"查资料失败：{exc}", stream_id, storage_message=False)
            return False, "失败", 1

        if info is None or not self._is_confident(keyword, info):
            # 名字对不上具体作品时给候选，**别硬锐评一部不相干的作品**
            try:
                items = await self._service.search(keyword, limit=5)
            except Exception:  # noqa: BLE001
                items = []
            if not items:
                await self.ctx.send.text(
                    f"没找到和「{keyword}」相关的作品。", stream_id, storage_message=False
                )
                return True, "无结果", 1
            await self.ctx.send.text(
                "没找到完全匹配的作品，这几个是候选（用完整名字再试一次）：\n"
                + "\n".join(build_search_lines(items)),
                stream_id,
                storage_message=False,
            )
            return True, "有候选", 1

        text = await self._generate_review(await self._build_review_prompt(self._build_review_material(info)))
        if not text:
            await self.ctx.send.text(
                "锐评生成失败（模型没返回内容），稍后再试一次。", stream_id, storage_message=False
            )
            return False, "生成失败", 1

        await self._send_review_forward(stream_id, text)
        return True, "已输出锐评", 1

    def _build_review_material(self, info: GameInfo) -> str:
        """把查到的资料拼成给模型看的「锐评素材」。

        分两段：**先给玩家自己写的话**（短评 + 长文感想），最后才是一小段「核对用的
        数字」。之前数字排在最前面，模型上来就看见一张参数表，写出来的锐评自然
        照着参数复述、像商品页 —— 人话在前，它才会跟着人话说。
        用可读文本而不是 JSON，是为了不让 ``egs_median`` 这种键名被当成事实写出来。
        """
        cfg = self.config

        human: list[str] = []
        if info.egs_reviews:
            human.append("【短评】")
            for review in info.egs_reviews[:3]:
                score = _score_text(review.get("score"))
                human.append(f"{score + '：' if score else ''}{(review.get('text') or '').strip()}")
        if info.egs_long_reviews:
            limit = int(cfg.egs.long_review_chars)
            human.append("")
            human.append(f"【长文感想】玩家写的完整评价，锐评主要参考这部分（每篇最多引 {limit} 字）：")
            for index, review in enumerate(info.egs_long_reviews, 1):
                flag = "含剧透" if review.get("spoiler") else "无剧透"
                score = _score_text(review.get("score"))
                human.append(
                    f"--- 第 {index} 篇 · {flag} · "
                    f"{score + ' / ' if score else ''}{review.get('length')}字 ---"
                )
                human.append((review.get("text") or "")[:limit])
        if not human:
            human.append("【短评 / 长文感想】一篇都没抓到，只能靠下面的数字，那就直说「资料不多」。")

        lines: list[str] = [f"《{info.display_title()}》"]
        if info.title_ja and info.title_ja != info.display_title():
            lines.append(f"原名：{info.title_ja}")

        meta: list[str] = []
        if info.released:
            meta.append(f"发售 {info.released}")
        if info.developer:
            meta.append(f"厂商 {info.developer}")
        meta.append(f"通关时长 {info.length_text()}")
        lines.append(" / ".join(meta))

        ratings: list[str] = []
        if info.egs_median:
            extra = f"（平均 {info.egs_mean:.1f}，{info.egs_votes} 人评分）" if info.egs_mean else ""
            ratings.append(f"批评空间中央値 {info.egs_median}{extra}")
        if info.vndb_rating is not None:
            ratings.append(f"VNDB {info.vndb_rating:.1f}/100（{info.vndb_votes} 票）")
        if ratings:
            lines.append("评分：" + "；".join(ratings))

        evals: list[str] = []
        if info.egs_praise_rate is not None:
            evals.append(f"好评率 {info.egs_praise_rate}%")
        if info.egs_stacked:
            evals.append(f"积压率 {info.egs_stacked.get('percent')}%")
        if info.egs_giveup:
            evals.append(f"弃坑率 {info.egs_giveup.get('percent')}%")
        if info.egs_interesting_minutes:
            evals.append(f"{info.egs_interesting_minutes / 60:.0f} 小时开始好看")
        if info.egs_play_minutes:
            evals.append(f"玩家实测时长中位数 {info.egs_play_minutes / 60:.0f} 小时")
        if evals:
            lines.append("评价强度：" + "；".join(evals))

        if info.egs_pov:
            pov_rows: list[str] = []
            for key in ["ここがいい", "傾向", "ネガティブ", "シナリオ", "グラフィック", "音"]:
                entries = info.egs_pov.get(key)
                if entries:
                    names = "、".join(f"{e['name']}({e['votes']})" for e in entries[:4])
                    pov_rows.append(f"  {key}：{names}")
            if pov_rows:
                lines.append("用户评价维度（括号内为票数）：")
                lines.extend(pov_rows)

        if info.tags:
            lines.append("标签：" + "、".join(str(t.get("name") or "") for t in info.tags[:12]))

        return "\n".join(
            human
            + ["", "=== 核对用的数字（正文里别罗列它们，最多带一句总分） ===", *lines]
        )

    async def _build_review_prompt(self, material: str) -> str:
        """锐评的 prompt，模板来自配置面板的「锐评提示词」。

        提示词整段都可以被用户改，所以这里只做占位符替换 + 兜底
        （见 ``_render_review_prompt``）。默认模板里的约束 —— 不许编造、别分点、
        别用「总的来说 / 作为一款」这类 AI 腔 —— 都在 ``gl_config.DEFAULT_REVIEW_PROMPT``。

        打开「沿用麦麦人设」时，{persona} 换成宿主的「人格设定」，锐评就跟麦麦
        平时说话一个腔调；读不到才退回插件自己的「锐评口吻」。
        """
        cfg = self.config
        persona = str(cfg.review.persona or "")
        if bool(getattr(cfg.review, "follow_bot", False)):
            host_persona = await _bot_persona(self.ctx)
            if host_persona:
                persona = host_persona
        template = str(getattr(cfg.review, "prompt", "") or "").strip() or DEFAULT_REVIEW_PROMPT
        return _render_review_prompt(
            template,
            persona=persona,
            max_chars=int(cfg.review.max_chars),
            material=material,
        )

    async def _generate_review(self, prompt: str) -> str:
        """调宿主的 LLM 生成锐评；失败返回空串，由调用方告诉用户。

        用哪个模型只看面板的「模型任务」（默认 replyer = 回复模型）；填了就听它的，
        留空则用宿主的默认任务。调不动时自动退回宿主的默认任务，不至于写不出来。
        """
        cfg = self.config
        max_tokens = max(256, min(2048, int(cfg.review.max_chars) * 3))
        model = str(cfg.review.model_task or "").strip()
        for route in ([model, ""] if model else [""]):
            try:
                result = await self.ctx.llm.generate(
                    prompt=prompt,
                    model=route,
                    temperature=0.85,
                    max_tokens=max_tokens,
                    timeout_ms=_LLM_RPC_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] /锐评 调用模型失败（%s）：%s", route or "默认任务", exc)
                continue
            if isinstance(result, dict) and result.get("success"):
                return _clean_review_output(str(result.get("response") or ""))
            detail = result.get("error") if isinstance(result, dict) else result
            self.ctx.logger.warning("[Galgame] /锐评 模型返回失败（%s）：%s", route or "默认任务", detail)
        return ""

    async def _send_review_forward(self, stream_id: str, text: str) -> None:
        """把锐评包成一条「聊天记录」（合并转发）发出去。

        为什么不直接发纯文本：锐评动辄几百字，铺满屏幕还会被别人的消息插断，
        想回看也得翻记录；合并转发只占一个气泡，点开才是完整的一段，转发也方便。
        正文里**不再附「依据：批评空间中央値 xx」这类来源小字** ——
        聊天记录该长得像聊天记录，像谁在群里认真聊过这部作品。

        适配器不支持转发（或转发失败）时自动退回纯文本，功能不受影响。
        """
        nodes = _split_review_nodes(text)
        if len(text) >= 80 and nodes:
            messages = [
                {
                    "nickname": await _bot_nickname(self.ctx),
                    "segments": [{"type": "text", "content": piece}],
                }
                for piece in nodes
            ]
            try:
                sent = await self.ctx.send.forward(messages, stream_id, storage_message=False)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] /锐评 发聊天记录失败，回退纯文本：%s", exc)
                sent = False
            if sent:
                return
            self.ctx.logger.warning("[Galgame] /锐评 转发被拒，回退纯文本")
        await self.ctx.send.text(text, stream_id, storage_message=False)

    async def _cmd_update(self, stream_id: str) -> tuple[bool, str, int]:
        """``/gal update`` —— **增量更新**索引：只重抓榜单前面若干页。

        榜单按中央值降序，新作与分数变化都在前面那一段；深处分段变化慢，
        不值得每次重抓。所以日常用这个就够，全量重建留着兜底。
        """
        egs = self._service.egs if self._service else None
        if not egs:
            await self.ctx.send.text("批评空间源没启用。", stream_id, storage_message=False)
            return True, "未启用", 1
        if egs.building:
            await self.ctx.send.text("索引正在更新了，别催 😤", stream_id, storage_message=False)
            return True, "进行中", 1
        pages = int(self.config.egs.update_pages)
        await self.ctx.send.text(
            f"开始增量更新（只重抓榜单前 {pages} 页，新作与分数变化都在那一段）…",
            stream_id,
            storage_message=False,
        )

        async def _job() -> None:
            try:
                stats = await egs.update_recent(pages=pages)
                await self.ctx.send.text(
                    f"增量更新完成：{stats.get('pages')} 页 / 新增 {stats.get('added')} 条 / "
                    f"更新 {stats.get('updated')} 条 / 索引共 {len(egs.index.items)} 条。",
                    stream_id,
                    storage_message=False,
                )
            except Exception as exc:  # noqa: BLE001
                await self.ctx.send.text(f"增量更新失败：{exc}", stream_id, storage_message=False)

        # 可能已经有一个自动增量任务在跑；引用被覆盖后旧任务就没人能取消（on_unload 只取消最后一个）
        await self._cancel_index_task()
        self._index_task = asyncio.create_task(_job())
        return True, "已开始", 1

    async def _cmd_sync(self, stream_id: str) -> tuple[bool, str, int]:
        egs = self._service.egs if self._service else None
        if not egs:
            await self.ctx.send.text("批评空间源没启用，先去插件设置里打开。", stream_id, storage_message=False)
            return True, "未启用", 1
        if egs.building:
            await self.ctx.send.text("索引正在建了，别催 😤", stream_id, storage_message=False)
            return True, "进行中", 1
        await self.ctx.send.text(
            f"开始重建批评空间索引（最多翻 {self.config.egs.max_pages} 页，慢活，"
            f"期间其它查询照常可用）…",
            stream_id,
            storage_message=False,
        )

        async def _job() -> None:
            try:
                count = await egs.build_index()
                stats = egs.last_build
                await self.ctx.send.text(
                    f"批评空间索引建好了：{count} 条"
                    f"（成功 {stats.get('pages_ok')} 页 / 失败 {stats.get('failed_pages')} 页，"
                    f"耗时 {stats.get('duration')} 秒）。\n{stats.get('stop_reason')}",
                    stream_id,
                    storage_message=False,
                )
            except Exception as exc:  # noqa: BLE001
                await self.ctx.send.text(f"索引建失败：{exc}", stream_id, storage_message=False)

        await self._cancel_index_task()
        self._index_task = asyncio.create_task(_job())
        return True, "已开始", 1

    async def _cmd_random(self, stream_id: str, count: int) -> tuple[bool, str, int]:
        service = self._service
        if not service or not service.ymgal:
            await self.ctx.send.text("月幕源没启用。", stream_id, storage_message=False)
            return True, "未启用", 1
        try:
            items = await service.ymgal.random_games(max(1, min(count, 10)))
        except Exception as exc:  # noqa: BLE001
            await self.ctx.send.text(f"查询失败：{exc}", stream_id, storage_message=False)
            return False, "失败", 1
        lines = [
            f"{it.get('title_zh') or it.get('title')}"
            + (f"（{it.get('released')}）" if it.get("released") else "")
            + ("　有汉化" if it.get("have_chinese") else "")
            for it in items
        ]
        # 和情报 / 预定 / 新作一样记一屏：不然紧接着的「/gal 详情 3」必然找不到
        self._remember_intel(stream_id, items)
        await self._send_release_card(
            stream_id,
            items,
            heading="随机抽取",
            subtitle=f"共 {len(items)} 部",
            fallback="随机抽到：\n" + "\n".join(lines),
        )
        return True, "已输出", 1

    async def _query_new_releases(self, days: int) -> tuple[list[dict[str, Any]], int, str, str]:
        """取最近 N 天发售的作品，返回 (条目, 实际天数, 起始日, 结束日)。

        走月幕的发售日历接口而不是 VNDB 的 ``released`` 过滤，理由是它**带中文名和
        汉化状态** —— 问「最近有什么新作」时这两样恰恰是最想知道的，VNDB 那边只有罗马音标题。

        接口有 **50 天上限**（官方限制），所以这里夹紧。
        """
        service = self._service
        if not service or not service.ymgal:
            raise RuntimeError("月幕源没启用")
        span = max(1, min(int(days), 50))
        today = datetime.date.today()
        start = (today - datetime.timedelta(days=span)).isoformat()
        items = await service.ymgal.new_releases(start, today.isoformat())
        return list(items), span, start, today.isoformat()

    @staticmethod
    def _new_release_lines(
        items: list[dict[str, Any]], *, start: int = 1
    ) -> list[str]:
        """新作列表的纯文本行（命令的回退文本、工具的 content 都用它）。

        带序号：卡片上本来就一条一个数字（gl_render 里 `{index + 1}. {title}`），
        文本回退不编号的话，用户看到的序号和 /gal 详情 <序号> 就对不上了。
        混排两段（最近 + 即将）时用 start 接着上一段往下编。
        """
        return [
            f"{start + i}. {item.get('released') or '?'} "
            f"{item.get('title_zh') or item.get('title')}"
            + (f"（{item.get('developer')}）" if item.get("developer") else "")
            + ("　有汉化" if item.get("have_chinese") else "")
            for i, item in enumerate(items)
        ]

    async def _query_upcoming_releases(self, days: int) -> tuple[list[dict[str, Any]], int, list[str]]:
        """取未来 N 天要发售的作品，返回 (条目, 实际天数, 警告)。

        两个源合起来用：月幕的发售日历页（有厂商名和汉化状态，但没有中文名）与
        VNDB（收录更全、带评分票数，可标题只有罗马音）。任一源挂了都只是少几条，
        失败原因作为警告返回，由调用方附在输出末尾 —— 不吭声地少一半数据更难查。
        """
        news = getattr(self.config, "news", None)
        if news is None or not news.enabled:
            raise RuntimeError("最新情报没启用（配置里的「最新情报」段）")
        service = self._service
        if not service:
            raise RuntimeError("插件未就绪")
        span = max(1, int(days or 0) or int(news.upcoming_days))
        items, warnings = await gl_news.collect_upcoming(
            service.ymgal,
            service.vndb,
            days=span,
            max_items=max(int(news.max_items), 10),
        )
        # 月幕日历给的是日文原名（VNDB 更是罗马音），「预定」那半以前整片日文 ——
        # 用户反馈「说好的翻译呢」。这里把还缺中文名的补翻一遍，用的是同一套提示词；
        # 翻译失败就保留原文，不影响出卡。
        if items and getattr(news, "translate", True):
            try:
                filled = await gl_news.translate_missing(items, self._generate_text)
                self.ctx.logger.info(
                    "[Galgame/情报] 发售标题补翻：%d/%d 条成功", filled, len(items)
                )
                if not filled:
                    self.ctx.logger.warning(
                        "[Galgame/情报] 发售标题一条都没补翻成功，检查模型返回格式（应为 response）"
                    )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame/情报] 发售标题补翻失败：%s", exc)
        return items, span, warnings

    async def _generate_text(self, prompt: str, *, max_tokens: int = 1200) -> str:
        """一次普通的模型调用（新闻标题翻译用）。失败返回空串，调用方保留原文。"""
        try:
            result = await self.ctx.llm.generate(
                prompt=prompt,
                temperature=0.2,
                max_tokens=max_tokens,
                timeout_ms=_LLM_RPC_TIMEOUT_MS,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame/情报] 调模型失败：%s", exc)
            return ""
        if not isinstance(result, dict) or not result.get("success"):
            detail = result.get("error") if isinstance(result, dict) else result
            self.ctx.logger.warning("[Galgame/情报] 模型返回失败：%s", detail)
            return ""
        # 宿主 ctx.llm.generate 回的是 response（见 src/common/data_models/
        # llm_service_data_models.py 的 to_capability_payload，里面**没有** content/text）。
        # 老代码只读 content/text → 永远拿到空串 → 翻译静默退原文，日志还显示「翻译成功」，
        # 线上表现就是「说有翻译，结果还是日文」。content/text 留作兜底，别删。
        text = result.get("response") or result.get("content") or result.get("text") or ""
        if not text:
            self.ctx.logger.warning(
                "[Galgame/情报] 模型返回里没有正文（keys=%s）", sorted(result.keys())
            )
        return str(text)

    async def _collect_news(self, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
        """抓一圈 gal 圈新闻（日文 RSS + 月幕中文文章），按需翻中文。

        返回 (条目, 警告)。单源失败只记警告 —— 新闻这种功能，一个站挂了不该让
        整条命令失败。
        """
        news = getattr(self.config, "news", None)
        if news is None or not news.enabled:
            raise RuntimeError("最新情报没启用（配置里的「最新情报」段）")
        if not self._http:
            raise RuntimeError("插件未就绪")
        items, warnings = await gl_news.collect_news(
            self._http,
            sources=list(news.sources),
            per_source=int(news.per_source),
            max_items=max(int(limit), 1),
            cache_minutes=int(news.cache_minutes),
        )
        if items and news.translate:
            titles = await gl_news.translate_titles(items, self._generate_text)
            if titles:
                gl_news.apply_translations(items, titles)
            # 用户反馈「说有翻译，结果还是日语原文」—— 到底翻了几条必须留痕，
            # 否则翻译整批失败（模型超时/返回格式变了）时只能靠猜。
            # 只数日文条目：月幕的中文条目天生就有 title_zh，把它算进来会让下面
            # 「一条都没翻出来」的告警永远不触发（诊断失真）。
            got = sum(
                1
                for item in items
                if str(item.get("lang") or "") == "ja" and item.get("title_zh")
            )
            want = sum(1 for item in items if str(item.get("lang") or "") == "ja")
            self.ctx.logger.info("[Galgame/情报] 新闻标题翻译：%d 条译文 / %d 条待翻", got, want)
            if want and not got:
                self.ctx.logger.warning(
                    "[Galgame/情报] 有 %d 条日文标题一条都没翻出来，检查模型返回格式（应为 response）", want
                )
        chars = int(news.summary_chars)
        for item in items:
            raw = item.get("summary")
            item["summary"] = (
                gl_news.clean_summary(raw, limit=chars) if chars > 0 else gl_news.strip_html(raw)
            )
        return items, warnings

    def _news_source_text(self) -> str:
        """配置里开的资讯源，翻译成中文标签后去重（b_game 与 bugbug 同名）。"""
        news = getattr(self.config, "news", None)
        names: list[str] = []
        for source in list(getattr(news, "sources", []) or []):
            label = NEWS_LABELS.get(str(source), str(source))
            if label and label not in names:
                names.append(label)
        return " · ".join(names) or "（没配资讯源）"

    @staticmethod
    def _news_lines(items: list[dict[str, Any]], *, start: int = 1) -> list[str]:
        """资讯列表的纯文本行（回退文本与工具的 content 都用它）。

        带序号，和卡片上的编号一致，这样 `/gal 详情 3` 在文本模式下也能用。
        （以前情报是「新闻 + 即将发售」两段拼的，后者要接着前者的号往下编；
        现在情报只出新闻，序号直接从 1 开始。）
        """
        lines: list[str] = []
        for index, item in enumerate(items):
            when = str(item.get("published") or item.get("released") or "")
            label = NEWS_LABELS.get(str(item.get("source") or ""), "")
            head = f"{when} " if when else ""
            tail = f"（{label}）" if label else ""
            lines.append(
                f"{start + index}. {head}{item.get('title_zh') or item.get('title')}{tail}"
            )
        return lines

    async def _cmd_new(self, stream_id: str, days: int) -> tuple[bool, str, int]:
        """/gal new [天数] —— **只出最近 N 天已发售的新作**。

        以前这里「最近 + 即将」两头一起出，和 /gal 预定、/gal 情报 三条命令的内容
        互相重叠（用户反馈「太相似了，各指令的信息要明确」）。现在各管一段：
        new ＝最近已发售，预定 ＝未来待发售，情报 ＝圈内新闻，互不混排。
        """
        span = max(1, min(int(days), 50))
        try:
            past, span, start, end = await self._query_new_releases(days)
        except RuntimeError as exc:
            await self.ctx.send.text(f"{exc}。", stream_id, storage_message=False)
            return True, "未启用", 1
        except Exception as exc:  # noqa: BLE001
            await self.ctx.send.text(f"查询失败：{exc}", stream_id, storage_message=False)
            return False, "失败", 1
        if not past:
            await self.ctx.send.text(
                f"最近 {span} 天（{start} ~ {end}）没有查到已发售的新作，"
                "可以把天数放宽一点（月幕的日历接口最多 50 天）。",
                stream_id,
                storage_message=False,
            )
            return True, "无结果", 1
        picks = past[:10]
        subtitle = f"已发售 {start} ~ {end} 共 {len(past)} 部"
        if len(past) > len(picks):
            subtitle += f"，这里列出前 {len(picks)} 部"
        self._remember_intel(stream_id, picks)
        await self._send_release_card(
            stream_id,
            picks,
            heading=f"新作 · 最近 {span} 天发售",
            subtitle=f"{subtitle}　{self._detail_hint()}",
            fallback="\n".join(
                [f"最近 {span} 天发售："]
                + self._new_release_lines(picks)
                + [self._detail_hint()]
            ),
        )
        return True, "已输出", 1

    def _take_warnings(self) -> str:
        """取走本轮查询里「被忽略掉的条件」提示。

        静默丢条件比报错更糟 —— 用户会以为筛过了，实际拿到未筛选的结果。
        """
        if not self._service:
            return ""
        notes = list(getattr(self._service, "last_warnings", []) or [])
        self._service.last_warnings = []
        return "\n".join(notes)

    async def _cmd_filter(self, stream_id: str, raw: str) -> tuple[bool, str, int]:
        """``/gal <条件…>`` —— 不经过模型的按条件筛选。

        和工具 ``gal_search`` 走的是**同一套** ``service.find()``，区别只在于条件
        由人直接给、结果直接发进聊天，不需要模型参与，也不会因为模型理解偏差而跑偏。
        """
        if not self._service:
            await self.ctx.send.text("插件未就绪。", stream_id, storage_message=False)
            return False, "未就绪", 1
        try:
            params = parse_filter(raw)
        except ValueError as exc:
            await self.ctx.send.text(
                f"条件看不懂：{exc}\n\n{_FILTER_HELP}", stream_id, storage_message=False
            )
            return False, "条件错误", 1
        if not params:
            # 条件为空（裸 /gal 已经去发用法卡了，正常到不了这里）—— 把筛选帮助发出去
            await self.ctx.send.text(_FILTER_HELP, stream_id, storage_message=False)
            return True, "已输出帮助", 1

        cfg = self.config
        query = str(params.get("query") or "")
        count = max(1, min(int(params.get("count") or 0) or int(cfg.recommend.default_count), 10))
        offset = max(0, int(params.get("offset") or 0))
        if offset >= MAX_OFFSET:
            await self.ctx.send.text(
                f"已经翻到底了：同一组条件最多只能翻到第 {MAX_OFFSET} 条，"
                "再往后没有更多作品；把 offset 调小，或换组条件。",
                stream_id,
                storage_message=False,
            )
            return False, "翻页到底", 1
        min_egs = float(params.get("min_egs") or 0)
        max_egs = float(params.get("max_egs") or 0)
        min_rating = float(params.get("min_rating") or 0)
        max_rating = float(params.get("max_rating") or 0)
        min_hours = float(params.get("min_hours") or 0)
        max_hours = float(params.get("max_hours") or 0)
        tags = list(params.get("tags") or [])
        year_from = int(params.get("year") or 0)
        votes = int(params.get("votes") or 0)
        sort = str(params.get("sort") or "votes")

        try:
            picks = await self._service.find(
                keyword=query,
                min_egs=min_egs,
                max_egs=max_egs,
                min_rating=min_rating,
                max_rating=max_rating,
                min_votes=votes,
                length_min_minutes=int(min_hours * 60) if min_hours else 0,
                length_max_minutes=int(max_hours * 60) if max_hours else 0,
                tags=tags,
                year_from=year_from,
                exclude_restricted=bool(cfg.recommend.exclude_restricted),
                prefer_chinese=bool(cfg.recommend.prefer_chinese),
                sort=sort,
                count=count,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] /gal <条件> 失败：%s", exc)
            await self.ctx.send.text(f"筛选失败：{exc}", stream_id, storage_message=False)
            return False, "失败", 1

        condition = self._condition_text(
            query, min_egs, max_egs, min_rating, max_rating,
            votes, min_hours, max_hours, tags, year_from, offset,
        )
        warn = self._take_warnings()
        hint = _hours_hint(params)
        if hint:
            warn = f"{warn}\n{hint}" if warn else hint
        if not picks:
            tail = self._empty_hint(
                min_egs=min_egs,
                max_egs=max_egs,
                min_rating=min_rating,
                max_rating=max_rating,
                offset=offset,
            )
            await self.ctx.send.text(
                f"按「{condition}」没筛出作品。"
                + (f"\n{warn}" if warn else "")
                + (f"\n{tail}" if tail else ""),
                stream_id,
                storage_message=False,
            )
            return True, "无结果", 1

        if warn:
            await self.ctx.send.text(warn, stream_id, storage_message=False)
        await self._send_recommend(stream_id, picks, condition)
        return True, "已输出", 1

    async def _cmd_genre(self, stream_id: str, rest: str) -> tuple[bool, str, int]:
        """``/gal 类型 SLG`` —— 按 EGS 的**玩法类型**筛作品。

        用的是批评空间自己的分类（VN / RPG / SLG / ACT / STG / 3D / 乙女 / BL…），
        VNDB 的标签体系里没有这一套，所以「想找个 SRPG」这类需求只能靠它。

        类型后面还能跟普通筛选条件：``/gal 类型 RPG 85+ n5``。
        """
        parts = rest.split()
        key = parts[0].lower() if parts else ""
        att_id = _ATT_GENRES.get(key)
        if att_id is None:
            await self.ctx.send.text(
                f"用法：/gal 类型 <类型> [其它条件]\n可用：{_ATT_GENRE_HELP}\n"
                "例：/gal 类型 RPG · /gal 类型 SLG 85+ n5",
                stream_id,
                storage_message=False,
            )
            return True, "已输出帮助", 1
        if not self._service or not self._service.egs:
            await self.ctx.send.text("批评空间源没启用。", stream_id, storage_message=False)
            return True, "未启用", 1

        # 类型后面允许继续跟普通条件（`85+`、`n5` 这些）
        try:
            extra = parse_filter(" ".join(parts[1:])) if len(parts) > 1 else {}
        except ValueError as exc:
            await self.ctx.send.text(
                f"条件看不懂：{exc}\n\n{_FILTER_HELP}", stream_id, storage_message=False
            )
            return False, "条件错误", 1
        min_hours = float(extra.get("min_hours") or 0)
        max_hours = float(extra.get("max_hours") or 0)
        count = max(
            1,
            min(int(extra.get("count") or 0) or int(self.config.recommend.default_count), 10),
        )

        try:
            picks = await self._service.find(
                keyword=str(extra.get("query") or ""),
                att_id=att_id,
                min_egs=float(extra.get("min_egs") or 0),
                max_egs=float(extra.get("max_egs") or 0),
                min_rating=float(extra.get("min_rating") or 0),
                max_rating=float(extra.get("max_rating") or 0),
                min_votes=int(extra.get("votes") or 0),
                year_from=int(extra.get("year") or 0),
                length_min_minutes=int(min_hours * 60) if min_hours else 0,
                length_max_minutes=int(max_hours * 60) if max_hours else 0,
                tags=list(extra.get("tags") or []),
                sort=str(extra.get("sort") or "votes"),
                count=count,
                offset=int(extra.get("offset") or 0),
                exclude_restricted=bool(self.config.recommend.exclude_restricted),
                prefer_chinese=bool(self.config.recommend.prefer_chinese),
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] /gal 类型 失败：%s", exc)
            await self.ctx.send.text(f"筛选失败：{exc}", stream_id, storage_message=False)
            return False, "失败", 1
        warn = self._take_warnings()
        if warn:
            await self.ctx.send.text(warn, stream_id, storage_message=False)
        if not picks:
            await self.ctx.send.text(
                f"「{key}」这个类型没筛出作品（也可能批评空间连不上，或本地索引还没铺好）。",
                stream_id,
                storage_message=False,
            )
            return True, "无结果", 1
        await self._send_recommend(stream_id, picks, f"类型 {key}")
        return True, "已输出", 1

    async def _cmd_year(self, stream_id: str, rest: str) -> tuple[bool, str, int]:
        """``/gal 年份 2015`` —— 某一年的中央值排行。

        批评空间有**专门的按年份排行页**，和全局榜单是两份数据（列数都不同）。
        直接问「那一年的高分作」比拿发售日再本地过滤更准。
        """
        year = int(rest.strip()) if (rest.strip().isascii() and rest.strip().isdigit()) else 0
        if not 1990 <= year <= 2100:
            await self.ctx.send.text(
                "用法：/gal 年份 2015（查看该年的中央值排行）",
                stream_id,
                storage_message=False,
            )
            return True, "已输出帮助", 1
        if not self._service or not self._service.egs:
            await self.ctx.send.text("批评空间源没启用。", stream_id, storage_message=False)
            return True, "未启用", 1
        try:
            rows = await self._service.egs.year_ranking(year)
            picks = await self._service.build_from_rows(
                rows, count=int(self.config.recommend.default_count)
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] /gal 年份 失败：%s", exc)
            await self.ctx.send.text(f"查询失败：{exc}", stream_id, storage_message=False)
            return False, "失败", 1
        if not picks:
            await self.ctx.send.text(
                f"{year} 年没查到排行（也可能是该年上榜作品为空）。",
                stream_id,
                storage_message=False,
            )
            return True, "无结果", 1
        await self._send_recommend(stream_id, picks, f"{year} 年 · 中央值排行")
        return True, "已输出", 1

    async def _cmd_datacount(self, stream_id: str, rest: str) -> tuple[bool, str, int]:
        """``/gal 人气`` —— 按**票数**排行。

        和按中央值排是**两个视角**：中央值告诉你「评价最高」，
        票数告诉你「玩的人最多 / 最有名」—— 后者更适合找入坑作。
        """
        del rest
        if not self._service or not self._service.egs:
            await self.ctx.send.text("批评空间源没启用。", stream_id, storage_message=False)
            return True, "未启用", 1
        try:
            rows = await self._service.egs.datacount_ranking(limit=200)
            picks = await self._service.build_from_rows(
                rows, count=int(self.config.recommend.default_count)
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] /gal 人气 失败：%s", exc)
            await self.ctx.send.text(f"查询失败：{exc}", stream_id, storage_message=False)
            return False, "失败", 1
        if not picks:
            await self.ctx.send.text("没查到排行。", stream_id, storage_message=False)
            return True, "无结果", 1
        await self._send_recommend(stream_id, picks, "按票数排行（名气最大的一批）")
        return True, "已输出", 1

    async def _send_recommend(
        self, stream_id: str, picks: list[GameInfo], condition: str
    ) -> None:
        """列表输出：图卡优先，渲染不出来就退纯文本。

        命令路径**没有模型兜底**（工具路径卡片挂了还能靠模型把 content 说出来），
        所以这里必须有文本回退，否则用户会什么都收不到。
        """
        if len(picks) == 1:
            if self.config.output.send_card and await self._send_info_card(picks[0], stream_id):
                return
            await self.ctx.send.text(self._summary_text(picks[0]), stream_id, storage_message=False)
            return

        if self.config.output.send_card:
            try:
                html = await build_recommend_html(
                    picks,
                    http=self._http,
                    accent=self.config.output.accent_color,
                    width=int(self.config.output.card_width),
                    condition_text=condition,
                )
                if await self._render_and_send(html, stream_id):
                    return
                self.ctx.logger.warning("[Galgame] 推荐卡没渲染出图片，退回文本")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] 推荐卡渲染失败（退回文本）：%s", exc)
        await self.ctx.send.text(
            f"按「{condition}」找到：\n" + "\n".join(build_search_lines(picks)),
            stream_id,
            storage_message=False,
        )

    async def _tool_new_releases(self, stream_id: str, days: int, count: int) -> dict[str, Any]:
        """「最近有什么新 gal 发售」的自然语言入口 —— **只答已发售的那一段**。

        和 :meth:`_cmd_new` 共用查询与呈现，区别只在于这个把结果**回给模型**
        （结构化 + 纯文本），而不只是发给人看。未来要出什么归 gal_news 的
        mode=upcoming，这里不再两头混着答。
        """
        try:
            items, span, start, end = await self._query_new_releases(days)
        except RuntimeError as exc:
            return {"success": True, "content": f"{exc}，查不了新作发售日历。"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"查询新作失败：{exc}"}
        if not items:
            return {
                "success": True,
                "content": (
                    f"最近 {span} 天（{start} ~ {end}）没有查到已发售的新作记录。"
                    "可以把时间范围放宽，比如 30 天或 50 天（月幕的日历接口最多支持 50 天）。"
                    "想知道「接下来要出什么」，改用 gal_news 的 mode=upcoming。"
                ),
            }
        # 问「最近有什么新作」时 5 条太少，最低给 8 条
        shown = items[: max(count, 8)]
        lines = self._new_release_lines(shown)
        subtitle = f"已发售 {start} ~ {end} 共 {len(items)} 部"
        if len(items) > len(shown):
            subtitle += f"，这里列出前 {len(shown)} 部"
        if stream_id:
            await self._send_release_card(
                stream_id,
                shown,
                heading=f"新作 · 最近 {span} 天发售",
                subtitle=subtitle,
                fallback="\n".join([f"最近 {span} 天发售："] + lines),
            )
        return {
            "success": True,
            "content": (
                f"{start} ~ {end} 期间发售的 galgame，共 {len(items)} 部"
                + (f"，这里列出 {len(shown)} 部" if len(items) > len(shown) else "")
                + "（月幕的日历数据，没有评分信息）：\n"
                + "\n".join(lines)
            ),
            "results": [
                {
                    "作品": item.get("title_zh") or item.get("title"),
                    "原名": item.get("title"),
                    "发售日": item.get("released"),
                    "厂商": item.get("developer"),
                    "有汉化": bool(item.get("have_chinese")),
                }
                for item in shown
            ],
        }

    async def _send_release_card(
        self,
        stream_id: str,
        items: list[dict[str, Any]],
        *,
        heading: str,
        subtitle: str,
        fallback: str,
    ) -> None:
        """发送发售列表卡。

        和资料卡一样的规矩：**图片是锦上添花，文本才是保底**。渲染或取图失败
        （宿主的浏览器没装好、CDN 抽风）就退回文本，绝不能因此什么都不发。
        """
        if self.config.output.send_card and items:
            try:
                from gl_render import build_release_html

                html = await build_release_html(
                    items,
                    http=self._http,
                    accent=self.config.output.accent_color,
                    width=int(self.config.output.card_width),
                    heading=heading,
                    subtitle=subtitle,
                )
                if await self._render_and_send(html, stream_id):
                    return
                self.ctx.logger.warning("[Galgame] 列表卡没渲染出图片，退回文本")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] 列表卡渲染失败（退回文本）：%s", exc)
        await self.ctx.send.text(fallback, stream_id, storage_message=False)

    async def _cmd_status(self, stream_id: str) -> tuple[bool, str, int]:
        cfg = self.config
        lines = [
            "【galgame领域大神】",
            f"数据源：月幕={'开' if cfg.source.ymgal_enabled else '关'} · "
            f"VNDB={'开' if cfg.source.vndb_enabled else '关'} · "
            f"批评空间={'开' if cfg.source.egs_enabled else '关'}",
            f"批评空间索引：{self._index_desc()}",
            f"详情页（评价数据）：{'抓' if cfg.egs.fetch_detail else '不抓'}；"
            f"卡片：{'开' if cfg.output.send_card else '关'}；代理：{cfg.source.proxy or '（直连）'}",
            f"最新情报：{'开' if cfg.news.enabled else '关'}"
            + (f"（{self._news_source_text()}）" if cfg.news.enabled else ""),
            "用法：直接问就行 ——「XX 好玩吗」「推荐批评空间 85 分以上的短篇」「XX 有多长」「XX 的声优是谁」。",
            "命令：/gal help 用法卡 · /锐评 <作品名> 让模型锐评 · "
            "/gal <条件> 手动筛选（例：/gal 85+ 10h）· "
            "/gal 类型 SLG 按玩法类型 · /gal 年份 2015 某年排行 · /gal 人气 按票数排行 · "
            "/gal update 增量更新 · /gal sync 全量重建 · /gal random 随机 · "
            "/gal new（新作）[天数] 最近已发售的 · /gal 预定 [天数] 未来待发售 · "
            "/gal 情报 [条数] 只出圈内新闻 · /gal 详情 <序号>（或直接 /gal 3）看某一条 · "
            "/gal 话术 贴吧话术（状态/刷新/体检/打标/恢复/清理）",
        ]
        await self.ctx.send.text("\n".join(lines), stream_id, storage_message=False)
        return True, "已输出状态", 1

    async def _cmd_help(self, stream_id: str) -> tuple[bool, str, int]:
        """``/gal help`` —— 发一张用法卡（渲染不出来就退回纯文本用法）。

        排版和文案在 gl_render.build_help_html 里，这里只管取图（插件目录下
        assets/help/ 里的图片，有就用、没有就不放）和失败回退。用法卡的主色
        固定是内置的暖金（output.accent_color 是白底资料卡的标题色，压不到
        这张深色卡上），所以这里不传 accent。
        """
        from gl_render import HELP_SECTIONS, build_help_html, help_asset_data_uris

        # 文本保底直接由卡片文案生成，省得两处各写一份、改了图忘了文
        lines = ["galgame 领域大神 · 用法"]
        for section, items in HELP_SECTIONS:
            lines.append(f"【{section}】")
            lines += [f"{cmd}　{desc}" for cmd, desc in items]
        if self.config.output.send_card:
            try:
                assets = help_asset_data_uris(_os.path.join(_PLUGIN_DIR, "assets", "help"))
                html = build_help_html(
                    version=_PLUGIN_VERSION,
                    width=int(self.config.output.card_width),
                    assets=assets,
                    bot_name=await _host_bot_name(self.ctx),
                )
                if await self._render_and_send(html, stream_id):
                    return True, "已输出", 1
                self.ctx.logger.warning("[Galgame] 用法卡没渲染出图片，退回文本")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] 用法卡渲染失败（退回文本）：%s", exc)
        await self.ctx.send.text("\n".join(lines), stream_id, storage_message=False)
        return True, "已输出", 1

    # -- 最新情报（新闻 / 预定） -------------------------------------------

    async def _send_news_card(
        self,
        stream_id: str,
        items: list[dict[str, Any]],
        *,
        heading: str,
        subtitle: str,
        fallback: str,
    ) -> None:
        """发送资讯卡（同一张卡也用来混排预定条目，渲染层两套字段都认）。

        规矩和发售卡一样：**图片锦上添花、文本才是保底** —— 渲染失败就退回文本。
        """
        if self.config.output.send_card and items:
            try:
                from gl_render import build_news_html

                html = await build_news_html(
                    items,
                    http=self._http,
                    accent=self.config.output.accent_color,
                    width=int(self.config.output.card_width),
                    heading=heading,
                    subtitle=subtitle,
                )
                if await self._render_and_send(html, stream_id):
                    return
                self.ctx.logger.warning("[Galgame] 资讯卡没渲染出图片，退回文本")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] 资讯卡渲染失败（退回文本）：%s", exc)
        await self.ctx.send.text(fallback, stream_id, storage_message=False)

    async def _send_detail_card(
        self,
        stream_id: str,
        item: dict[str, Any],
        *,
        heading: str,
        subtitle: str,
        fallback: str,
    ) -> None:
        """发送详情卡（大图 + 标题 + 整篇正文）。

        和列表卡分开：正文动辄两三千字，塞进列表那种一行一条的版式没法看。
        同样是「图片锦上添花、文本才是保底」，渲染不出来就退回文本。
        """
        if self.config.output.send_card and item:
            try:
                from gl_render import build_detail_html

                html = await build_detail_html(
                    item,
                    http=self._http,
                    accent=self.config.output.accent_color,
                    width=int(self.config.output.card_width),
                    heading=heading,
                    subtitle=subtitle,
                )
                if await self._render_and_send(html, stream_id):
                    return
                self.ctx.logger.warning("[Galgame] 详情卡没渲染出图片，退回文本")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] 详情卡渲染失败（退回文本）：%s", exc)
        await self.ctx.send.text(fallback, stream_id, storage_message=False)

    @staticmethod
    def _with_notes(lines: list[str], notes: list[str]) -> list[str]:
        """把降级警告附在结果末尾 —— 少了几条要说得出来，别让人以为就这么多。"""
        return lines + ([""] + list(notes) if notes else [])

    # -- 序号详情 ---------------------------------------------------------

    def _remember_intel(self, stream_id: str, items: list[dict[str, Any]]) -> None:
        """记住刚列出的这一屏，供 /gal 详情 <序号> 回查（按会话分开，30 分钟过期）。

        只存内存、不落盘：卡上那一屏本来就是临时的，重启后重新列一次就行；
        落盘反而要处理条目过期和记录膨胀。
        """
        if not stream_id or not items:
            return
        now = time.monotonic()
        for key, cached in list(self._intel_cache.items()):
            if now - cached[0] > _INTEL_CACHE_TTL:
                self._intel_cache.pop(key, None)
        while len(self._intel_cache) >= _INTEL_CACHE_MAX:
            oldest = min(self._intel_cache, key=lambda k: self._intel_cache[k][0])
            self._intel_cache.pop(oldest, None)
        self._intel_cache[stream_id] = (now, list(items))

    def _recall_intel(self, stream_id: str, index: int) -> dict[str, Any] | None:
        """取回序号对应的那一条；没有 / 过期 / 越界都返回 None。"""
        cached = self._intel_cache.get(stream_id)
        if not cached:
            return None
        stamp, items = cached
        if time.monotonic() - stamp > _INTEL_CACHE_TTL:
            self._intel_cache.pop(stream_id, None)
            return None
        if 1 <= index <= len(items):
            return items[index - 1]
        return None

    @staticmethod
    def _detail_hint() -> str:
        return "看详情：/gal 详情 <序号>（30 分钟内有效）"

    @staticmethod
    def _detail_lines(item: dict[str, Any]) -> list[str]:
        """一条新闻 / 一部预定的详情文本（卡片渲染失败时的保底）。"""
        title = str(item.get("title_zh") or item.get("title") or "")
        original = str(item.get("title") or "")
        lines = [f"【{title}】"]
        if original and original != title:
            lines.append(f"原名：{original}")
        when = item.get("published") or item.get("released") or ""
        if when:
            lines.append(f"时间：{when}")
        label = NEWS_LABELS.get(str(item.get("source") or ""), "")
        if label:
            lines.append(f"来源：{label}")
        if item.get("developer"):
            lines.append(f"厂商：{item['developer']}")
        flags: list[str] = []
        if item.get("have_chinese"):
            flags.append("有中文版")
        if item.get("restricted"):
            flags.append("18+")
        score = item.get("score") or item.get("rating")
        if score:
            flags.append(f"VNDB {score}")
        if item.get("votecount"):
            flags.append(f"{item['votecount']} 票")
        if flags:
            lines.append("　".join(flags))
        text = str(item.get("body") or item.get("summary") or "").strip()
        if text:
            lines.append("")
            lines.append(gl_news.clean_summary(text, limit=0))
        url = str(item.get("url") or "")
        if url:
            lines.append("")
            lines.append(url)
        return lines

    async def _fetch_full_article(self, item: dict[str, Any], *, limit: int) -> str:
        """按需把这一条的原文页整篇抓回来（详情专用）。失败返回空串。

        RSS 里的正文是截断过的，只有点开详情才值得再跑一趟站点；抓不到就让调用方
        退回 RSS 那段摘要 —— 详情宁可短一点，也不能空白。
        """
        url = str(item.get("url") or "").strip()
        if not url:
            return ""
        try:
            return await gl_news.fetch_article(self._http, url, limit=limit)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame/情报] 详情抓原文失败：%s", exc)
            return ""

    async def _translate_detail(self, body: str) -> str:
        """详情正文的翻译：按段落分块翻，逐块降级。

        ``translate_text`` 一次只吃 900 字，整篇丢进去会被截断（后半段还是日文），
        所以这里自己切块。任何一块翻不动（模型挂了 / 那块本来就是中文）就原样保留
        那一块 —— 混着日文总比丢掉半篇文章好。
        """
        chunks = _split_text_chunks(body, _DETAIL_CHUNK_CHARS)
        parts: list[str] = []
        for chunk in chunks:
            if not gl_news.looks_japanese(chunk):
                parts.append(chunk)
                continue
            try:
                zh = await gl_news.translate_text(chunk, self._generate_text, limit=0)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame/情报] 详情正文翻译失败（保留原文）：%s", exc)
                zh = ""
            parts.append(zh.strip() if zh else chunk)
        return "\n\n".join(part for part in parts if part)

    async def _detail_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """详情要显示的那一条：先抓整篇正文，日文正文顺手翻成中文。

        列表页给的是截断摘要，所以这里**另跑一趟原文页**（``news.detail_full``）；
        抓不到 / 关掉了就退回 RSS 里那段更长的描述。翻不动（模型挂了 / 关掉翻译）
        就照原样显示日文 —— 详情页空着比日文更糟。
        """
        detail = dict(item)
        news = getattr(self.config, "news", None)
        limit = int(getattr(news, "detail_chars", 0) or 0) if news is not None else 0
        body = str(detail.get("body") or detail.get("summary") or "").strip()

        if news is None or bool(getattr(news, "detail_full", True)):
            full = await self._fetch_full_article(item, limit=limit)
            if full:
                body = full

        if limit > 0 and len(body) > limit:
            body = body[: max(1, limit - 1)].rstrip() + "…"
        detail["body"] = body
        detail["summary"] = body

        if body and news is not None and news.translate and gl_news.looks_japanese(body):
            zh = await self._translate_detail(body)
            if zh:
                detail["body"] = zh
                detail["summary"] = zh
        return detail

    async def _cmd_detail(self, stream_id: str, raw: str) -> tuple[bool, str, int]:
        """``/gal 详情 <序号>``（等价于直接打 ``/gal 3``）—— 看刚列出那一屏里的某一条。

        序号用的是卡片和文本里都标了的编号，所以「先列一屏，再挑一条细看」这条
        路径在图片模式和纯文本模式下都走得通。
        """
        text = str(raw or "").strip()
        if not (text.isascii() and text.isdigit()):
            await self.ctx.send.text(
                "用法：/gal 详情 <序号>，序号是刚列出的那一屏里的编号"
                "（也可以直接打 /gal 3）。先 /gal 情报 或 /gal 预定 列一屏吧。",
                stream_id,
                storage_message=False,
            )
            return True, "用法", 1
        index = int(text)
        item = self._recall_intel(stream_id, index)
        if item is None:
            cached = self._intel_cache.get(stream_id)
            if cached:
                msg = (
                    f"最近那一屏只有 {len(cached[1])} 条，序号要在这个范围里"
                    "（也可能已经过了 30 分钟）。重新 /gal 情报 列一次吧。"
                )
            else:
                msg = "这边还没有列过新闻/预定。先 /gal 情报（或 /gal 预定）列一屏，再 /gal 详情 <序号>。"
            await self.ctx.send.text(msg, stream_id, storage_message=False)
            return True, "无记录", 1
        # 详情要整篇正文：列表页只有截断摘要，这里再跑一趟原文页把全文抓回来，
        # 日文正文分块翻成中文（抓不到 / 翻不动都逐级降级，不会发空）
        detail = await self._detail_item(item)
        await self._send_detail_card(
            stream_id,
            detail,
            heading="详情",
            subtitle=NEWS_LABELS.get(str(item.get("source") or ""), ""),
            fallback="\n".join(self._detail_lines(detail)),
        )
        return True, "已输出", 1

    async def _cmd_upcoming(self, stream_id: str, days: int) -> tuple[bool, str, int]:
        news = self.config.news
        try:
            items, span, warnings = await self._query_upcoming_releases(days)
        except RuntimeError as exc:
            await self.ctx.send.text(f"{exc}。", stream_id, storage_message=False)
            return True, "未启用", 1
        except Exception as exc:  # noqa: BLE001
            await self.ctx.send.text(f"查询失败：{exc}", stream_id, storage_message=False)
            return False, "失败", 1
        picks = items[: max(int(news.max_items), 10)]
        if not picks:
            text = f"未来 {span} 天暂时没有查到预定发售。"
            if warnings:
                text += "\n" + "\n".join(warnings)
            await self.ctx.send.text(text, stream_id, storage_message=False)
            return True, "无结果", 1
        today = datetime.date.today()
        subtitle = (
            f"{today.isoformat()} ~ {(today + datetime.timedelta(days=span)).isoformat()}"
            f"　共 {len(items)} 部"
        )
        if len(items) > len(picks):
            subtitle += f"，这里列出前 {len(picks)} 部"
        self._remember_intel(stream_id, picks)
        await self._send_release_card(
            stream_id,
            picks,
            heading=f"未来 {span} 天预定",
            subtitle=f"{subtitle}　{self._detail_hint()}",
            fallback="\n".join(
                self._with_notes(self._new_release_lines(picks), warnings)
                + [self._detail_hint()]
            ),
        )
        return True, "已输出", 1

    async def _cmd_intel(self, stream_id: str, count: int) -> tuple[bool, str, int]:
        """/gal 情报 [条数] —— **只出圈内新闻**。

        以前这里把「新闻 + 即将发售」拼成一屏，和 /gal 预定、/gal new 重叠，
        用户反馈三条命令给的东西太像。现在情报只给新闻，发售相关的交给
        /gal 预定（未来）和 /gal new（最近已发售）。
        """
        news = self.config.news
        limit = int(count) or int(news.max_items)
        warnings: list[str] = []
        items: list[dict[str, Any]] = []
        try:
            items, warns = await self._collect_news(limit)
            warnings += warns
        except RuntimeError as exc:
            await self.ctx.send.text(f"{exc}。", stream_id, storage_message=False)
            return True, "未启用", 1
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"抓新闻失败：{exc}")
        if not items:
            text = "这次没抓到新闻（源可能挂了，或者站点改版了）。"
            if warnings:
                text += "\n" + "\n".join(warnings)
            await self.ctx.send.text(text, stream_id, storage_message=False)
            return True, "无结果", 1
        picks = items[:limit]
        body = self._news_lines(picks)
        self._remember_intel(stream_id, picks)
        await self._send_news_card(
            stream_id,
            picks,
            heading="gal 圈新闻",
            subtitle=f"{self._news_source_text()}　共 {len(items)} 条　{self._detail_hint()}",
            fallback="\n".join(self._with_notes(body, warnings) + [self._detail_hint()]),
        )
        return True, "已输出", 1

    # -- 贴吧话术 ---------------------------------------------------------

    def _maybe_start_style_loop(self) -> None:
        """按配置起一个后台循环，到点自己去贴吧学话术。

        只在插件启用着的时候跑；抓取或提炼失败一律吞掉写日志 ——
        学话术是锦上添花，不能因为它把插件搞挂。
        """
        style = getattr(self.config, "style", None)
        if style is None or not style.enabled or not style.auto_learn:
            return
        if self._style_task is not None and not self._style_task.done():
            return
        try:
            self._style_task = asyncio.create_task(self._style_loop())
        except RuntimeError as exc:  # 没有事件循环，等 /gal 话术 刷新 手动来
            self._style_task = None
            self.ctx.logger.debug("[Galgame] 话术循环起不来：%s", exc)

    async def _cancel_style_task(self) -> None:
        task = self._style_task
        self._style_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] 话术任务退出时出错：%s", exc)

    def _style_inject_kinds(self) -> tuple[str, ...]:
        """当前允许写进麦麦表（也就是会被麦麦挑去用）的两类。

        表达方式和黑话各有一个开关。关掉的那一类既不再写，也要把已经写进去的撤掉 ——
        宿主的注入是「表里有什么就用什么」，插件这一侧只有不写/撤行才管得住它。
        """
        style = getattr(self.config, "style", None)
        if style is None:
            return ("expressions", "jargons")
        kinds: list[str] = []
        if bool(getattr(style, "inject_expressions", True)):
            kinds.append("expressions")
        if bool(getattr(style, "inject_jargons", True)):
            kinds.append("jargons")
        return tuple(kinds)

    def _maybe_sync_style_switches(self) -> None:
        """把麦麦表调成「两个注入开关说了算」的样子（后台跑，不卡加载）。

        关掉的桶：撤掉已经写进去的行，语料留在花名册里（keep=True）。
        打开的桶：有留档就写回去 —— 「关掉插件再打开」和「关掉注入再打开」用的是同一条路，
        所以以前那个只认 clean_on_disable 的写回逻辑就并到这里了。
        两边都不需要动的时候一个任务都不起。
        """
        ledger = self._style_ledger
        if ledger is None or getattr(self.ctx, "db", None) is None:
            return
        kinds = self._style_inject_kinds()
        need_clear = any(ledger.written(b) for b in ("expressions", "jargons") if b not in kinds)
        need_restore = any(ledger.pending_restore(b) for b in kinds)
        if not need_clear and not need_restore:
            return
        if self._style_sync_task is not None and not self._style_sync_task.done():
            return
        try:
            self._style_sync_task = asyncio.create_task(self._sync_style_bg())
        except RuntimeError as exc:  # 没有事件循环，等 /gal 话术 刷新、恢复 手动来
            self._style_sync_task = None
            self.ctx.logger.debug("[Galgame] 话术注入开关同步起不来：%s", exc)

    async def _sync_style_bg(self) -> None:
        ledger = self._style_ledger
        db = getattr(self.ctx, "db", None)
        if ledger is None or db is None:
            return
        kinds = self._style_inject_kinds()
        off = tuple(b for b in ("expressions", "jargons") if b not in kinds)
        try:
            if off:
                stats = await gl_style.clear_written(db, ledger, kinds=off, keep=True)
                self.ctx.logger.info(
                    "[Galgame] 话术注入开关：撤掉 %s（删 %s 行 · 留档 %s 行 · 你改过的 %s 行）",
                    "、".join(off),
                    stats["deleted"],
                    stats["missing"],
                    stats["changed"],
                )
            if kinds:
                stats = await gl_style.restore_written(db, ledger, kinds=kinds)
                if stats["restored"] or stats["failed"]:
                    self.ctx.logger.info(
                        "[Galgame] 话术注入开关：写回 %s 条（本来就在麦麦表里 %s 条 · 失败 %s 条）",
                        stats["restored"],
                        stats["alive"],
                        stats["failed"],
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] 话术注入开关同步失败：%s", exc)

    async def _cancel_style_sync(self) -> None:
        task = self._style_sync_task
        self._style_sync_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] 话术开关同步任务退出时出错：%s", exc)

    async def _style_loop(self) -> None:
        style = self.config.style
        interval = max(1.0, float(style.interval_hours)) * 3600.0
        # 加载完先歇一会儿：宿主启动那阵子正忙，这时候去戳贴吧没意义
        await asyncio.sleep(min(120.0, interval / 4.0))
        while True:
            if (
                not self.config.style.enabled
                or not self.config.style.auto_learn
                or not self._style_inject_kinds()
            ):
                return
            try:
                if self._style_learn_lock.locked():
                    # 手动 /gal 话术 刷新 正在跑，这一轮跳过，别重复抓贴吧
                    text = ""
                else:
                    async with self._style_learn_lock:
                        text = await self._run_style_learn()
                        if text:
                            self.ctx.logger.info(
                                "[Galgame] 贴吧话术：%s", text.replace("\n", " | ")
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame] 贴吧话术这一轮失败（等下一轮）：%s", exc)
            await asyncio.sleep(interval)

    async def _extract_style(
        self, forum: str, dump: Any, *, target: int
    ) -> gl_style.StyleCorpus:
        """把抓来的帖子分批丢给模型提炼，凑够 target 条表达就收手。"""
        corpus = gl_style.StyleCorpus()
        units: list[str] = []
        for thread, posts in dump.posts:
            group = [f"【{thread.title}】（回复 {thread.replies}）"]
            group.extend(post.content for post in posts if post.content)
            unit = "\n".join(group).strip()
            if unit:
                units.append(unit)
        if not units:
            return corpus
        want = max(5, int(target))
        for batch in gl_style.chunked(units, 6):
            # 每批只让它写十来条：条数要多了模型会顶到 max_tokens，
            # JSON 会烂在中间（已加 _repair_json 兜底，但截断本身就等于白跑）。
            prompt, used = gl_style.build_extract_prompt(
                forum, list(batch), max_phrases=14, max_jargons=8
            )
            if not used:
                break
            try:
                result = await self.ctx.llm.generate(
                    prompt=prompt,
                    temperature=0.4,
                    max_tokens=2600,
                    timeout_ms=_LLM_RPC_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame/话术] 调模型失败（跳过这批）：%s", exc)
                continue
            if not isinstance(result, dict) or not result.get("success"):
                detail = result.get("error") if isinstance(result, dict) else result
                self.ctx.logger.warning("[Galgame/话术] 模型返回失败（跳过这批）：%s", detail)
                continue
            reply = str(result.get("response") or "")
            corpus.merge(gl_style.parse_extract_reply(reply, forum=forum))
            if len(corpus.phrases) >= want:
                break
        return corpus

    async def _run_style_learn(self, forums: list[str] | None = None) -> str:
        """抓一轮 → 提炼 → 写进宿主表。返回给人看的逐吧说明。"""
        style = self.config.style
        if not style.enabled:
            return "贴吧话术没开（配置 → 贴吧话术 → 启用）"
        kinds = self._style_inject_kinds()
        if not kinds:
            return (
                "表达方式和黑话的注入都关着（配置 → 贴吧话术 → 注入表达方式 / 注入黑话），"
                "本轮不学 —— 学完也没地方用，就省下这趟抓取和模型调用。"
            )
        client = self._tieba
        db = getattr(self.ctx, "db", None)
        ledger = self._style_ledger
        if client is None or db is None or ledger is None:
            return "话术模块没准备好（缺数据库能力或抓取客户端）"

        targets = await gl_style.resolve_session_ids(
            getattr(self.ctx, "chat", None), list(style.chat_ids)
        )
        if style.chat_ids and not targets:
            return (
                "配的群一个都没查到会话 id —— 机器人得先在那几个群里说过话，"
                "否则宁可不写也不要写错群。本轮不写库。"
            )
        # 没配群 = 写成全局（session_id=None / is_global=True）
        session_ids: list[str | None] = list(targets.values()) or [None]

        names = [str(x).strip() for x in (forums or list(style.forums)) if str(x).strip()]
        if not names:
            return "一个吧都没配（配置 → 贴吧话术 → 吧名列表）"

        lines: list[str] = []
        # 刚把开关打开的话，上次撤下来的语料在这里写回去
        if any(ledger.pending_restore(b) for b in kinds):
            back = await gl_style.restore_written(db, ledger, kinds=kinds)
            if back["restored"] or back["failed"]:
                lines.append(
                    f"把上次撤下来的语料写回去：{back['restored']} 条"
                    f"（本来就在麦麦表里 {back['alive']} 条 · 失败 {back['failed']} 条）"
                )
        for forum in names:
            try:
                dump = await client.dump_forum(
                    forum,
                    list_pages=int(style.list_pages),
                    max_threads=int(style.posts_per_forum),
                    min_replies=int(style.min_replies),
                    max_replies=int(style.max_replies),
                    hot=True,
                    max_posts=int(style.max_floors),
                    max_chars=800,
                    reply_min_chars=int(style.reply_min_chars),
                    reply_max_chars=int(style.reply_max_chars),
                    skip_tids=ledger.known_threads(),
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[Galgame/话术] 抓 %s吧 失败：%s", forum, exc)
                lines.append(f"{forum}吧：抓取失败（{exc}）")
                continue
            # 只有真读到正文的才记进缓存；失败的下一轮还要再试
            ledger.mark_threads([t.tid for t, _posts in dump.posts])
            corpus = await self._extract_style(forum, dump, target=int(style.target_phrases))
            if "expressions" in kinds:
                ph = await gl_style.push_phrases(
                    db,
                    ledger,
                    corpus.phrases,
                    session_ids=session_ids,
                    forum=forum,
                    tag=bool(getattr(style, "tag_forum", True)),
                )
            else:
                ph = {"created": 0, "skipped": 0}
            if "jargons" in kinds:
                jg = await gl_style.push_jargons(
                    db, ledger, corpus.jargons, session_ids=session_ids, forum=forum
                )
            else:
                jg = {"created": 0, "skipped": 0}
            await asyncio.to_thread(ledger.save)
            head = f"{forum}吧："
            if dump.blocked:
                # 「被拦住了」≠「这个吧是空的」。以前这两种情况打印出来一模一样，
                # 差点让人以为 galgame笑话吧 没有帖子。
                head += "被贴吧反爬拦住（百度安全验证），本轮提前收工；"
            lines.append(
                head
                + f"扫到 {dump.seen_threads} 帖（低于 {style.min_replies} 回复 "
                f"{dump.skipped_low} · 高于 {style.max_replies} 回复 {dump.skipped_high}"
                + (f" · 已读过 {dump.skipped_known}" if dump.skipped_known else "")
                + "）"
                f"→ 读 {dump.fetched_threads} 帖（失败 {dump.failed_threads}）"
                f"→ 提炼 {len(corpus.phrases)} 条表达 / {len(corpus.jargons)} 条黑话"
                f"→ 新写 {ph['created']} 行表达（重复 {ph['skipped']}）"
                f"、{jg['created']} 条黑话（重复 {jg['skipped']}）"
                + (
                    ""
                    if len(kinds) == 2
                    else "（"
                    + ("黑话" if "jargons" not in kinds else "表达方式")
                    + "的注入关着，这一类没写）"
                )
            )
        return "\n".join(lines)

    async def _cmd_style(self, stream_id: str, rest: str) -> tuple[bool, str, int]:
        """``/gal 话术 [状态|刷新|打标|恢复|清理|体检]`` —— 不带参数就是状态。

        子命令同样只认用法卡上写的那几个词，不再挂 refresh/学/学习/tag 之类同义词。
        """
        parts = rest.split()
        word = parts[0].lower() if parts else "状态"
        if word == "刷新":
            if self._style_learn_lock.locked():
                await self.ctx.send.text(
                    "上一轮还在学，等它跑完再看（/gal 话术 状态 能看进度）。",
                    stream_id,
                    storage_message=False,
                )
                return True, "正在进行", 1
            try:
                async with self._style_learn_lock:
                    text = await self._run_style_learn([x for x in parts[1:] if x] or None)
            except Exception as exc:  # noqa: BLE001
                # 以前这里没有兜底：一轮里任何一次异常都会让用户收不到任何回复
                self.ctx.logger.warning("[Galgame] 手动刷新话术失败：%s", exc)
                text = f"这一轮没跑成：{exc}"
            await self.ctx.send.text(text, stream_id, storage_message=False)
            return True, "话术学习完成", 1
        if word == "打标":
            return await self._cmd_style_retag(stream_id)
        if word == "恢复":
            return await self._cmd_style_restore(stream_id)
        if word == "清理":
            return await self._cmd_style_clear(stream_id)
        if word == "体检":
            return await self._cmd_style_audit(stream_id)
        return await self._cmd_style_status(stream_id)

    async def _cmd_style_status(self, stream_id: str) -> tuple[bool, str, int]:
        style = self.config.style
        ledger = self._style_ledger
        stats = ledger.stats() if ledger is not None else {"expressions": 0, "jargons": 0}
        stocked = len(ledger.pending_restore()) if ledger is not None else 0
        lines = [
            "【贴吧话术】",
            f"开关：{'开' if style.enabled else '关'} · 自动学习：{'开' if style.auto_learn else '关'}"
            f"（每 {style.interval_hours} 小时）",
            f"注入：表达方式 {'开' if 'expressions' in self._style_inject_kinds() else '关'}"
            f" · 黑话 {'开' if 'jargons' in self._style_inject_kinds() else '关'}"
            "（关掉的那类会从麦麦表里撤出来，语料留在花名册，重新打开会自动写回）",
            f"吧：{'、'.join(style.forums) or '（没配）'}",
            f"写入群：{'、'.join(style.chat_ids) or '（没配群 → 写成全局）'}",
            f"门槛：回复数 {style.min_replies}~{style.max_replies} · 每吧最多读 "
            f"{style.posts_per_forum} 帖 · 目标 {style.target_phrases} 条表达",
            f"花名册：记着 {stats['expressions']} 行表达 / {stats['jargons']} 条黑话"
            + (f"（其中 {stocked} 条留档、当前不在麦麦表里，可用 /gal 话术 恢复 写回）" if stocked else "")
            + f" · 记住 {stats.get('threads', 0)} 个已读帖子（读过的不再重复抓）",
            f"吧名标签：{'开' if getattr(style, 'tag_forum', True) else '关'}"
            "（情境写成「[galgame吧] …」，在 WebUI 表达方式页能按吧搜索）",
            f"关闭插件时清空：{'开' if getattr(style, 'clean_on_disable', True) else '关'}"
            "（把「启用插件」关掉就会撤掉插件写过的行，语料留在花名册里）",
            "写入的行是「AI 学过、待审核」状态。麦麦用不用这类行由它自己的设置决定："
            "开着「仅使用人工精选的表达」时要点通过，关着的时候已经在用了。"
            "查一下实际状态用 /gal 话术 体检。",
            "命令：/gal 话术 刷新 [吧名] 立刻学一轮 · /gal 话术 体检 看效果 · "
            "/gal 话术 打标 补上/去掉吧名标签 · /gal 话术 恢复 把留档的语料写回去 · "
            "/gal 话术 清理 把插件写过的行删掉（连语料一起）",
        ]
        await self.ctx.send.text("\n".join(lines), stream_id, storage_message=False)
        return True, "已输出话术状态", 1

    async def _cmd_style_audit(self, stream_id: str) -> tuple[bool, str, int]:
        db = getattr(self.ctx, "db", None)
        ledger = self._style_ledger
        if db is None or ledger is None:
            await self.ctx.send.text("话术模块没准备好。", stream_id, storage_message=False)
            return False, "话术模块未就绪", 1
        targets = await gl_style.resolve_session_ids(
            getattr(self.ctx, "chat", None), list(self.config.style.chat_ids)
        )
        report = await gl_style.audit(db, ledger, targets)
        exp = report["expressions"]
        jg = report["jargons"]
        checked_only = await self._host_checked_only()
        lines = [
            "【贴吧话术 · 体检】",
            f"插件写的表达方式：还在 {exp['alive']} 行"
            f"（已审核 {exp['approved']} · 待审核 {exp['pending']}）"
            f" · 带吧名标签 {exp['tagged']} 行 · 已被删 {exp['missing']} 行",
            f"插件写的黑话：还在 {jg['alive']} 条（已认定 {jg['enabled']} · "
            f"待认定 {jg['draft']}）· 已被删 {jg['missing']} 条",
        ]
        if exp.get("stocked") or jg.get("stocked"):
            lines.append(
                f"留档待恢复：{exp.get('stocked', 0)} 行表达 / {jg.get('stocked', 0)} 条黑话"
                "（语料在花名册里、当前不在麦麦表里；/gal 话术 恢复 可以写回去）"
            )
        if checked_only is True:
            lines.append("麦麦的设置：仅使用人工精选的表达 —— 插件写的行要点通过才会被选中")
        elif checked_only is False:
            lines.append("麦麦的设置：不要求人工精选 —— 插件写的行已经在候选池里了，不必审核")
        else:
            lines.append("麦麦的设置：读不到「仅使用人工精选的表达」这一项，下面按需审核口径算")
        for item in report["chats"]:
            lines.append(
                f"群 {item['group_id']}：人工精选表达 {item['visible']} 条"
                f"（门槛 {gl_style.VISIBLE_FLOOR} 条，{'够' if item['enough'] else '不够'}）"
                f" · 待审核 {item['pending']} 条"
            )
            if not item["enough"] and checked_only is not False:
                lines.append("    ↑ 人工精选不足门槛；开着「仅使用人工精选」时这个群一条都不会注入")
        await self.ctx.send.text("\n".join(lines), stream_id, storage_message=False)
        return True, "已输出话术体检", 1

    async def _host_checked_only(self) -> bool | None:
        """读宿主的「仅使用人工精选的表达」（expression.expression_checked_only）。

        读它是有意义的：这一项决定插件写的行到底会不会被用上，而它由用户在宿主配置里
        改，插件看不见也改不了（宿主没有给插件写配置的能力）。读不到就返回 None，
        按「不确定」措辞，不猜。
        """
        config = getattr(self.ctx, "config", None)
        if config is None:
            return None
        try:
            value = await config.get("expression.expression_checked_only")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("[Galgame/话术] 读宿主表达设置失败：%s", exc)
            return None
        return value if isinstance(value, bool) else None

    async def _cmd_style_retag(self, stream_id: str) -> tuple[bool, str, int]:
        """把已经写进去的表达方式按当前配置补上 / 去掉吧名标签。"""
        db = getattr(self.ctx, "db", None)
        ledger = self._style_ledger
        if db is None or ledger is None:
            await self.ctx.send.text("话术模块没准备好。", stream_id, storage_message=False)
            return False, "话术模块未就绪", 1
        enabled = bool(getattr(self.config.style, "tag_forum", True))
        stats = await gl_style.retag_expressions(db, ledger, enabled=enabled)
        await self.ctx.send.text(
            f"【贴吧话术 · 打标】{'补上' if enabled else '去掉'}吧名标签："
            f"改了 {stats['updated']} 行 · 本来就是这样 {stats['kept']} 行"
            f" · 找不到了 {stats['missing']} 行 · 失败 {stats['failed']} 行。"
            f"\n（依据配置「情境里标出吧名」= {'开' if enabled else '关'}）",
            stream_id,
            storage_message=False,
        )
        return True, "已更新吧名标签", 1

    async def _cmd_style_restore(self, stream_id: str) -> tuple[bool, str, int]:
        """把花名册里留档的语料写回宿主表（关掉插件再打开之后用）。"""
        db = getattr(self.ctx, "db", None)
        ledger = self._style_ledger
        if db is None or ledger is None:
            await self.ctx.send.text("话术模块没准备好。", stream_id, storage_message=False)
            return False, "话术模块未就绪", 1
        kinds = self._style_inject_kinds()
        if not kinds:
            await self.ctx.send.text(
                "【贴吧话术 · 恢复】表达方式和黑话的注入都关着 —— 写回去等于没关。"
                "先去配置里把要用的那个开关打开（配置 → 贴吧话术 → 注入表达方式 / 注入黑话）。",
                stream_id,
                storage_message=False,
            )
            return True, "注入开关都关着", 1
        # 关着注入的那一类不写回去：开关说了算，留档继续躺在花名册里
        off = [b for b in ("expressions", "jargons") if b not in kinds]
        note = (
            "\n注意："
            + "、".join("表达方式" if b == "expressions" else "黑话" for b in off)
            + "的注入开关关着，那部分留档没有写回。"
            if off
            else ""
        )
        stats = await gl_style.restore_written(db, ledger, kinds=kinds)
        if not stats["restored"] and not stats["failed"]:
            await self.ctx.send.text(
                f"【贴吧话术 · 恢复】没有需要写回的语料（花名册里 {stats['alive']} 条都还在麦麦表里）。"
                + note,
                stream_id,
                storage_message=False,
            )
            return True, "无需恢复", 1
        await self.ctx.send.text(
            f"【贴吧话术 · 恢复】写回 {stats['restored']} 条。"
            f"\n本来就在麦麦表里 {stats['alive']} 条 · 缺数据写不了 {stats['skipped']} 条"
            f" · 失败 {stats['failed']} 条。"
            "\n（写回的行仍是「AI 学过」状态，要不要审核由麦麦的设置决定：/gal 话术 体检）"
            + note,
            stream_id,
            storage_message=False,
        )
        return True, "已恢复话术", 1

    async def _cmd_style_clear(self, stream_id: str) -> tuple[bool, str, int]:
        db = getattr(self.ctx, "db", None)
        ledger = self._style_ledger
        if db is None or ledger is None:
            await self.ctx.send.text("话术模块没准备好。", stream_id, storage_message=False)
            return False, "话术模块未就绪", 1
        stats = await gl_style.clear_written(db, ledger)
        await self.ctx.send.text(
            f"【贴吧话术 · 清理】删掉 {stats['deleted']} 行（表达方式 + 黑话），花名册里的语料也一并清掉。"
            f"\n本来就不在了 {stats['missing']} 行 · 你改过的（保留）{stats['changed']} 行"
            f" · 删失败 {stats['failed']} 行。",
            stream_id,
            storage_message=False,
        )
        return True, "已清理话术", 1

    async def _purge_style_if_disabled(self) -> None:
        """on_unload 时判断「是不是用户把插件关了」，是就把写过的行撤掉。

        只有 config.toml 里 [plugin] enabled = false 才动手：宿主是在写入新配置**之后**
        才以 config_disabled 为由卸载插件的（integration.py:1744-1766），而重启、热重载、
        关掉整个机器人时读到的都是 true。判断不出来（读不到 / TOML 坏了）也不动手。

        行删掉、语料留在花名册里，重新打开插件后用 /gal 话术 恢复 写回。
        """
        style = getattr(self.config, "style", None)
        if style is None or not bool(getattr(style, "clean_on_disable", True)):
            return
        if gl_style.plugin_enabled_in_config(self._config_path()) is not False:
            return
        db = getattr(self.ctx, "db", None)
        ledger = self._style_ledger
        if db is None or ledger is None:
            return
        try:
            stats = await gl_style.clear_written(db, ledger, keep=True)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame/话术] 关闭插件时清理失败：%s", exc)
            return
        self.ctx.logger.info(
            "[Galgame] 插件已被关闭，撤掉写进麦麦的表达方式/黑话："
            "删 %s 行 · 保留（你改过的）%s 行 · 本来就不在 %s 行 · 失败 %s 行",
            stats["deleted"],
            stats["changed"],
            stats["missing"],
            stats["failed"],
        )

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _is_confident(query: str, info: GameInfo) -> bool:
        """判断这次名称检索是不是"就是它"，够确定才直接发资料卡。"""
        names = [info.title_zh, info.title_ja, info.title_main, info.title_en, *info.aliases[:6]]
        best = max((similarity(query, n) for n in names if n), default=0)
        return best >= 80

    def _empty_hint(
        self,
        *,
        min_egs: float,
        max_egs: float,
        min_rating: float,
        max_rating: float,
        offset: int,
    ) -> str:
        """空结果时说清楚**为什么**没找到，而不是丢一句「放宽条件」了事。

        这个插件的失败几乎都不是「条件太严」，而是**数据源压根没有那一段**：
        本地批评空间索引只覆盖高分（榜单页按中央值降序），VNDB 的评分是贝叶斯分、
        低分段稀疏到接近不存在。不把这两件事点破，用户只会拿同样的条件反复重试。
        """
        hints: list[str] = []
        # ① 零宽区间：min == max 等于「分数正好等于这个值」，评分是浮点数，基本必空
        for label, low, high in [
            ("批评空间", min_egs, max_egs),
            ("VNDB", min_rating, max_rating),
        ]:
            if low and high and float(low) == float(high):
                hints.append(
                    f"{label} 的上下界都写成了 {float(low):g}，等于要求「分数正好等于 "
                    f"{float(low):g}」——评分是小数，这样几乎一定搜不到。"
                    f"给个区间吧，比如 {float(low):g}~{float(low) + 5:g}"
                )
        # ② 批评空间区间落在本地索引覆盖范围之外
        index = self._service.egs.index if (self._service and self._service.egs) else None
        if index is not None and index.items and (min_egs or max_egs):
            low, high = index.median_range()
            wanted_low = min_egs or 0
            wanted_high = max_egs or 100
            if wanted_high < low or wanted_low > high:
                hints.append(
                    f"本地批评空间索引目前覆盖 {low}~{high} 分，"
                    f"你要的 {wanted_low:g}~{wanted_high:g} 分这一段里面没有数据。"
                    f"索引是翻 EGS 榜单页建的，翻到底能覆盖到很低的分段；"
                    f"若这个范围明显偏窄，多半是建索引时网络中断过，用 /gal sync 重建即可"
                )
        elif (min_egs or max_egs) and not self._service.egs_ready():
            hints.append(
                "批评空间连不上、本地索引也是空的，这次评分是按 VNDB 分近似筛的，两边分布不一样"
            )
        # ③ VNDB 低分段本身就稀疏 —— 这是「用 VNDB 分找烂作」搜不到的根本原因
        if (min_rating or max_rating) and not (min_egs or max_egs) and (max_rating or 100) <= 60:
            hints.append(
                "VNDB 的评分是贝叶斯平均，票数少的作品会被拉向均值（约 70 分），"
                "所以 60 分以下的作品本身就极少（全库 40 分以下只有百来部、30 分以下个位数）。"
                "找「低分 / 烂作」改用批评空间的中央值更有效：把 max_rating 换成 max_egs"
                "（如 max_egs=60），本地索引是覆盖低分段的"
            )
        if offset:
            hints.append(
                f"另外这次是从第 {offset + 1} 部开始取的，前面已经翻完了，"
                f"可以换组条件或把 offset 调小"
            )
        if offset >= MAX_OFFSET:
            hints.append(
                f"同一组条件最多只能翻到第 {MAX_OFFSET} 条，到这儿就没有更多了"
            )
        return "".join(f"\n· {hint}" for hint in hints) or "把条件放宽点再试（降低评分下限、去掉标签或放宽时长）。"

    @staticmethod
    def _range_text(label: str, low: float, high: float) -> str:
        """把一对上下界写成人话；都不限时返回空串。"""
        if low and high:
            return f"{label} {low:g}~{high:g}"
        if low:
            return f"{label} ≥ {low:g}"
        if high:
            return f"{label} ≤ {high:g}"
        return ""

    def _condition_text(
        self,
        query: str,
        min_egs: float,
        max_egs: float,
        min_rating: float,
        max_rating: float,
        min_votes: int,
        min_hours: float,
        max_hours: float,
        tags: list[str],
        year_from: int,
        offset: int = 0,
    ) -> str:
        parts = []
        if query:
            parts.append(f"关键词 {query}")
        egs_text = self._range_text("批评空间", min_egs, max_egs)
        rating_text = self._range_text("VNDB", min_rating, max_rating)
        # 批评空间连不上时 min_egs 是折算到 VNDB 分上执行的，文案要跟着说实话
        if egs_text and not (self._service and self._service.egs_ready()):
            egs_text += "（批评空间不可用，已按 VNDB 分近似）"
        if egs_text:
            parts.append(egs_text)
        if rating_text:
            parts.append(rating_text)
        if min_votes:
            parts.append(f"评分人数 ≥ {int(min_votes)}")
        if min_hours:
            parts.append(f"≥ {min_hours:g} 小时")
        if max_hours:
            parts.append(f"≤ {max_hours:g} 小时")
        if tags:
            parts.append("标签 " + "、".join(tags))
        if year_from:
            parts.append(f"{year_from} 年后")
        if offset:
            parts.append(f"跳过前 {int(offset)} 部")
        return " · ".join(parts) or "不限条件"

    @staticmethod
    def _summary_text(info: GameInfo) -> str:
        """给模型的文字摘要（卡片给人看，这段给 LLM 用）。"""
        lines = [f"《{info.display_title()}》"]
        if info.title_ja and info.title_ja != info.display_title():
            lines.append(f"原名：{info.title_ja}")
        meta = []
        if info.released:
            meta.append(f"发售 {info.released}")
        if info.developer:
            meta.append(f"厂商 {info.developer}")
        meta.append(f"时长 {info.length_text()}")
        meta.append(info.chinese_text())
        lines.append(" / ".join(meta))
        if info.egs_median:
            extra = f"（平均 {info.egs_mean:.1f}，{info.egs_votes} 人评分）" if info.egs_mean else ""
            lines.append(f"批评空间 中央値 {info.egs_median}{extra}")
        if info.vndb_rating is not None:
            lines.append(f"VNDB {info.vndb_rating:.1f}/100（{info.vndb_votes} 票）")
        evals = []
        if info.egs_praise_rate is not None:
            evals.append(f"好评率 {info.egs_praise_rate}%")
        if info.egs_stacked:
            evals.append(f"积压率 {info.egs_stacked.get('percent')}%")
        if info.egs_interesting_minutes:
            evals.append(f"{info.egs_interesting_minutes / 60:.0f} 小时开始好看")
        if evals:
            lines.append("；".join(evals))
        character_lines = info.character_lines()
        if character_lines:
            lines.append("角色/声优：" + "、".join(character_lines))
        return "\n".join(lines)

    def _info_summary(self, info: GameInfo) -> dict[str, Any]:
        """结构化摘要：把"评价如何"也一并交给模型，而不是只给它一个分数。"""
        ratings: dict[str, Any] = {}
        if info.egs_median:
            ratings["批评空间中央值"] = info.egs_median
            ratings["批评空间平均"] = info.egs_mean
            ratings["批评空间评分人数"] = info.egs_votes
            ratings["好评率(80分以上)"] = info.egs_praise_rate
        if info.vndb_rating is not None:
            ratings["VNDB"] = info.vndb_rating
            ratings["VNDB票数"] = info.vndb_votes

        evaluation: dict[str, Any] = {}
        if info.egs_stacked:
            evaluation["积压率"] = f"{info.egs_stacked.get('percent')}%"
        if info.egs_giveup:
            evaluation["弃坑率"] = f"{info.egs_giveup.get('percent')}%"
        if info.egs_interesting_minutes:
            evaluation["多久开始好看"] = f"{info.egs_interesting_minutes / 60:.1f} 小时"
        if info.egs_play_minutes:
            evaluation["玩家实测时长中位数"] = f"{info.egs_play_minutes / 60:.0f} 小时"
        if info.egs_genre:
            evaluation["官方类型"] = info.egs_genre
        # 评价维度：只给票数最高的几类前三条，别把上下文塞爆
        if info.egs_pov:
            pov: dict[str, list[str]] = {}
            for key in ["ここがいい", "傾向", "ネガティブ", "シナリオ", "グラフィック", "音"]:
                entries = info.egs_pov.get(key)
                if entries:
                    pov[key] = [f"{e['name']}({e['votes']})" for e in entries[:3]]
            if pov:
                evaluation["用户评价维度(括号内为票数)"] = pov
        if info.egs_reviews:
            evaluation["最新短评"] = [
                {"得分": r.get("score"), "内容": (r.get("text") or "")[:120]}
                for r in info.egs_reviews[:2]
            ]
        # 长文感想 —— 真正能拿来「锐评」的素材。短评只有一两句话，
        # 长文才有完整的批评、吐槽与拆解。按配置截断，别把几千字整篇塞进上下文。
        if info.egs_long_reviews:
            limit = int(self.config.egs.long_review_chars)
            evaluation["长文感想(锐评素材)"] = [
                {
                    "得分": r.get("score"),
                    "字数": r.get("length"),
                    "剧透": bool(r.get("spoiler")),
                    "正文": (r.get("text") or "")[:limit],
                }
                for r in info.egs_long_reviews
            ]

        return {
            "中文名": info.title_zh,
            "原名": info.title_ja,
            "别名": info.aliases[:8],
            "发售日": info.released,
            "厂商": info.developer,
            "通关时长": info.length_text(),
            "有汉化": info.have_chinese,
            "18禁": info.restricted,
            "评分": ratings,
            "评价": evaluation,
            "标签": [t.get("name") for t in info.tags[:15]],
            "平台": info.platforms,
            "角色声优": info.character_lines() or None,
            "简介": (info.intro or "")[:300],
            "数据来源": info.sources,
        }

    async def _send_info_card(self, info: GameInfo, stream_id: str) -> bool:
        """发资料卡，返回**图片是否真的发出去了**。

        调用方要靠这个返回值决定要不要退回文本 —— 命令路径没有模型兜底。
        """
        cfg = self.config
        try:
            from gl_render import cover_data_uri

            html = build_info_html(
                info,
                cover_uri=await cover_data_uri(self._http, info.cover),
                accent=cfg.output.accent_color,
                width=int(cfg.output.card_width),
                max_tags=int(cfg.output.max_tags),
                show_egs_distribution=bool(cfg.output.show_egs_distribution),
                show_reviews=bool(cfg.output.show_reviews),
                hide_adult_pov=bool(cfg.output.hide_adult_pov),
            )
            return await self._render_and_send(html, stream_id)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[Galgame] 资料卡渲染失败（已跳过图片）：%s", exc)
            return False

    async def _render_and_send(self, html: str, stream_id: str) -> bool:
        """渲染 HTML 并发图，返回**图片是否真的发出去了**。

        调用方要靠这个返回值决定要不要退回文本 —— 渲染失败时宿主返回的是
        ``{"success": False, "error": ...}``，取不到 ``image_base64``，这里会安静地
        什么都不发。以前返回 None，调用方无从得知，结果就是「渲染一挂，用户什么也收不到」。
        """
        result = await self.ctx.render.html2png(
            html,
            selector="#card",
            full_page=True,
            device_scale_factor=2.0,
        )
        image_b64 = ""
        if isinstance(result, dict):
            image_b64 = str(result.get("image_base64") or "")
        if not image_b64:
            return False
        await self.ctx.send.image(image_b64, stream_id, storage_message=False)
        return True


def create_plugin() -> GalgamePlugin:
    return GalgamePlugin()
