"""galgame领域大神 —— 贴吧话术入库。

把贴吧语料变成麦麦自己的「表达方式」和「黑话」，直接写进宿主的 expressions /
jargons 表，让麦麦用它原生的那一套去挑、去注入。

为什么不是「插件自己存语料、再用 hook 塞进 prompt」：

* 宿主的挑选逻辑有一堆细节（候选不足 10 条就一条都不注入、按会话隔离、
  checked_only 开关、权重抽样……），自己复刻一遍必然走样；
* 写进宿主表之后，WebUI 的「表达方式」「黑话」页面能直接看见、编辑、审核、导出，
  用户不需要为这个插件再学一套管理界面；
* 反过来，插件只负责往里写，注入与否完全由宿主自己的设置决定；
* 用户把插件关掉时，插件会把自己写过的行删干净（on_unload 里读自己的 config.toml
  判断是不是被关了），语料本体留在花名册里，重新打开后 /gal 话术 恢复 可以原样写回。

四条硬性的对齐规则（不照做的话写进去也是白写，依据见
src/chat/replyer/maisaka_expression_selector.py 与 src/learners/expression_learner.py）：

1. 本模块写入的行一律是 checked=False + modified_by=AI，也就是停在宿主自己的
   「AI 学过、等人审核」状态，绝不假冒人工精选。宿主**要不要**只采信人工精选的行由
   它自己的 expression.expression_checked_only 决定（该项为 true 时要求 checked=True
   且 modified_by=USER），插件读得到、改不了，所以只在体检里如实报出来。
2. 宿主会跳过 style 里含 SELF / [表情包 / [图片 的行，也会跳过正好等于它 few-shot
   示例的那三条（我嘞个xxxx / 对对对 / 这么强！），所以这些在这里就先丢掉。
   style 以「使用」开头会被宿主截掉前缀，这里也先归一化。
3. 宿主注入表达方式时有个硬门槛：**候选不足 10 条就一条都不注入**
   （maisaka_expression_selector.py 里 if len(all_candidates) < 10: return []，
   而且是按会话分别算的）。所以 audit() 会把这个数报出来，避免「学了半天却没效果」。
4. 按「吧 → 群」分隔靠的是宿主原生的 session_id 隔离：一个 Expression 行只能挂一个
   session_id，所以 N 个群就写 N 行；黑话的 session_id_dict 天生是 {会话id: 次数}
   的 JSON，一行可以挂多个。

宿主这两张表**没有「哪个插件写的」这一列**，所以插件在自己数据目录维护一份花名册
（style_ledger.json）：记下行 id + 内容指纹 + 目标会话。清理时按 id 把行读回来、
指纹对得上才删 —— 因为 SQLite 的自增 id 在删掉最大行之后会被复用，光记 id
有误删用户后来新建行的风险。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

EXPRESSION_MODEL = "Expression"
JARGON_MODEL = "Jargon"

# 送进宿主之前自己先卡的长度。宿主的字段上限是 255，但太长的话术本来也不好用，
# 而且会在 WebUI 列表里把界面撑爆。超了整条丢，不截断（截出来是半句话）。
SOFT_SITUATION_CHARS = 30
SOFT_STYLE_CHARS = 40
SOFT_CONTENT_CHARS = 24
SOFT_MEANING_CHARS = 60

# 宿主会主动跳过的 style（见 expression_learner.py 的过滤规则）
_BAD_STYLE_SUBSTRINGS = ("SELF", "[表情包", "[图片")
# 宿主的 few-shot 示例，学出这些等于把 prompt 抄回来了
_PROMPT_EXAMPLE_STYLES = {"我嘞个xxxx", "对对对", "这么强！"}
# 明显是「没想好」的占位写法
_PLACEHOLDER_RE = re.compile(r"此处省略|略\s*\.{2,}|x{3,}|X{3,}|xxx+|待补充|示例")
# 模型很爱把「怎么说」写成对说话方式的描述（例如「用夸张的语气表达不满」），
# 而宿主那套要的是真能说出口的话，所以这些也丢
_DESCRIPTIVE_RE = re.compile(r"语气|口吻|表达方式|说话方式|地表达")
# 不该出现在语料里的东西：链接、QQ 号、@某人
_NOISE_RE = re.compile(r"https?://|www\.|[Qq]{2}\s*[:：]?\s*\d{4,}|@[\w\u4e00-\u9fff]{2,}")

_LEDGER_VERSION = 1
# 帖子正文读过之后多久算「过期」。贴吧帖子会一直有新回复，但话术是稳定的，
# 一个月重读一次足够。
_THREAD_TTL_DAYS = 30


# --- 文本清洗 --------------------------------------------------------------


def _clean_text(value: Any, limit: int) -> str:
    """把模型给的一小段文本收拾干净：去空白、去引号、压成一整句。

    超过 limit 直接返回空串（宁可少一条，也不要半句话）。
    """
    if not isinstance(value, str):
        return ""
    text = value.strip().strip("\"'“”「」『』")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > max(1, int(limit)):
        return ""
    return text


def normalize_style(style: str) -> str:
    """跟宿主 normalize_expression_style_for_learning 对齐：去掉开头的「使用」。"""
    text = (style or "").strip()
    if text.startswith("使用"):
        text = text[2:].strip()
    return re.sub(r"\s+", "", text)


def style_key(style: str) -> str:
    """去重用的指纹。"""
    return normalize_style(style).casefold()


def jargon_key(content: str) -> str:
    return re.sub(r"\s+", "", (content or "")).casefold()


# --- 吧名标签 --------------------------------------------------------------
#
# 贴吧语料写进的是麦麦自己的 expressions 表，和麦麦自己学到的行混在一起。宿主没给插件
# 留「来源」列，也不允许插件给 WebUI 加筛选项，所以「按吧分开看」只能借宿主已有的字段：
# 把吧名做成「[galgame吧]」前缀写进 situation —— WebUI 列表的「情境」列会直接显示，
# 搜索框（按 situation/style 做 contains）也能直接筛出某个吧。
#
# 代价是这个前缀会跟着进提示词（宿主拼的是「当"{situation}"时，可以用…」），
# 所以做成配置项 style.tag_forum，不想要就关掉。

_FORUM_TAG_RE = re.compile(r"^\[([^\[\]]{1,20})吧\]\s*")
FORUM_TAG_CHARS = 12


def forum_label(forum: str) -> str:
    """galgame -> galgame吧。"""
    text = re.sub(r"\s+", "", str(forum or "")).strip("[]")
    if not text:
        return ""
    if not text.endswith("吧"):
        text += "吧"
    return text[:20]


def forum_tag(forum: str) -> str:
    label = forum_label(forum)
    return f"[{label}]" if label else ""


def split_forum_tag(situation: str) -> tuple[str, str]:
    """拆成 (去掉标签的情境, 标签里的吧名)。没有标签时吧名为空串。"""
    text = str(situation or "").strip()
    match = _FORUM_TAG_RE.match(text)
    if not match:
        return text, ""
    return text[match.end() :].strip(), match.group(1)


def tag_situation(situation: str, forum: str, *, enabled: bool = True) -> str:
    """给情境加上「[吧名吧]」前缀。重复调用不会叠标签。"""
    base, _existing = split_forum_tag(situation)
    if not enabled:
        return base
    tag = forum_tag(forum)
    return f"{tag} {base}".strip() if tag else base


# --- 数据类 ----------------------------------------------------------------


@dataclass
class StylePhrase:
    """一条表达方式：什么情境 -> 怎么说。"""

    situation: str
    style: str
    forum: str = ""


@dataclass
class StyleJargon:
    """一条黑话。"""

    content: str
    meaning: str
    forum: str = ""


@dataclass
class StyleCorpus:
    """一次提炼的产物，可以多批合并。"""

    phrases: list[StylePhrase] = field(default_factory=list)
    jargons: list[StyleJargon] = field(default_factory=list)

    def merge(self, other: "StyleCorpus") -> int:
        """把另一批并进来，按指纹去重，返回新增条数。"""
        added = 0
        seen_p = {style_key(p.style) for p in self.phrases}
        for phrase in other.phrases:
            key = style_key(phrase.style)
            if not key or key in seen_p:
                continue
            seen_p.add(key)
            self.phrases.append(phrase)
            added += 1
        seen_j = {jargon_key(j.content) for j in self.jargons}
        for jargon in other.jargons:
            key = jargon_key(jargon.content)
            if not key or key in seen_j:
                continue
            seen_j.add(key)
            self.jargons.append(jargon)
            added += 1
        return added

    def empty(self) -> bool:
        return not self.phrases and not self.jargons


def chunked(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    step = max(1, int(size))
    for index in range(0, len(seq), step):
        yield seq[index : index + step]


# --- 提炼 ------------------------------------------------------------------


_EXTRACT_TEMPLATE = """你在帮一个中文 QQ 机器人（网名「麦麦」）学习百度贴吧「{forum}吧」网友的说话方式。

