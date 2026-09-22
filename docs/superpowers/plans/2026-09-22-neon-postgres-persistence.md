# Neon PostgreSQL Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist investment research data in Neon PostgreSQL so Render Free web-service restarts no longer lose runtime changes.

**Architecture:** Preserve SQLite as the local-development default. Normalize external PostgreSQL URLs to SQLAlchemy's psycopg driver, make schema and upsert code dialect-aware, then copy the committed SQLite data into an initially empty Neon database with row-count verification. Render receives the Neon URL only through its environment settings.

**Tech Stack:** Python 3.12, SQLAlchemy 2, psycopg 3, Streamlit, Neon PostgreSQL, Render.

**Spec:** `docs/superpowers/specs/2026-09-22-neon-postgres-persistence-design.md`

## Global Constraints

- Do not commit a database password, complete Neon connection string, or export containing secrets.
- SQLite remains the default when `INVESTMENT_DB_URL` is unset.
- Target database import must refuse nonempty targets and preserve primary-key IDs.
- Render Free SQLite must not be documented as durable storage.
- The implementation must support both SQLite and PostgreSQL for schema updates and repository upserts.

## Review Focus

- A `postgres://` URL must resolve to a usable `postgresql+psycopg://` URL without changing query or fragment components.
- A new PostgreSQL database must create current tables without issuing SQLite-only `PRAGMA` statements.
- SQLite must still ingest and upsert data after generic upsert support is introduced.
- A pre-populated PostgreSQL target must be rejected before any source rows are inserted.
- Primary keys copied from SQLite must advance PostgreSQL sequences so the next application insert cannot collide.

---

### Task 1: Database URL and schema portability

**Files:**
- Modify: `requirements.txt`
- Modify: `pyproject.toml`
- Modify: `src/db/engine.py`
- Modify: `src/db/migrations.py`
- Create: `tests/test_db_engine.py`
- Create: `tests/test_db_migrations.py`

**Interfaces:**
- Produces: `normalize_database_url(db_url: str) -> str`
- Produces: `_existing_columns(engine: Engine, table: str) -> set[str]`
- Consumes: `make_engine(db_url: str, echo: bool = False) -> Engine`

- [ ] **Step 1: Write failing URL tests**

```python
def test_normalize_database_url_uses_psycopg_for_neon_urls():
    assert normalize_database_url("postgresql://u:p@host/db") == "postgresql+psycopg://u:p@host/db"
    assert normalize_database_url("postgres://u:p@host/db?sslmode=require") == "postgresql+psycopg://u:p@host/db?sslmode=require"
```

- [ ] **Step 2: Run the URL test to verify it fails**

Run: `pytest tests/test_db_engine.py::test_normalize_database_url_uses_psycopg_for_neon_urls -q`

Expected: FAIL because `normalize_database_url` does not exist.

- [ ] **Step 3: Implement URL normalization and add psycopg**

```python
def normalize_database_url(db_url: str) -> str:
    if db_url.startswith("postgres://"):
        return "postgresql+psycopg://" + db_url.removeprefix("postgres://")
    if db_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + db_url.removeprefix("postgresql://")
    return db_url
```

Add `psycopg[binary]>=3.2` to both runtime dependency lists and call the helper at the start of `make_engine`.

- [ ] **Step 4: Write failing portable-schema test**

```python
def test_existing_columns_uses_sqlalchemy_inspector(monkeypatch, sqlite_engine):
    monkeypatch.setattr(migrations, "inspect", lambda engine: FakeInspector({"id", "risk_level"}))
    assert migrations._existing_columns(sqlite_engine, "portfolio_snapshots") == {"id", "risk_level"}
```

- [ ] **Step 5: Run portable-schema test to verify it fails**

Run: `pytest tests/test_db_migrations.py::test_existing_columns_uses_sqlalchemy_inspector -q`

Expected: FAIL because the migration code executes `PRAGMA table_info` directly.

- [ ] **Step 6: Implement portable schema inspection**

Replace the `PRAGMA table_info` query with `sqlalchemy.inspect(engine).get_columns(table)`, returning each column `name`. Keep `ALTER TABLE ... ADD COLUMN` guarded by the same idempotent membership test. Preserve the SQLite foreign-key event listener only for SQLite engines.

- [ ] **Step 7: Run Task 1 tests**

