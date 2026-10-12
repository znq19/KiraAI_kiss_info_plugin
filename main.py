# -*- coding: utf-8 -*-
"""
KISS 官方情报插件（kiss_info_plugin）v1.0.1

- 抓取 KISS 官网，获取 COM3D2/CM3D2/CRC3D/CR EditSystem 最新版本、公告、活动、DLC
- 轻量 HTTP 抓取 + 正则解析，只提取关键字段，不把整页 HTML 丢给 LLM
- 本地缓存增量比对：命中缓存零 token 秒回，无变化不重复抓取
- 两级深度：brief=只版本号/标题，full=含链接等更多细节
- 入口：LLM 工具 kiss_info_query（默认开）+ /kiss 命令（默认关）

v1.0.1 变更（兼容 + 稳定性，功能语义不变）
  1. 修正 3.0/2.x 下两个不存在的 API 调用：`ctx.send_to` → `ctx.send_message_chain`、
     `event.reply` → `ctx.send_message_chain(event.session.sid, …)`
     （此前自动推送推不出去、命令一条回复都发不出来，异常被吞）
  2. 修正 `event.user_id` 取不到发送者导致命令主人校验恒为假；改用 `event.message.sender.user_id`
  3. 抓取/缓存 I/O 全部移出事件循环（asyncio.to_thread），多源并发抓取
     （原先 6 个源串行阻塞，最坏可卡住事件循环 ~100s）
  4. 缓存改为**结构化数据**（零 token 秒回且内容保真）；老格式自动升级
     （旧格式命中时只回一行拼接串，版本-only 源还会显示 `date=None`）
  5. 自动检查任务纳入生命周期（terminate 取消、禁用即停、配置改动即时生效）；
     抓取失败只在日志与工具输出里体现，不再每轮推送给群
  6. 推送/开关命令真正落到插件配置文件（此前写进了没人读的 kiss_info_config.json）
  7. depth=full 现在真的更详细（更多条目 + 原文链接）；schema/manifest 补全
"""
from __future__ import annotations

import asyncio
import html as _html
import inspect
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

from core.plugin import BasePlugin, logger, on, Priority, register
from core.chat import MessageChain, User, Group
from core.chat.message_elements import Text
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent, KiraIMMessage
from core.utils.path_utils import get_config_path

PLUGIN_ID = "kiss_info_plugin"
OWNER_ID = "769690776"          # 紫小贱：命令里的主人校验保留原设计
CACHE_VERSION = 2               # 结构化缓存；旧版本字符串缓存自动升级

# ============ 数据源定义 ============
SOURCES = {
    "cm3d2": {"name": "CM3D2", "url": "https://www.kisskiss.tv/cm3d2/update.php", "type": "update"},
    "com3d2": {"name": "COM3D2&2.5", "url": "https://com3d2.jp/update/", "type": "update"},
    "crc3d": {"name": "CRC3D", "url": "https://crc3d3.jp/", "type": "news"},
    "cres": {"name": "CR EditSystem", "url": "https://cr-edit.jp/update", "type": "update"},
    "news": {"name": "KISS公告", "url": "https://www.kisskiss.tv/kiss/diary.php", "type": "news"},
    "dlc_shop": {"name": "DLC商店", "url": "https://www.kisskiss.tv/kiss/diary.php?category=DLC", "type": "news"},
}

DEFAULT_COMMANDS = ["/kiss"]
MAX_ITEMS_BRIEF = 5
MAX_ITEMS_FULL = 12
BODY_ITEMS_FULL = 3            # full 深度下，每源抓几篇正文摘要
BODY_EXCERPT_CHARS = 400       # 单篇正文摘要上限（控 token）

# bot 播报用的引导语：把情报交给模型，让它用自己的人设口吻说出来
PUSH_PROMPT = (
    "【系统定时检查】下面是 KISS 官网的最新情报，请你**用你自己的口吻**转述给这个会话里的人们"
    "（日文标题可以顺手翻成中文、版本号与日期要点清楚，别照抄这段系统说明）：\n\n"
)


