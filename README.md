# astrbot_plugin_pic_rater

`astrbot_plugin_pic_rater` 是一个 **AstrBot 插件项目**：  
- 前端交互层是 AstrBot 指令插件（本仓库根目录）。  
- 图片服务层是 `picapi`（建议 Docker 部署）。  

整体目标：随机发图、标签检索、评分统计、XMP 元数据回写。

## 快速启动

1. 使用 Docker 启动 `picapi`（参考 `picapi/docker-compose.example` 和 `picapi/.env.example`）。  
2. 将本项目作为插件放入 AstrBot：`data/plugins/astrbot_plugin_pic_rater`。  
3. 配置插件环境变量 `PICAPI_URL`（默认 `http://picapi:8000`）。  
4. 在 AstrBot 面板启用/重载插件。  

---

## 功能说明

### 1) 随机发图
- 指令：`#来一张 [关键词|分类表达式]`
- 支持两种筛选：
  - 关键词检索（`q`）：例如 `#来一张 1girl`
  - 分类表达式（`cat`）：例如 `#来一张 风景:3,人像:1` 或 `#来一张 壁纸/风景`
- 择图策略支持少评分优先/加权随机，降低重复命中。

### 2) 图片评分
- 指令：`#评分 <0~5> [备注]`
- 针对“当前会话上次发送的图片”打分。
- 插件会优先用 `relpath`，失败时回退 `id` 调用后端评分接口。
- 达到阈值后会将评分结果回写到 XMP（依赖 `exiftool`）。

### 3) 类目浏览
- 指令：`#图类目 [path]`
- 不带参数列出顶级目录；带参数可逐级下钻子目录并查看数量。

### 4) 图库整理
- 指令：`#整理图库 [清理]`
- 串联执行：
  - 扫盘入库（`/reindex`）
  - 同步 XMP 标签（`/sync_subjects`）
  - 可配合 FTS 重建流程用于检索加速

---

## 接口说明（picapi）

### 核心业务接口
- `GET /random_pic`：随机取图（支持 `q`/`cat`/`bias`/`alpha`）
- `POST /rate`：写入评分与备注，更新均分/次数，按阈值回写 XMP
- `GET /categories`：获取顶级分类
- `GET /dirs`：获取指定目录下的子目录统计
- `POST /reindex`：重建/补齐图库索引，可选清理失效记录
- `POST /sync_subjects`：同步 XMP:Subject 到标签表
- `GET /search`：检索（FTS 优先，失败时降级 LIKE）

### 运维接口
- `GET /health`：健康检查（图库、配置、基础状态）
- `GET /admin/sync_progress`：同步任务进度
- `POST /admin/rebuild_fts`：重建 FTS
- `POST /admin/refresh_fts_tags`：刷新 FTS 标签映射

### 错误返回规范
- 统一结构：`{"code": "...", "message": "...", "detail": ...}`

---

## 架构说明

```mermaid
flowchart LR
  User[User/AstrBot Chat] --> Plugin[AstrBot Plugin Commands]
  Plugin --> Client[PicApiClient]
  Client --> Api[picapi FastAPI Routes]
  Api --> Service[Application Services]
  Service --> Repo[SQLite Repository]
  Service --> Meta[ExifTool Metadata Worker]
  Repo --> DB[(SQLite)]
```

- **部署形态**
  - `picapi`：Docker 容器部署（推荐通过 compose）
  - 插件：作为 AstrBot 插件加载运行
- **调用链路**
  - 聊天指令 -> 插件命令层 -> HTTP 调用 `picapi` -> 服务层执行业务 -> DB/ExifTool

## 故障定位

- 搜索无结果：先执行 `#整理图库` 触发重建索引与标签同步。
- 评分 404：重新 `#来一张`，插件会保存新 `id/relpath`。
- XMP 未回写：检查 `WRITE_META_MIN_COUNT` 与 `exiftool` 可执行性。

---

## 模块划分

### 插件模块（AstrBot）
- `main.py`：插件入口（薄封装）
- `plugin/commands.py`：指令处理（来一张/评分/类目/整理）
- `plugin/picapi_client.py`：HTTP 客户端与请求封装
- `plugin/parsers.py`：参数解析（关键词/分类、评分解析等）
- `plugin/session_store.py`：会话态缓存（last sent image）

### 后端模块（picapi）
- `picapi/app.py`：FastAPI 应用装配、异常处理、启动迁移
- `picapi/api/routes.py`：路由层（HTTP interface）
- `picapi/services/gallery_service.py`：图库、检索、索引、FTS 逻辑
- `picapi/services/rating_service.py`：评分聚合与回写流程
- `picapi/infra/db.py`：数据库连接与事务
- `picapi/infra/metadata.py`：ExifTool 读写
- `picapi/infra/migrations.py`：schema 迁移入口
- `picapi/models.py`：请求/响应 DTO（Pydantic）

### 文档与测试
- `docs/baseline_audit.md`：重构基线与回归清单
- `tests/`：解析与检索核心单元测试

---

## 常用命令速查

- `#来一张 [关键词|分类表达式]`
- `#评分 <0~5> [备注]`
- `#图类目 [path]`
- `#整理图库 [清理]`
