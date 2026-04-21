# Baseline Audit

## Core User Flows
- `#来一张 [关键词|分类表达式]` -> `GET /random_pic`
- `#评分 <0~5> [备注]` -> `POST /rate`
- `#图类目 [path]` -> `GET /categories` / `GET /dirs`
- `#整理图库 [清理]` -> `POST /reindex` + `POST /sync_subjects`

## Regression Checklist
- Random picture returns valid URL and metadata.
- Rating writes count/avg and supports id/relpath.
- Category listing supports nested path.
- Reindex and subject sync complete without 5xx.
- Search returns candidates for FTS and LIKE fallback.

## Risks Before Refactor
- Single-file plugin and backend reduce maintainability.
- Runtime schema changes mixed into request path.
- Inconsistent error payload shape.
- Search path dependent on large LIKE scans.
