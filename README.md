[astrbot_plugin_pic_rater 265cf65bbd5b80338707e6aff2b2c655.md](https://github.com/user-attachments/files/22179588/astrbot_plugin_pic_rater.265cf65bbd5b80338707e6aff2b2c655.md)
# astrbot_plugin_pic_rater

---

# astrbot_plugin_pic_rater

随机抽图 + 打分写入元数据 的 **AstrBot** 插件。

配合后端 **picapi** 使用，可按 **分类/权重** 抽图，支持 **“评分次数最少优先”** 择图策略，并把均分 **覆写写入 XMP 元数据（整数分）**。

> 适配：AstrBot v3（基于 @filter.command 路由）
> 
> 
> 默认后端：`http://picapi:8000`
> 

---

## 🧠 这是什么？

- 在群聊/私聊里用命令让机器人**发图**、**给图打分**。
- 分数会累计到数据库，达到阈值后把**整数分**写进图片的 XMP 元数据（并**覆写**旧值，避免重复）。
- 后端会优先抽“**评分次数更少**”的图片，帮助你快速给图库做“冷启动标注”。

---

## 🚀 1 分钟上手

1）**起后端 picapi**（Docker）

将你的图片目录映射到 `/data/gallery`，数据库映射到 `/data/db`：

```yaml
# 片段：docker-compose（或放进你的 astrbot.yml）
services:
  picapi:
    build:
      context: ./picapi
    container_name: picapi
    environment:
      - TZ=Asia/Shanghai
      - PICK_BIAS=min                 # 择图：off|min|weighted
      - PICK_BIAS_ALPHA=1.0           # weighted 指数
      - WEIGHTED_POOL=500
      - OVERWRITE_SUBJECT_SCORE=true  # XMP 评分覆写
      - WRITE_META_MIN_COUNT=1        # 评分次数≥阈值才写 XMP
      - ALLOWED_SUFFIXES=.jpg,.jpeg,.png,.gif,.webp
      - STATIC_PREFIX=/static
      - RECURSIVE=true
    volumes:
      # Windows/WSL2 用 /mnt/c/...；Linux/Mac 用绝对路径
      - /mnt/c/Users/YourName/Pictures/gallery:/data/gallery
      - picdb:/data/db
    ports:
      - "8000:8000"
    restart: always
    networks: [astrbot_network]

volumes:
  picdb:
networks:
  astrbot_network: { driver: bridge }

```

> **WSL/Windows 提醒：**不要用 C:\...，请用 /mnt/c/...。
> 

2）**安装插件**

把本项目放到 AstrBot 插件目录（容器内通常是 `/AstrBot/data/plugins/`）：

```
/AstrBot/data/plugins/astrbot_plugin_pic_rater/
├─ main.py
├─ metadata.yaml
└─ requirements.txt

```

3）**在 AstrBot 面板启用并重载**（或重启容器）

可选环境变量（设在 `astrbot` 容器里）：

- `PICAPI_URL`（默认 `http://picapi:8000`）
- `PICAPI_TIMEOUT`（默认 `15` 秒）

4）**重建索引一次**（把磁盘图片登记到数据库）

```bash
# 仅补齐，不清理缺失：
curl -X POST http://localhost:8000/reindex -H 'Content-Type: application/json' -d 'false'
# 补齐并清理数据库里已不存在的记录：
curl -X POST http://localhost:8000/reindex -H 'Content-Type: application/json' -d 'true'

```

5）**在聊天里使用**

```
#来一张
#评分 4.5 配色舒服
#图类目
#重建索引
#重建索引 清理

```

---

## ✨ 功能

- `#来一张 [分类表达式]`
    - 从图库抽图（支持顶级/多级目录与权重，如 `风景:3,人像:1`）。
    - 默认“最少评分优先”，减少重复、优先探索未评分图片。
- `#评分 <0~5任意小数> [备注]`
    - 给“本会话上一张发出的图片”评分。
    - 数据库累加**浮点均分**；写 XMP 时**四舍五入为整数分**并**覆写**旧值。
- `#图类目`
    - 列出顶级分类（顶层文件夹名）。
- `#重建索引 [清理]`（别名：`#更新图库` / `#刷新图库` / `#reindex`）
    - 扫描磁盘登记到数据库；追加参数“清理”会移除数据库里已不存在的记录。

---

## 🧩 工作原理

```
AstrBot(插件)  ──HTTP──▶  picapi(后端)
   │  #来一张              ├─ GET /random_pic     （择图 + 返回直链）
   │  #评分                ├─ POST /rate          （累计评分 → 达阈值写回XMP）
   │  #图类目              ├─ GET /categories     （顶级分类）
   └  #重建索引            └─ POST /reindex       （扫盘入库 / 可选清理）

```

- 插件：调用 API、发图片/文本；保存上一张图片的 `id` 与 `relpath`，评分时**优先用 relpath**，失败再退回 id。
- 后端：少评优先/加权、写 XMP（`exiftool -overwrite_original`）、覆写评分标签。

---

## 🧭 分类表达式写法

- **单一分类**：`#来一张 风景`（顶层文件夹名）
- **多级目录**：`#来一张 壁纸/风景`（从图库根开始的相对路径）
- **多分类加权**：`#来一张 风景:3,人像:1,建筑:1`（未写权重默认 1）
- 不确定？先 `#图类目` 看顶级，再逐级补全测试。

---

## ⚙️ 后端常用配置（picapi）

```
# 择图策略
PICK_BIAS=min            # off|min|weighted
PICK_BIAS_ALPHA=1.0      # weighted 指数（大=更偏未评分）
WEIGHTED_POOL=500        # weighted：先取最少的一批 N，再随机

# 写 XMP（覆写 & 整数分）
OVERWRITE_SUBJECT_SCORE=true   # 清旧(rated/score:*/count:*)再写新
WRITE_META_MIN_COUNT=1         # 评分次数 ≥ 阈值才写回

# 静态与扫描
ALLOWED_SUFFIXES=.jpg,.jpeg,.png,.gif,.webp
STATIC_PREFIX=/static
RECURSIVE=true

```

**写回规则**

- 数据库存 **浮点**均分；
- `XMP:Rating = 四舍五入后的 0~5 整数分`；
- `XMP:Subject`：清理旧的 `rated / score:* / count:*`，保留其它标签，写入最新三项。

---

## 🧪 自测（命令行）

```bash
# 接口列表（没 jq 就用 Python）
curl -s http://localhost:8000/openapi.json \
| python3 -c "import sys,json; d=json.load(sys.stdin); print('\n'.join(sorted(d.get('paths',{}).keys())))"
# 预期有：/random_pic /rate /reindex /categories /stats /health

# 取一张
RESP=$(curl -s 'http://localhost:8000/random_pic'); echo "$RESP" | python3 -m json.tool

# 评分（id 不通就用 relpath；后端已双通道兼容）
ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
REL=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['relpath'])")
curl -i -X POST http://localhost:8000/rate -H 'Content-Type: application/json' -d "{\"id\":\"$ID\",\"score\":4.5}"
curl -i -X POST http://localhost:8000/rate -H 'Content-Type: application/json' -d "{\"id\":\"$REL\",\"score\":4.5}"

```

---

## 🧯 故障排查

- **发 `#来一张` 却走普通聊天**
    - 插件是否启用；命令是否用 `#来一张`（不带 `/`）；日志是否显示插件已加载。
- **发不出图/404**
    - `picapi` 在不在；`/random_pic` 可不可达；`PICAPI_URL` 是否正确（容器内推荐 `http://picapi:8000`）。
- **评分 404**
    - 插件已优先用 `relpath`；后端 `/rate` 同时支持 id/relpath。若脚本自测，请确保用的是**同一次 `/random_pic` 返回**的数据。
- **XMP 出现多个 score/count**
    - 确认 `OVERWRITE_SUBJECT_SCORE=true`；我们的写法是“清旧→写新”，不会越积越多。
- **重建索引慢**
    - 大图库属正常；仅在“批量变更后”执行即可；也可直接 请求后端 `/reindex`。
- **改了 app.py 没生效**
    - 需要 **rebuild**：`docker compose -f ./astrbot.yml up -d --build picapi`。
- **Windows/WSL2 映射报 invalid volume**
    - 使用 `/mnt/c/...` 路径；尽量避免中文空格或注意转义。

---

## 🔐 权限（可选）

“重建索引”会扫盘，建议仅管理员可用：在插件的重建索引命令前加白名单判断：

```python
ALLOWED_UIDS = {"你的QQ号", "另一个管理员QQ号"}
uid = str(getattr(event, "user_id", "") or getattr(event, "sender_id", ""))
if uid not in ALLOWED_UIDS:
    yield event.plain_result("需要管理员权限。"); return

```

---

## 🛠 开发

- 依赖：`httpx>=0.27.0`
- 指令：`@filter.command("来一张")` / `@filter.command("评分")` / `@filter.command("图类目")` / `@filter.command("重建索引")`
- 发消息：`yield event.image_result(url)`、`yield event.plain_result(text)`
- 会话键：`event.unified_msg_origin`（用于将“上一张图片”与“评分”关联）

---

## 🤝 贡献

这个项目是「AI + 社区」的产物：

如果你发现 BUG、想要**聊天里切换策略**（如“随机/加权1.5”）、**排行榜/未评分清单**、**更多平台适配**，欢迎提 Issue/PR。

---

## 📄 许可证

MIT

---

### 小记

> 本项目最初由“AI 协助 + 实测修修补补”完成。
> 
> 
> 如果你正好熟悉 AstrBot 或 FastAPI，非常欢迎一起把它打磨得更好。
>
