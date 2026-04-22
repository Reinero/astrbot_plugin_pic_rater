import asyncio
import re
import time
from typing import Any

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


_CAT_HINT_RE = re.compile(r"[,:/]")


def _build_random_params(arg_text: str) -> dict:
    t = (arg_text or "").strip()
    if not t:
        return {}
    low = t.lower()
    if low.startswith("?"):
        return {"q": t[1:].strip()}
    if low.startswith("q:"):
        return {"q": t[2:].strip()}
    if _CAT_HINT_RE.search(t):
        return {"cat": t}
    return {"q": t}


def _parse_purge_flag(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"清理", "purge", "cleanup", "clean", "true", "1", "是", "yes"}


def _parse_rating_text(text: str):
    txt = (text or "").strip()
    if not txt:
        raise ValueError("empty")
    parts = txt.split(maxsplit=1)
    score = float(parts[0])
    if not (0.0 <= score <= 5.0):
        raise ValueError("out_of_range")
    note = parts[1] if len(parts) >= 2 else None
    return score, note


class _SessionStore:
    def __init__(self):
        self._last_sent: dict[str, dict[str, Any]] = {}

    @staticmethod
    def session_key(event: AstrMessageEvent) -> str:
        sid = getattr(event, "unified_msg_origin", None)
        if sid:
            return sid
        mt = getattr(event, "message_type", "")
        if mt == "group" and hasattr(event, "group_id"):
            return f"onebot:group:{getattr(event, 'group_id')}"
        if mt == "private" and hasattr(event, "user_id"):
            return f"onebot:private:{getattr(event, 'user_id')}"
        return "unknown"

    def remember(self, event: AstrMessageEvent, image_id: str | None, relpath: str | None):
        self._last_sent[self.session_key(event)] = {"id": image_id, "relpath": relpath}

    def get_last(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        return self._last_sent.get(self.session_key(event))


class _PicApiClient:
    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        timeout_cfg = cfg.get("http_timeout", {}) if isinstance(cfg.get("http_timeout", {}), dict) else {}
        self.timeout = httpx.Timeout(
            connect=float(timeout_cfg.get("connect", 10.0)),
            read=float(timeout_cfg.get("read", 1200.0)),
            write=float(timeout_cfg.get("write", 1200.0)),
            pool=float(timeout_cfg.get("pool", 10.0)),
        )
        self.base_url = str(cfg.get("picapi_url") or "http://picapi:8000").rstrip("/")

    def abs_url(self, u: str) -> str:
        if not u:
            return u
        if u.startswith("http://") or u.startswith("https://"):
            return u
        if not u.startswith("/"):
            u = "/" + u
        return f"{self.base_url}{u}"

    async def get(self, endpoint: str, **params):
        url = f"{self.base_url}{endpoint}"
        payload = {k: v for k, v in params.items() if v is not None and v != ""}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, params=payload)
            r.raise_for_status()
            return r.json()

    async def post(self, endpoint: str, payload: Any, **params):
        url = f"{self.base_url}{endpoint}"
        query = {k: v for k, v in params.items() if v is not None and v != ""}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, params=query, json=payload)
            r.raise_for_status()
            return r.json()


