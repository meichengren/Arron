# Neon PostgreSQL 持久化迁移设计

## 目标

将投研看板的数据存储从 Render 免费 Web Service 的本地 SQLite 文件迁移到 Neon PostgreSQL，确保网页添加标的、同步行情和生成研究快照后，数据不随 Render 服务休眠、重启或重新部署而丢失。

## 范围

- 保持现有 Streamlit 页面、业务逻辑和数据模型不变。
- 使用环境变量 `INVESTMENT_DB_URL` 提供 Neon 连接地址；GitHub 不保存数据库密码。
- 支持现有 SQLite 数据库中已提交的基础数据导入 Neon。
- 将运行时迁移逻辑和数据写入层从 SQLite 专用实现改为 SQLite 与 PostgreSQL 均可运行。
- 更新 Render 部署说明，移除“免费实例 SQLite 会持久保存”的错误描述。

不包含：用户登录、数据备份服务、数据库读写分离或数据源业务逻辑改造。

## 架构

```
Streamlit on Render
  └── INVESTMENT_DB_URL
        └── Neon PostgreSQL
              ├── SQLAlchemy models / create_all
              ├── portable schema column migration
              └── persisted securities, market data, financial reports, research snapshots
```

本地开发默认继续使用 `sqlite:///data/investment.db`。当 `INVESTMENT_DB_URL` 是 `postgresql://` 或 `postgres://` 时，应用会规范化为 SQLAlchemy 的 `postgresql+psycopg://` 驱动地址。

## 数据迁移

新增一次性 CLI：

```
python -m src.cli --migrate-sqlite-to-postgres
```

该命令从本地 SQLite 读取现有表，创建目标 PostgreSQL 表结构，并按依赖顺序写入数据。写入前检查目标库是否为空；若目标库已有数据则拒绝执行，避免重复导入。写入后逐表比对源与目标行数，任何不匹配均以失败退出。

迁移在 Neon 建库、Render 环境变量配置完成后，从 Render Shell 或可信本地环境执行。连接串只作为环境变量使用，不会写入日志、源码或提交记录。

## 数据写入兼容

当前仓库层直接使用 SQLite 专属的 \`sqlite_insert\`，PostgreSQL 连接会在新增标的或同步数据时失败。抽取按引擎方言选择冲突更新语句的辅助函数：SQLite 使用 \`sqlite_insert\`，PostgreSQL 使用 \`postgresql.insert\`。现有唯一键与更新字段保持不变。

## 兼容迁移

当前列补丁使用 `PRAGMA table_info`，仅适用于 SQLite。改为 SQLAlchemy Inspector 获取已有列；保留现有的 `ALTER TABLE ... ADD COLUMN` 语句，使其对 SQLite 与 PostgreSQL 均可使用。每项迁移仍保持幂等。

## Render 配置

Render Web Service 的 `INVESTMENT_DB_URL` 改为 Neon 连接串。由于 Neon 不属于 Render Blueprint 资源，`render.yaml` 不保存连接串或 Neon 凭据；部署文档提供在 Render Dashboard 中设置环境变量的步骤。

## 回滚

在确认 Neon 数据导入成功、Render 页面可读写前，不删除仓库中的 SQLite 文件。若连接失败，可删除或清空 Render 中的 `INVESTMENT_DB_URL`，应用会恢复使用 SQLite 默认地址。回滚会保留 Neon 数据，不会自动删除任何数据库。

## 验证

- 单元测试：PostgreSQL URL 规范化、SQLite 与 PostgreSQL 列检查、SQLite 与 PostgreSQL 的冲突更新语句选择、重复导入保护。
- 集成测试：使用临时 SQLite 源库验证表创建、导入与行数比对。
- 部署验证：Render 中打开已有标的、添加一个新标的、重启服务后确认标的与研究快照仍存在。
- 安全检查：仓库、PR diff 和部署日志中不含 Neon 密码或完整连接串。