def _decode(raw: bytes, charset: Optional[str]) -> str:
    """按页面声明的编码解码，失败再退 utf-8 / cp932（避免 Shift-JIS 页面变乱码）。"""
    for enc in (charset, "utf-8", "cp932"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def _fetch(url: str, timeout: int = 5) -> Optional[str]:
    """轻量抓取页面，失败重试 2 次（同步函数，调用方一律走线程池）。"""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (KISS-Info-Bot)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                try:
                    charset = resp.headers.get_content_charset()
                except Exception:
                    charset = None
                return _decode(resp.read(), charset)
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1)
    return None


def _parse_update(html: Optional[str]) -> dict:
    """解析更新页，提取版本号和日期。"""
    if not html:
        return {}
    versions = re.findall(r"Ver\.?\s*(\d+\.\d+(?:\.\d+)?)", html, re.IGNORECASE)
    dates = re.findall(r"(\d{4})[/年.](\d{1,2})[/月.](\d{1,2})", html)
    latest = versions[0] if versions else None
    latest_date = None
    if dates:
        y, m, d = dates[0]
        latest_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return {"version": latest, "date": latest_date}


def _parse_news(html: Optional[str]) -> List[dict]:
    """解析公告/新闻页，提取标题、日期与链接。"""
    if not html:
        return []
    items = []
    for m in re.finditer(
        r'<table class="blog_frame_top">.*?<th[^>]*>([^<]+)</th>.*?<td[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>([^<]+)</a>',
        html, re.S | re.I,
    ):
        date_raw = m.group(1).strip()
        href = m.group(2).strip()
        title = _html.unescape(m.group(3)).strip()     # 页面里会有 &amp; 之类实体
        dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_raw)
        if dm:
            y, mo, d = dm.groups()
            date = f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
        else:
            date = date_raw
        items.append({"date": date, "title": title, "link": href})
    if items:
        return items
    # 兜底：老结构（无 href 也能出标题）
    for m in re.finditer(
        r'<table class="blog_frame_top">.*?<th[^>]*>([^<]+)</th>.*?<td[^>]*>.*?<a[^>]*>([^<]+)</a>',
        html, re.S | re.I,
    ):
        date_raw = m.group(1).strip()
        title = _html.unescape(m.group(2)).strip()
        dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_raw)
        date = f"{dm.group(1)}-{dm.group(2).zfill(2)}-{dm.group(3).zfill(2)}" if dm else date_raw
        items.append({"date": date, "title": title})
    return items


def _parse_crc3d(html: Optional[str]) -> List[dict]:
    """解析 CRC3D 官网 NEWS 板块。"""
    if not html:
        return []
    items = []
    pattern = re.findall(
        r"(\d{4})[年/.](\d{1,2})[月/.](\d{1,2})[日]?\s*([^<\n]{5,80})", html
    )
    for y, m, d, title in pattern[:10]:
        items.append({"date": f"{y}-{m.zfill(2)}-{d.zfill(2)}", "title": title.strip()})
    return items


def _fetch_source(key: str, timeout: int) -> dict:
    """抓取并解析单个数据源（同步，调用方走线程池）。"""
    src = SOURCES[key]
    html = _fetch(src["url"], timeout)
    if html is None:
        return {"source": key, "name": src["name"], "error": "抓取失败"}
    if src["type"] == "update":
        return {"source": key, "name": src["name"], **_parse_update(html)}
    items = _parse_crc3d(html) if key == "crc3d" else _parse_news(html)
    for it in items:                      # 站内相对链接 → 绝对地址（展示即可点）
        if it.get("link"):
            it["link"] = _abs_url(it["link"], src["url"])
    return {"source": key, "name": src["name"], "items": items}


