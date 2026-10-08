"""galgame领域大神 —— 配置模型。

三个数据源（月幕 / VNDB / 批评空间）各自可开关，任一源不可用都不影响其余源出结果。

面板上的文字约定：节的说明来自类的 docstring，字段下方那行小灰字来自 hint ——
所以两者都只写一句话，细节留 README；字段的 description 不进面板，保持一句话即可。
"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase
from pydantic import model_validator


class PluginSection(PluginConfigBase):
    """插件总开关，装好后先来这里打开。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=False,
        description="是否启用插件（安装后默认关闭）",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本号，请勿手动修改",
        json_schema_extra={"label": "配置版本", "hidden": True},
    )


class SourceSection(PluginConfigBase):
    """三个数据源各自开关，任一源不可用不影响其余源。"""

    __ui_label__ = "数据源"
    __ui_icon__ = "database"
    __ui_order__ = 1

    ymgal_enabled: bool = Field(
        default=True,
        description="启用月幕：中文名、汉化状态、别名检索、中文简介",
        json_schema_extra={"label": "启用月幕（中文源）"},
    )
    vndb_enabled: bool = Field(
        default=True,
        description="启用 VNDB：通关时长、内容标签、多语言标题、角色声优",
        json_schema_extra={"label": "启用 VNDB（深度元数据）"},
    )
    egs_enabled: bool = Field(
        default=True,
        description="启用批评空间：中央值、得点分布",
        json_schema_extra={"label": "启用批评空间（评分）", "hint": "要先有一份本地索引，见「批评空间索引」"},
    )
    timeout: int = Field(
        default=15,
        ge=5,
        le=60,
        description="单次 HTTP 请求超时（秒）",
        json_schema_extra={"label": "请求超时（秒）"},
    )
    user_agent: str = Field(
        default="maibot-galgame/1.0.0 (https://github.com/mizuku003/maibot-galgame)",
        description="请求时使用的 User-Agent",
        json_schema_extra={"label": "User-Agent", "hint": "VNDB 要求带上项目地址，别改成浏览器 UA"},
    )
    proxy: str = Field(
        default="",
        description="三个源共用的 HTTP 代理地址",
        json_schema_extra={"label": "（高级）代理", "hint": "留空直连；如 http://127.0.0.1:7890"},
    )
    ymgal_client_id: str = Field(
        default="ymgal",
        description="月幕开放 API 的 client_id",
        json_schema_extra={"label": "月幕 client_id", "hint": "默认是官方公开凭证，一般不用改"},
    )
    ymgal_client_secret: str = Field(
        default="luna0327",
        description="月幕开放 API 的 client_secret",
        # 键名必须是 x-widget：SDK 的 _build_field_schema 只认它（和 x-icon），
        # 写成 widget 会被静默忽略，密钥就以明文输入框露在 WebUI 上。
        json_schema_extra={"label": "月幕 client_secret", "x-widget": "password"},
    )


