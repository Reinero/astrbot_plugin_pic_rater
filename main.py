from typing import Dict, Any, Optional
import os
import httpx

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_pic_rater",
    "nero",
    "随机发图 + 评分写入元数据（配合 picapi 使用）",
    "0.1.1",
    "https://example.com/repo"
)
class PicRater(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 与 docker-compose 在同一网络时可用服务名；需要的话用环境变量覆盖
        self.base_url = os.getenv("PICAPI_URL", "http://picapi:8000").rstrip("/")
        self.last_sent: Dict[str, Dict[str, Any]] = {}
        logger.info("[pic_rater] init: PICAPI_URL=%s", self.base_url)

    # --------- 小工具 ----------
    def _session_key(self, event: AstrMessageEvent) -> str:
        # 优先 v3 的统一会话ID；无则回退 OneBot v11 字段
        sid = getattr(event, "unified_msg_origin", None)
        if sid:
            return sid
        mt = getattr(event, "message_type", "")
        if mt == "group" and hasattr(event, "group_id"):
            return f"onebot:group:{getattr(event,'group_id')}"
        if mt == "private" and hasattr(event, "user_id"):
            return f"onebot:private:{getattr(event,'user_id')}"
        return "unknown"

    def _abs_url(self, u: str) -> str:
        # /static/xxx → 拼成 http://picapi:8000/static/xxx
        if not u:
            return u
        if u.startswith("http://") or u.startswith("https://"):
            return u
        if not u.startswith("/"):
            u = "/" + u
        return f"{self.base_url}{u}"

    async def _get(self, path: str, **params):
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={k: v for k, v in params.items() if v is not None and v != ""})
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, payload: dict):
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()

    # --------- 指令 ----------
    # 用法：#来一张   或   #来一张 风景:3,人像:1   或   #来一张 壁纸/风景
    @filter.command("来一张")
    async def cmd_send_random(self, event, text: str = ""):
        cat = (text or "").strip() or None
        try:
            data = await self._get("/random_pic", cat=cat)
            img_url = self._abs_url(data["url"])
            iid = data.get("id")
            relpath = data.get("relpath")  # ★ 取出 relpath
            fname = data.get("filename", "")
            category = data.get("category") or "*"

            # ★ 同时保存 id 和 relpath，后续评分更稳
            self.last_sent[self._session_key(event)] = {"id": iid, "relpath": relpath}

            yield event.image_result(img_url)
            hint = (
                f"ID: {iid}\n分类: {category}\n文件: {fname}\n"
                f"评分指令：#评分 <分值> [备注]\n[请打分，分值0~5，优先抽取打分次数少的图]\n示例：#评分 4 颜色舒服"
            )
            yield event.plain_result(hint)
        except Exception as e:
            from astrbot.api import logger
            logger.error(f"[pic_rater] /来一张 失败: {e}")
            yield event.plain_result("发图失败：请检查 picapi 是否在线、分类是否存在（或查看控制台日志）。")

    # === 放在 PicRater 类里，和其它方法同级 ===

    async def _reindex(self, purge: bool) -> dict:
        """
        调用 picapi 的 /reindex。
        兼容两种写法：
          1) 请求体为 boolean（json=true/false）
          2) 请求体为 {"purge_missing": true/false}
        先试 boolean，422/400 再回退到对象。
        """
        url = f"{self.base_url}/reindex"
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                r = await client.post(url, json=purge)  # 发送裸布尔
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError:
                # 回退到 embed 对象
                r2 = await client.post(url, json={"purge_missing": purge})
                r2.raise_for_status()
                return r2.json()

    def _parse_purge_flag(self, text: str) -> bool:
        t = (text or "").strip().lower()
        return t in {"清理", "purge", "cleanup", "clean", "true", "1", "是", "yes"}

    @filter.command("重建索引")
    async def cmd_reindex(self, event, text: str = ""):
        purge = self._parse_purge_flag(text)
        try:
            resp = await self._reindex(purge)
            indexed = resp.get("indexed")
            purged = resp.get("purged")
            msg = f"索引已更新：扫描入库 {indexed} 条"
            if purged is not None:
                msg += f"；清理 {purged} 条"
            msg += "。"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"索引更新失败：{e}")

    # —— 别名：更新图库 / 刷新图库 / reindex ——
    @filter.command("更新图库")
    async def cmd_update_gallery(self, event, text: str = ""):
        purge = self._parse_purge_flag(text)
        try:
            resp = await self._reindex(purge)
            indexed = resp.get("indexed");
            purged = resp.get("purged")
            msg = f"索引已更新：扫描入库 {indexed} 条"
            if purged is not None: msg += f"；清理 {purged} 条"
            msg += "。"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"索引更新失败：{e}")

    @filter.command("刷新图库")
    async def cmd_refresh_gallery(self, event, text: str = ""):
        purge = self._parse_purge_flag(text)
        try:
            resp = await self._reindex(purge)
            indexed = resp.get("indexed");
            purged = resp.get("purged")
            msg = f"索引已更新：扫描入库 {indexed} 条"
            if purged is not None: msg += f"；清理 {purged} 条"
            msg += "。"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"索引更新失败：{e}")

    @filter.command("reindex")
    async def cmd_reindex_alias(self, event, text: str = ""):
        purge = self._parse_purge_flag(text)
        try:
            resp = await self._reindex(purge)
            indexed = resp.get("indexed");
            purged = resp.get("purged")
            msg = f"索引已更新：扫描入库 {indexed} 条"
            if purged is not None: msg += f"；清理 {purged} 条"
            msg += "。"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"索引更新失败：{e}")

    # 用法：#/评分 4.5   或   #/评分 4 不错
    @filter.command("评分")
    async def cmd_rate(self, event, text: str = ""):
        txt = (text or "").strip()
        if not txt:
            yield event.plain_result("用法：#/评分 <分值> [备注]  例如：#/评分 4.5 配色舒服")
            return
        parts = txt.split(maxsplit=1)
        try:
            score = float(parts[0])
        except ValueError:
            # 非数字直接忽略（不回复）
            return
        if not (0.0 <= score <= 5.0):
            # 超范围直接忽略（不回复）
            return
        note = parts[1] if len(parts) >= 2 else None

        sess = self._session_key(event)
        last = self.last_sent.get(sess)
        if not last:
            yield event.plain_result("本会话还没有待评分的图片，请先发送：#/来一张")
            return

        # ★ 优先尝试 relpath，失败再退回 id
        tried = []
        try_order = []
        if last.get("relpath"): try_order.append(last["relpath"])
        if last.get("id"):      try_order.append(last["id"])

        import httpx
        for ident in try_order:
            try:
                payload = {"id": ident, "score": score}
                if note: payload["note"] = note
                resp = await self._post("/rate", payload)
                avg = resp.get("avg");
                cnt = resp.get("count")
                yield event.plain_result(f"已记录：{score} 分。当前均分：{avg}（共 {cnt} 次）")
                return
            except httpx.HTTPStatusError as e:
                tried.append((ident, e.response.status_code))
                # 404 就换下一个 ident，其他错误直接报
                if e.response.status_code != 404:
                    yield event.plain_result(f"评分失败：{e}")
                    return
            except Exception as e:
                yield event.plain_result(f"评分失败：{e}")
                return

        # 都失败了
        yield event.plain_result(f"评分失败：服务器找不到对应图片（尝试键：{tried}）。请先重新来一张。")

    # 用法：#/图类目
    @filter.command("图类目")
    async def cmd_categories(self, event: AstrMessageEvent, text: str = ""):
        try:
            data = await self._get("/categories")
            cats = data.get("categories", [])
            if not cats:
                yield event.plain_result("没有检测到分类（顶级子文件夹）。")
                return
            joined = "、".join(cats[:100])
            yield event.plain_result(f"顶级分类（前{min(100,len(cats))}个）：\n{joined}\n例如：#来一张 {cats[0]}")
        except Exception as e:
            logger.error(f"[pic_rater] /图类目 失败: {e}")
            yield event.plain_result("获取分类失败：请检查 picapi 是否在线。")
