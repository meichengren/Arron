# 国内 ETF 与性能优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持国内场内 ETF、免费东方财富 ETF 备用数据源、增量同步和页面缓存，并初始化用户截图中的默认自选池。

**Architecture:** 在现有 `DataProvider` 抽象下增加 ETF 专用能力，由 `ProviderRouter` 依资产类型选择 A 股或 ETF 候选源。同步服务查询本地最新交易日，只抓取缺失行情；默认池由独立模块以幂等方式写入证券库，页面只读数据采用短期缓存并在写入后清除。

**Tech Stack:** Python 3.11、Streamlit、SQLAlchemy、Pandas、AKShare、东方财富公开 ETF 数据端点、PostgreSQL/Neon。

**Spec:** `docs/superpowers/specs/2026-09-29-domestic-etf-performance-design.md`

## Global Constraints

- 仅支持沪深场内 ETF 与现有 A 股；不添加海外、港股、场外基金或后台定时刷新。
- 保持 Tushare 和 AKShare 的现有 A 股行为与自动降级。
- 免费数据源失败时必须继续尝试下一候选源并向 UI 返回来源错误。
- 默认池仅在数据库没有任何标的时导入一次；用户删除后不能自动恢复。
- 首次同步可取完整历史，后续同步只能获取本地最后交易日之后的缺口。
- 显式同步或评分后必须使页面缓存失效。

## Review Focus

- 带错误交易所后缀的 ETF 代码必须被拒绝，不能悄悄改为另一交易所。
- 非 ETF 的 51/15 开头六位代码不能被错误归类而写入错误市场。
- ETF 首选源超时或返回空表时，备用源仍应完成同步或提供包含两个来源的错误。
- 周末、节假日或本地数据已是最新交易日时，同步不能发起空区间网络调用。
- 默认池已删除一个标的后，重启或重新部署不能把它重新写回。

---

### Task 1: 资产识别与默认自选池

**Files:**
- Modify: `src/ingestion/security_sync.py`
- Modify: `src/db/models.py`
- Create: `src/defaults/watchlist.py`
- Modify: `src/db/engine.py`
- Test: `tests/test_symbol_normalization.py`
- Test: `tests/test_default_watchlist.py`

**Interfaces:**
- Consumes: `normalize_a_share_symbol(raw, lookup_by_name=None)`。
- Produces: `normalize_cn_symbol(raw, lookup_by_name=None) -> CNSymbol`，其中 `CNSymbol` 包含 `code, exchange, canonical, asset_type`；`seed_default_watchlist(engine) -> int`。

- [ ] **Step 1: 写入失败测试，覆盖 A 股、510300、159915、`.ETF` 输入与不匹配后缀**

```python
assert normalize_cn_symbol("510300").asset_type == "ETF"
assert normalize_cn_symbol("159915.ETF").canonical == "159915.SZ"
with pytest.raises(ValueError):
    normalize_cn_symbol("510300.SZ")
```

- [ ] **Step 2: 运行符号测试确认失败**

Run: `pytest tests/test_symbol_normalization.py -v`  
Expected: FAIL，因为 ETF 规范化接口尚不存在。

- [ ] **Step 3: 实现 `CNSymbol` 和 `normalize_cn_symbol`，并保留 `normalize_a_share_symbol` 兼容调用**

股票保留原有前缀规则。ETF 仅接受 510/511/512/513/515/518/588（上海）和 159（深圳）前缀；规范形式持久化为 `.SH` 或 `.SZ`，资产类型持久化为 `ETF`。

- [ ] **Step 4: 写入默认池失败测试**

```python
assert seed_default_watchlist(engine) == 27
assert seed_default_watchlist(engine) == 0
delete_security(engine, security_id)
assert seed_default_watchlist(engine) == 0
```

- [ ] **Step 5: 实现 `seed_default_watchlist` 并在数据库初始化后的受控入口调用**

将 27 个截图代码作为常量保存。使用一个持久化的初始化标记，保证首次空库导入后即使用户后来删除全部标的也不会再次导入。

- [ ] **Step 6: 运行 Task 1 测试**

Run: `pytest tests/test_symbol_normalization.py tests/test_default_watchlist.py -v`  
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add src/ingestion/security_sync.py src/db/models.py src/db/engine.py src/defaults/watchlist.py tests/test_symbol_normalization.py tests/test_default_watchlist.py
git commit -m "feat: support domestic ETF symbols and default watchlist"
```

### Task 2: ETF 数据提供器与路由

**Files:**
- Modify: `src/providers/base.py`
- Create: `src/providers/eastmoney_etf_provider.py`
- Modify: `src/providers/akshare_provider.py`
- Modify: `src/providers/provider_router.py`
- Modify: `src/config/settings.py`
- Modify: `config/app.yaml`
- Test: `tests/test_etf_provider_router.py`

**Interfaces:**
- Consumes: `CNSymbol.asset_type` 和现有 `DataProvider`。
- Produces: `ProviderRouter.get_security_basic(symbol, asset_type)`、`get_daily_bars(symbol, start, end, asset_type)`、`get_daily_basic(symbol, start, end, asset_type)`，每项返回 `(value, provider_name)`。

- [ ] **Step 1: 写入路由失败测试**

```python
router = ProviderRouter(settings, providers={"eastmoney_etf": primary, "akshare": fallback})
result, source = router.get_daily_bars("510300.SH", "2026-01-01", "2026-01-31", "ETF")
assert source == "akshare"
```

模拟东方财富异常与 AKShare 成功；另一测试模拟两个源都失败，断言异常同时包含两个来源。

- [ ] **Step 2: 运行路由测试确认失败**

Run: `pytest tests/test_etf_provider_router.py -v`  
Expected: FAIL，因为 ETF 路由与提供器不存在。

- [ ] **Step 3: 扩展 `DataProvider` 的 ETF 能力并实现 `EastmoneyEtfProvider`**

提供器返回现有标准行情列和基础信息字典；ETF 无企业财报时返回空的规范 DataFrame。网络、空响应和字段变化转换为 `ProviderError`。

- [ ] **Step 4: 在 `AkshareProvider` 增加 ETF 备用方法，并把路由候选源按资产类型拆分**

A 股仍使用 `tushare -> akshare`；ETF 使用 `eastmoney_etf -> akshare`。配置中增加 ETF 主源与备用源，不改变已有 A 股配置。

- [ ] **Step 5: 运行 Task 2 测试**

Run: `pytest tests/test_etf_provider_router.py -v`  
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/providers src/config/settings.py config/app.yaml tests/test_etf_provider_router.py
git commit -m "feat: add ETF provider routing with fallback"
```