下面是从该吧抓来的真实帖子，标题和楼层回复混在一起，用 --- 分隔。

请提炼两样东西。

【phrases】该吧网友的表达方式，每条是一对「什么情境下」+「怎么说」。
style 必须是**能直接说出口的一句话或半句话**，不是对说话方式的描述。
  好例子：
    情境：对某个剧情表示强烈不满 → 表达：这剧情是人能写出来的？
    情境：推荐作品时顺带损一句 → 表达：不玩后悔一辈子，玩了后悔半辈子
    情境：表示自己已经玩腻了 → 表达：已经电子阳痿了
  坏例子（绝对不要这样写）：
    情境：吐槽剧情 → 表达：用夸张的语气表达不满     ← 这是描述，不是话术
    情境：推荐作品 → 表达：用热情的语气推荐          ← 同上

【jargons】该吧特有的黑话、简称、谐音、固定梗，给出词和它的意思。
只收**圈外人看不懂**的；不要收普通词，也不要收作品名本身（除非它正是当梗在用）。

硬性要求：
- situation 不超过 30 个字，style 不超过 40 个字，都写简体中文。
- style 里不要用省略号占位，不要写「此处省略」「xxx」这类东西。
- 黑话的 meaning 不超过 60 个字。
- 不要出现人名、QQ 号、链接、@某人。
- 必须来自下面的材料，不要凭印象编；没把握就不写。
- 宁缺毋滥：材料里没有就少写甚至一条都不写，绝不硬凑。
- phrases 最多 {max_phrases} 条，jargons 最多 {max_jargons} 条。

严格只输出这个 JSON，不要解释、不要 markdown 代码块：
{{"phrases":[{{"situation":"...","style":"..."}}],"jargons":[{{"content":"...","meaning":"..."}}]}}