class EgsSection(PluginConfigBase):
    """批评空间的本地索引：中央值、得点分布、短评与长文感想。"""

    __ui_label__ = "批评空间索引"
    __ui_icon__ = "list-ordered"
    __ui_order__ = 2

    auto_build: bool = Field(
        default=True,
        description="插件加载时若索引不存在或已过期，自动在后台建一次",
        json_schema_extra={"label": "自动建索引", "hint": "在后台跑，不阻塞聊天"},
    )
    refresh_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="索引多少天后算过期，过期后下次自动重建",
        json_schema_extra={"label": "索引有效期（天）"},
    )
    min_votes: int = Field(
        default=5,
        ge=1,
        le=500,
        description="进索引的最低数据数（EGS 的 count 参数）",
        json_schema_extra={"label": "上榜最低数据数", "hint": "调高：索引小而快、冷门查不到；调低：覆盖全、翻页多"},
    )
    max_pages: int = Field(
        default=200,
        ge=1,
        le=600,
        description="全量重建时最多翻多少页（每页 100 条）",
        json_schema_extra={"label": "全量重建最多翻页数", "hint": "翻到底会自动停；调小省时间，低分段作品跟着变少"},
    )
    update_pages: int = Field(
        default=30,
        ge=1,
        le=600,
        description="增量更新时重抓榜单前几页",
        json_schema_extra={"label": "增量更新页数", "hint": "榜单按中央值降序，新作与变化都在前面这段"},
    )
    scan_pages: int = Field(
        default=5,
        ge=1,
        le=50,
        description="没有本地索引时扫榜的页数",
        json_schema_extra={"label": "无索引时扫榜页数", "hint": "定位到分数档后前后多取几页，取多更全更慢"},
    )
    delay_seconds: float = Field(
        default=1.2,
        ge=0.3,
        le=10.0,
        description="翻页之间的间隔（秒）",
        json_schema_extra={"label": "翻页间隔（秒）", "hint": "EGS 是个人站，别调太小"},
    )
    match_threshold: int = Field(
        default=72,
        ge=50,
        le=100,
        description="名称匹配的最低相似度（百分比），低于此值视为没匹配上",
        json_schema_extra={"label": "名称匹配阈值", "hint": "宁缺毋滥"},
    )
    fetch_detail: bool = Field(
        default=True,
        description="查资料时额外抓一次详情页，用来换评价数据",
        json_schema_extra={"label": "抓取详情页（评价数据）", "hint": "关掉只有中央值一个数字：更快，但少很多料"},
    )
    max_reviews: int = Field(
        default=3,
        ge=0,
        le=8,
        description="详情页里取几条短评给模型参考（0 = 不取）",
        json_schema_extra={"label": "短评条数"},
    )
    fetch_long_reviews: bool = Field(
        default=True,
        description="抓批评空间的长文感想（每篇一次请求）",
        json_schema_extra={"label": "抓长文感想（锐评）", "hint": "短评只有一两句话，长文才有完整的批评与拆解"},
    )
    max_long_reviews: int = Field(
        default=2,
        ge=0,
        le=5,
        description="抓几篇长文感想（0 = 不抓）",
        json_schema_extra={"label": "长文感想条数", "hint": "剧透的永远排在不剧透的后面"},
    )
    long_review_chars: int = Field(
        default=600,
        ge=100,
        le=3000,
        description="每篇长文感想截断到多少字再交给模型",
        json_schema_extra={"label": "长文感想截断字数", "hint": "长文动辄几千字，全塞进去会把上下文撑爆"},
    )
    proxy: str = Field(
        default="",
        description="批评空间专用代理地址",
        json_schema_extra={"label": "（高级）批评空间专用代理", "hint": "留空跟随「数据源」代理；基本只能用日本节点，其它地区多半连不上"},
    )


class RecommendSection(PluginConfigBase):
    """没给明确条件时的随机推荐规则。"""

    __ui_label__ = "推荐"
    __ui_icon__ = "sparkles"
    __ui_order__ = 3

    default_count: int = Field(
        default=3,
        ge=1,
        le=10,
        description="每次推荐返回几部作品",
        json_schema_extra={"label": "默认推荐条数"},
    )
    min_vndb_rating: float = Field(
        default=70.0,
        ge=0.0,
        le=100.0,
        description="推荐时的 VNDB 评分下限（0 = 不限）",
        json_schema_extra={"label": "VNDB 评分下限", "hint": "VNDB 的是 0~100 的贝叶斯分"},
    )
    min_vndb_votes: int = Field(
        default=50,
        ge=0,
        le=10000,
        description="推荐时的 VNDB 票数下限",
        json_schema_extra={"label": "VNDB 票数下限", "hint": "过滤只有几个人评过的冷门作"},
    )
    exclude_restricted: bool = Field(
        default=False,
        description="推荐时排除 18 禁作品（月幕的 restricted 字段）",
        json_schema_extra={"label": "排除 18 禁"},
    )
    prefer_chinese: bool = Field(
        default=True,
        description="推荐时给有中文版的作品加分（月幕的汉化标记）",
        json_schema_extra={"label": "优先有汉化的"},
    )