@register(
    "astrbot_plugin_pic_rater",
    "nero",
    "随机发图 + 评分写入元数据（配合 picapi 使用）",
    "0.2.0",
    "https://example.com/repo",
)
class PicRater(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.client = _PicApiClient(self.config)
        self.sessions = _SessionStore()
        self.cleanup_keywords = self._parse_cleanup_keywords()
        self.first_hint_after = int(self._cfg_get("sync_progress.first_hint_after_sec", 5))
        self.ping_every = max(1, int(self._cfg_get("sync_progress.ping_every_sec", 15)))
        self.show_progress_bar = bool(self._cfg_get("sync_progress.show_progress_bar", True))
        self.show_image_meta = bool(self._cfg_get("message.show_image_meta", True))
        self.show_usage_on_invalid_score = bool(self._cfg_get("message.show_usage_on_invalid_score", True))
        logger.info("[pic_rater] init: PICAPI_URL=%s", self.client.base_url)

    def _cfg_get(self, path: str, default: Any = None) -> Any:
        cur = self.config
        for part in path.split("."):
            if not isinstance(cur, dict):
                return default
            cur = cur.get(part)
            if cur is None:
                return default
        return cur

    def _parse_cleanup_keywords(self) -> set[str]:
        words = self._cfg_get("command_aliases.cleanup_keywords", [])
        if not isinstance(words, list):
            words = []
        normalized = {str(w).strip().lower() for w in words if str(w).strip()}
        if not normalized:
            normalized = {"清理", "purge", "cleanup", "clean", "true", "1", "是", "yes"}
        return normalized

    @staticmethod
    def _render_bar(done: int, total: int, width: int = 24) -> str:
        if total <= 0:
            return f"[{'?' * width}] ?% ({done}/?)"
        pct = max(0.0, min(1.0, (done or 0) / float(total)))
        fill = int(round(pct * width))
        bar = "█" * fill + "░" * (width - fill)
        return f"[{bar}] {int(pct * 100)}% ({done}/{total})"

    async def _get_progress_json(self) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(f"{self.client.base_url}/admin/sync_progress")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            return None
        return None

    @filter.command("来一张")
    async def cmd_send_random(self, event: AstrMessageEvent, text: str = ""):
        params = _build_random_params(text)
        try:
            data = await self.client.get("/random_pic", **params)
            img_url = self.client.abs_url(data["url"])
            iid = data.get("id")
            relpath = data.get("relpath")
            fname = data.get("filename", "")
            category = data.get("category") or "*"
            self.sessions.remember(event, iid, relpath)
            yield event.image_result(img_url)
            if self.show_image_meta:
                lines = [
                    f"ID: {iid}",
                    f"分类: {category}",
                    f"文件: {fname}",
                    "评分指令：#评分 <分值> [备注]（0~5，可小数；写回XMP会四舍五入为整数）",
                ]
                if "q" in params:
                    lines.append(f"检索：{params['q']}")
                elif "cat" in params:
                    lines.append(f"分类表达式：{params['cat']}")
                yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error("[pic_rater] /来一张 失败: %s", e)
            yield event.plain_result("发图失败：没有匹配到图片，或 picapi 不可用。")

    @filter.command("整理图库")
    async def cmd_clean_gallery(self, event: AstrMessageEvent, text: str = ""):
        tokens = {t.strip().lower() for t in (text or "").split() if t.strip()}
        purge = _parse_purge_flag(text) or any(t in self.cleanup_keywords for t in tokens)
        yield event.plain_result("开始整理图库：扫描入库 → 同步 XMP 标签…")

        async def do_reindex():
            # Keep request format stable: /reindex accepts boolean body.
            return await self.client.post("/reindex", purge)

        t0 = time.monotonic()
        task = asyncio.create_task(do_reindex())
        next_ping = t0 + max(0, self.first_hint_after)
        while True:
            try:
                resp1 = await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now >= next_ping:
                    elapsed = int(time.monotonic() - t0)
                    yield event.plain_result(f"⏳ 扫盘入库进行中（已用时 {elapsed}s）")
                    next_ping = now + self.ping_every

        if not isinstance(resp1, dict):
            yield event.plain_result(f"❌ 扫盘入库失败：{resp1!r}")
            return

        t1 = time.monotonic()
        # Explicitly pass sync_subjects default limit via query for protocol clarity.
        task2 = asyncio.create_task(self.client.post("/sync_subjects", {}, limit=0))
        next_ping = t1 + max(0, self.first_hint_after)
        while True:
            try:
                resp2 = await asyncio.wait_for(asyncio.shield(task2), timeout=1.0)
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now >= next_ping:
                    elapsed = int(time.monotonic() - t1)
                    prog = await self._get_progress_json()
                    bar_txt = ""
                    if self.show_progress_bar and prog and int(prog.get("total", 0)) > 0:
                        bar_txt = " " + self._render_bar(int(prog.get("done", 0)), int(prog.get("total", 0)))
                    yield event.plain_result(f"⏳ 同步 XMP 标签进行中（已用时 {elapsed}s）{bar_txt}")
                    next_ping = now + self.ping_every

        indexed = resp1.get("indexed")
        purged = resp1.get("purged")
        processed = resp2.get("processed")
        msg = f"✅ 完成：入库 {indexed} 条"
        if purged is not None:
            msg += f"，清理 {purged} 条"
        msg += f"，同步标签 {processed} 条。"
        yield event.plain_result(msg)

    @filter.command("评分")
    async def cmd_rate(self, event: AstrMessageEvent, text: str = ""):
        try:
            score, note = _parse_rating_text(text)
        except ValueError:
            if self.show_usage_on_invalid_score:
                yield event.plain_result("用法：#/评分 <分值> [备注]  例如：#/评分 4.5 配色舒服")
            return

        last = self.sessions.get_last(event)
        if not last:
            yield event.plain_result("本会话还没有待评分的图片，请先发送：#/来一张")
            return

        for ident in [last.get("relpath"), last.get("id")]:
            if not ident:
                continue
            try:
                payload = {"id": ident, "score": score}
                if note:
                    payload["note"] = note
                resp = await self.client.post("/rate", payload)
                yield event.plain_result(f"已记录：{score} 分。当前均分：{resp.get('avg')}（共 {resp.get('count')} 次）")
                return
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    yield event.plain_result(f"评分失败：{e}")
                    return
            except Exception as e:
                yield event.plain_result(f"评分失败：{e}")
                return
        yield event.plain_result("评分失败：服务器找不到对应图片，请先重新来一张。")

    @filter.command("图类目")
    async def cmd_categories(self, event: AstrMessageEvent, text: str = ""):
        arg = (text or "").strip().strip("/")
        try:
            if not arg:
                data = await self.client.get("/categories")
                cats = data.get("categories", [])
                if not cats:
                    yield event.plain_result("没有检测到分类（顶级子文件夹）。")
                    return
                yield event.plain_result(
                    f"顶级分类（前{min(100, len(cats))}个）：\n"
                    + "、".join(cats[:100])
                    + f"\n\n下钻查看子文件夹示例：\n#图类目 {cats[0]}\n直接按分类发图示例：\n#来一张 {cats[0]}"
                )
                return

            data = await self.client.get("/dirs", path=arg)
            base = data.get("base", "")
            entries = sorted(data.get("dirs", []), key=lambda d: (-int(d.get("count", 0)), d.get("name", "")))
            files_here = data.get("files_here", 0)
            lines = [f"📂 {base or '/'} 下的子文件夹（显示前 {len(entries[:120])} 项）:"]
            for d in entries[:120]:
                lines.append(
                    f"- {d.get('name','')}  ({int(d.get('count',0))} 张)   →  下钻：#图类目 {d.get('path','')}   |  发图：#来一张 {d.get('path','')}"
                )
            if files_here:
                lines.append(f"\n此外，‘{base or '/'}’ 本层还有 {files_here} 张图片。")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error("[pic_rater] /图类目 失败: %s", e)
            yield event.plain_result("获取分类失败：请检查 picapi 是否在线。")

    @filter.command("服务状态")
    async def cmd_health(self, event: AstrMessageEvent):
        try:
            data = await self.client.get("/health")
            lines = [
                f"服务状态: {'OK' if data.get('ok') else 'UNKNOWN'}",
                f"图库目录: {data.get('gallery')}",
                f"递归扫描: {data.get('recursive')}",
                f"支持后缀: {','.join(data.get('allowed_suffixes', []))}",
                f"顶级分类数: {len(data.get('top_categories', []))}",
                f"图片总数: {data.get('total_files')}",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error("[pic_rater] /服务状态 失败: %s", e)
            yield event.plain_result("获取服务状态失败：请检查 picapi 是否在线。")


__all__ = ["PicRater"]