Run: `pytest tests/test_db_engine.py tests/test_db_migrations.py -q`

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add requirements.txt pyproject.toml src/db/engine.py src/db/migrations.py tests/test_db_engine.py tests/test_db_migrations.py
git commit -m "feat: support PostgreSQL database connections"
```

### Task 2: Dialect-aware repository upserts

**Files:**
- Modify: `src/db/repositories.py`
- Create: `tests/test_repositories.py`

**Interfaces:**
- Produces: `dialect_insert(engine: Engine, table: Table) -> Insert`
- Consumes: every repository constructor's `Engine`

- [ ] **Step 1: Write failing dialect-selection tests**

```python
def test_dialect_insert_uses_sqlite_upsert_for_sqlite(sqlite_engine):
    assert dialect_insert(sqlite_engine, models.Security.__table__).dialect.name == "sqlite"

def test_dialect_insert_uses_postgresql_upsert_for_postgres_mock():
    engine = create_mock_engine("postgresql+psycopg://user:pass@host/db", lambda *args: None)
    assert dialect_insert(engine, models.Security.__table__).dialect.name == "postgresql"
```

- [ ] **Step 2: Run dialect-selection tests to verify they fail**

Run: `pytest tests/test_repositories.py -q`

Expected: FAIL because repository code imports and uses only `sqlite_insert`.

- [ ] **Step 3: Implement a dialect insert factory**

Import PostgreSQL `insert` alongside SQLite `insert`. Implement `dialect_insert` that returns the matching insert constructor for `engine.dialect.name` and raises `ValueError` for unsupported dialects. Store the engine in each repository and replace all direct `sqlite_insert` calls with the factory output. Keep each existing `on_conflict_do_update` key and update set unchanged.

- [ ] **Step 4: Add a SQLite ingestion regression test**

```python
def test_security_repository_upsert_updates_existing_symbol(sqlite_engine):
    repo = SecurityRepository(sqlite_engine)
    first = repo.upsert({"symbol": "000333.SZ", "display_symbol": "000333", "name": "美的集团"})
    second = repo.upsert({"symbol": "000333.SZ", "display_symbol": "000333", "name": "美的集团更新"})
    assert second.id == first.id
    assert repo.get_by_symbol("000333.SZ").name == "美的集团更新"
```

- [ ] **Step 5: Run Task 2 tests**

Run: `pytest tests/test_repositories.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/db/repositories.py tests/test_repositories.py
git commit -m "feat: make repository upserts PostgreSQL compatible"
```

### Task 3: Safe SQLite-to-PostgreSQL transfer command

**Files:**
- Create: `src/db/transfer.py`
- Modify: `src/cli.py`
- Create: `tests/test_db_transfer.py`

**Interfaces:**
- Produces: `copy_sqlite_database(source_url: str, target_url: str) -> dict[str, int]`
- Produces: `assert_empty_target(engine: Engine) -> None`
- Consumes: `models.Base.metadata.sorted_tables`, `make_engine`, `init_database`

- [ ] **Step 1: Write failing transfer test**

```python
def test_copy_sqlite_database_copies_rows_and_preserves_ids(tmp_path):
    source_url = f"sqlite:///{tmp_path / 'source.db'}"
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    seed_security(source_url, id=17, symbol="000333.SZ")
    counts = copy_sqlite_database(source_url, target_url)
    assert counts["securities"] == 1
    assert fetch_security_id(target_url, "000333.SZ") == 17
```

- [ ] **Step 2: Run transfer test to verify it fails**

Run: `pytest tests/test_db_transfer.py::test_copy_sqlite_database_copies_rows_and_preserves_ids -q`

Expected: FAIL because `copy_sqlite_database` does not exist.

- [ ] **Step 3: Implement safe table transfer**

Create both engines, require a SQLite source and PostgreSQL target in production CLI mode, call `init_database(target)`, and reject any target table containing rows. Iterate `models.Base.metadata.sorted_tables`; select mappings from each source table and insert those mappings into the target inside a transaction. Return a table-to-row-count mapping only after source and target counts match. For PostgreSQL, run `setval(pg_get_serial_sequence(...), max(id), true)` for each table with an `id` column after copying.

- [ ] **Step 4: Write failing nonempty-target test**

```python
def test_copy_sqlite_database_rejects_nonempty_target(tmp_path):
    source_url, target_url = make_seeded_source_and_target(tmp_path)
    with pytest.raises(ValueError, match="target database is not empty"):
        copy_sqlite_database(source_url, target_url)