def _strip_tags(raw: str) -> str:
    """粗剥 HTML：去脚本/样式、去标签、反转义实体、压缩空白。"""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _abs_url(link: str, base: str = "") -> str:
    """把站内相对链接拼成绝对地址（博客页给的是 ./diary.php?no=…）。"""
    link = (link or "").strip()
    if not link:
        return ""
    if link.startswith("http"):
        return link
    if link.startswith("//"):
        return "https:" + link
    return urllib.parse.urljoin(base or "https://www.kisskiss.tv/kiss/diary.php", link)


def _meta_description(raw: str) -> str:
    """文章页自带的摘要（twitter:description / og:description / description），最干净。"""
    for m in re.finditer(r"<meta\b[^>]*>", raw or "", re.I):
        tag = m.group(0)
        if not re.search(r'(?:name|property)=["\'](?:twitter:description|og:description|description)["\']',
                         tag, re.I):
            continue
        c = re.search(r'content=["\'](.*?)["\']', tag, re.S | re.I)
        if c:
            txt = re.sub(r"\s+", " ", _html.unescape(c.group(1))).strip()
            if len(txt) >= 40:
                return txt
    return ""


def _extract_article_text(raw: str, limit: int = BODY_EXCERPT_CHARS) -> str:
    """取文章正文摘要：优先站点自带 meta 摘要 → blog_data 正文容器 → 整页剥标签兜底。

    （直接剥整页会带上一大串导航菜单，所以按优先级来；实在取不到才退回最后一档。）
    """
    if not raw:
        return ""
    text = _meta_description(raw)
    if not text:
        m = re.search(r'<div class="blog_data"[^>]*>(.*?)</div>\s*</div>', raw, re.S | re.I)
        if not m:
            m = re.search(r'<div class="blog_data"[^>]*>(.*)', raw, re.S | re.I)
        text = _strip_tags(m.group(1) if m else raw)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _fetch_item_text(link: str, base: str = "", limit: int = BODY_EXCERPT_CHARS) -> str:
    """抓一篇文章正文并截断成摘要（同步，调用方走线程池；失败返回空串）。"""
    url = _abs_url(link, base)
    if not url:
        return ""
    html = _fetch(url, 8)
    if not html:
        return ""
    return _extract_article_text(html, limit)


def _fingerprint(data: dict) -> str:
    """计算数据指纹用于增量比对（版本源按版本+日期，新闻源按前若干条）。"""
    if "version" in data:
        return f"{data.get('version')}|{data.get('date') or ''}"
    items = data.get("items", [])
    return "|".join(f"{i.get('date', '')}{i.get('title', '')}" for i in items[:5])


def _format(data: dict, depth: str = "brief") -> str:
    """把结构化结果格式化成可读文本。

    brief：版本号 / 标题 + 原文链接（链接用于查证与引用，两种深度都给）；
    full ：条目更多（12 条）+ 每源最新几篇的正文摘要。
    """
    full = depth == "full"
    if "error" in data:
        return f"[{data.get('name')}] {data['error']}"
    if "version" in data:
        if not data.get("version"):
            return f"[{data.get('name')}] 暂无版本信息"
        line = f"[{data.get('name')}] 最新版本 {data['version']}"
        if data.get("date"):
            line += f"（{data['date']}更新）"
        return line
    if "items" in data:
        items = data.get("items") or []
        if not items:
            return f"[{data.get('name')}] 暂无动态"
        limit = MAX_ITEMS_FULL if full else MAX_ITEMS_BRIEF
        lines = [f"[{data.get('name')}] 最新动态（{len(items)} 条）："]
        for it in items[:limit]:
            date = it.get("date") or ""
            title = it.get("title") or ""
            line = f"- {date} {title}".rstrip()
            if it.get("link"):
                # 链接在 brief/full 都给：bot 可以据此查证、引用或让用户直接点开
                line += f" → {it['link']}"
            if full and it.get("excerpt"):
                line += f"\n  {it['excerpt']}"
            lines.append(line)
        if len(items) > limit:
            lines.append(f"…还有 {len(items) - limit} 条，想看更多可以直接翻官网")
        return "\n".join(lines)
    return f"[{data.get('name')}] 无数据"


class KissInfoPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        sec = cfg.get("section_main", {})
        self.enabled = bool(sec.get("enabled", True))
        self.enable_llm_tool = bool(sec.get("enable_llm_tool", True))
        self.enable_command = bool(sec.get("enable_command", False))
        self.content_depth = str(sec.get("content_depth", "brief"))
        self.auto_check = bool(sec.get("auto_check", False))
        self.check_interval = int(sec.get("check_interval", 86400))
        self.push_targets = self._as_list(sec.get("push_targets", []))
        mode = str(sec.get("push_mode", "bot") or "bot").strip().lower()
        self.push_mode = mode if mode in ("bot", "raw") else "bot"   # bot=让 AI 用自己的口吻说；raw=机械投递原文
        self.push_also_raw = bool(sec.get("push_also_raw", False))   # 允许两种方式同时发（先落原文，再让 bot 播报）
        srcs = cfg.get("section_sources", {})
        self.source_keys = [k for k in SOURCES if bool(srcs.get(f"src_{k}", True))]
        self.commands = self._as_list(sec.get("commands", DEFAULT_COMMANDS)) or list(DEFAULT_COMMANDS)
        self.cache_path: Optional[Path] = None
        self._auto_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()          # 串行化抓取，避免工具与自动检查同时开抓

    @staticmethod
    def _as_list(value) -> List[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [v.strip() for v in str(value or "").splitlines() if v.strip()]

    # ==================== 生命周期 ====================

    async def initialize(self):
        self.cache_path = self.ctx.get_plugin_data_dir() / "kiss_info_cache.json"
        await asyncio.to_thread(self.cache_path.parent.mkdir, parents=True, exist_ok=True)
        logger.info(
            f"[kiss-info] KISS 官方情报插件加载完成 v1.0.1 "
            f"(llm_tool={self.enable_llm_tool} cmd={self.enable_command} "
            f"auto_check={self.auto_check} push_mode={self.push_mode}"
            f"{'+raw' if self.push_also_raw else ''} depth={self.content_depth} "
            f"sources={self.source_keys} push={self.push_targets})"
        )
        self._sync_auto_task()

    async def terminate(self):
        """取消后台任务（可重入：热重载会再次调用）。"""
        await self._stop_auto_task()

    # ==================== 自动检查任务 ====================

    def _sync_auto_task(self):
        """按当前配置（启用 + 自动检查 + 有推送目标）启停后台任务。"""
        should_run = bool(self.auto_check and self.push_targets)
        running = self._auto_task is not None and not self._auto_task.done()
        if should_run and not running:
            self._auto_task = asyncio.ensure_future(self._auto_check_loop())
        elif not should_run and running:
            self._auto_task.cancel()
            self._auto_task = None

    async def _stop_auto_task(self):
        task, self._auto_task = self._auto_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _auto_check_loop(self):
        """后台自动检查，有更新时推送到配置目标。"""
        while True:
            try:
                if self.enabled:
                    changes, errors = await self._check_all(force=False)
                    if changes:
                        msgs = [_format(d, self.content_depth) for d in changes]
                        await self._push("\n".join(msgs))
                    if errors:
                        logger.warning(
                            "[kiss-info] 本轮 %d 个源抓取失败：%s",
                            len(errors), "、".join(e.get("name") or e.get("source", "?") for e in errors),
                        )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[kiss-info] 自动检查异常")
            try:
                await asyncio.sleep(max(int(self.check_interval), 60))
            except asyncio.CancelledError:
                return

    async def _push(self, text: str):
        """按配置推送：

        · `push_mode=bot`：构造系统消息注入管线，让 AI **用自己的口吻**播报；
        · `push_mode=raw`：不经过 AI，直接投递整理好的原文；
        · `push_also_raw=true`：两者**同时启用** —— 先落一份原文，再让 bot 播报
          （原文与 bot 的播报各一份，互不替代；`push_mode=raw` 时该开关无效果，只发一次）。
        """
        for target in list(self.push_targets):
            raw_sent = False
            try:
                if self.push_mode == "raw" or self.push_also_raw:
                    await self.ctx.send_message_chain(target, MessageChain([Text(text)]))
                    raw_sent = True
                if self.push_mode == "bot":
                    ok = await self._push_as_event(target, text)
                    if not ok and not raw_sent:
                        # 事件注入不可用（适配器缺失等）⇒ 退回机械投递，情报不能丢
                        logger.warning(f"[kiss-info] {target} 无法让 bot 播报，改为直接投递原文")
                        await self.ctx.send_message_chain(target, MessageChain([Text(text)]))
            except Exception as e:
                logger.warning(f"[kiss-info] 推送到 {target} 失败: {e}")

    async def _push_as_event(self, target: str, content: str) -> bool:
        """构造一条「系统消息」注入消息管线，让 AI 以自己的人格口吻播报（双世代）。

        与 hotnews 插件同一套做法：KiraIMMessage(is_notice/is_mentioned) + KiraMessageEvent，
        `event.trigger(force=True)` 跳过 discard 策略，再 publish 到事件总线
        （两代 lifecycle 都把 KiraMessageEvent 订阅到各自的消息管线）。
        """
        parts = (target or "").split(":")
        if len(parts) != 3:
            logger.warning(f"[kiss-info] 推送目标格式应为 adapter:gm|dm:id：{target}")
            return False
        ada_name, st, sid = parts
        ada = self.ctx.adapter_mgr.get_adapter(ada_name)
        if ada is None:
            return False
        prompt = PUSH_PROMPT + content
        chain = MessageChain([Text(prompt)])
        now = int(time.time())
        self_id = None
        try:
            cfg = getattr(ada, "config", None)
            self_id = cfg.get("self_id") if hasattr(cfg, "get") else None
        except Exception:
            self_id = None
        msg = KiraIMMessage(
            timestamp=now,
            sender=User(user_id=sid if st == "dm" else "unknown", nickname="系统"),
            group=Group(group_id=sid) if st == "gm" else None,
            message_id=f"kiss_sys_push_{now}",
            self_id=self_id,
            is_notice=True,
            is_mentioned=True,
            chain=chain,
        )
        msg.message_str = prompt          # 不补这两个字段，AI 侧会看到空内容
        msg.message_repr = " ".join(str(getattr(e, "repr", "")) for e in chain)
        event = await self._make_event(ada, msg, now)
        event.trigger(force=True)
        await self.ctx.event_bus.publish(event)
        logger.info(f"[kiss-info] 情报已注入消息管线，交由 bot 播报 → {target}")
        return True

    async def _adapter_elements(self, adapter) -> list:
        """取适配器可输出元素清单（双世代：3.0 能力元数据 → 2.x message_types）。"""
        try:
            from core.adapter import IMCapability       # 3.0 能力架构才有
        except ImportError:
            IMCapability = None
        try:
            get_cap = getattr(adapter, "get_capability", None)
            if IMCapability is not None and get_cap is not None:
                im = get_cap(IMCapability)
                if im is not None:
                    meta = im.get_message_metadata()
                    meta = await meta if inspect.isawaitable(meta) else meta
                    elems = getattr(meta, "supported_elements", None)
                    if elems is not None:
                        return list(elems)
        except Exception:
            pass
        return list(getattr(adapter, "message_types", []) or [])

    async def _make_event(self, adapter, msg, ts) -> KiraMessageEvent:
        """构造事件：3.0 字段名是 supported_elements，2.x 是 message_types（按签名实测分流）。"""
        elements = await self._adapter_elements(adapter)
        try:
            params = set(inspect.signature(KiraMessageEvent.__init__).parameters)
        except Exception:
            params = {"message_types"}
        kw = ({"supported_elements": elements} if "supported_elements" in params
              else {"message_types": elements})
        return KiraMessageEvent(adapter=adapter.info, message=msg, timestamp=ts, **kw)

    # ==================== 缓存 ====================

    def _load_cache(self) -> dict:
        if self.cache_path and self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def _save_cache(self, cache: dict):
        if self.cache_path:
            self.cache_path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # ==================== 抓取逻辑 ====================

    async def _check_all(self, force: bool = False):
        """增量检查所有启用数据源，返回 (有变化的结果, 抓取失败的结果)。"""
        async with self._lock:
            cache = await asyncio.to_thread(self._load_cache)
            keys = list(self.source_keys)
            fetched = await asyncio.gather(
                *[asyncio.to_thread(_fetch_source, k, 5) for k in keys]
            ) if keys else []
            changes, errors = [], []
            for key, data in zip(keys, fetched):
                if "error" in data:
                    errors.append(data)
                    continue
                data = dict(data)
                data["_fp"] = _fingerprint(data)
                if force or cache.get(key, {}).get("fp") != data["_fp"]:
                    changes.append(data)
                cache[key] = {"fp": data["_fp"], "at": int(time.time()), "data": data}
            cache["_v"] = CACHE_VERSION
            await asyncio.to_thread(self._save_cache, cache)
            return changes, errors

    async def _check_cached(self) -> List[dict]:
        """命中缓存直接返回（零网络、零 token）；无缓存则先抓一次。"""
        cache = await asyncio.to_thread(self._load_cache)
        results = []
        for key in self.source_keys:
            entry = cache.get(key)
            if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
                data = dict(entry["data"])
                data.pop("_fp", None)
                results.append(data)
        if results:
            return results
        changes, errors = await self._check_all(force=True)
        return changes or errors

    async def _fetch_month(self, yyyymm: str) -> List[dict]:
        """抓取指定月份（YYYYMM）的 KISS 博客归档，返回该月公告列表。"""
        url = f"https://www.kisskiss.tv/kiss/diary.php?date={yyyymm}"
        html = await asyncio.to_thread(_fetch, url, 8)
        if not html:
            return []
        return _parse_news(html)

    # ==================== LLM 工具 ====================

    @register.tool(
        name="kiss_info_query",
        description=(
            "查询 KISS 官方情报：COM3D2/CM3D2/CRC3D/CR EditSystem 的最新版本号、更新日期、"
            "官方公告、活动、DLC。可用参数做定向查询：month=YYYYMM 查某月博客归档、"
            "source=数据源（cm3d2/com3d2/crc3d/cres/news/dlc_shop 或其中文名）只看某一源、"
            "keyword=关键词按标题过滤、depth=full 时附带正文摘要与原文链接。"
            "默认命中缓存零 token 秒回；想看最新传 force=true 强制重抓。"
            "返回结构化文本，你负责把日文标题翻译成中文再讲给用户。"
        ),
        params={
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "是否强制重新抓取最新信息（默认 false=用缓存）",
                    "default": False,
                },
                "depth": {
                    "type": "string",
                    "enum": ["brief", "full"],
                    "description": "brief=只版本号/标题；full=更多条目 + 原文链接 + 正文摘要",
                },
                "month": {
                    "type": "string",
                    "description": "查历史归档，格式 YYYYMM（如 202608）。给了它就只查这个月的公告",
                },
                "source": {
                    "type": "string",
                    "description": "只看某个数据源，如 com3d2 / news / dlc_shop，也认中文名（COM3D2、KISS公告、DLC商店）",
                },
                "keyword": {
                    "type": "string",
                    "description": "按标题关键词过滤（日文原文匹配），如 アップデート、DLC、発売",
                },
            },
        },
    )
    async def tool_query(self, event: KiraMessageBatchEvent, force: bool = False, depth: str = "",
                        month: str = "", source: str = "", keyword: str = "") -> str:
        if not self.enabled:
            return "KISS 官方情报插件未启用"
        if not self.enable_llm_tool:
            return "KISS 官方情报的 LLM 工具已关闭（可在插件配置中开启）"
        eff_depth = depth if depth in ("brief", "full") else self.content_depth
        kw = (keyword or "").strip().lower()
        try:
            # —— 历史归档：只查这一个月的公告 ——
            if (month or "").strip():
                m = month.strip()
                if not re.fullmatch(r"\d{6}", m):
                    return "month 格式要 YYYYMM，例如 202608"
                items = await self._fetch_month(m)
                if kw:
                    items = [i for i in items if kw in (i.get("title") or "").lower()]
                if not items:
                    return f"{m} 没抓到公告（可能月份太早、页面结构变了，或该关键词无匹配）"
                data = [{"name": f"KISS公告 {m}", "items": items}]
                if eff_depth == "full":
                    await self._attach_bodies(data)
                return "\n".join(_format(d, eff_depth) for d in data)

            data = await self._cached_or_fetch(force)

            # —— 指定数据源 ——
            if (source or "").strip():
                want = source.strip().lower()
                picked = [d for d in data
                          if want == (d.get("source") or "").lower()
                          or want == (d.get("name") or "").lower()
                          or want in (d.get("name") or "").lower()]
                if not picked:
                    names = "、".join(f"{k}（{SOURCES[k]['name']}）" for k in self.source_keys)
                    return f"没有叫「{source}」的数据源，可选：{names}"
                data = picked

            # —— 关键词过滤（只对列表类源生效）——
            if kw:
                filtered = []
                for d in data:
                    if "items" not in d:
                        continue
                    hit = [i for i in (d.get("items") or []) if kw in (i.get("title") or "").lower()]
                    if hit:
                        filtered.append({**d, "items": hit})
                if not filtered:
                    return f"没有标题包含「{keyword}」的条目（可换关键词，或去掉 keyword 看全部）"
                data = filtered

            if not data:
                return "KISS 各源暂无数据"
            if eff_depth == "full":
                await self._attach_bodies(data)
            return "\n".join(_format(d, eff_depth) for d in data)
        except Exception as e:
            logger.exception("[kiss-info] 工具调用失败")
            return f"KISS 官方情报查询失败：{e}"

    async def _cached_or_fetch(self, force: bool):
        if force:
            changes, errors = await self._check_all(force=True)
            return changes + errors
        return await self._check_cached()

    async def _attach_bodies(self, data: list):
        """full 深度：给每个列表源的最新几篇抓正文摘要（并发 + 限流，控 token）。"""
        targets = []
        for d in data:
            for it in (d.get("items") or [])[:BODY_ITEMS_FULL]:
                if it.get("link") and not it.get("excerpt"):
                    targets.append(it)
        if not targets:
            return
        texts = await asyncio.gather(
            *[asyncio.to_thread(_fetch_item_text, it.get("link") or "",
                                it.get("_base") or "") for it in targets]
        )
        for it, txt in zip(targets, texts):
            if txt:
                it["excerpt"] = txt

    # ==================== 命令 ====================

    @on.im_message(priority=Priority.HIGH)
    async def on_cmd(self, event: KiraMessageEvent, *_):
        if not self.enabled or not self.enable_command:
            return
        text = "".join(m.text for m in event.message.chain if isinstance(m, Text)).strip()
        cmd = self._match_command(text)
        if cmd is None:
            return
        arg = text[len(cmd):].strip()
        try:
            await self._handle_cmd(event, arg)
        except Exception:
            logger.exception("[kiss-info] 命令处理失败")
        event.discard()

    def _match_command(self, text: str) -> Optional[str]:
        if not text:
            return None
        hit = None
        for c in self.commands:
            if text.startswith(c) and (hit is None or len(c) > len(hit)):
                hit = c
        return hit

    @staticmethod
    def _sender_id(event: KiraMessageEvent) -> str:
        """取发送者 ID：3.0/2.x 都在 event.message.sender 上（哨兵字段 user_id 不存在）。"""
        try:
            sender = getattr(getattr(event, "message", None), "sender", None)
            sid = getattr(sender, "user_id", "") if sender is not None else ""
            return str(sid or "")
        except Exception:
            return ""

    async def _reply(self, event: KiraMessageEvent, text: str):
        """把文本回给当前会话（KiraMessageEvent 上没有 reply 方法，走统一发送入口）。"""
        try:
            sid = event.session.sid
        except Exception:
            return
        await self.ctx.send_message_chain(sid, MessageChain([Text(text)]))

    async def _handle_cmd(self, event: KiraMessageEvent, arg: str):
        sender = self._sender_id(event)
        parts = arg.split()
        sub = parts[0] if parts else "查"

        if sub == "查":
            data = await self._check_cached()
            msgs = [_format(d, self.content_depth) for d in data] or ["KISS 各源暂无数据"]
        elif sub == "详情":
            changes, errors = await self._check_all(force=True)
            msgs = [_format(d, "full") for d in (changes + errors)] or ["暂无数据"]
        elif sub == "历史":
            if len(parts) < 2:
                msgs = ["用法：/kiss 历史 YYYYMM（如 202608）"]
            else:
                yyyymm = parts[1]
                if not re.fullmatch(r"\d{6}", yyyymm):
                    msgs = ["月份格式不对，要 YYYYMM 如 202608"]
                else:
                    items = await self._fetch_month(yyyymm)
                    if not items:
                        msgs = [f"{yyyymm} 月没抓到公告，可能月份太早或页面结构变了"]
                    else:
                        lines = [f"{yyyymm} 月 KISS 公告（{len(items)}条）："]
                        for it in items:
                            lines.append(f"- {it['date']} {it['title']}")
                        msgs = ["\n".join(lines)]
        elif sub == "推送":
            if sender != OWNER_ID:
                msgs = ["只有主人能配置这个插件哦"]
            elif len(parts) < 2:
                msgs = ["用法：/kiss 推送 群号 或 QQ号（可多个，空格分隔）"]
            else:
                full = [self._normalize_target(t) for t in parts[1:]]
                full = [t for t in full if t]
                await self._update_config({"push_targets": full, "auto_check": True})
                msgs = [f"已配置推送作用域：{full}，有更新时会推送到这些目标"]
        elif sub == "开":
            if sender != OWNER_ID:
                msgs = ["只有主人能配置这个插件哦"]
            else:
                await self._update_config({"auto_check": True})
                state = "已开启自动推送" if self.push_targets else "已开启自动检查，但还没配置推送目标（/kiss 推送 群号）"
                msgs = [state]
        elif sub == "关":
            if sender != OWNER_ID:
                msgs = ["只有主人能配置这个插件哦"]
            else:
                await self._update_config({"auto_check": False})
                msgs = ["已关闭自动推送"]
        else:
            msgs = ["用法：/kiss 查 | 详情 | 历史 YYYYMM | 推送 群号 | 开 | 关"]

        for m in msgs:
            await self._reply(event, m)

    @staticmethod
    def _normalize_target(raw: str) -> str:
        """把用户输入规范成作用域 ID：裸数字→qq:gm:<号>；已带 adapter:type:id 的原样保留。"""
        t = str(raw or "").strip()
        if not t:
            return ""
        if t.isdigit():
            return f"qq:gm:{t}"
        return t

    async def _update_config(self, updates: dict) -> bool:
        """改插件配置：内存 + 落盘（data/config/plugins/<id>.json），并同步后台任务。

        注：这里写的是**真实的插件配置**，面板与框架读的就是它；
        历史版本写的是 data/plugin_data/kiss_info_config.json（没人读，等于没生效）。
        """
        try:
            sec = self.plugin_cfg.setdefault("section_main", {})
            for k, v in updates.items():
                sec[k] = v
                if k == "push_targets":
                    self.push_targets = self._as_list(v)
                elif k == "auto_check":
                    self.auto_check = bool(v)
            path = get_config_path() / "plugins" / f"{PLUGIN_ID}.json"
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                path.write_text,
                json.dumps(self.plugin_cfg, ensure_ascii=False, indent=4),
                "utf-8",
            )
            self._sync_auto_task()
            return True
        except Exception as e:
            logger.error(f"[kiss-info] 配置回写失败: {e}")
            return False