class OutputSection(PluginConfigBase):
    """资料卡画什么、发不发图。"""

    __ui_label__ = "输出"
    __ui_icon__ = "image"
    __ui_order__ = 4

    send_card: bool = Field(
        default=True,
        description="把资料卡渲染成图片发出去（关闭则只回文本）",
        json_schema_extra={"label": "发送资料卡图片"},
    )
    card_width: int = Field(
        default=830,
        ge=480,
        le=1200,
        description="资料卡渲染宽度（像素）",
        json_schema_extra={"label": "卡片宽度", "hint": "资料卡与 /gal help 用法卡共用这个宽度"},
    )
    max_tags: int = Field(
        default=12,
        ge=3,
        le=30,
        description="资料卡上最多显示几个标签",
        json_schema_extra={"label": "标签显示上限", "hint": "VNDB 标签动辄上百个，只挑权重最高的"},
    )
    accent_color: str = Field(
        default="#c2355f",
        # 这个值是直接内插进 CSS 的，不卡死格式就等于把 <style> 交给配置面板。
        # 只收 #RGB / #RRGGBB 两种写法。
        pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$",
        description="资料卡主色调（十六进制）",
        json_schema_extra={"label": "卡片主色", "hint": "填 #RGB 或 #RRGGBB；用法卡固定暖金，不受这里影响"},
    )
    show_egs_distribution: bool = Field(
        default=False,
        description="资料卡上是否画批评空间的得点分布柱状图",
        json_schema_extra={"label": "显示得点分布"},
    )
    show_reviews: bool = Field(
        default=True,
        description="资料卡上是否放两条批评空间短评（带得分）",
        json_schema_extra={"label": "显示短评"},
    )
    hide_adult_pov: bool = Field(
        default=True,
        description="资料卡上隐藏成人向评价维度",
        json_schema_extra={"label": "隐藏成人向评价维度", "hint": "只影响卡片，给模型的数据不变"},
    )
    tool_card_param: bool = Field(
        default=False,
        description="工具调用时要不要允许出图",
        json_schema_extra={
            "label": "允许工具出图",
            "hint": "关：工具只回文字资料，卡片只由指令发；开：模型可以传 send_card=true 出图",
        },
    )


# /锐评 的提示词模板，可以在配置面板里整段改。
# 可用占位符：{persona} 锐评口吻 / {max_chars} 篇幅上限。
# 还认一个 {material} —— 故意不写进默认模板：查到的资料由代码在背后追加到末尾，
# 提示词本身保持干净（面板里看到的全是「怎么说话」，没有资料骨架）。
# 第 6 条那串词组是「人机味」的主要来源：模型一旦用上「总的来说」「作为一款」
# 就自动切回评测稿腔调；宁可写得糙一点，也要像群里的人说话。
DEFAULT_REVIEW_PROMPT = (
    "你在一个 galgame 群里，人设是{persona}。"
    "群友刚问起下面这作，你顺手说两句自己的看法 —— 这就是「锐评」。\n"
    "写法要求：\n"
    "1. 只准用素材里的事实。素材里没有的（销量、staff、续作、剧情细节）一律不许编；\n"
    "2. 像在群里打字，不像写评测。开头别寒暄、别先报参数，第一句就要有观点；\n"
    "3. 别写成「优点 + 缺点 + 总结」的模板，也别分点、别用 markdown 标题或加粗；\n"
    "4. 要具体：说清是哪一段 / 哪个机制 / 哪种走向让你觉得爽或者烦 ——"
    "「中盘开始日常变得又长又平」比「节奏一般」有用得多；\n"
    "5. 【长文感想】是别的玩家的原话，可以顺着他们的观点说，"
    "但别整句照抄，也别复述标了剧透的剧情；\n"
    "6. 别出现"
    "「总的来说」「综上所述」「值得一提」「无论是…还是…」「堪称」「不得不说」"
    "「作为一款…」「本作」这类书面语，也不要自称 AI、不要提「资料显示」；\n"
    "7. 全篇 {max_chars} 字以内。可以分成 2~4 段短话"
    "（像连着发出去的好几条消息，段与段之间空一行），也可以一段说完；\n"
    "8. 资料太少就直说「这作资料不多」，然后只讲你确实能讲的；\n"
    "9. 末尾会附一段参考资料，那是给你看的 —— 别把它的标题、字段名、"
    "「素材」「资料」这类词写进正文，也别整段照抄。\n"
)