```

- [ ] **Step 5: Run transfer tests**

Run: `pytest tests/test_db_transfer.py -q`

Expected: PASS.

- [ ] **Step 6: Add CLI command**

Add `--migrate-sqlite-to-postgres` and `--source-db-url` to `src.cli`. The command reads the PostgreSQL target only from `INVESTMENT_DB_URL`; it prints table names and row counts but never prints the URL. Reject non-PostgreSQL target URLs before copying.

- [ ] **Step 7: Test CLI target validation**

```python
def test_cli_migration_requires_postgresql_target(monkeypatch):
    monkeypatch.setenv("INVESTMENT_DB_URL", "sqlite:///data/investment.db")
    with pytest.raises(SystemExit):
        main(["--migrate-sqlite-to-postgres"])
```

Run: `pytest tests/test_db_transfer.py -q`

Expected: PASS.

- [ ] **Step 8: Commit Task 3**

```bash
git add src/db/transfer.py src/cli.py tests/test_db_transfer.py
git commit -m "feat: add safe SQLite to PostgreSQL migration"
```

### Task 4: Render configuration and operator documentation

**Files:**
- Modify: `render.yaml`
- Modify: `RENDER_DEPLOY.md`
- Create: `docs/neon-postgres-setup.md`

**Interfaces:**
- Consumes: `INVESTMENT_DB_URL` and the CLI migration command from Task 3.
- Produces: operator steps that keep the Neon secret outside version control.

- [ ] **Step 1: Write failing configuration assertions**

```python
def test_render_blueprint_does_not_commit_sqlite_runtime_url():
    config = yaml.safe_load(Path("render.yaml").read_text())
    assert all(var.get("key") != "INVESTMENT_DB_URL" for var in config["services"][0]["envVars"])

def test_render_deploy_docs_warn_that_free_disk_is_ephemeral():
    text = Path("RENDER_DEPLOY.md").read_text(encoding="utf-8")
    assert "数据不会丢失" not in text
    assert "Neon" in text
```

- [ ] **Step 2: Run configuration assertions to verify they fail**

Run: `pytest tests/test_render_configuration.py -q`

Expected: FAIL because the Blueprint commits a SQLite runtime URL and the deployment document claims it persists.

- [ ] **Step 3: Implement safe Render configuration**

Remove `INVESTMENT_DB_URL` from `render.yaml`; Render preserves manually configured environment variables. Update `RENDER_DEPLOY.md` to explain that Free web-service files are ephemeral. Add `docs/neon-postgres-setup.md` with Neon account creation, Render Dashboard environment-variable entry, one-time CLI transfer, deploy verification, rollback, and monthly free-plan quota checks. Do not include a connection string example containing realistic credentials.

- [ ] **Step 4: Run configuration assertions**

Run: `pytest tests/test_render_configuration.py -q`

Expected: PASS.

- [ ] **Step 5: Run the complete suite**

Run: `pytest -q`

Expected: PASS with no skipped migration safety tests.

- [ ] **Step 6: Commit Task 4**

```bash
git add render.yaml RENDER_DEPLOY.md docs/neon-postgres-setup.md tests/test_render_configuration.py
git commit -m "docs: document Neon persistence deployment"
```

### Task 5: Provision and verify the managed database

**Files:**
- No repository file changes.

**Interfaces:**
- Consumes: merged Tasks 1–4, a user-owned Neon project, and Render's `INVESTMENT_DB_URL` environment variable.
- Produces: a Render deployment backed by Neon and an import report.

- [ ] **Step 1: Create Neon Free PostgreSQL project**

Create a user-owned Neon Free project in the region nearest the Render service. Keep scale-to-zero enabled. Copy the pooled or direct PostgreSQL URL only into Render's secret environment-variable field.

- [ ] **Step 2: Configure Render**

Set `INVESTMENT_DB_URL` in the Render Web Service Environment page. Do not place the URL in GitHub, the PR body, terminal history, screenshots, or logs.

- [ ] **Step 3: Import existing data once**

From Render Shell or a trusted environment with both database connections available, run:

```bash
python -m src.cli --migrate-sqlite-to-postgres
```

Expected: each listed source table has the same target row count and the process exits with status 0.

- [ ] **Step 4: Deploy application code**

Merge the migration PR and wait until Render reports a successful deploy. If the deploy fails, clear `INVESTMENT_DB_URL` to return to SQLite and inspect the deploy log before retrying.

- [ ] **Step 5: Verify persistence**

Open an existing research detail, add one new test stock, then restart the Render service. Confirm that both the existing research snapshot and the newly added security remain visible.

- [ ] **Step 6: Record completion**

Record the Neon project region, the database creation date, and the free-plan monthly quota reminder in the private operator record; never commit credentials.

