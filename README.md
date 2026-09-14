---
title: 投资投研与组合量化决策系统
emoji: 📈
colorFrom: blue
colorTo: green
sdk: streamlit
sdk_version: "1.63.0"
app_file: app.py
pinned: false
---

# 📈 投资投研与组合量化决策系统

严格按《投资投研与组合量化决策系统_技术与逻辑规格_V1.0》分 Phase 实现的 Streamlit + SQLite 量化投研 Dashboard。

**技术栈**：Python / SQLite(SQLAlchemy 2.0) / Streamlit + Plotly / 自动数据源（Tushare 主源 + AKShare 兜底，自动 Failover）

**包含页面**：
- **主页**：搜索 / 研究标的库（最新快照：Research 分数、置信度、价格状态、估值、标准买价）
- **个股研究详情**：5 档价格状态估值、三情景收益分布（乐观/中性/悲观）、买价（标准/保守）
- **组合风险**：Beta / 回撤 / 压力测试 / Risk Contribution / 组合决策中心（信号、机会分、再平衡带）
- **➕ 添加标的**：录入 A 股代码 → 自动同步行情/财报 → 立即生成研究评分与估值

**数据说明**：仓库内置最近一次同步的 SQLite 快照（`data/investment.db`），打开即可浏览；如需更新数据，可重新触发同步（依赖 AKShare 公网数据源，免 Token）。

---

## 🔐 权限模型（了解链接即可用，写操作可选密码保护）

| 场景 | 查看/浏览 | 写操作（重新评分、刷新风险快照/决策、**添加新标的**） |
|---|---|---|
| 本地 / 局域网运行 | ✅ 免登录 | ✅ 免密码（单机自用，默认放行） |
| HF Space **未设置** `ADMIN_PASSWORD` | ✅ 免登录 | ✅ **知道链接的任何人可操作**（默认开放，适合自己私用） |
| HF Space **已设置** `ADMIN_PASSWORD`（推荐） | ✅ 免登录 | 🔒 需在页面输入管理密码（会话内解锁后放行） |

实现：`src/ui/auth.py` 统一门禁。未配置密码时 `admin_required()` 直接放行（本地行为不变）；配置后仅拦截写操作，查看不受影响。

> ⚠️ **公共 Space 数据是共享的**：SQLite 是单库，任何访客在"开放模式"下添加的标的、刷新的快照，所有访问者都能看到。要保障"只有你能改、别人只能看"，务必部署后设置 `ADMIN_PASSWORD`（见下）。

---

## 🚀 部署到 Hugging Face（永久在线链接，任何联网电脑可打开）

### 第 1 步：创建 Space
1. 打开 https://huggingface.co/new-space （需注册免费账号，仅此一次，用于获得永久链接）
2. 填写：Space name（例如 `investment-dashboard`）、License 随意、**SDK 选 Streamlit**
3. 点 **Create Space**

### 第 2 步：上传本目录全部文件
方式 A（网页拖拽，最快）：
- 本机把 `hf_space/` 目录内所有文件**打包成 zip**（README.md、app.py、pages/、src/、config/、data/、requirements.txt、.streamlit/ 等），
- 在 Space 页 **Files → Add file → Upload files**，拖入 zip 内的全部文件（保持目录结构），提交 commit。

方式 B（`huggingface_hub` CLI，适合批量）：
```bash
python -m pip install -U huggingface_hub
huggingface-cli login          # 粘贴你的 HF Token（Settings → Access Tokens → Write 权限）
huggingface upload <yourname>/investment-dashboard ./hf_space --repo-type=space
```

### 第 3 步：等待自动构建
HF 检测到 `sdk: streamlit` 会自动装 `requirements.txt` 并启动应用（约 2-5 分钟）。页面 **Settings → Repo info** 里出现 build 日志，绿勾即成功。

### 第 4 步：设置管理密码（推荐，控制"谁能添加标的/写操作"）
1. Space 页 → **Settings → Variables and secrets → New secret**
2. Name 填 **`ADMIN_PASSWORD`**，Value 填你自定义的管理密码（如 `MyInv2026!`）
3. Save，Space 自动重启后生效
   - 之后：**任何访客可直接查看**全部页面；
   - 「⚡ 立即生成/更新研究报告」「🔄 生成/刷新风险快照」「🔄 生成/刷新决策」「➕ 添加标的」四个写入口都需要输入该密码。
4. 也可在创建 Space 时用 `hf secret set`：
```bash
huggingface-cli secret set ADMIN_PASSWORD <your-password> --space <yourname>/investment-dashboard
```
> 不设置该 Secret = 开放模式：知道链接的任何人都能添加标的、刷新快照（数据共享，见上表）。

### 第 5 步：获得永久链接
- 永久链接格式：`https://<你的用户名>-<space名>.hf.space`
- 例如：`https://alice-investment-dashboard.hf.space`
- 任何人、任何设备、任何联网浏览器直接打开即可使用，**不依赖你的电脑在线**（与 trycloudflare 临时隧道不同）。

### 第 6 步：添加新标的（两种权限模式下的操作）
1. 打开左侧 **➕ 添加标的** 页
2. 输入 A 股代码（如 `600036` / `601318.SH` / `601899`），选分析基准日
3. 开放模式：直接点「🚀 同步并分析」；密码模式：先在页面输入管理密码解锁
4. 等待同步（首只约 1-3 分钟，联网拉取 AKShare 行情/财报）→ 自动生成研究评分、价格状态、公允价值、标准买入价
5. 之后在 **主页 / 个股研究详情** 即可检索该标的

### 升级数据（可选）
网页添加新标的即触发完整同步；若想刷新全部既有标的，可用同一台能联网的电脑在本地命令行跑：
```bash
python src/cli.py --sync 600036.SH,601318.SH,601138.SH,601899.SH --research
```
然后把 `data/investment.db` 重新上传覆盖（后续会用 HF 定时任务方案做自动化，本版本暂不内置）。

---

> 仅供研究参考，不构成投资建议。