class ReviewSection(PluginConfigBase):
    """/锐评 命令：插件再调一次模型，把资料压成一段成品锐评。"""

    __ui_label__ = "锐评"
    __ui_icon__ = "message-square-quote"
    __ui_order__ = 5

    enabled: bool = Field(
        default=True,
        description="允许用 /锐评 <作品名> 写一段锐评",
        json_schema_extra={"label": "启用 /锐评 命令", "hint": "依据批评空间的资料，不依赖主对话上下文"},
    )
    persona: str = Field(
        default="懂 galgame 的老玩家",
        description="锐评的口吻 / 人设",
        json_schema_extra={"label": "锐评口吻", "hint": "填进提示词的 {persona}"},
    )
    follow_bot: bool = Field(
        default=False,
        description="锐评沿用机器人自己的人设",
        json_schema_extra={
            "label": "沿用麦麦人设",
            "hint": "开：{persona} 换成麦麦的「人格设定」；关：用上面的「锐评口吻」（默认关）",
        },
    )
    prompt: str = Field(
        default=DEFAULT_REVIEW_PROMPT,
        description="锐评用的提示词模板",
        json_schema_extra={
            "label": "锐评提示词",
            "hint": "资料由插件接在末尾；占位符 {persona} / {max_chars}，也可自己写 {material}",
            # 宿主面板认的是 x-widget（映射 ui_type）和 rows（多行高度）
            "x-widget": "textarea",
            "rows": 14,
        },
    )
    max_chars: int = Field(
        default=300,
        ge=80,
        le=1200,
        description="锐评的篇幅上限（字）",
        json_schema_extra={"label": "锐评篇幅（字）"},
    )
    model_task: str = Field(
        default="replyer",
        description="用哪个模型任务来写锐评（默认 replyer = 回复模型）",
        json_schema_extra={
            "label": "模型任务",
            "hint": "默认 replyer（回复模型）；可填宿主其它任务名，留空用宿主默认任务 utils",
        },
    )