材料：
{body}"""


def build_extract_prompt(
    forum: str,
    texts: Sequence[str],
    *,
    max_phrases: int = 40,
    max_jargons: int = 20,
    max_chars: int = 7000,
) -> tuple[str, int]:
    """拼提炼用的 prompt，返回（prompt, 实际用了几条材料）。

    材料按顺序塞，塞到 max_chars 就停 —— 不让某一批把模型的上下文撑爆。
    """
    body_parts: list[str] = []
    used = 0
    total = 0
    for text in texts:
        piece = (text or "").strip()
        if not piece:
            continue
        if total + len(piece) > max(200, int(max_chars)):
            break
        body_parts.append(piece)
        total += len(piece)
        used += 1
    body = "\n---\n".join(body_parts)
    prompt = _EXTRACT_TEMPLATE.format(
        forum=forum or "贴吧",
        max_phrases=max(1, int(max_phrases)),
        max_jargons=max(1, int(max_jargons)),
        body=body,
    )
    return prompt, used


def _close_brackets(fragment: str) -> str:
    """把一段被截断的 JSON 补成语法完整的：先补没关的字符串，再按栈补右括号。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append(ch)
        elif ch in "]}":
            if stack:
                stack.pop()
    out = fragment + ('"' if in_string else "")
    for opener in reversed(stack):
        out += "]" if opener == "[" else "}"
    return out


