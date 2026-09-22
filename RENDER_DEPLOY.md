# 投资投研与组合量化决策系统 - Render 部署包

## 快速部署到 Render

### 方式一：GitHub + Render（推荐）

1. 在 GitHub 创建新仓库（如 `investment-dashboard`）
2. 将本目录全部文件推送到 GitHub
3. 打开 https://dashboard.render.com/
4. 点击 **"New +"** → **"Blueprint"**
5. 连接你的 GitHub 账号，选择刚才创建的仓库
6. Render 自动读取 `render.yaml` 并创建 Web Service
7. 等待构建完成（2-3 分钟），获得永久链接

### 方式二：手动创建 Web Service

1. 打开 https://dashboard.render.com/
2. 点击 **"New +"** → **"Web Service"**
3. 连接 GitHub 仓库或上传代码
4. 设置：
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true`
5. 添加环境变量：
   - `INVESTMENT_DB_URL` = Neon 提供的 PostgreSQL 连接地址（见 `docs/neon-postgres-setup.md`）
   - `ADMIN_PASSWORD` = 你的管理密码（可选，不设则开放编辑）
6. 点击 **"Create Web Service"**
7. 等待构建完成

## 免费计划说明

- **休眠策略**：15 分钟无流量自动休眠，访问时自动唤醒（约 30 秒冷启动）
- **磁盘**：免费实例的本地文件系统是临时的；重启、重新部署或休眠后 SQLite 数据可能丢失
- **带宽**：每月 100GB
- **如果需要不休眠**：可升级 Starter 计划（$7/月）

## 获得永久链接

构建完成后，Render 会分配永久链接：
```
https://investment-dashboard.onrender.com
```

此链接在任何联网设备上均可访问，不需要你的电脑在线。

## 权限说明

- **查看**：任何人知道链接即可访问
- **添加标的 / 重新评分 / 决策**：
  - 如果设置了 `ADMIN_PASSWORD` 环境变量：需要输入密码
  - 如果未设置：开放编辑（任何人可操作）

## 数据持久化

生产环境请将 `INVESTMENT_DB_URL` 配置为 Neon PostgreSQL。Render 免费实例的本地 SQLite 文件不能作为持久化存储；部署、重启或休眠后文件可能丢失。