class StyleSection(PluginConfigBase):
    """抓贴吧帖子学表达方式与黑话，写进麦麦自己的那两页（默认关闭）。"""

    __ui_label__ = "贴吧话术"
    __ui_icon__ = "message-circle"
    __ui_order__ = 6

    enabled: bool = Field(
        default=False,
        description="允许学习贴吧话术，学到的会写进麦麦的表达方式/黑话页",
        json_schema_extra={"label": "启用贴吧话术学习", "hint": "默认关闭。写进去的行是否被采用由麦麦自己的审核设置决定"},
    )
    inject_expressions: bool = Field(
        default=False,
        description="让麦麦使用学到的表达方式",
        json_schema_extra={"label": "注入表达方式", "hint": "默认关闭。关掉会从麦麦表里撤出；语料留档，打开自动写回"},
    )
    inject_jargons: bool = Field(
        default=False,
        description="让麦麦使用学到的黑话",
        json_schema_extra={"label": "注入黑话", "hint": "默认关闭。关掉会从麦麦表里撤出；语料留档，打开自动写回"},
    )
    forums: list[str] = Field(
        default_factory=lambda: ["galgame", "galgame笑话"],
        description="学哪些吧（填吧名，不带「吧」字）",
        json_schema_extra={"label": "要学习的吧"},
    )
    clean_on_disable: bool = Field(
        default=True,
        description="关闭插件时自动撤掉写进麦麦的行",
        json_schema_extra={"label": "关闭插件时清空已写入的行", "hint": "语料留档，重开后用 /gal 话术 恢复 写回"},
    )
    chat_ids: list[str] = Field(
        default_factory=list,
        description="学到的表达方式挂到哪些群（填群号，一行一个）",
        json_schema_extra={"label": "挂到哪些群", "hint": "默认留空 = 写成全局表达方式；填群号就只挂到这些群"},
    )
    auto_learn: bool = Field(
        default=False,
        description="插件启用后按下面的间隔自动学一次",
        json_schema_extra={"label": "自动学习", "hint": "默认关闭。关掉就只能手动 /gal 话术 刷新"},
    )
    interval_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="自动学习的间隔（小时）",
        json_schema_extra={"label": "学习间隔（小时）", "hint": "贴吧话术变得不快，一天一次足够"},
    )
    list_pages: int = Field(
        default=4,
        ge=1,
        le=20,
        description="每个吧先翻几页列表（每页 30 条）",
        json_schema_extra={"label": "每个吧翻页数", "hint": "回复数印在列表页上，多翻几页挑得更准"},
    )
    posts_per_forum: int = Field(
        default=50,
        ge=1,
        le=200,
        description="每个吧最多读几个帖子（读帖子第一页楼层）",
        json_schema_extra={"label": "每个吧读几个帖子"},
    )
    target_phrases: int = Field(
        default=50,
        ge=1,
        le=300,
        description="每个吧攒到多少条表达方式就停手",
        json_schema_extra={"label": "每个吧目标条数"},
    )
    tag_forum: bool = Field(
        default=True,
        description="把吧名做成「[galgame吧]」前缀写进情境",
        json_schema_extra={"label": "情境里标出吧名", "hint": "这样在表达方式页能按吧搜索、看出是哪学的"},
    )
    min_replies: int = Field(
        default=10,
        ge=0,
        le=1000000,
        description="回复数低于这个数的帖子不读",
        json_schema_extra={"label": "回复数下限", "hint": "人太少，话术不典型"},
    )
    max_replies: int = Field(
        default=2000,
        ge=1,
        le=10000000,
        description="回复数高于这个数的帖子不读",
        json_schema_extra={"label": "回复数上限", "hint": "水楼/直播楼里全是「+1」「顶」，学不到东西"},
    )
    max_floors: int = Field(
        default=30,
        ge=1,
        le=100,
        description="每个帖子最多读几层",
        json_schema_extra={"label": "每帖最多读几层", "hint": "帖子页只给第一页，实测上限 30 层"},
    )
    reply_min_chars: int = Field(
        default=4,
        ge=1,
        le=100,
        description="楼层回复短于这个字数就不收",
        json_schema_extra={"label": "回复最短字数", "hint": "太短的多半只有表情"},
    )
    reply_max_chars: int = Field(
        default=200,
        ge=20,
        le=2000,
        description="楼层回复长于这个字数就不收",
        json_schema_extra={"label": "回复最长字数", "hint": "长文是大段感想，不是话术"},
    )
    delay_seconds: float = Field(
        default=3.0,
        ge=0.3,
        le=30.0,
        description="抓取之间的间隔（秒）",
        json_schema_extra={"label": "抓取间隔（秒）", "hint": "帖子正文页很敏感，容易触发百度安全验证"},
    )
    proxy: str = Field(
        default="",
        description="贴吧专用代理地址",
        json_schema_extra={"label": "（高级）贴吧专用代理", "hint": "留空跟随「数据源」代理；贴吧一般能直连"},
    )


    @model_validator(mode="after")
    def _check_reply_windows(self) -> "StyleSection":
        """回复数 / 字数的下限不能大于上限。

        两个字段各自都合法，合起来却会把每一层都过滤掉（结果永远显示「没学到东西」），
        用户从面板上根本看不出是配置问题 —— 所以保存配置时就直说。
        """
        if self.min_replies > self.max_replies:
            raise ValueError("回复数下限不能大于上限")
        if self.reply_min_chars > self.reply_max_chars:
            raise ValueError("回复最短字数不能大于最长字数")
        return self


