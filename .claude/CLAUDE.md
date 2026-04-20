# ICT Trading Bot — Claude Code Guidelines

## Project Overview
Multi-threaded options trading bot using Interactive Brokers. PostgreSQL database, 
React/FastAPI dashboard, IB connection pool. 51+ bugs fixed, 6 architecture principles.
Branch: feature/dashboard. Docs: docs/backlog.md has full project state.

## Architecture Principles (MUST FOLLOW)

### ARCH-001: Database is the Single Source of Truth
- NEVER use in-memory cache for state decisions
- ALWAYS read from DB for trade state (open? closed? qty?)
- PostgreSQL has its own caching — don't second-guess it

### ARCH-002: Row-Level Locking
- ALL trade state transitions use SELECT FOR UPDATE NOWAIT
- If lock not acquired → skip and retry next cycle
- NEVER proceed without verifying the lock

### ARCH-005: Single Close Authority
- ONLY _atomic_close() closes trades
- Flow: lock DB → cancel ALL IB orders → verify cancelled → check position → sell → update DB → commit
- If any step fails → rollback, retry next cycle

### ARCH-006: Single Open Authority
- ONLY add_trade() creates trades in DB
- Check for existing open trade on same ticker/conId before INSERT

### Assume Nothing
- After cancelling IB orders → VERIFY they are cancelled (poll up to 3s)
- After sending sell → VERIFY position qty changed
- If brackets were expected but not found → bracket may have JUST FIRED, wait 2s
- NEVER use stale cached data for critical decisions

### Test Every Feature, Every Change
- **Every new feature MUST ship with at least a handful of tests** that lock
  in the intended behavior. No exceptions — if it's worth writing, it's
  worth a test.
- **Before calling any unit of work "done," run the full regression suite**
  (`python -m pytest tests/unit/`). Any pre-existing test failing is a
  regression — fix it or roll the change back. Never ship red.
- Concurrency-sensitive features get concurrency tests (see
  `tests/unit/test_concurrency.py` for the pattern). DB-touching features
  get integration tests under `tests/integration/`.
- The dashboard Tests tab ("Run Unit" / "Run Concurrency" / "Run Integration")
  is the fastest way to confirm the whole suite still passes.

### Keep RESTART_PROMPT.md Current
- **After every push, update `RESTART_PROMPT.md` at the repo root.**
  The user keeps a fixed restart prompt that points at this file;
  if the file drifts from reality, restarts get confused.
- Refresh at minimum: the "Last updated" line (commit hash + branch),
  the test pass count, any new branches pushed, and the next-step list.
- The file has a stable structure — just edit the sections that changed.

## Commands
```bash
# Compile check
python -c "import py_compile; py_compile.compile('file.py', doraise=True)"

# Start Docker services
docker compose up -d

# Rebuild API (after route changes)
docker compose build api && docker compose up -d api

# Rebuild frontend (after React changes)  
docker compose build frontend && docker compose up -d frontend

# Start bot sidecar
python bot_manager.py  (from C:\src\trading\ict-bot)

# Start/stop bot
curl -X POST http://localhost:9000/start
curl -X POST http://localhost:9000/stop

# Check bot status
curl http://localhost:9000/status

# Dashboard
http://localhost

# Run SQL
docker exec ict-bot-postgres-1 psql -U ict_bot -d ict_bot -c "SQL HERE"
```

## Code Rules

### Before ANY Code Change
1. Read the relevant file(s) first
2. Understand the current flow before modifying
3. Consider: does this change affect the close flow? The open flow? Reconciliation?

### Error Handling
- Use handle_error() from strategy/error_handler.py
- Log to system_log DB via _trace() for dashboard visibility
- NEVER bare except/pass — every error must be visible

### IB Event Loop
- NEVER do DB writes on the IB event loop thread
- NEVER do blocking calls on the IB event loop thread
- IB error callbacks must be non-blocking (log only)

### Testing After Changes
1. Compile check ALL modified files
2. Check for import errors
3. If close flow changed → verify with bot.log trace
4. If DB schema changed → run ALTER TABLE on postgres container

### Committing
- Each commit should be ONE logical change
- Include bug/enhancement number in commit message
- Always push to remote after committing
- Track every bug in docs/backlog.md

## Key Files
- docs/backlog.md — ALL bugs, enhancements, architecture principles
- strategy/exit_executor.py — THE close flow (cancel → verify → check → sell)
- strategy/exit_manager.py — Trade monitoring loop (DB-backed)
- broker/ib_client.py — IB API facade
- broker/ib_pool.py — Connection pool (3-4 connections)
- db/writer.py — ALL DB operations (row-level locking)
- strategy/reconciliation.py — Two-pass DB↔IB sync

## What NOT To Do
- Do NOT add in-memory lists as parallel sources of truth
- Do NOT send sell orders without checking IB position qty first
- Do NOT call IB directly from random threads — use _submit_to_ib()
- Do NOT assume an IB call succeeded — verify the result
- Do NOT make large refactors without incremental commits
- Do NOT change the close flow without full trace logging