### Task 3: 增量同步与 ETF 评分

**Files:**
- Modify: `src/ingestion/market_sync.py`
- Modify: `src/ingestion/financial_sync.py`
- Modify: `src/dashboard/services.py`
- Modify: `src/research/datapoints.py`
- Modify: `src/research/scorer.py`
- Test: `tests/test_market_sync_incremental.py`
- Test: `tests/test_etf_scoring.py`

**Interfaces:**
- Consumes: `Security.market` / 资产类型、`MarketRepository.latest_trade_date(security_id)` 和 ETF 提供器路由。
- Produces: `MarketSyncService.sync(...)` 的最小请求区间，以及可持久化 ETF 研究快照。

- [ ] **Step 1: 写入增量同步失败测试**

```python
result = service.sync(security_id, "510300.SH", end_date=date(2026, 9, 29))
provider.get_daily_bars.assert_called_once_with("510300.SH", "2026-09-26", "2026-09-29")
```

另一个测试让本地最新日期等于目标日期，断言提供器未被调用、结果行数为零。

- [ ] **Step 2: 运行同步测试确认失败**

Run: `pytest tests/test_market_sync_incremental.py -v`  
Expected: FAIL，因为同步始终拉取完整历史。

- [ ] **Step 3: 实现按最新本地交易日增量抓取**

无历史记录时使用历史窗口；有记录时从最新日期的下一日开始。若起始日超过目标日则直接返回，不进行网络调用。

- [ ] **Step 4: 写入 ETF 缺少财报时仍可评分的失败测试**

```python
result = scorer.score_security(etf_id, "510300.SH", "沪深300ETF", "GENERIC", date.today())
assert result.research_score is not None
assert result.confidence_score < 1
```

- [ ] **Step 5: 让数据点与评分器按 ETF 规则忽略企业财报维度并降低置信度**

不得伪造财报数据；价格、波动、估值和新鲜度保持参与评分。

- [ ] **Step 6: 运行 Task 3 测试**

Run: `pytest tests/test_market_sync_incremental.py tests/test_etf_scoring.py -v`  
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add src/ingestion src/dashboard/services.py src/research tests/test_market_sync_incremental.py tests/test_etf_scoring.py
git commit -m "feat: add incremental ETF sync and scoring"
```

### Task 4: 页面缓存与 ETF 用户流

**Files:**
- Modify: `pages/3_添加标的.py`
- Modify: `pages/4_当日综合积分榜.py`
- Modify: `src/dashboard/services.py`
- Test: `tests/test_dashboard_cache_invalidation.py`

**Interfaces:**
- Consumes: `add_security_and_research`、`refresh_daily_research_scores`、`seed_default_watchlist`。
- Produces: ETF 输入提示、资产类型列和写操作后的缓存失效。

- [ ] **Step 1: 写入缓存失效失败测试**

```python
clear_dashboard_caches()
assert cached_library_rows.cache_info().currsize == 0
```

- [ ] **Step 2: 运行缓存测试确认失败**

Run: `pytest tests/test_dashboard_cache_invalidation.py -v`  
Expected: FAIL，因为缓存清理接口不存在。

- [ ] **Step 3: 将只读榜单/详情查询封装为短期缓存函数，并实现 `clear_dashboard_caches() -> None`**

缓存只包裹无副作用的读取。添加、同步、刷新评分和删除成功后调用清理函数；页面不缓存写操作或认证状态。

- [ ] **Step 4: 更新添加页和积分榜**

输入提示明确 A 股与 ETF 格式；积分榜显示“类型”列。首次加载时显示默认池已准备好，刷新按钮继续是唯一联网批量评分入口。

- [ ] **Step 5: 运行 Task 4 测试与全量测试**

Run: `pytest tests/test_dashboard_cache_invalidation.py tests/test_symbol_normalization.py tests/test_default_watchlist.py tests/test_etf_provider_router.py tests/test_market_sync_incremental.py tests/test_etf_scoring.py -v`  
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add pages src/dashboard/services.py tests/test_dashboard_cache_invalidation.py
git commit -m "feat: cache dashboard reads and show ETF rankings"
```

### Task 5: 部署前验证与发布

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: 完成的 Task 1-4。
- Produces: 面向用户的 A 股/ETF 支持说明、可配置数据源说明和验证结果。

- [ ] **Step 1: 更新 README 的输入格式、数据源优先级和默认池行为**

说明 Tushare Token 为可选，ETF 免费源按 `eastmoney_etf -> akshare` 降级；说明用户删除的默认标的不自动恢复。

- [ ] **Step 2: 运行完整测试套件**

Run: `pytest -q`  
Expected: PASS。

- [ ] **Step 3: 手工检查 Streamlit 页面**

Run: `streamlit run app.py`  
Expected: 添加页接受 `510300`；积分榜显示类型列；默认池出现且删除后刷新不复原。

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document domestic ETF support"
```