class NewsSection(PluginConfigBase):
    """情报 / 预定 / 新作 三条命令的资讯源与翻译。"""

    __ui_label__ = "最新情报"
    __ui_icon__ = "newspaper"
    __ui_order__ = 7

    enabled: bool = Field(
        default=True,
        description="是否启用 /gal 情报 与 /gal 预定",
        json_schema_extra={"label": "启用最新情报", "hint": "/gal new 走月幕发售日历，不受这里影响"},
    )
    sources: list[str] = Field(
        default_factory=lambda: ["bugbug", "dgame", "ymgal"],
        description="资讯源，可多选",
        json_schema_extra={"label": "资讯源", "hint": "bugbug 日文新闻 / dgame 同人新闻 / ymgal 月幕中文文章"},
    )
    translate: bool = Field(
        default=True,
        description="用模型把日文标题与正文翻成中文",
        json_schema_extra={"label": "翻译日文标题", "hint": "关掉显示日文省调用；正文分块翻，长文多花几次"},
    )
    upcoming_days: int = Field(
        default=90,
        ge=7,
        le=365,
        description="/gal 预定 默认往后看多少天",
        json_schema_extra={"label": "预定天数", "hint": "月幕日历按月取，跨月会自动多抓几页"},
    )
    per_source: int = Field(
        default=6,
        ge=1,
        le=30,
        description="每个源最多取几条",
        json_schema_extra={"label": "每源条数", "hint": "防止一个源刷屏把别家挤掉"},
    )
    max_items: int = Field(
        default=12,
        ge=3,
        le=50,
        description="一次最多展示几条",
        json_schema_extra={"label": "最多展示条数"},
    )
    summary_chars: int = Field(
        default=90,
        ge=0,
        le=300,
        description="新闻列表里的摘要截断字数（0 = 不截断）",
        json_schema_extra={"label": "列表摘要字数"},
    )
    detail_full: bool = Field(
        default=True,
        description="看详情时再跑一趟原文页，把整篇正文抓回来",
        json_schema_extra={"label": "详情抓完整正文", "hint": "抓失败自动退回 RSS 摘要，不会发空"},
    )
    detail_chars: int = Field(
        default=3000,
        ge=0,
        le=20000,
        description="详情正文最多保留多少字（0 = 不限制）",
        json_schema_extra={"label": "详情正文字数上限", "hint": "太长既费翻译调用又不好读"},
    )
    cache_minutes: int = Field(
        default=15,
        ge=0,
        le=720,
        description="同一进程内的抓取缓存（分钟）",
        json_schema_extra={"label": "抓取缓存（分钟）", "hint": "只是本进程缓存，重启失效；太大看不到新新闻"},
    )


class GalgameConfig(PluginConfigBase):
    """galgame领域大神 —— 顶层配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    source: SourceSection = Field(default_factory=SourceSection)
    egs: EgsSection = Field(default_factory=EgsSection)
    recommend: RecommendSection = Field(default_factory=RecommendSection)
    output: OutputSection = Field(default_factory=OutputSection)
    review: ReviewSection = Field(default_factory=ReviewSection)
    style: StyleSection = Field(default_factory=StyleSection)
    news: NewsSection = Field(default_factory=NewsSection)
