
# astrbot\_plugin\_pic\_rater

**AstrBot 插件** —— 随机发图 + XMP 标签搜索 + 打分写入元数据。
配合后端 **picapi** 使用，可按 **分类/权重** 或 **模糊搜索 (q=)** 抽图，支持“评分次数最少优先”，并把均分 **覆写写入 XMP 元数据（整数分）**。

> 适配：AstrBot v3（基于 @filter.command 路由）
> 默认后端：`http://picapi:8000`

---

## 🧠 功能概览

* `#来一张 [关键词|分类表达式]`

  * 支持两种模式：

    * **分类/权重**：`风景:3,人像:1` 或 `壁纸/风景`
    * **搜索 (q=)**：模糊匹配文件名 + XMP\:Subject 标签（如 `1girl`, `水着`）
  * 默认策略：少评分优先，避免重复。

* `#评分 <0~5任意小数> [备注]`

  * 给“本会话上一张发出的图片”打分。
  * 分数浮点累计，写 XMP 时四舍五入为整数，并覆写旧值。

* `#图类目`

  * 列出顶级分类（图库顶层文件夹）。

* `#整理图库 [清理]`

  * 一条命令完成：

    1. 扫盘入库（新文件补齐；带参数“清理”会移除已删除文件）
    2. 全量同步 XMP\:Subject → 数据库 tags
    3. 重建 FTS 索引（用于模糊搜索）

---

## 🚀 快速上手

1）**起后端 picapi**（Docker）

```yaml
services:
  picapi:
    build: ./picapi
    container_name: picapi
    environment:
      - TZ=Asia/Shanghai
      - PICK_BIAS=min
      - OVERWRITE_SUBJECT_SCORE=true
      - WRITE_META_MIN_COUNT=1
      - ALLOWED_SUFFIXES=.jpg,.jpeg,.png,.gif,.webp
      - STATIC_PREFIX=/static
      - RECURSIVE=true
    volumes:
      - /mnt/c/Users/YourName/Pictures/gallery:/data/gallery
      - picdb:/data/db
    ports: ["8000:8000"]
    restart: always
    networks: [astrbot_network]
```

2）**放置插件**

```
/AstrBot/data/plugins/astrbot_plugin_pic_rater/
├─ main.py
├─ metadata.yaml
└─ requirements.txt
```

3）**在 AstrBot 面板启用并重载**

环境变量（可选）：

* `PICAPI_URL`（默认 `http://picapi:8000`）
* `PICAPI_TIMEOUT`（默认 15 秒）

---

## 🧩 工作原理

```
AstrBot 插件    ──HTTP──▶  picapi 后端
   #来一张          ├─ GET /random_pic?q=关键词 或 cat=分类
   #评分            ├─ POST /rate
   #图类目          ├─ GET /categories
   #整理图库        └─ POST /reindex → /sync_subjects → /admin/rebuild_fts
```

* 插件保存 `id` 和 `relpath`，评分时优先 relpath，失败退回 id。
* 后端写 XMP：清理旧的 score/count 标签 → 写新值。

---

## ⚙️ 常用环境变量

* **择图策略**
  `PICK_BIAS=off|min|weighted`
  `PICK_BIAS_ALPHA=1.0`
  `WEIGHTED_POOL=500`

* **写回控制**
  `OVERWRITE_SUBJECT_SCORE=true`
  `WRITE_META_MIN_COUNT=1`

* **扫描与静态**
  `ALLOWED_SUFFIXES=.jpg,.jpeg,.png,.gif,.webp`
  `RECURSIVE=true`

---

## 🧪 自测

```bash
# 健康检查
curl -s http://localhost:8000/health | python3 -m json.tool

# 随机一张
curl -s 'http://localhost:8000/random_pic?q=1girl' | python3 -m json.tool

# 评分
curl -X POST http://localhost:8000/rate -H 'Content-Type: application/json' \
  -d '{"id":"<relpath或id>","score":4.5}'
```

---

## 🧯 常见问题

* **搜索报错/无结果**

  * 确保 `#整理图库` 已执行（同步 XMP 标签 + 重建 FTS）。
  * 中文搜索不要加 `*`，英文/数字自动加后缀通配。

* **评分失败 404**

  * 插件已兼容 relpath/id；若仍报错，请重新 `#来一张`。

* **写回 XMP 没更新**

  * 检查 `OVERWRITE_SUBJECT_SCORE=true`
  * 确认评分次数是否达到 `WRITE_META_MIN_COUNT`。

---
