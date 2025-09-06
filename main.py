from typing import Dict, Any, Optional
import os
import httpx

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import re

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

    @filter.command("整理图库")
    async def cmd_maintain_gallery(self, event, text: str = ""):
        """
        一键维护：
          1) /reindex（可带清理）
          2) /sync_subjects 分批同步 XMP Subject
          3) （可选）重建 FTS
        用法：
          #整理图库
          #整理图库 清理
          #整理图库 清理 1000
          #整理图库 1000 fts
        """
        purge, batch, want_fts = self._parse_cleanup_batch_fts(text)
        try:
            # 1) 扫盘入库（可清理）
            r1 = await self._reindex(purge)
            indexed = r1.get("indexed")
            purged  = r1.get("purged")

            # 2) 分批同步 XMP
            total = await self._sync_subjects_all(batch=batch)

            # 3) 可选 FTS 重建
            fts_msg = ""
            if want_fts:
                fts_msg = await self._rebuild_fts_safe()

            # 汇总消息
            lines = [
                f"已扫描入库：{indexed} 张",
                f"已清理：{purged or 0} 条" if purged is not None else "未执行清理",
                f"已同步 XMP 标签：{total} 张（批大小 {batch}）",
            ]
            if fts_msg:
                lines.append(fts_msg)

            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"整理失败：{e}")


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
