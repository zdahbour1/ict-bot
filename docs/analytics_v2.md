# Analytics Tab v2 — Architecture Improvements

## Date: 2026-04-10
## Status: Planned

## 1. Pacific Timezone Consistency

All timestamps in charts must use Pacific Time (PT):
- Entry time, exit time displayed as PT
- Hourly charts grouped by PT hour (not UTC)
- Axis labels show PT (e.g., "8:00 AM PT", "9:00 AM PT")
- Tooltip timestamps in PT
- Backend: convert UTC timestamps to PT before aggregation

## 2. Database Views for Analytics

Create reusable PostgreSQL views that support both aggregation AND drill-down.
The same view with different WHERE clauses serves both the chart and the detail popup.

### Views to Create

```sql
-- Base view: all trade data with PT timestamps and computed fields
CREATE OR REPLACE VIEW v_trades_analytics AS
SELECT
    t.*,
    t.entry_time AT TIME ZONE 'America/Los_Angeles' AS entry_time_pt,
    t.exit_time AT TIME ZONE 'America/Los_Angeles' AS exit_time_pt,
    EXTRACT(HOUR FROM t.entry_time AT TIME ZONE 'America/Los_Angeles') AS entry_hour_pt,
    EXTRACT(HOUR FROM t.exit_time AT TIME ZONE 'America/Los_Angeles') AS exit_hour_pt,
    (t.entry_time AT TIME ZONE 'America/Los_Angeles')::date AS trade_date_pt,
    CASE WHEN t.direction = 'LONG' THEN 'Call' ELSE 'Put' END AS contract_type,
    COALESCE(t.entry_price, 0) * 100 * t.contracts_entered AS risk_capital,
    CASE 
        WHEN t.exit_time IS NOT NULL AND t.entry_time IS NOT NULL 
        THEN EXTRACT(EPOCH FROM (t.exit_time - t.entry_time)) / 60 
        ELSE NULL 
    END AS hold_minutes
FROM trades t;

-- Aggregation: P&L by ticker for a date range
CREATE OR REPLACE VIEW v_pnl_by_ticker AS
SELECT
    trade_date_pt,
    ticker,
    COUNT(*) AS total_trades,
    COUNT(*) FILTER (WHERE exit_result = 'WIN') AS wins,
    COUNT(*) FILTER (WHERE exit_result = 'LOSS') AS losses,
    COALESCE(SUM(pnl_usd), 0) AS total_pnl,
    ROUND(AVG(pnl_pct) FILTER (WHERE status = 'closed'), 4) AS avg_pnl_pct
FROM v_trades_analytics
GROUP BY trade_date_pt, ticker;

-- Aggregation: P&L by hour (exit)
CREATE OR REPLACE VIEW v_pnl_by_exit_hour AS
SELECT
    trade_date_pt,
    exit_hour_pt AS hour,
    COUNT(*) AS trades,
    COALESCE(SUM(pnl_usd), 0) AS pnl
FROM v_trades_analytics
WHERE status = 'closed'
GROUP BY trade_date_pt, exit_hour_pt;

-- Aggregation: P&L by hour (entry)
CREATE OR REPLACE VIEW v_pnl_by_entry_hour AS
SELECT
    trade_date_pt,
    entry_hour_pt AS hour,
    COUNT(*) AS trades,
    COALESCE(SUM(pnl_usd), 0) AS pnl
FROM v_trades_analytics
GROUP BY trade_date_pt, entry_hour_pt;

-- Aggregation: Risk capital by hour
CREATE OR REPLACE VIEW v_risk_by_hour AS
SELECT
    trade_date_pt,
    entry_hour_pt AS hour,
    SUM(risk_capital) AS capital,
    COUNT(*) AS contracts
FROM v_trades_analytics
GROUP BY trade_date_pt, entry_hour_pt;

-- Aggregation: By contract type
CREATE OR REPLACE VIEW v_pnl_by_contract_type AS
SELECT
    trade_date_pt,
    contract_type,
    COUNT(*) AS trades,
    COALESCE(SUM(pnl_usd), 0) AS pnl
FROM v_trades_analytics
GROUP BY trade_date_pt, contract_type;

-- Aggregation: By exit reason
CREATE OR REPLACE VIEW v_pnl_by_exit_reason AS
SELECT
    trade_date_pt,
    exit_reason,
    COUNT(*) AS trades,
    COALESCE(SUM(pnl_usd), 0) AS pnl
FROM v_trades_analytics
WHERE status = 'closed'
GROUP BY trade_date_pt, exit_reason;
```

### Drill-Down Pattern

When user clicks a bar in "P&L by Ticker" chart (e.g., TSLA):
```sql
SELECT * FROM v_trades_analytics 
WHERE trade_date_pt BETWEEN '2026-04-09' AND '2026-04-09'
  AND ticker = 'TSLA'
ORDER BY entry_time_pt;
```

Same base view, just filtered — returns individual trades for the popup.

### API Pattern

```
GET /api/analytics?start=2026-04-09&end=2026-04-09
  → aggregated charts data from views

GET /api/analytics/drilldown?start=2026-04-09&end=2026-04-09&ticker=TSLA
  → individual trades for popup
  
GET /api/analytics/drilldown?start=2026-04-09&end=2026-04-09&exit_hour=10
  → trades that exited at 10:xx PT
```

## 3. Date Range Filter

- Start date + End date inputs (default: today to today)
- Quick buttons: "Today", "Yesterday", "This Week", "Last 5 Days", "All Time"
- Date range persists across tab switches
- Charts aggregate across the full date range
- Date picker uses calendar widget for easy selection

## 4. Architecture for Scalability

- All analytics queries use database views (not Python aggregation)
- Views are indexed and performant
- Drill-down uses the same base view with filters
- New charts = new view + API endpoint + React component
- Easy to add: weekly/monthly rollups, strategy comparison, etc.
