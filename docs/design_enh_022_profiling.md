# ENH-022 Design — Profiling + Runtime Monitoring

## 1. Goals (dev + prod)

- **Dev**: find hot-path slowdowns on demand — IB call latency, DB query latency, scan-loop overhead — without code changes or restarts.
- **Prod**: continuous visibility into CPU/RAM per thread, IB request queue depth, scan-cycle wall-clock, DB pool utilization.
- **Multi-tenant ready**: every metric emitted with a `user_id` label (stub now, populated once ENH-020 lands). Label vocabulary governed up-front to avoid cardinality explosion.

## 2. Two-track approach

Profiling and monitoring answer different questions; keep them separate.

- **Dev-time (ad-hoc profiling)**: `py-spy` attached to a running PID for flamegraphs; `pytest-profiling` on the backtest suite for deterministic runs. No code changes, no restart.
- **Prod-time (structured metrics)**: `prometheus_client` in-process counters and histograms exposed on a `/metrics` endpoint. Time-series store scrapes every 15s.

Two tracks prevents the common trap of "log everything to the DB and grep later" — fine for events, useless for p95 latency over 24h.

## 3. Recommended stack

| Need | Tool | Notes |
|---|---|---|
| On-demand CPU profiling | py-spy | Rust-based, out-of-process, negligible overhead (safe on prod). `py-spy top --pid <bot>`, `py-spy record -o flame.svg --pid <bot> --duration 60`. |
| In-process metrics | prometheus_client | Histograms + counters. Well-supported, tiny footprint. |
| Metrics scrape + dashboards (local) | Prometheus + Grafana via docker-compose | Already fits our existing docker setup. |
| Metrics scrape + dashboards (cloud) | Grafana Cloud free tier | 10k active series / 50GB logs / 50GB traces free — plenty for single tenant, budgeted for Phase 2. |
| Distributed tracing (later) | OpenTelemetry auto-instrumentation for FastAPI + SQLAlchemy | Only once we have multiple services in cloud. Skip for Phase A/B. |
| Human-readable events | Existing `system_log` table | Keep. Not a replacement for metrics, complements them. |

