# Automated Testing Framework — Design Document

## Purpose

Prevent regressions, catch bugs before deployment, and ensure code quality.
Every code change must pass the test suite before merging. Tests run
automatically on every push via GitHub Actions CI/CD.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    TESTING PYRAMID                                │
│                                                                   │
│                        ╱╲                                        │
│                       ╱  ╲     E2E Tests (5-10)                  │
│                      ╱ E2E╲    Full system with paper IB          │
│                     ╱──────╲   ~5 min to run                      │
│                    ╱        ╲                                     │
│                   ╱Integration╲  Integration Tests (20-30)        │
│                  ╱  Tests      ╲  DB + mock IB                    │
│                 ╱───────────────╲  ~30 sec to run                 │
│                ╱                 ╲                                │
│               ╱   Unit Tests     ╲  Unit Tests (50-100)           │
│              ╱   (no dependencies) ╲  Pure logic, fast             │
│             ╱───────────────────────╲  ~5 sec to run              │
│            ╱                         ╲                            │
│           ╱──────────────────────────╲                           │
└─────────────────────────────────────────────────────────────────┘
```

---

## Database Design — Test Results

### Table: `test_runs`

```sql
CREATE TABLE test_runs (
    id              SERIAL PRIMARY KEY,
    run_type        VARCHAR(20) NOT NULL,   -- 'unit', 'integration', 'e2e', 'all'
    trigger         VARCHAR(30),             -- 'push', 'pr', 'manual', 'cron'
    branch          VARCHAR(100),
    commit_hash     VARCHAR(40),
    commit_message  TEXT,
    
    -- Results
    status          VARCHAR(20) NOT NULL DEFAULT 'running',
                    -- running, passed, failed, error
    total_tests     INT DEFAULT 0,
    passed          INT DEFAULT 0,
    failed          INT DEFAULT 0,
    skipped         INT DEFAULT 0,
    errors          INT DEFAULT 0,
    duration_sec    NUMERIC(10,2),
    
    -- Coverage
    coverage_pct    NUMERIC(5,2),           -- overall code coverage %
    coverage_detail JSONB DEFAULT '{}',     -- per-file coverage
    
    -- Metadata
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Artifacts
    log_output      TEXT,                   -- full pytest output
    error_summary   TEXT                    -- failed test summary
);