def _repair_json(text: str) -> dict[str, Any] | None:
    """模型被 max_tokens 截断时的兜底：从后往前丢掉残件、补齐括号再试。

    一次生成撞上 max_tokens 会让 JSON 烂在中间，直接 json.loads 必然失败，
    整批材料就白跑了。这里从最后一个右括号往回找能解析出来的最长前缀。
    """
    positions = [i for i, ch in enumerate(text) if ch in "}]"]
    for pos in reversed(positions):
        try:
            obj = json.loads(_close_brackets(text[: pos + 1]))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            return obj
    return None


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """从模型回复里抠出那个 JSON 对象。

    模型很爱加 markdown 代码围栏或者在前后写一句话，所以不能直接 json.loads。
    """
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^[\x60]{3,}[a-zA-Z]*\s*", "", text)
    text = re.sub(r"[\x60]{3,}\s*$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        obj = _repair_json(text[start:])
    return obj if isinstance(obj, dict) else None


def _clean_phrase(item: Any, forum: str) -> StylePhrase | None:
    if not isinstance(item, dict):
        return None
    situation = _clean_text(item.get("situation"), SOFT_SITUATION_CHARS)
    style = _clean_text(item.get("style"), SOFT_STYLE_CHARS)
    if not situation or not style:
        return None
    norm = normalize_style(style)
    if not norm:
        return None
    if any(bad in norm for bad in _BAD_STYLE_SUBSTRINGS):
        return None
    if norm.casefold() in {normalize_style(x).casefold() for x in _PROMPT_EXAMPLE_STYLES}:
        return None
    if _PLACEHOLDER_RE.search(norm) or _NOISE_RE.search(norm):
        return None
    if _PLACEHOLDER_RE.search(situation) or _NOISE_RE.search(situation):
        return None
    # 带引号的话术里出现「语气」是正常的（引用的就是原文），没引号的才当描述丢掉
    if _DESCRIPTIVE_RE.search(norm) and not re.search(r"[「」“”]", style):
        return None
    return StylePhrase(situation=situation, style=style, forum=forum)


def _clean_jargon(item: Any, forum: str) -> StyleJargon | None:
    if not isinstance(item, dict):
        return None
    content = _clean_text(item.get("content"), SOFT_CONTENT_CHARS)
    meaning = _clean_text(item.get("meaning"), SOFT_MEANING_CHARS)
    if not content or not meaning:
        return None
    if _PLACEHOLDER_RE.search(content) or _NOISE_RE.search(content):
        return None
    if _NOISE_RE.search(meaning):
        return None
    # 黑话本身可能就叫 xxx，所以 content 不查占位符，只查 meaning
    if re.search(r"此处省略|待补充|示例", meaning):
        return None
    return StyleJargon(content=content, meaning=meaning, forum=forum)


def parse_extract_reply(raw: str, *, forum: str = "") -> StyleCorpus:
    """把模型的一次回复变成干净的语料。解析不出来就返回空语料（不抛异常）。"""
    obj = extract_json_object(raw)
    if not obj:
        logger.debug("[Galgame/话术] 模型回复里没有可解析的 JSON：%s", (raw or "")[:160])
        return StyleCorpus()
    corpus = StyleCorpus()
    raw_phrases = obj.get("phrases")
    if isinstance(raw_phrases, list):
        for item in raw_phrases:
            phrase = _clean_phrase(item, forum)
            if phrase:
                corpus.phrases.append(phrase)
    raw_jargons = obj.get("jargons")
    if isinstance(raw_jargons, list):
        for item in raw_jargons:
            jargon = _clean_jargon(item, forum)
            if jargon:
                corpus.jargons.append(jargon)
    # 同一批里自己也可能重复
    deduped = StyleCorpus()
    deduped.merge(corpus)
    return deduped


# --- 花名册 ----------------------------------------------------------------


class StyleLedger:
    """插件写进宿主表的花名册（记 id + 指纹 + 目标会话）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {
            "version": _LEDGER_VERSION,
            "expressions": [],
            "jargons": [],
        }
        self.load()

    # -- 持久化 --

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Galgame/话术] 读花名册失败：%s", exc)
            return
        try:
            obj = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Galgame/话术] 花名册不是合法 JSON（当空处理）：%s", exc)
            return
        if not isinstance(obj, dict):
            return
        for key in ("expressions", "jargons"):
            items = obj.get(key)
            if isinstance(items, list):
                self._data[key] = [x for x in items if isinstance(x, dict)]
        threads = obj.get("threads")
        if isinstance(threads, dict):
            self._data["threads"] = {str(k): str(v) for k, v in threads.items() if k}
        self._data["version"] = obj.get("version", _LEDGER_VERSION)

    def save(self) -> None:
        self._data["version"] = _LEDGER_VERSION
        self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    # -- 读 --

    @property
    def expressions(self) -> list[dict[str, Any]]:
        return list(self._data.get("expressions") or [])

    @property
    def jargons(self) -> list[dict[str, Any]]:
        return list(self._data.get("jargons") or [])

    def stats(self) -> dict[str, int]:
        return {
            "expressions": len(self.expressions),
            "jargons": len(self.jargons),
            "threads": len(self._data.get("threads") or {}),
        }

    def written(self, bucket: str) -> list[dict[str, Any]]:
        """当前还带 id（也就是此刻确实在麦麦表里）的条目。"""
        return [x for x in (self._data.get(bucket) or []) if x.get("id")]

    # -- 写 --

    def _add(self, bucket: str, row: dict[str, Any]) -> None:
        """同一条语料只保留一条记录：先认天然键（风格/内容+会话+吧），再认 id。"""
        items = self._data.setdefault(bucket, [])
        row_id = row.get("id")
        key = entry_key(bucket, row)
        for index, exist in enumerate(items):
            if entry_key(bucket, exist) == key or (row_id is not None and exist.get("id") == row_id):
                items[index] = row
                return
        items.append(row)

    def add_expression(
        self,
        *,
        row_id: Any,
        situation: str,
        style: str,
        session_id: str | None,
        forum: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "id": row_id,
            "situation": situation,
            "style": style,
            "session_id": session_id,
            "forum": forum,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if payload:
            # 存一份写进宿主表的原始数据：行被删掉之后靠它原样写回去
            row["payload"] = dict(payload)
        self._add("expressions", row)

    def add_jargon(
        self,
        *,
        row_id: Any,
        content: str,
        session_id_dict: str,
        forum: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "id": row_id,
            "content": content,
            "session_id_dict": session_id_dict,
            "forum": forum,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if payload:
            row["payload"] = dict(payload)
        self._add("jargons", row)

    def forget(self, bucket: str, row_id: Any) -> None:
        items = self._data.get(bucket) or []
        self._data[bucket] = [x for x in items if x.get("id") != row_id]

    def unlink(self, bucket: str, row_id: Any) -> bool:
        """行已从宿主表里删掉，但语料留着 —— 只摘掉 id，不丢记录。"""
        for entry in self._data.get(bucket) or []:
            if entry.get("id") == row_id:
                entry.pop("id", None)
                entry["removed_at"] = datetime.now().isoformat(timespec="seconds")
                return True
        return False

    def pending_restore(self, bucket: str | None = None) -> list[dict[str, Any]]:
        """语料留着、当前不在宿主表里的条目（能恢复的）。"""
        buckets = [bucket] if bucket else ["expressions", "jargons"]
        out: list[dict[str, Any]] = []
        for name in buckets:
            out.extend(
                x
                for x in (self._data.get(name) or [])
                if not x.get("id") and isinstance(x.get("payload"), dict)
            )
        return out

    # -- 已读帖子缓存 --
    #
    # 帖子正文页正是触发「百度安全验证」的元凶，而同一个帖子读第二遍毫无收益
    # （提炼出来的话术早就被去重挡掉了）。所以记下读过哪些 tid，下一轮直接跳过，
    # 把请求额度留给新帖子。

    def known_threads(self, *, ttl_days: int = _THREAD_TTL_DAYS) -> set[str]:
        """最近读过、且没超过保鲜期的帖子 id。"""
        now = datetime.now()
        out: set[str] = set()
        for tid, stamp in (self._data.get("threads") or {}).items():
            try:
                when = datetime.fromisoformat(str(stamp))
            except (TypeError, ValueError):
                continue
            if (now - when).days <= ttl_days:
                out.add(str(tid))
        return out

    def mark_threads(self, tids: Any) -> int:
        """记下这一轮读过正文的帖子。只记成功的 —— 没读到的下次还要再试。"""
        bucket = self._data.setdefault("threads", {})
        stamp = datetime.now().isoformat(timespec="seconds")
        added = 0
        for tid in tids or ():
            key = str(tid or "").strip()
            if key and key not in bucket:
                bucket[key] = stamp
                added += 1
        return added

    def prune_threads(self, *, ttl_days: int = _THREAD_TTL_DAYS * 2) -> int:
        """丢掉过期的记录，免得花名册无限长大。"""
        bucket = self._data.get("threads") or {}
        now = datetime.now()
        keep: dict[str, str] = {}
        for tid, stamp in bucket.items():
            try:
                when = datetime.fromisoformat(str(stamp))
            except (TypeError, ValueError):
                continue
            if (now - when).days <= ttl_days:
                keep[str(tid)] = str(stamp)
        removed = len(bucket) - len(keep)
        self._data["threads"] = keep
        return removed


# --- 关闭插件时的自我清理 --------------------------------------------------
#
# 用户把插件关掉（config.toml 里 [plugin] enabled = false）时，宿主会先把新配置写进
# 文件，再以「配置把它禁用」为理由卸载插件 —— 见
# src/plugin_runtime/integration.py:1744-1766 _handle_plugin_config_changes：
#   if plugin_is_loaded and not snapshot.enabled:
#       reloaded = await self.reload_plugins_globally([plugin_id], reason="config_disabled")
# 所以插件的 on_unload 里读到 enabled=False 就是「被关了」；而重启、热重载、关掉整个
# 机器人时读到的都是 True，什么都不该做。


def plugin_enabled_in_config(config_path: str | Path) -> bool | None:
    """读插件自己的 config.toml，判断用户是不是把插件关了。

    判定语义与宿主 runner_main.py:1063-1087 _is_plugin_enabled 对齐：没有 [plugin]
    段就算启用，enabled 缺省也是启用，字符串还认 0/false/no/off 与 1/true/yes/on。
    文件读不到 / 解析失败返回 None —— 不猜，宁可不删。
    """
    try:
        raw = Path(config_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Galgame/话术] 读插件配置失败：%s", exc)
        return None
    try:
        import tomllib
    except ImportError:  # pragma: no cover —— 宿主环境是 Python 3.12
        return None
    try:
        obj = tomllib.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Galgame/话术] 插件配置不是合法 TOML：%s", exc)
        return None
    if not isinstance(obj, dict):
        return None
    section = obj.get("plugin")
    if not isinstance(section, dict):
        return True
    value = section.get("enabled", True)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"0", "false", "no", "off"}:
            return False
        if text in {"1", "true", "yes", "on"}:
            return True
    return bool(value)


def entry_key(bucket: str, row: dict[str, Any]) -> str:
    """花名册条目的天然键：表达方式看 (风格, 会话, 吧)，黑话看内容。

    用天然键而不是 id 认人，是因为「关掉插件 → 行被删 → 再打开插件恢复」之后 id
    会变，而风格 / 内容 / 目标会话这几样不变。
    """
    if bucket == "expressions":
        return "|".join(
            [
                style_key(str(row.get("style") or "")),
                str(row.get("session_id") or ""),
                str(row.get("forum") or ""),
            ]
        )
    return jargon_key(str(row.get("content") or ""))


# 宿主 expressions / jargons 上的时间列（SQLAlchemy DateTime）。花名册是 JSON，
# 里面存成了字符串，写回前必须还原成 datetime。
_DATETIME_FIELDS = (
    "create_time",
    "last_active_time",
    "created_timestamp",
    "updated_timestamp",
)


def _revive_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """花名册里的 payload（JSON）→ 能直接交给宿主 db_save 的 dict。

    宿主 db_save 走 SQLAlchemy，DateTime 列**只接受 datetime / date**：喂字符串会抛
    ``SQLite DateTime type only accepts Python datetime and date objects as input``，
    而 db_save 自己把异常吞掉、返回 None —— 插件这边只看得到「没有返回 id」。
    实测里 88 条行就是这么静悄悄写不回去的（日志里能搜到上面那句 TypeError）。
    认不出来的时间字符串就**不写这个字段**，让宿主用默认值，别拿字符串硬碰。
    """
    data = dict(payload)
    for key in _DATETIME_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and value:
            try:
                data[key] = datetime.fromisoformat(value)
            except ValueError:
                data.pop(key, None)
    return data


def host_key(bucket: str, row: dict[str, Any]) -> str:
    """宿主表里认一行用的天然键。

    和 entry_key 的区别：宿主表**没有 forum 这一列**（吧名只写在 situation 里），
    所以对照宿主行时只能拿 (风格, 会话) 或内容来认。
    """
    if bucket == "expressions":
        return "|".join(
            [style_key(str(row.get("style") or "")), str(row.get("session_id") or "")]
        )
    return jargon_key(str(row.get("content") or ""))


async def _host_keys(db: Any, bucket: str) -> dict[str, Any]:
    """宿主表里现存的行 → {天然键: 行 id}。"""
    model = EXPRESSION_MODEL if bucket == "expressions" else JARGON_MODEL
    rows = await _db_get(db, model, {}, limit=5000)
    out: dict[str, Any] = {}
    for row in rows:
        key = host_key(bucket, row)
        if key and row.get("id") is not None:
            out.setdefault(key, row["id"])
    return out


# --- 宿主能力封装 ----------------------------------------------------------


def _rows_of(result: Any) -> list[dict[str, Any]]:
    """把 ctx.db 的返回值统一成 list[dict]。

    宿主侧真实形状（src/services/database_service.py）：db_get 返回 list[dict]，
    single_result 时返回 dict|None，出错时返回 [] / None；
    db_save 返回 dict|None；db_delete/db_update/db_count 返回 int。
    另外宿主在 model_name 缺失/模型找不到时会返回 {"success": False, "error": ...}。
    """
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        if result.get("success") is False:
            logger.warning("[Galgame/话术] 宿主数据库能力报错：%s", result.get("error"))
            return []
        for key in ("data", "rows", "items", "results", "records"):
            value = result.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return [result] if result.get("id") is not None else []
    return []


async def _db_get(
    db: Any, model_name: str, filters: dict[str, Any], *, limit: int | None = None
) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        result = await db.get(model_name=model_name, filters=filters, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Galgame/话术] 查询 %s 失败：%s", model_name, exc)
        return []
    return _rows_of(result)


async def resolve_session_ids(chat: Any, chat_ids: Sequence[str]) -> dict[str, str]:
    """群号 -> session_id。

    宿主的 hook 只给一个 MD5 出来的 session_id
    （SessionUtils.calculate_session_id 就是 md5(platform_account_scope_group)），
    **不可能从它反推群号**，所以「哪个群」只能靠 ctx.chat.get_all_streams() 反查 ——
    它返回的每条 stream 带 session_id / group_id / is_group_session。
    查不到的群会被安静跳过（宁可不写，也不要写错群）。
    """
    wanted = {str(x).strip() for x in (chat_ids or []) if str(x).strip()}
    if not wanted or chat is None:
        return {}
    try:
        resp = await chat.get_all_streams(platform="qq")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Galgame/话术] 取会话列表失败，本轮不写库：%s", exc)
        return {}
    streams = resp.get("streams") if isinstance(resp, dict) else resp
    if not isinstance(streams, list):
        logger.warning("[Galgame/话术] 会话列表形状不认识，本轮不写库")
        return {}
    out: dict[str, str] = {}
    for item in streams:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id") or "").strip()
        session_id = str(item.get("session_id") or "").strip()
        if not group_id or not session_id or group_id not in wanted:
            continue
        if item.get("is_group_session") is False:
            continue
        out[group_id] = session_id
    missing = sorted(wanted - set(out))
    if missing:
        logger.warning(
            "[Galgame/话术] 这些群查不到会话（可能机器人还没在那个群说过话）：%s",
            "、".join(missing),
        )
    return out


# --- 写入宿主表 ------------------------------------------------------------


async def _existing_style_keys(db: Any, session_id: str | None) -> set[str]:
    rows = await _db_get(
        db, EXPRESSION_MODEL, {"session_id": session_id}, limit=2000
    )
    return {style_key(str(r.get("style") or "")) for r in rows}


async def push_phrases(
    db: Any,
    ledger: StyleLedger,
    phrases: Sequence[StylePhrase],
    *,
    session_ids: Sequence[str | None],
    forum: str,
    tag: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    """把表达方式写进宿主 expressions 表。

    每个目标会话各写一行（宿主的 Expression 只能挂一个 session_id）。
    写入状态固定为 checked=False + modified_by=AI —— 也就是「AI 学过、等人审核」。
    注意宿主是否要求人工精选由它自己的 expression.checked_only 决定，默认配置下
    未审核的行同样会被选中，所以别把这里当成开关。

    tag=True 时情境前会加上「[吧名吧]」前缀，方便在 WebUI 里按吧查看/搜索。

    返回 {"created": n, "skipped": n, "failed": n}。
    """
    stats = {"created": 0, "skipped": 0, "failed": 0}
    targets: list[str | None] = list(session_ids) or [None]
    if not phrases:
        return stats
    for session_id in targets:
        existing = await _existing_style_keys(db, session_id)
        for phrase in phrases:
            key = style_key(phrase.style)
            if not key or key in existing:
                stats["skipped"] += 1
                continue
            if dry_run:
                existing.add(key)
                stats["created"] += 1
                continue
            situation = tag_situation(phrase.situation, forum or phrase.forum, enabled=tag)
            data = {
                "situation": situation,
                "style": phrase.style,
                # content_list 是宿主用来累积同一情境历史说法的 JSON 数组
                "content_list": json.dumps([situation], ensure_ascii=False),
                "count": 1,
                "session_id": session_id,
                "checked": False,
                "modified_by": "AI",
            }
            try:
                saved = await db.save(model_name=EXPRESSION_MODEL, data=data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Galgame/话术] 写表达方式失败（%s）：%s", phrase.style, exc)
                stats["failed"] += 1
                continue
            row = _rows_of(saved)
            if not row or row[0].get("id") is None:
                logger.warning("[Galgame/话术] 写表达方式没有返回 id：%s", phrase.style)
                stats["failed"] += 1
                continue
            existing.add(key)
            ledger.add_expression(
                row_id=row[0]["id"],
                situation=situation,
                style=phrase.style,
                session_id=session_id,
                forum=forum or phrase.forum,
                payload=data,
            )
            stats["created"] += 1
    return stats


async def _existing_jargon_keys(db: Any) -> set[str]:
    rows = await _db_get(db, JARGON_MODEL, {}, limit=5000)
    return {jargon_key(str(r.get("content") or "")) for r in rows}


async def push_jargons(
    db: Any,
    ledger: StyleLedger,
    jargons: Sequence[StyleJargon],
    *,
    session_ids: Sequence[str | None],
    forum: str,
    dry_run: bool = False,
) -> dict[str, int]:
    """把黑话写进宿主 jargons 表。

    is_jargon 固定写 False（也就是「候选词、还没被认定成黑话」），用户在 WebUI
    黑话页确认之后再勾成黑话。这与表达方式的 checked=False 是同一个思路：
    插件只负责把料摆上来，认不认由人决定。

    session_id_dict 是宿主原生的多会话挂载方式（JSON: 会话id -> 次数），
    所以一行就能同时服务多个群，不需要按群复制。
    """
    stats = {"created": 0, "skipped": 0, "failed": 0}
    if not jargons:
        return stats
    target_ids = [sid for sid in (session_ids or []) if sid]
    session_dict = json.dumps({sid: 1 for sid in target_ids}, ensure_ascii=False)
    is_global = not target_ids
    existing = await _existing_jargon_keys(db)
    for jargon in jargons:
        key = jargon_key(jargon.content)
        if not key or key in existing:
            stats["skipped"] += 1
            continue
        if dry_run:
            existing.add(key)
            stats["created"] += 1
            continue
        data = {
            "content": jargon.content,
            "meaning": jargon.meaning,
            # 出处：让用户在 WebUI 里一眼看出这条是贴吧学来的、来自哪个吧
            "evidence_messages": f"贴吧 {forum or jargon.forum} 吧",
            "session_id_dict": session_dict,
            "count": 1,
            "is_jargon": False,
            "is_complete": True,
            "is_global": is_global,
            "created_by": "AI",
        }
        try:
            saved = await db.save(model_name=JARGON_MODEL, data=data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Galgame/话术] 写黑话失败（%s）：%s", jargon.content, exc)
            stats["failed"] += 1
            continue
        row = _rows_of(saved)
        if not row or row[0].get("id") is None:
            logger.warning("[Galgame/话术] 写黑话没有返回 id：%s", jargon.content)
            stats["failed"] += 1
            continue
        existing.add(key)
        ledger.add_jargon(
            row_id=row[0]["id"],
            content=jargon.content,
            session_id_dict=session_dict,
            forum=forum or jargon.forum,
            payload=data,
        )
        stats["created"] += 1
    return stats


# --- 清理与体检 ------------------------------------------------------------


async def clear_written(
    db: Any,
    ledger: StyleLedger,
    *,
    kinds: Sequence[str] = ("expressions", "jargons"),
    keep: bool = False,
) -> dict[str, int]:
    """按花名册把插件写过的行删掉。

    必须先按 id 读回来、比对内容指纹，对得上才删：SQLite 的自增 id 在删掉最大行
    之后会被复用，如果用户在这中间自己新建过表达方式，光凭 id 删就会误伤。

    keep=True 给「关掉插件」用：行照样删，但花名册里的语料留住（摘掉 id、记
    removed_at），重新打开插件后 restore_written 能原样写回去。用户自己改过的行
    两种模式下都不删。

    keep=True 的顺手活：删之前把当前那一行的全部字段刷进 payload（老语料的
    payload 可能是旧版本插件写的、缺 NOT NULL 的列，照它写回去会失败）。
    """
    stats = {"deleted": 0, "missing": 0, "changed": 0, "failed": 0}

    if "expressions" in kinds:
        for entry in ledger.expressions:
            row_id = entry.get("id")
            rows = await _db_get(db, EXPRESSION_MODEL, {"id": row_id}, limit=1)
            if not rows:
                stats["missing"] += 1
                if keep:
                    ledger.unlink("expressions", row_id)
                else:
                    ledger.forget("expressions", row_id)
                continue
            if style_key(str(rows[0].get("style") or "")) != style_key(
                str(entry.get("style") or "")
            ):
                # 用户改过内容 -> 说明他想要这条，别删
                stats["changed"] += 1
                if not keep:
                    ledger.forget("expressions", row_id)
                continue
            if keep:
                # 行还在，就用活的那一行把 payload 刷一遍：老语料的 payload 可能是旧版本
                # 插件写的、缺列（模型里 NOT NULL 的列少一个，写回去就失败），既然这里
                # 正好把整行读出来了，就顺手补齐，省得「关掉再打开」丢语料。
                entry["payload"] = {k: v for k, v in rows[0].items() if k != "id"}
            try:
                await db.delete(model_name=EXPRESSION_MODEL, filters={"id": row_id})
                stats["deleted"] += 1
                if keep:
                    ledger.unlink("expressions", row_id)
                else:
                    ledger.forget("expressions", row_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Galgame/话术] 删除表达方式 %s 失败：%s", row_id, exc)
                stats["failed"] += 1

    if "jargons" in kinds:
        for entry in ledger.jargons:
            row_id = entry.get("id")
            rows = await _db_get(db, JARGON_MODEL, {"id": row_id}, limit=1)
            if not rows:
                stats["missing"] += 1
                if keep:
                    ledger.unlink("jargons", row_id)
                else:
                    ledger.forget("jargons", row_id)
                continue
            if jargon_key(str(rows[0].get("content") or "")) != jargon_key(
                str(entry.get("content") or "")
            ):
                stats["changed"] += 1
                if not keep:
                    ledger.forget("jargons", row_id)
                continue
            if keep:
                # 同表达方式：用活的那一行刷新 payload，缺列的旧语料也能原样写回来
                entry["payload"] = {k: v for k, v in rows[0].items() if k != "id"}
            try:
                await db.delete(model_name=JARGON_MODEL, filters={"id": row_id})
                stats["deleted"] += 1
                if keep:
                    ledger.unlink("jargons", row_id)
                else:
                    ledger.forget("jargons", row_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Galgame/话术] 删除黑话 %s 失败：%s", row_id, exc)
                stats["failed"] += 1

    ledger.save()
    return stats


async def restore_written(
    db: Any,
    ledger: StyleLedger,
    *,
    kinds: Sequence[str] = ("expressions", "jargons"),
    dry_run: bool = False,
) -> dict[str, int]:
    """把花名册里留着语料、当前却不在宿主表里的行写回去。

    给「关掉插件 → 再打开」用：关闭时 clear_written(keep=True) 只把行删掉、语料留住，
    这里按条目里存的 payload 原样重建。会话 id 照旧写回 —— 群的 session_id 由群号
    算出来，是稳定的，重开之后还是同一个。

    返回 {"restored": n, "alive": n, "relinked": n, "skipped": n, "failed": n}。
    """
    stats = {"restored": 0, "alive": 0, "relinked": 0, "skipped": 0, "failed": 0}
    # 「本来就在麦麦表里」的条数：动手之前就带 id 的那些（要恢复的恰恰是不带 id 的）
    stats["alive"] = sum(
        1
        for bucket in ("expressions", "jargons")
        if bucket in kinds
        for entry in (ledger._data.get(bucket) or [])
        if entry.get("id")
    )
    for bucket in ("expressions", "jargons"):
        if bucket not in kinds:
            continue
        model = EXPRESSION_MODEL if bucket == "expressions" else JARGON_MODEL
        # 先把宿主表现存的行读一遍：花名册的 id 有可能丢过（比如上一次保存被
        # 旧进程的副本覆盖），此时行其实还在表里 —— 那就把 id 认回来，
        # 而不是再插一条一模一样的（重复行会让用户在 WebUI 里看到双份）。
        known = await _host_keys(db, bucket)
        for entry in ledger.pending_restore(bucket):
            payload = entry.get("payload")
            if not isinstance(payload, dict) or not payload:
                stats["skipped"] += 1
                continue
            key = host_key(bucket, entry)
            if key and key in known:
                entry["id"] = known[key]
                entry.pop("removed_at", None)
                stats["relinked"] += 1
                continue
            if dry_run:
                stats["restored"] += 1
                continue
            label = entry.get("style") or entry.get("content") or "?"
            try:
                saved = await db.save(model_name=model, data=_revive_payload(payload))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Galgame/话术] 恢复失败（%s）：%s", label, exc)
                stats["failed"] += 1
                continue
            row = _rows_of(saved)
            if not row or row[0].get("id") is None:
                logger.warning("[Galgame/话术] 恢复没有返回 id：%s", label)
                stats["failed"] += 1
                continue
            entry["id"] = row[0]["id"]
            entry.pop("removed_at", None)
            stats["restored"] += 1
    ledger.save()
    return stats


async def retag_expressions(
    db: Any, ledger: StyleLedger, *, enabled: bool
) -> dict[str, int]:
    """给已经写进去的表达方式补上 / 去掉「[吧名吧]」前缀。

    style.tag_forum 是后加的配置项，之前写的行没有标签；把开关关掉的用户也会希望
    把标签抹掉。两个方向都用这个函数：按花名册把行读回来，用它的 forum 重新拼一次，
    和库里现在的文本不一样才写回去。
    """
    stats = {"updated": 0, "kept": 0, "missing": 0, "failed": 0}
    for entry in ledger.expressions:
        row_id = entry.get("id")
        rows = await _db_get(db, EXPRESSION_MODEL, {"id": row_id}, limit=1)
        if not rows:
            stats["missing"] += 1
            continue
        row = rows[0]
        # 风格被改过 -> 不是我们那条了，别动
        if style_key(str(row.get("style") or "")) != style_key(str(entry.get("style") or "")):
            stats["missing"] += 1
            continue
        current = str(row.get("situation") or "")
        base, _existing = split_forum_tag(current)
        target = tag_situation(base, str(entry.get("forum") or ""), enabled=enabled)
        if target == current:
            entry["situation"] = current
            stats["kept"] += 1
            continue
        try:
            await db.query(
                model_name=EXPRESSION_MODEL,
                query_type="update",
                data={
                    "situation": target,
                    "content_list": json.dumps([target], ensure_ascii=False),
                },
                filters={"id": row_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Galgame/话术] 改标签失败（id=%s）：%s", row_id, exc)
            stats["failed"] += 1
            continue
        entry["situation"] = target
        stats["updated"] += 1
    ledger.save()
    return stats


# 宿主注入表达方式时的硬门槛（maisaka_expression_selector.py）
VISIBLE_FLOOR = 10

# 宿主认为「可见」的表达方式：人工审核通过
_VISIBLE_FILTER = {"checked": True, "modified_by": "USER"}


async def audit(
    db: Any, ledger: StyleLedger, sessions: dict[str, str] | None = None
) -> dict[str, Any]:
    """体检：插件写的行还在不在、审了没有、每个群的可见条数够不够。"""
    report: dict[str, Any] = {
        "ledger": ledger.stats(),
        "expressions": {
            "alive": 0,
            "approved": 0,
            "pending": 0,
            "tagged": 0,
            "missing": 0,
            # 语料还在花名册里、但当前不在宿主表里的条数（关掉插件之后就是这个状态）
            "stocked": 0,
        },
        "jargons": {"alive": 0, "enabled": 0, "draft": 0, "missing": 0, "stocked": 0},
        "chats": [],
    }

    alive_styles: list[str] = []
    for entry in ledger.expressions:
        rows = await _db_get(db, EXPRESSION_MODEL, {"id": entry.get("id")}, limit=1)
        if not rows:
            report["expressions"]["missing"] += 1
            continue
        row = rows[0]
        if style_key(str(row.get("style") or "")) != style_key(str(entry.get("style") or "")):
            report["expressions"]["missing"] += 1
            continue
        report["expressions"]["alive"] += 1
        alive_styles.append(str(row.get("style") or ""))
        if split_forum_tag(str(row.get("situation") or ""))[1]:
            report["expressions"]["tagged"] += 1
        if row.get("checked") and str(row.get("modified_by") or "") == "USER":
            report["expressions"]["approved"] += 1
        else:
            report["expressions"]["pending"] += 1

    for entry in ledger.jargons:
        rows = await _db_get(db, JARGON_MODEL, {"id": entry.get("id")}, limit=1)
        if not rows:
            report["jargons"]["missing"] += 1
            continue
        row = rows[0]
        if jargon_key(str(row.get("content") or "")) != jargon_key(
            str(entry.get("content") or "")
        ):
            report["jargons"]["missing"] += 1
            continue
        report["jargons"]["alive"] += 1
        if row.get("is_jargon"):
            report["jargons"]["enabled"] += 1
        else:
            report["jargons"]["draft"] += 1

    report["expressions"]["stocked"] = len(ledger.pending_restore("expressions"))
    report["jargons"]["stocked"] = len(ledger.pending_restore("jargons"))

    for group_id, session_id in (sessions or {}).items():
        visible = await _db_get(db, EXPRESSION_MODEL, dict(_VISIBLE_FILTER, session_id=session_id), limit=1000)
        pending = await _db_get(db, EXPRESSION_MODEL, {"session_id": session_id, "checked": False}, limit=1000)
        report["chats"].append(
            {
                "group_id": group_id,
                "session_id": session_id,
                "visible": len(visible),
                "pending": len(pending),
                "enough": len(visible) >= VISIBLE_FLOOR,
            }
        )
    return report