**Rejected**: Datadog/New Relic (cost, lock-in); `system_log` as primary mechanism (can't compute histograms without full scans).

## 4. Key metrics to instrument

All histograms use default buckets plus a tail bucket at 30s.

| Metric | Type | Labels | Source |
|---|---|---|---|
| `ib_call_duration_seconds` | Histogram | `method`, `strategy_id`, `user_id` | `broker/ib_orders.py::_submit_to_ib` |
| `ib_request_queue_depth` | Gauge | `user_id` | IB client wrapper |
| `scan_cycle_wall_seconds` | Histogram | `strategy`, `ticker`, `user_id` | `strategy/scanner.py::scan_once` |
| `db_query_duration_seconds` | Histogram | `operation` (insert/update/select kind) | `db/writer.py` |
| `db_pool_in_use` / `db_pool_size` | Gauge | — | SQLAlchemy pool events |
| `delta_hedger_rebalance_total` | Counter | `ticker`, `user_id` | delta-hedger |
| `trade_entry_latency_seconds` | Histogram | `strategy`, `user_id` | signal timestamp → IB fill event |
| `thread_cpu_seconds_total` | Counter | `thread_name` | `psutil` sampler, 10s tick |

**Cardinality budget**: `ticker` (~30) × `strategy` (~6) × `user_id` (1 today, cap at 100 Phase 2) = ~18k series worst case. Over the Grafana Cloud free tier ceiling once multi-tenant. Mitigation in §10.

## 5. Architecture

```
[bot process]                    [FastAPI dashboard]
  prometheus_client                prometheus_client
  :9100/metrics                    :8000/metrics
       \                              /
        \                            /
         +--> Prometheus (scrape) <-+
                   |
               Grafana (local or Cloud)
```

- Bot exposes `/metrics` on its own port (default 9100), distinct from the existing 9000 sidecar.
- FastAPI adds `prometheus_client.make_asgi_app()` mount at `/metrics`.
- Scraper config lives in `docker/prometheus.yml`.
- Prod cloud path: Grafana Agent on the bot host ships to Grafana Cloud remote-write.

**Multiprocess note**: today the bot is threaded (single process), so default prometheus_client registry works. If we ever fork worker subprocesses, switch to `PROMETHEUS_MULTIPROC_DIR` mode.

## 6. Code changes (estimate LOC)

- **New** `broker/metrics.py` (~60 LOC): defines `ib_call_duration_seconds`, context-manager `time_ib_call(method)`.
- **New** `db/metrics.py` (~50 LOC): same pattern, plus SQLAlchemy `pool_checkout`/`pool_checkin` event hooks for pool gauges.
- **Edit** `broker/ib_orders.py`: wrap every `_submit_to_ib` with `with time_ib_call("place_order"):`.
- **Edit** `db/writer.py`: wrap each write op.
- **Edit** `strategy/scanner.py`: wrap `scan_once`.
- **Edit** `api/main.py` (FastAPI): mount `/metrics` ASGI app.
- **New** `ops/metrics_server.py` (~40 LOC): starts bot-side HTTP exporter on port 9100.
- **Add deps**: `prometheus_client` (prod), `py-spy` (dev-only, `requirements-dev.txt`).
- **Add** `docker/prometheus.yml`, `docker/grafana/dashboards/bot.json`.

Total: ~300 LOC of instrumentation + compose config. No behavioural code touched.

## 7. Dashboard additions

- New **Profiling** tab in the React frontend:
  - Last 15 min scan latency (p50/p95/p99) per strategy, from our own API `/api/metrics/scan_latency` that queries Prometheus via HTTP.
  - IB call quantiles, DB pool utilization sparkline.
  - "Open in Grafana" link for long-range / cross-tenant views.
- Kept simple — full time-series exploration belongs in Grafana, not in the React app.

## 8. Rollout phases

| Phase | Scope | Duration |
|---|---|---|
| **A** | `prometheus_client` counters/histograms on hot paths; local Prometheus + Grafana via docker-compose; hand-built dashboard JSON. | 2–3 days |
| **B** | React Profiling tab consuming our own `/api/metrics/*` endpoints; alert rules for p95 scan latency > 2s and IB queue depth > 50. | 2–3 days |
| **C** | Grafana Cloud remote-write from prod host; OpenTelemetry auto-instrumentation for FastAPI + SQLAlchemy tracing; user_id label wired through (ties to ENH-020). | ~1 week |

Phase A ships the biggest win alone: p95 latency dashboards and on-demand `py-spy` flamegraphs.

## 9. Effort

- Phase A: 2–3 days.
- Phase B: 2–3 days.
- Phase C: ~1 week, gated on ENH-020 cloud infra and tenant identity plumbing.

Total to useful: 1 week. Full cloud story: 2–3 weeks aggregate.

## 10. Open questions

- **Cardinality under multi-tenant**: `ticker × strategy × user_id` will breach Grafana Cloud's 10k free-tier series ceiling somewhere around 50 users. Options: drop `ticker` from low-value metrics, keep it only on `trade_entry_latency_seconds`; or pre-aggregate via recording rules and publish rolled-up series. Decide before Phase C.
- **Trace sampling rate** for OTel in prod — start at 10% head sampling.
- **py-spy in containers**: needs `SYS_PTRACE` capability. Fine for our self-hosted setup; may be blocked in managed k8s — revisit at ENH-020 time.
- **Alert routing**: PagerDuty? email-only for now? Defer to Phase B kickoff.

Sources:
- [py-spy](https://github.com/benfred/py-spy)
- [prometheus_client multiprocess mode](https://prometheus.github.io/client_python/multiprocess/)
- [OpenTelemetry FastAPI instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html)
- [OpenTelemetry SQLAlchemy instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/sqlalchemy/sqlalchemy.html)
- [Grafana Cloud usage limits](https://grafana.com/docs/grafana-cloud/cost-management-and-billing/understand-your-invoice/usage-limits/)
- [Managing high cardinality in Prometheus](https://grafana.com/blog/2022/10/20/how-to-manage-high-cardinality-metrics-in-prometheus-and-kubernetes/)
