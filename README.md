# astrbot_plugin_pic_rater

AstrBot 插件 + picapi 后端，用于随机发图、标签检索、评分统计与 XMP 回写。

## 快速启动

1. 启动 `picapi`（参考 `picapi/docker-compose.example` 和 `picapi/.env.example`）。
2. 将插件目录部署到 AstrBot 的 `data/plugins/astrbot_plugin_pic_rater`。
3. 配置 `PICAPI_URL`（默认 `http://picapi:8000`）并重载插件。

## 运维手册

- 常用命令
  - `#来一张 [关键词|分类表达式]`
  - `#评分 <0~5> [备注]`
  - `#图类目 [path]`
  - `#整理图库 [清理]`
- 后端健康检查：`GET /health`
- 后端同步进度：`GET /admin/sync_progress`
- 搜索策略：FTS 优先，异常自动回退 LIKE

## 故障定位

- 搜索无结果：先执行 `#整理图库` 触发重建索引与标签同步。
- 评分 404：重新 `#来一张`，插件会保存新 `id/relpath`。
- XMP 未回写：检查 `WRITE_META_MIN_COUNT` 与 `exiftool` 可执行性。

## 研发说明

- 插件分层：`plugin/commands.py`、`plugin/picapi_client.py`、`plugin/parsers.py`、`plugin/session_store.py`
- 后端分层：`picapi/api`、`picapi/services`、`picapi/infra`
- 数据库迁移入口：`picapi/infra/migrations.py`
- 基线审查与回归清单：`docs/baseline_audit.md`
