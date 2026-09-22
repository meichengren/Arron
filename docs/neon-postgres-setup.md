# Neon PostgreSQL 设置与数据迁移

Render 免费实例的本地磁盘是临时的，因此生产数据应放在外部 PostgreSQL。Neon 的免费 PostgreSQL 可用于这个项目。

## 1. 创建数据库

1. 登录 [Neon](https://console.neon.tech/) 并创建一个 PostgreSQL 项目。
2. 在 **Connection Details** 中选择 SQLAlchemy/Python 连接地址。
3. 不要把连接地址提交到 GitHub，也不要粘贴到聊天或源码。

## 2. 配置 Render

在 Render 服务的 **Environment** 页面添加或更新：

- Key：`INVESTMENT_DB_URL`
- Value：Neon 提供的完整 PostgreSQL 连接地址

保存后执行一次 Manual Deploy。应用启动时会自动建表。

## 3. 迁移现有 SQLite 数据（可选，一次性）

先在 Render Shell 或一台能同时访问原 SQLite 文件和 Neon 的电脑上执行：

```bash
python -m src.cli --migrate-sqlite-to-postgres \
  --source-db-url "sqlite:///data/investment.db"
```

迁移会拒绝写入已有数据的 PostgreSQL 数据库，并在完成后按表核对行数。确认成功后，日常访问和“添加新标的”会直接写入 Neon。

## 4. 验证

在网页中添加一个标的，然后重新部署或等待服务休眠后再打开页面；该标的仍存在即表示持久化已生效。
