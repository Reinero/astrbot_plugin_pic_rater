import asyncio
import time

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .parsers import build_random_params, parse_purge_flag, parse_rating_text
from .picapi_client import PicApiClient
from .session_store import SessionStore


@register(
    "astrbot_plugin_pic_rater",
    "nero",
    "随机发图 + 评分写入元数据（配合 picapi 使用）",
    "0.2.0",
    "https://example.com/repo",
)
class PicRater(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.client = PicApiClient()
        self.sessions = SessionStore()
        logger.info("[pic_rater] init: PICAPI_URL=%s", self.client.base_url)

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
        params = build_random_params(text)
        try:
            data = await self.client.get("/random_pic", **params)
            img_url = self.client.abs_url(data["url"])
            iid = data.get("id")
            relpath = data.get("relpath")
            fname = data.get("filename", "")
            category = data.get("category") or "*"
            self.sessions.remember(event, iid, relpath)
            yield event.image_result(img_url)
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
        purge = parse_purge_flag(text)
        yield event.plain_result("开始整理图库：扫描入库 → 同步 XMP 标签…")

        async def do_reindex():
            try:
                return await self.client.post("/reindex", purge)
            except Exception:
                return await self.client.post("/reindex", {"purge_missing": purge})

        t0 = time.monotonic()
        task = asyncio.create_task(do_reindex())
        while True:
            try:
                resp1 = await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                break
            except asyncio.TimeoutError:
                if int(time.monotonic() - t0) % 15 == 0:
                    elapsed = int(time.monotonic() - t0)
                    yield event.plain_result(f"⏳ 扫盘入库进行中（已用时 {elapsed}s）")

        if not isinstance(resp1, dict):
            yield event.plain_result(f"❌ 扫盘入库失败：{resp1!r}")
            return

        t1 = time.monotonic()
        task2 = asyncio.create_task(self.client.post("/sync_subjects", {}))
        while True:
            try:
                resp2 = await asyncio.wait_for(asyncio.shield(task2), timeout=1.0)
                break
            except asyncio.TimeoutError:
                if int(time.monotonic() - t1) % 15 == 0:
                    elapsed = int(time.monotonic() - t1)
                    prog = await self._get_progress_json()
                    bar_txt = ""
                    if prog and int(prog.get("total", 0)) > 0:
                        bar_txt = " " + self._render_bar(int(prog.get("done", 0)), int(prog.get("total", 0)))
                    yield event.plain_result(f"⏳ 同步 XMP 标签进行中（已用时 {elapsed}s）{bar_txt}")

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
            score, note = parse_rating_text(text)
        except ValueError:
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