CREATE INDEX idx_test_runs_status ON test_runs(status);
CREATE INDEX idx_test_runs_created ON test_runs(created_at DESC);
CREATE INDEX idx_test_runs_branch ON test_runs(branch);
```

### Table: `test_results`

```sql
CREATE TABLE test_results (
    id              SERIAL PRIMARY KEY,
    run_id          INT NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    
    test_name       VARCHAR(200) NOT NULL,   -- e.g., 'test_close_trade_locking'
    test_file       VARCHAR(200),            -- e.g., 'tests/test_db_writer.py'
    test_class      VARCHAR(100),            -- e.g., 'TestCloseTrade'
    category        VARCHAR(20),             -- 'unit', 'integration', 'e2e'
    
    status          VARCHAR(20) NOT NULL,    -- passed, failed, error, skipped
    duration_sec    NUMERIC(10,4),
    error_message   TEXT,
    traceback       TEXT,
    
    -- For regression tracking
    bug_ref         VARCHAR(20),             -- e.g., 'BUG-047' if this test covers a specific bug
    
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_test_results_run ON test_results(run_id);
CREATE INDEX idx_test_results_status ON test_results(status);
CREATE INDEX idx_test_results_bug ON test_results(bug_ref);
```

---

## Test Categories & Examples

### 1. Unit Tests (tests/unit/)

No external dependencies. Fast. Test pure logic.

```
tests/unit/
├── test_occ_parser.py          — OCC symbol parsing
│   ├── test_parse_valid_symbol
│   ├── test_parse_with_spaces
│   ├── test_parse_invalid
│   ├── test_is_expired
│   ├── test_build_occ
│   └── test_normalize_symbol
│
├── test_exit_conditions.py      — Exit logic
│   ├── test_tp_hit
│   ├── test_sl_hit
│   ├── test_trailing_stop
│   ├── test_tp_to_trail_conversion
│   ├── test_roll_trigger
│   ├── test_time_exit_90min
│   ├── test_eod_exit
│   └── test_no_exit_normal_range
│
├── test_signal_engine.py        — Signal detection
│   ├── test_detect_long_ifvg
│   ├── test_detect_short_ob
│   ├── test_deduplication
│   ├── test_seen_setups_filter
│   └── test_reset_daily
│
└── test_config.py               — Config loading
    ├── test_default_values
    ├── test_db_override
    └── test_env_override
```

### 2. Integration Tests (tests/integration/)

Require PostgreSQL. Use fresh DB per test (pytest-postgresql or Docker).

```
tests/integration/
├── test_db_writer.py            — DB operations
│   ├── test_insert_trade_returns_id
│   ├── test_close_trade_locking       ← BUG-022, BUG-047
│   │   └── Two threads close same trade → only one succeeds
│   ├── test_close_already_closed      ← BUG-038
│   │   └── close_trade returns False for closed trade
│   ├── test_update_price_greatest     ← peak never downgrades
│   ├── test_get_open_trades_from_db
│   ├── test_add_trade_duplicate_guard ← BUG-046
│   │   └── Same ticker → second insert blocked
│   └── test_finalize_close_jsonb      ← BUG-042
│       └── CAST(:ee AS jsonb) syntax
│
├── test_reconciliation.py       — DB↔IB sync
│   ├── test_pass1_close_stale_db_trade  ← BUG-037
│   ├── test_pass2_adopt_orphan          ← BUG-030
│   ├── test_negative_position_adopted   ← negative qty in DB
│   ├── test_duplicate_adoption_blocked  ← BUG-033
│   └── test_safety_check_zero_ib       ← BUG-027
│
└── test_exit_manager.py         — DB-backed cache
    ├── test_open_trades_from_db
    ├── test_cache_invalidation
    ├── test_add_trade_db_first
    └── test_atomic_close_reads_locked_data
```

### 3. E2E Tests (tests/e2e/)

Full system with mock IB or paper trading.

```
tests/e2e/
├── test_trade_lifecycle.py      — Full cycle
│   ├── test_signal_to_close
│   │   └── Signal → order → fill → DB → monitor → exit → DB closed
│   ├── test_rolling_lifecycle
│   │   └── Open → roll → old closed → new opened → no double-close
│   └── test_ui_close
│       └── Dashboard close → exit_manager → IB → DB
│
├── test_race_conditions.py      — Concurrency
│   ├── test_two_threads_close_same_trade
│   ├── test_exit_manager_dashboard_race
│   ├── test_reconciliation_exit_race
│   └── test_scanner_roller_race     ← BUG-046
│
└── test_bracket_handling.py     — IB bracket orders
    ├── test_cancel_before_sell
    ├── test_bracket_just_fired       ← BUG-049
    └── test_bracket_cancel_timeout
```

---

## CI/CD Pipeline (GitHub Actions)

### Workflow: `.github/workflows/test.yml`

```yaml
name: Test Suite
on:
  push:
    branches: [feature/dashboard, main]
  pull_request:
    branches: [main]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.14' }
      - run: pip install -r requirements.txt -r requirements-test.txt
      - run: pytest tests/unit/ -v --tb=short --junitxml=results/unit.xml
      - uses: actions/upload-artifact@v4
        with: { name: unit-results, path: results/ }

  integration-tests:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env: { POSTGRES_USER: test, POSTGRES_PASSWORD: test, POSTGRES_DB: test }
        ports: ['5432:5432']
        options: --health-cmd pg_isready --health-interval 10s
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.14' }
      - run: pip install -r requirements.txt -r requirements-test.txt
      - run: pytest tests/integration/ -v --tb=short --junitxml=results/integration.xml
        env:
          DATABASE_URL: postgresql://test:test@localhost:5432/test
      - uses: actions/upload-artifact@v4
        with: { name: integration-results, path: results/ }

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.14' }
      - run: pip install ruff
      - run: ruff check . --select E,W,F --ignore E501
```

### Pre-commit Hook

```bash
#!/bin/sh
# .git/hooks/pre-commit
echo "Running unit tests..."
python -m pytest tests/unit/ -q --tb=line
if [ $? -ne 0 ]; then
    echo "Unit tests failed — commit blocked"
    exit 1
fi
echo "Running compile check..."
python -c "
import py_compile, glob
for f in glob.glob('**/*.py', recursive=True):
    py_compile.compile(f, doraise=True)
"
echo "All checks passed ✓"
```

---

## Test Results Dashboard (UI)

### TestsTab Wireframe

```
┌─────────────────────────────────────────────────────────────────┐
│  [Trades] [Analytics] [Threads] [Tickers] [Settings]            │
│  [Backtest] [Tests]  ← NEW TAB                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  [Run All Tests] [Run Unit] [Run Integration]     Last: 2m ago   │
│                                                                   │
│  ┌─── Recent Runs ───────────────────────────────────────────┐   │
│  │ Status│ Type       │ Branch    │ Tests │ Pass │ Fail│ Time │  │
│  │ ✅    │ all        │ dashboard │ 85    │ 85   │ 0   │ 35s  │  │
│  │ ❌    │ integration│ dashboard │ 28    │ 26   │ 2   │ 12s  │  │
│  │ ✅    │ unit       │ dashboard │ 52    │ 52   │ 0   │ 4s   │  │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                   │
│  ┌─── Failed Tests (click run to expand) ────────────────────┐   │
│  │ test_close_trade_locking [BUG-047]                         │  │
│  │   AssertionError: Expected False, got True                 │  │
│  │   File: tests/integration/test_db_writer.py:45             │  │
│  │                                                            │  │
│  │ test_bracket_just_fired [BUG-049]                          │  │
│  │   TimeoutError: IB mock did not respond                    │  │
│  │   File: tests/e2e/test_bracket_handling.py:78              │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌─── Coverage ──────────────────────────────────────────────┐   │
│  │ Overall: 72%  ████████████████████░░░░░░░░                │   │
│  │                                                            │  │
│  │ File                        │ Coverage │ Missing Lines     │  │
│  │ strategy/exit_executor.py   │ 89%      │ 142-148           │  │
│  │ strategy/exit_manager.py    │ 78%      │ 290-310, 345-360  │  │
│  │ broker/ib_client.py         │ 45%      │ (IB methods)      │  │
│  │ strategy/reconciliation.py  │ 82%      │ 170-180           │  │
│  │ db/writer.py                │ 91%      │ 280-285           │  │
│  └────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## How to Generate and Update Tests

### Adding a Test for a New Bug Fix

1. **Identify the bug** — e.g., BUG-051 (wrong exit price in reconciliation)
2. **Write the test FIRST** (test-driven):
```python
# tests/integration/test_reconciliation.py
def test_exit_price_uses_fill_not_entry(db_session, mock_ib):
    """BUG-051: Reconciliation should use IB fill price, not entry price."""
    # Setup: trade in DB with entry_price=2.90
    trade_id = insert_trade({"ticker": "MU", "entry_price": 2.90, ...})
    
    # Mock: IB has no position (closed by bracket)
    # Mock: IB fill shows exit at $0.70
    mock_ib.fills.return_value = [MockFill(price=0.70, conId=123)]
    
    # Act: run reconciliation
    periodic_reconciliation(mock_ib, exit_manager)
    
    # Assert: DB exit_price should be 0.70, not 2.90
    trade = db_session.query(Trade).get(trade_id)
    assert trade.exit_price == 0.70  # Not 2.90!
    assert trade.status == "closed"
```
3. **Verify test fails** with the bug present
4. **Fix the bug** — code change
5. **Verify test passes** after the fix
6. **Tag the test** with `bug_ref='BUG-051'`

### Updating Tests After Refactoring

When refactoring (e.g., ARCH-003):
1. Run the full test suite BEFORE refactoring
2. All tests should pass (baseline)
3. Make refactoring changes
4. Run tests again — any failures indicate regression
5. Fix the failures (update test if interface changed, fix code if behavior changed)
6. Commit with both code and test changes

---

## Code Profiling & Optimization

### Profiling Tools

```python
# 1. cProfile — built-in Python profiler
python -m cProfile -s cumtime main.py 2>&1 | head -50

# 2. line_profiler — line-by-line timing
pip install line_profiler
@profile
def _check_exits(self):
    ...
kernprof -l -v strategy/exit_manager.py

# 3. memory_profiler — memory usage
pip install memory_profiler
@profile
def _refresh_cache(self):
    ...
python -m memory_profiler strategy/exit_manager.py

# 4. py-spy — sampling profiler (no code changes)
pip install py-spy
py-spy record -o profile.svg -- python main.py
```

### Performance Monitoring Endpoint

```python
# GET /api/performance
{
    "db_query_avg_ms": 12.3,
    "ib_call_avg_ms": 450.2,
    "cache_hit_rate": 0.92,
    "exit_cycle_avg_ms": 1200,
    "trades_per_second": 0.8,
    "memory_usage_mb": 245,
    "db_pool_active": 3,
    "db_pool_idle": 2,
    "ib_connections": 4,
}
```

### Known Optimization Areas

| Area | Current | Potential Fix | Impact |
|------|---------|--------------|--------|
| DB cache refresh | Every 5s full query | Incremental updates (WHERE updated_at > last_check) | Reduce DB load |
| Batch pricing | One IB call for all trades | IB streaming (ENH-001) | Sub-second prices |
| OCC parsing | Regex per call | Compiled regex + cache | Minor |
| Exit evaluation | Per-trade every 5s | Skip if price unchanged | Reduce CPU |
| Thread count | 17 scanner + 4 IB | Dynamic based on active tickers | Resource savings |
| DB connections | New session per call | Connection pooling tuning | Reduce overhead |

---

## Test Dependencies

### requirements-test.txt

```
pytest>=8.0
pytest-asyncio>=0.23
pytest-cov>=5.0
pytest-postgresql>=6.0
pytest-timeout>=2.3
unittest-mock
httpx  # for API testing
factory-boy  # for test data generation
freezegun  # for time-dependent tests
```
