from typing import Dict, Any, Optional
import os

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import re
import asyncio, time, httpx
import contextlib  # new: _wait_with_progress 里用到了 suppress


_CAT_HINT_RE = re.compile(r"[,:/]")

def _build_random_params(arg_text: str) -> dict:
    """
    把“#来一张”后面的参数转成 /random_pic 的查询参数：
    - 以 '?' 或 'q:' 开头：走 q（模糊搜索/FTS）
    - 含 , : / 任一字符：判为 cat（分类/权重/多级）
    - 其他：默认 q
    """
    t = (arg_text or "").strip()
    if not t:
        return {}  # 纯随机

    low = t.lower()
    if low.startswith("?"):
        return {"q": t[1:].strip()}
    if low.startswith("q:"):
        return {"q": t[2:].strip()}

    if _CAT_HINT_RE.search(t):
        return {"cat": t}

    # 默认当作搜索关键词
    return {"q": t}



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
        self.http_timeout = httpx.Timeout(connect=10.0, read=1200.0, write=1200.0, pool=10.0)
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
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            r = await client.get(url, params={k: v for k, v in params.items() if v is not None and v != ""})
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, payload):
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()

    def _render_bar(self, done: int, total: int, width: int = 24) -> str:
        if total <= 0:
            return f"[{'?' * width}] ?% ({done}/?)"
        pct = max(0.0, min(1.0, (done or 0) / float(total)))
        fill = int(round(pct * width))
        bar = "█" * fill + "░" * (width - fill)
        return f"[{bar}] {int(pct * 100)}% ({done}/{total})"

    async def _get_progress_json(self) -> dict | None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self.base_url}/admin/sync_progress")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return None

    # --------- 指令 ----------
    # 用法：#来一张   或   #来一张 风景:3,人像:1   或   #来一张 壁纸/风景
    @filter.command("来一张")
    async def cmd_send_random(self, event, text: str = ""):
        # ★ 新：把用户参数转成 q 或 cat
        params = _build_random_params(text)

        try:
            # ★ 原来是 cat=cat；现在改成 **params
            data = await self._get("/random_pic", **params)

            img_url = self._abs_url(data["url"])
            iid = data.get("id")
            relpath = data.get("relpath")
            fname = data.get("filename", "")
            category = data.get("category") or "*"

            # ★ 同时保存 id 和 relpath，评分更稳（后端 /rate 兼容二者）
            self.last_sent[self._session_key(event)] = {"id": iid, "relpath": relpath}

            yield event.image_result(img_url)

            # 可选：给用户一个提示
            hint_lines = [
                f"ID: {iid}",
                f"分类: {category}",
                f"文件: {fname}",
                "评分指令：#评分 <分值> [备注]（0~5，可小数；写回XMP会四舍五入为整数）",
            ]
            # 根据模式追加一行提示
            if "q" in params:
                hint_lines.append(f"检索：{params['q']}")
            elif "cat" in params:
                hint_lines.append(f"分类表达式：{params['cat']}")
            yield event.plain_result("\n".join(hint_lines))

        except Exception as e:
            from astrbot.api import logger
            logger.error(f"[pic_rater] /来一张 失败: {e}")
            # 404 场景常见是“q 没命中”或“cat 不存在”
            yield event.plain_result("发图失败：没有匹配到图片，或 picapi 不可用。请更换关键词/分类或查看控制台日志。")

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



    # ====== 加在 PicRater 类里（与其它方法同级）======

    async def _sync_subjects_all(self, batch: int = 800) -> int:
        """
        分批调用 /sync_subjects?limit=...，直到处理完。
        返回总处理条数。
        """
        import httpx
        total = 0
        url = f"{self.base_url}/sync_subjects"
        async with httpx.AsyncClient(timeout=None) as client:
            while True:
                r = await client.post(url, params={"limit": batch})
                r.raise_for_status()
                js = r.json()
                n = int(js.get("processed", 0))
                total += n
                # 批次回报（可选）
                if n > 0 and total % (batch * 5) == 0:
                    # 这里不直接 yield，返回给上层统一回复
                    pass
                if n < batch:  # 小于批量，说明已处理完
                    break
        return total

    async def _rebuild_fts_safe(self) -> str:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                r = await client.post(f"{self.base_url}/admin/rebuild_fts", params={"full": "true"})
                if r.status_code == 200:
                    # 有的后端返回JSON，有的返回空体，这里不强制解析
                    return "FTS 已重建"
                return f"FTS 重建返回 {r.status_code}"
        except Exception as e:
            return f"FTS 重建跳过（{e}）"

    def _parse_cleanup_batch_fts(self, text: str):
        """
        解析“清理/批大小/fts”参数：
          - 清理：中文“清理”、英文 'purge'/'clean'/'cleanup'/'true'/'yes'/'1'
          - 批大小：第一个纯数字词
          - fts：包含 'fts' 字样则尝试重建 FTS
        """
        t = (text or "").strip().lower()
        tokens = t.split()
        purge = any(tok in {"清理", "purge", "cleanup", "clean", "true", "yes", "1"} for tok in tokens)
        batch = next((int(tok) for tok in tokens if tok.isdigit()), 800)
        want_fts = any("fts" in tok for tok in tokens)
        return purge, batch, want_fts

    async def _wait_with_progress(self, work_coro, event, label: str,
                                  first_hint_after: float = 5.0,  # 首条提示在 5s 后
                                  ping_every: float = 15.0,  # 之后每 15s 提示一次
                                  hard_timeout: float | None = None):  # 设 None 表示不硬超时
        """
        等待一个协程执行完。超过 first_hint_after 秒开始提示，每 ping_every 秒提醒一次。
        可选：hard_timeout 到达则取消任务并抛出 TimeoutError。
        返回：work_coro 的结果。
        """
        t0 = time.monotonic()
        task = asyncio.create_task(work_coro)
        hinted = False
        try:
            while True:
                try:
                    # 如果设置了硬超时（不建议），就用 wait_for 限制单次 wait 的最长时间
                    if hard_timeout is not None:
                        remaining = max(0.0, hard_timeout - (time.monotonic() - t0))
                        res = await asyncio.wait_for(asyncio.shield(task), timeout=min(ping_every, remaining))
                    else:
                        res = await asyncio.wait_for(asyncio.shield(task), timeout=ping_every)
                    return res  # 任务结束

                except asyncio.TimeoutError:
                    # 任务还在跑
                    elapsed = int(time.monotonic() - t0)

                    # 试着拉一次后端进度（仅 sync_subjects 会有 total/done）
                    prog = await self._get_progress_json()
                    bar_txt = ""
                    if prog and int(prog.get("total", 0)) > 0:
                        bar_txt = " " + self._render_bar(int(prog.get("done", 0)),
                                                         int(prog.get("total", 0)))



                    # 硬超时可选
                    if hard_timeout is not None and (time.monotonic() - t0) >= hard_timeout:
                        task.cancel()
                        raise TimeoutError(f"{label} 超过 {int(hard_timeout)}s 超时")

        finally:
            if not task.done():
                # 清理
                with contextlib.suppress(Exception):
                    task.cancel()

    @filter.command("整理图库")
    async def cmd_clean_gallery(self, event, text: str = ""):
        purge = self._parse_purge_flag(text)

        # 第一条提示：必须用 yield（不能 await）
        yield event.plain_result("开始整理图库：扫描入库 → 同步 XMP 标签…")

        # ---------- 1) 扫盘入库 ----------
        async def do_reindex():
            import httpx
            url = f"{self.base_url}/reindex"
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                try:
                    r = await client.post(url, json=purge)  # 兼容裸 boolean
                    r.raise_for_status()
                    return r.json()
                except httpx.HTTPStatusError:
                    r2 = await client.post(url, json={"purge_missing": purge})  # 兼容对象
                    r2.raise_for_status()
                    return r2.json()

        import asyncio, time
        # 入库阶段：轮询任务 + 心跳提示
        t0 = time.monotonic()
        task1 = asyncio.create_task(do_reindex())
        first_hint_after = 5.0
        ping_every = 15.0
        next_ping = t0 + first_hint_after
        resp1 = None
        while True:
            try:
                resp1 = await asyncio.wait_for(asyncio.shield(task1), timeout=1.0)
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now >= next_ping:
                    elapsed = int(now - t0)
                    # 入库没有 total/done，就发心跳
                    yield event.plain_result(f"⏳ 扫盘入库进行中（已用时 {elapsed}s）")
                    next_ping = now + ping_every

        if not isinstance(resp1, dict):
            yield event.plain_result(f"❌ 扫盘入库失败：返回内容异常：{resp1!r}")
            return

        # ---------- 2) 同步 XMP ----------
        async def do_sync_all():
            import httpx
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                r = await client.post(f"{self.base_url}/sync_subjects", params={"limit": 0})
                r.raise_for_status()
                return r.json()

        t1 = time.monotonic()
        task2 = asyncio.create_task(do_sync_all())
        next_ping = t1 + first_hint_after
        resp2 = None
        while True:
            try:
                resp2 = await asyncio.wait_for(asyncio.shield(task2), timeout=1.0)
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now >= next_ping:
                    elapsed = int(now - t1)
                    # 这个阶段可以试着拉进度条（如果后端实现了 /progress）
                    prog = await self._get_progress_json()
                    bar_txt = ""
                    if prog and int(prog.get("total", 0)) > 0:
                        bar_txt = " " + self._render_bar(int(prog.get("done", 0)),
                                                         int(prog.get("total", 0)))
                    yield event.plain_result(f"⏳ 同步 XMP 标签进行中（已用时 {elapsed}s）{bar_txt}")
                    next_ping = now + ping_every

        if not isinstance(resp2, dict):
            yield event.plain_result(f"❌ 同步标签失败：返回内容异常：{resp2!r}")
            return

        # ---------- 汇总 ----------
        indexed = resp1.get("indexed")
        purged = resp1.get("purged")
        processed = resp2.get("processed")
        msg = f"✅ 完成：入库 {indexed} 条"
        if purged is not None:
            msg += f"，清理 {purged} 条"
        msg += f"，同步标签 {processed} 条。"
        yield event.plain_result(msg)

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
