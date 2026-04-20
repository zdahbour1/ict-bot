import { useEffect, useMemo, useState } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell,
} from 'recharts';

// Static page: refresh button, direct API queries, no polling.
// Two drill-down flows both funnel into the same TradesModal:
//   1. Click a runs-table row  → fetches /backtests/{id}/trades (paginated)
//   2. Click any Analytics bar → fetches /backtests/analytics/trades
// That keeps the UX consistent across the page.

const CHART_COLORS = {
  green: '#3fb950', red: '#f85149', blue: '#58a6ff',
  yellow: '#d29922', purple: '#bc8cff', cyan: '#39d2c0',
};

interface Strategy {
  strategy_id: number;
  name: string;
  display_name: string;
  enabled: boolean;
  is_default: boolean;
}

interface RunRow {
  id: number;
  name: string | null;
  status: string;
  strategy_name: string | null;
  tickers: string[];
  start_date: string | null;
  end_date: string | null;
  total_trades: number;
  wins: number;
  losses: number;
  total_pnl: number;
  win_rate: number;
  profit_factor: number | null;
  max_drawdown: number;
  avg_hold_min: number;
  duration_sec: number | null;
  created_at: string | null;
  error_message: string | null;
}

interface TradeRow {
  id: number;
  ticker: string;
  symbol: string | null;
  direction: string;
  entry_price: number | null;
  exit_price: number | null;
  pnl_usd: number;
  pnl_pct: number;
  entry_time: string | null;
  exit_time: string | null;
  hold_minutes: number | null;
  signal_type: string | null;
  exit_reason: string | null;
  exit_result: string | null;
}

function fmtUsd(v: number | null | undefined): string {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : '';
  return `${s}$${Number(v).toFixed(2)}`;
}

function pnlColor(v: number): string {
  return v > 0 ? 'text-green-400' : v < 0 ? 'text-red-400' : 'text-gray-400';
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}


// ─────────────────────────────────────────────────────────
// Launch dialog — minimal
// ─────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────
// Sortable-table primitives — shared by RunsTable + TradesTable
// ─────────────────────────────────────────────────────────

type SortDir = 'asc' | 'desc' | null;

interface ColDef<T> {
  key: string;
  label: string;
  get: (row: T) => unknown;     // value for sort + filter
  render: (row: T) => React.ReactNode;  // cell UI
  filterable?: boolean;
  filterType?: 'text' | 'number';
  align?: 'left' | 'right';
}

interface ServerSortCtrl {
  sortKey: string | null;
  sortDir: SortDir;
  onChange: (key: string | null, dir: SortDir) => void;
}

interface ServerFilterCtrl {
  filters: Record<string, string>;
  onChange: (filters: Record<string, string>) => void;
}

function useSortableFilterable<T>(
  rows: T[], cols: ColDef<T>[],
  serverSort?: ServerSortCtrl,
  serverFilter?: ServerFilterCtrl,
) {
  const [localSortKey, setLocalSortKey] = useState<string | null>(null);
  const [localSortDir, setLocalSortDir] = useState<SortDir>(null);
  const [localFilters, setLocalFilters] = useState<Record<string, string>>({});

  // If server-side sort/filter is in use, bypass local state entirely.
  const sortKey = serverSort ? serverSort.sortKey : localSortKey;
  const sortDir = serverSort ? serverSort.sortDir : localSortDir;
  const filters = serverFilter ? serverFilter.filters : localFilters;

  const toggleSort = (key: string) => {
    const advance = (curKey: string | null, curDir: SortDir): [string | null, SortDir] => {
      if (curKey !== key) return [key, 'asc'];
      if (curDir === 'asc') return [key, 'desc'];
      if (curDir === 'desc') return [null, null];
      return [key, 'asc'];
    };
    if (serverSort) {
      const [k, d] = advance(serverSort.sortKey, serverSort.sortDir);
      serverSort.onChange(k, d);
    } else {
      const [k, d] = advance(localSortKey, localSortDir);
      setLocalSortKey(k); setLocalSortDir(d);
    }
  };

  const setFilter = (key: string, value: string) => {
    if (serverFilter) {
      serverFilter.onChange({ ...serverFilter.filters, [key]: value });
    } else {
      setLocalFilters(f => ({ ...f, [key]: value }));
    }
  };

  const processed = useMemo(() => {
    let out = rows;
    // Filter — skip locally when backend is filtering
    if (serverFilter) return (() => {
      // still apply sort locally if serverSort absent
      if (!serverSort && sortKey && sortDir) {
        const col = cols.find(c => c.key === sortKey);
        if (col) {
          const mul = sortDir === 'asc' ? 1 : -1;
          out = [...out].sort((a, b) => {
            const av = col.get(a), bv = col.get(b);
            if (av == null && bv == null) return 0;
            if (av == null) return 1;
            if (bv == null) return -1;
            if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * mul;
            return String(av).localeCompare(String(bv)) * mul;
          });
        }
      }
      return out;
    })();
    for (const c of cols) {
      const raw = filters[c.key];
      if (!raw || !c.filterable) continue;
      const needle = raw.trim().toLowerCase();
      if (!needle) continue;
      if (c.filterType === 'number') {
        // Accept "=N", ">N", "<N", ">=N", "<=N", or plain number (contains)
        const m = needle.match(/^(>=|<=|>|<|=)?\s*(-?\d+(\.\d+)?)\s*$/);
        if (m) {
          const op = m[1] || '=';
          const n = parseFloat(m[2]);
          out = out.filter(r => {
            const v = Number(c.get(r) ?? NaN);
            if (isNaN(v)) return false;
            if (op === '>=') return v >= n;
            if (op === '<=') return v <= n;
            if (op === '>')  return v > n;
            if (op === '<')  return v < n;
            return v === n;
          });
        } else {
          out = out.filter(r => String(c.get(r) ?? '').toLowerCase().includes(needle));
        }
      } else {
        out = out.filter(r => String(c.get(r) ?? '').toLowerCase().includes(needle));
      }
    }
    // Sort — skip locally when backend is handling it
    if (!serverSort && sortKey && sortDir) {
      const col = cols.find(c => c.key === sortKey);
      if (col) {
        const mul = sortDir === 'asc' ? 1 : -1;
        out = [...out].sort((a, b) => {
          const av = col.get(a), bv = col.get(b);
          if (av == null && bv == null) return 0;
          if (av == null) return 1;
          if (bv == null) return -1;
          if (typeof av === 'number' && typeof bv === 'number') {
            return (av - bv) * mul;
          }
          return String(av).localeCompare(String(bv)) * mul;
        });
      }
    }
    return out;
  }, [rows, cols, filters, sortKey, sortDir, !!serverSort, !!serverFilter]);

  return { processed, sortKey, sortDir, toggleSort, filters, setFilter };
}

function SortHeader<T>({ col, sortKey, sortDir, onClick }: {
  col: ColDef<T>;
  sortKey: string | null;
  sortDir: SortDir;
  onClick: () => void;
}) {
  const active = sortKey === col.key;
  const indicator = active ? (sortDir === 'asc' ? ' ▲' : ' ▼') : ' ⇅';
  return (
    <th
      onClick={onClick}
      className={`px-3 py-2 text-xs text-gray-500 border-b border-[#30363d] cursor-pointer select-none hover:text-gray-300 ${col.align === 'right' ? 'text-right' : 'text-left'}`}
      title="Click to sort">
      {col.label}<span className={`${active ? 'text-blue-400' : 'text-gray-700'}`}>{indicator}</span>
    </th>
  );
}


// ─────────────────────────────────────────────────────────
// RunsTable — sortable + per-column filters, no inner scroll
// ─────────────────────────────────────────────────────────

function RunsTable({ runs, total, selectedRunId, onRunClick, onDelete,
                     sortKey, sortDir, onSortChange,
                     columnFilters, onColumnFiltersChange }: {
  runs: RunRow[];
  total?: number;
  selectedRunId: number | null;
  onRunClick: (id: number) => void;
  onDelete: (id: number) => void;
  sortKey: string | null;
  sortDir: SortDir;
  onSortChange: (key: string | null, dir: SortDir) => void;
  columnFilters: Record<string, string>;
  onColumnFiltersChange: (f: Record<string, string>) => void;
}) {
  const cols: ColDef<RunRow>[] = useMemo(() => [
    { key: 'id',          label: 'ID',      get: r => r.id,          render: r => <span className="text-gray-500">#{r.id}</span>, filterable: true, filterType: 'number' },
    { key: 'name',        label: 'Name',    get: r => r.name || '',  render: r => <span className="text-gray-200">{r.name || `run-${r.id}`}</span>, filterable: true, filterType: 'text' },
    { key: 'strategy',    label: 'Strategy', get: r => r.strategy_name || '', render: r => <span className="text-gray-400">{r.strategy_name || '—'}</span>, filterable: true, filterType: 'text' },
    { key: 'tickers',     label: 'Tickers', get: r => (r.tickers || []).join(','), render: r => <span className="text-gray-400">{(r.tickers || []).join(', ')}</span>, filterable: true, filterType: 'text' },
    { key: 'period',      label: 'Period', get: r => `${r.start_date} → ${r.end_date}`, render: r => <span className="text-gray-500">{r.start_date} → {r.end_date}</span>, filterable: true, filterType: 'text' },
    { key: 'status',      label: 'Status', get: r => r.status, filterable: true, filterType: 'text',
      render: r => <span className={`px-2 py-0.5 rounded text-xs ${r.status === 'completed' ? 'bg-green-500/20 text-green-400' : r.status === 'running' ? 'bg-blue-500/20 text-blue-400 animate-pulse' : r.status === 'failed' ? 'bg-red-500/20 text-red-400' : 'bg-gray-700 text-gray-400'}`}>{r.status}</span> },
    { key: 'trades',      label: 'Trades', get: r => r.total_trades, render: r => r.total_trades, filterable: true, filterType: 'number', align: 'right' },
    { key: 'win_rate',    label: 'Win%',   get: r => r.win_rate,     render: r => `${Number(r.win_rate || 0).toFixed(1)}%`, filterable: true, filterType: 'number', align: 'right' },
    { key: 'total_pnl',   label: 'P&L',    get: r => r.total_pnl,    render: r => <span className={`font-mono ${pnlColor(r.total_pnl)}`}>{fmtUsd(r.total_pnl)}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'profit_factor', label: 'PF',   get: r => r.profit_factor ?? null, render: r => r.profit_factor != null ? Number(r.profit_factor).toFixed(2) : '—', filterable: true, filterType: 'number', align: 'right' },
    { key: 'max_drawdown', label: 'Max DD', get: r => r.max_drawdown, render: r => <span className={`font-mono ${pnlColor(r.max_drawdown)}`}>{fmtUsd(r.max_drawdown)}</span>, filterable: true, filterType: 'number', align: 'right' },
  ], []);

  const {
    processed,
    sortKey: effSortKey, sortDir: effSortDir,
    toggleSort, filters, setFilter,
  } = useSortableFilterable(runs, cols, {
    sortKey, sortDir, onChange: onSortChange,
  }, {
    filters: columnFilters,
    onChange: onColumnFiltersChange,
  });
  const hasFilters = Object.values(filters).some(v => v?.trim());

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
      <div className="px-4 py-2 border-b border-[#30363d] flex items-center justify-between text-xs">
        <span className="text-gray-500">
          Showing <span className="text-gray-300">{processed.length}</span> of {runs.length} loaded
          {total != null && total > runs.length && <> · <span className="text-gray-300">{total}</span> total in DB</>}
          {hasFilters && <button onClick={() => Object.keys(filters).forEach(k => setFilter(k, ''))}
            className="ml-2 text-blue-400 hover:text-blue-300">(clear filters)</button>}
        </span>
        <span className="text-gray-600">Sort + filters are server-side across ALL runs (numeric: &gt;N, &lt;N, &gt;=N, &lt;=N, =N)</span>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-[#21262d]">
            {cols.map(c => (
              <SortHeader key={c.key} col={c} sortKey={effSortKey} sortDir={effSortDir}
                          onClick={() => toggleSort(c.key)} />
            ))}
            <th className="px-3 py-2 border-b border-[#30363d]"></th>
          </tr>
          <tr className="bg-[#161b22]">
            {cols.map(c => (
              <th key={c.key} className="px-2 py-1 border-b border-[#21262d]">
                {c.filterable ? (
                  <input value={filters[c.key] || ''}
                         onChange={e => setFilter(c.key, e.target.value)}
                         placeholder={c.filterType === 'number' ? '>100' : 'filter...'}
                         className="w-full px-1.5 py-0.5 text-xs bg-[#0d1117] border border-[#30363d] rounded text-gray-300 placeholder:text-gray-600" />
                ) : null}
              </th>
            ))}
            <th className="px-2 py-1 border-b border-[#21262d]"></th>
          </tr>
        </thead>
        <tbody>
          {processed.map(r => {
            const isSelected = r.id === selectedRunId;
            return (
              <tr key={r.id}
                  onClick={() => onRunClick(r.id)}
                  className={`cursor-pointer border-b border-[#21262d] hover:bg-[#1c2128] ${isSelected ? 'bg-[#1c2128]' : ''}`}>
                {cols.map(c => (
                  <td key={c.key} className={`px-3 py-2 text-xs ${c.align === 'right' ? 'text-right' : ''}`}>
                    {c.render(r)}
                  </td>
                ))}
                <td className="px-3 py-2 text-right">
                  <button onClick={e => { e.stopPropagation(); onDelete(r.id); }}
                          className="text-xs text-gray-500 hover:text-red-400">Delete</button>
                </td>
              </tr>
            );
          })}
          {processed.length === 0 && (
            <tr><td colSpan={cols.length + 1} className="px-3 py-6 text-center text-sm text-gray-500">
              {runs.length === 0
                ? <>No runs yet. Click <b>+ Run Backtest</b> above.</>
                : 'No runs match the current filters.'}
            </td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}


// ─────────────────────────────────────────────────────────
// TradesTable — sortable + filterable trades list for the modal
// ─────────────────────────────────────────────────────────

function TradesTable({ trades, serverSort, serverFilter }: {
  trades: TradeRow[];
  serverSort?: ServerSortCtrl;
  serverFilter?: ServerFilterCtrl;
}) {
  const cols: ColDef<TradeRow>[] = useMemo(() => [
    { key: 'ticker',      label: 'Ticker',   get: t => t.ticker,       render: t => <span className="text-gray-200">{t.ticker}</span>, filterable: true, filterType: 'text' },
    { key: 'direction',   label: 'Dir',      get: t => t.direction,    render: t => <span className="text-gray-400">{t.direction}</span>, filterable: true, filterType: 'text' },
    { key: 'entry_price', label: 'Entry $',  get: t => t.entry_price,  render: t => <span className="font-mono text-gray-400">{t.entry_price != null ? Number(t.entry_price).toFixed(2) : '—'}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'exit_price',  label: 'Exit $',   get: t => t.exit_price,   render: t => <span className="font-mono text-gray-400">{t.exit_price != null ? Number(t.exit_price).toFixed(2) : '—'}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'pnl_usd',     label: 'P&L',      get: t => t.pnl_usd,      render: t => <span className={`font-mono ${pnlColor(t.pnl_usd)}`}>{fmtUsd(t.pnl_usd)}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'hold_minutes',label: 'Hold (m)', get: t => t.hold_minutes, render: t => <span className="text-gray-500">{t.hold_minutes != null ? Math.round(t.hold_minutes) : '—'}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'signal_type', label: 'Signal',   get: t => t.signal_type || '', render: t => <span className="text-gray-400">{t.signal_type || '—'}</span>, filterable: true, filterType: 'text' },
    { key: 'exit_reason', label: 'Reason',   get: t => t.exit_reason || '', render: t => <span className="text-gray-400">{t.exit_reason || '—'}</span>, filterable: true, filterType: 'text' },
    { key: 'exit_result', label: 'Result',   get: t => t.exit_result || '', filterable: true, filterType: 'text',
      render: t => <span className={t.exit_result === 'WIN' ? 'text-green-400' : t.exit_result === 'LOSS' ? 'text-red-400' : 'text-gray-500'}>{t.exit_result || '—'}</span> },
    { key: 'entry_time',  label: 'Entry Time', get: t => t.entry_time || '', render: t => <span className="text-gray-500 text-[11px]">{t.entry_time ? t.entry_time.replace('T', ' ').slice(0, 19) : '—'}</span>, filterable: true, filterType: 'text' },
  ], []);

  const { processed, sortKey, sortDir, toggleSort, filters, setFilter } =
    useSortableFilterable(trades, cols, serverSort, serverFilter);
  const hasFilters = Object.values(filters).some(v => v?.trim());

  return (
    <>
      {(hasFilters || serverSort) && (
        <div className="px-4 py-1.5 text-xs text-gray-500 bg-[#0d1117] border-b border-[#21262d]">
          {serverSort && <span className="text-gray-600 mr-2">Sort is server-side (entire dataset).</span>}
          {hasFilters && <>
            Showing <span className="text-gray-300">{processed.length}</span> of {trades.length} on this page
            <button onClick={() => Object.keys(filters).forEach(k => setFilter(k, ''))}
                    className="ml-2 text-blue-400 hover:text-blue-300">(clear)</button>
          </>}
        </div>
      )}
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-[#21262d]">
          <tr>
            {cols.map(c =>
              <SortHeader key={c.key} col={c} sortKey={sortKey} sortDir={sortDir}
                          onClick={() => toggleSort(c.key)} />
            )}
          </tr>
          <tr className="bg-[#161b22]">
            {cols.map(c =>
              <th key={c.key} className="px-2 py-1 border-b border-[#21262d]">
                {c.filterable ? (
                  <input value={filters[c.key] || ''}
                         onChange={e => setFilter(c.key, e.target.value)}
                         placeholder={c.filterType === 'number' ? '>0' : 'filter'}
                         className="w-full px-1.5 py-0.5 text-xs bg-[#0d1117] border border-[#30363d] rounded text-gray-300 placeholder:text-gray-600" />
                ) : null}
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {processed.map(t => (
            <tr key={t.id} className="border-b border-[#21262d] hover:bg-[#1c2128]">
              {cols.map(c =>
                <td key={c.key} className={`px-3 py-1.5 ${c.align === 'right' ? 'text-right' : ''}`}>
                  {c.render(t)}
                </td>
              )}
            </tr>
          ))}
          {processed.length === 0 && (
            <tr><td colSpan={cols.length} className="px-3 py-8 text-center text-gray-500">
              {trades.length === 0 ? 'No trades loaded for this page.' : 'No trades match the current filters.'}
            </td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}


// ─────────────────────────────────────────────────────────
// AnalyticsPanel — cross-run slice/dice from /backtests/analytics/cross_run
// ─────────────────────────────────────────────────────────

interface CrossRunAnalytics {
  by_ticker_strategy: {
    ticker: string; strategy: string; trades: number; pnl: number;
    wins: number; decided: number; win_rate: number; runs: number;
  }[];
  by_strategy: {
    strategy: string; trades: number; pnl: number; wins: number;
    decided: number; win_rate: number; runs: number;
  }[];
  by_ticker: {
    ticker: string; trades: number; pnl: number; wins: number;
    decided: number; win_rate: number; strategies: string[];
  }[];
  top_runs: {
    id: number; name: string | null; strategy: string; tickers: string[];
    trades: number; pnl: number; win_rate: number;
    profit_factor: number | null; max_drawdown: number;
    created_at: string | null;
  }[];
  bottom_runs: CrossRunAnalytics['top_runs'];
  run_count: number;
  trade_count: number;
}

type AnalyticsView = 'charts' | 'tables' | 'features';

interface FeatureRow {
  feature: string;
  n_total: number;
  n_wins: number;
  n_losses: number;
  win_mean: number | null;
  loss_mean: number | null;
  edge: number | null;
  quartile_win_rates: { label: string; count: number; win_rate: number }[];
  quartile_spread: number | null;
}

function ChartCard({ title, children, hint }: {
  title: string; children: React.ReactNode; hint?: string;
}) {
  return (
    <div className="bg-[#0d1117] border border-[#30363d] rounded p-3">
      <div className="flex items-baseline justify-between mb-2">
        <h4 className="text-xs text-gray-400 font-semibold">{title}</h4>
        {hint && <span className="text-[10px] text-gray-600">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

function StatBox({ label, value, sub, color }: {
  label: string; value: string; sub?: string; color?: string;
}) {
  const clr = color === 'green' ? 'text-green-400' : color === 'red' ? 'text-red-400' : 'text-gray-200';
  return (
    <div className="bg-[#0d1117] border border-[#30363d] rounded p-3">
      <div className="text-[11px] text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={`text-lg font-bold ${clr}`}>{value}</div>
      {sub && <div className="text-[11px] text-gray-500 mt-0.5">{sub}</div>}
    </div>
  );
}

// Applied filter used to drive the runs table from chart/table clicks.
interface RunsFilter {
  strategy?: string;
  ticker?: string;
}

function AnalyticsPanel({ onOpenRun, onApplyFilter, filter }: {
  onOpenRun: (id: number) => void;
  onApplyFilter: (filter: RunsFilter) => void;
  filter: RunsFilter;   // drives re-fetch so charts/KPIs re-slice
}) {
  const [open, setOpen] = useState(true);
  const [data, setData] = useState<CrossRunAnalytics | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [view, setView] = useState<AnalyticsView>('charts');

  const load = () => {
    setLoading(true);
    setErr(null);
    const qs = new URLSearchParams();
    qs.set('limit_runs', '500');
    if (filter.strategy) qs.set('strategy', filter.strategy);
    if (filter.ticker)   qs.set('ticker',   filter.ticker);
    fetch(`/api/backtests/analytics/cross_run?${qs.toString()}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { setData(d); setLoading(false); })
      .catch(e => { setErr(e.message); setLoading(false); });
  };

  // Refetch whenever the panel opens or the filter changes.
  useEffect(() => {
    if (open) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, filter.strategy, filter.ticker]);

  const tsCols: ColDef<CrossRunAnalytics['by_ticker_strategy'][0]>[] = useMemo(() => [
    { key: 'ticker',   label: 'Ticker',   get: r => r.ticker,   render: r => <span className="text-gray-200">{r.ticker}</span>, filterable: true, filterType: 'text' },
    { key: 'strategy', label: 'Strategy', get: r => r.strategy, render: r => <span className="text-gray-400">{r.strategy}</span>, filterable: true, filterType: 'text' },
    { key: 'trades',   label: 'Trades',   get: r => r.trades,   render: r => r.trades, filterable: true, filterType: 'number', align: 'right' },
    { key: 'pnl',      label: 'P&L',      get: r => r.pnl,      render: r => <span className={`font-mono ${pnlColor(r.pnl)}`}>{fmtUsd(r.pnl)}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'win_rate', label: 'Win%',     get: r => r.win_rate, render: r => `${r.win_rate.toFixed(1)}%`, filterable: true, filterType: 'number', align: 'right' },
    { key: 'runs',     label: 'Runs',     get: r => r.runs,     render: r => <span className="text-gray-500">{r.runs}</span>, filterable: true, filterType: 'number', align: 'right' },
  ], []);

  const sCols: ColDef<CrossRunAnalytics['by_strategy'][0]>[] = useMemo(() => [
    { key: 'strategy', label: 'Strategy', get: r => r.strategy, render: r => <span className="text-gray-200">{r.strategy}</span>, filterable: true, filterType: 'text' },
    { key: 'runs',     label: 'Runs',     get: r => r.runs,     render: r => r.runs, filterable: true, filterType: 'number', align: 'right' },
    { key: 'trades',   label: 'Trades',   get: r => r.trades,   render: r => r.trades, filterable: true, filterType: 'number', align: 'right' },
    { key: 'pnl',      label: 'Total P&L', get: r => r.pnl,     render: r => <span className={`font-mono ${pnlColor(r.pnl)}`}>{fmtUsd(r.pnl)}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'win_rate', label: 'Win%',     get: r => r.win_rate, render: r => `${r.win_rate.toFixed(1)}%`, filterable: true, filterType: 'number', align: 'right' },
    { key: 'avg_pnl',  label: 'P&L/Trade', get: r => r.trades ? r.pnl / r.trades : 0, render: r => <span className={`font-mono ${pnlColor(r.trades ? r.pnl / r.trades : 0)}`}>{fmtUsd(r.trades ? r.pnl / r.trades : 0)}</span>, filterable: true, filterType: 'number', align: 'right' },
  ], []);

  const tCols: ColDef<CrossRunAnalytics['by_ticker'][0]>[] = useMemo(() => [
    { key: 'ticker',   label: 'Ticker',     get: r => r.ticker,     render: r => <span className="text-gray-200">{r.ticker}</span>, filterable: true, filterType: 'text' },
    { key: 'trades',   label: 'Trades',     get: r => r.trades,     render: r => r.trades, filterable: true, filterType: 'number', align: 'right' },
    { key: 'pnl',      label: 'Total P&L',  get: r => r.pnl,        render: r => <span className={`font-mono ${pnlColor(r.pnl)}`}>{fmtUsd(r.pnl)}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'win_rate', label: 'Win%',       get: r => r.win_rate,   render: r => `${r.win_rate.toFixed(1)}%`, filterable: true, filterType: 'number', align: 'right' },
    { key: 'strategies', label: 'Strategies', get: r => r.strategies.join(','), render: r => <span className="text-gray-500 text-[11px]">{r.strategies.join(', ')}</span>, filterable: true, filterType: 'text' },
  ], []);

  const rCols: ColDef<CrossRunAnalytics['top_runs'][0]>[] = useMemo(() => [
    { key: 'id',        label: 'ID',       get: r => r.id,       render: r => <button onClick={e => { e.stopPropagation(); onOpenRun(r.id); }} className="text-blue-400 hover:underline">#{r.id}</button>, filterable: true, filterType: 'number' },
    { key: 'strategy',  label: 'Strategy', get: r => r.strategy, render: r => <span className="text-gray-400">{r.strategy}</span>, filterable: true, filterType: 'text' },
    { key: 'tickers',   label: 'Tickers',  get: r => (r.tickers || []).join(','), render: r => <span className="text-gray-500 text-[11px]">{(r.tickers || []).slice(0, 3).join(', ')}{(r.tickers || []).length > 3 ? `… (+${r.tickers.length - 3})` : ''}</span>, filterable: true, filterType: 'text' },
    { key: 'trades',    label: 'Trades',   get: r => r.trades,   render: r => r.trades, filterable: true, filterType: 'number', align: 'right' },
    { key: 'pnl',       label: 'P&L',      get: r => r.pnl,      render: r => <span className={`font-mono ${pnlColor(r.pnl)}`}>{fmtUsd(r.pnl)}</span>, filterable: true, filterType: 'number', align: 'right' },
    { key: 'win_rate',  label: 'Win%',     get: r => r.win_rate, render: r => `${r.win_rate.toFixed(1)}%`, filterable: true, filterType: 'number', align: 'right' },
    { key: 'profit_factor', label: 'PF',   get: r => r.profit_factor ?? null, render: r => r.profit_factor != null ? r.profit_factor.toFixed(2) : '—', filterable: true, filterType: 'number', align: 'right' },
    { key: 'max_drawdown', label: 'Max DD', get: r => r.max_drawdown, render: r => <span className={`font-mono ${pnlColor(r.max_drawdown)}`}>{fmtUsd(r.max_drawdown)}</span>, filterable: true, filterType: 'number', align: 'right' },
  ], [onOpenRun]);

  // Chart datasets
  const strategyChart = useMemo(() => (data?.by_strategy || []).map(r => ({
    name: r.strategy, pnl: r.pnl, trades: r.trades, win_rate: r.win_rate, runs: r.runs,
  })), [data]);

  const tickerChart = useMemo(() =>
    [...(data?.by_ticker || [])]
      .sort((a, b) => b.pnl - a.pnl)
      .slice(0, 20)
      .map(r => ({ name: r.ticker, pnl: r.pnl, trades: r.trades, win_rate: r.win_rate })),
  [data]);

  const tsChart = useMemo(() =>
    [...(data?.by_ticker_strategy || [])]
      .sort((a, b) => b.pnl - a.pnl)
      .slice(0, 20)
      .map(r => ({
        name: `${r.ticker}/${r.strategy}`, ticker: r.ticker, strategy: r.strategy,
        pnl: r.pnl, trades: r.trades, win_rate: r.win_rate,
      })),
  [data]);

  const topRunsChart = useMemo(() =>
    (data?.top_runs || []).slice(0, 15).map(r => ({
      name: `#${r.id} ${r.strategy}`, id: r.id, strategy: r.strategy,
      pnl: r.pnl, trades: r.trades, win_rate: r.win_rate,
    })),
  [data]);

  // Aggregate stats
  const stats = useMemo(() => {
    if (!data) return null;
    const totalPnl = (data.by_strategy || []).reduce((s, r) => s + r.pnl, 0);
    const totalTrades = data.trade_count;
    const totalDecided = (data.by_strategy || []).reduce((s, r) => s + r.decided, 0);
    const totalWins = (data.by_strategy || []).reduce((s, r) => s + r.wins, 0);
    const overallWR = totalDecided ? (100 * totalWins) / totalDecided : 0;
    const bestStrat = [...(data.by_strategy || [])].sort((a, b) => b.pnl - a.pnl)[0];
    const bestTicker = [...(data.by_ticker || [])].sort((a, b) => b.pnl - a.pnl)[0];
    return { totalPnl, totalTrades, overallWR, bestStrat, bestTicker };
  }, [data]);

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-lg">
      <div className="px-4 py-2 border-b border-[#30363d] flex items-center gap-3">
        <button onClick={() => setOpen(o => !o)} className="text-gray-400 hover:text-white text-sm">
          {open ? '▼' : '▶'} <span className="font-semibold">Analytics</span>
        </button>
        <span className="text-xs text-gray-500">
          {data ? `${data.run_count} runs · ${data.trade_count.toLocaleString()} trades aggregated` : 'cross-run slice/dice'}
        </span>
        {(filter.strategy || filter.ticker) && (
          <span className="text-[11px] text-blue-300 bg-blue-500/10 border border-blue-500/30 rounded px-2 py-0.5">
            Scoped to
            {filter.strategy && <> strategy=<b>{filter.strategy}</b></>}
            {filter.strategy && filter.ticker && ' · '}
            {filter.ticker && <>ticker=<b>{filter.ticker}</b></>}
          </span>
        )}
        <button onClick={load} disabled={loading}
                className="ml-auto px-2 py-0.5 text-xs bg-[#21262d] border border-[#30363d] text-gray-400 rounded hover:text-white">
          {loading ? 'Loading...' : 'Refresh'}
        </button>
      </div>
      {open && (
        <div className="p-3 space-y-4">
          {err && <div className="text-red-400 text-xs">Error: {err}</div>}
          {/* View switcher */}
          <div className="flex items-center gap-1 flex-wrap">
            {(['charts', 'tables', 'features'] as AnalyticsView[]).map(v => (
              <button key={v} onClick={() => setView(v)}
                      className={`px-2.5 py-1 text-xs rounded ${
                        view === v
                          ? 'bg-blue-500/20 text-blue-400 border border-blue-500/40'
                          : 'bg-[#21262d] border border-[#30363d] text-gray-400 hover:text-white'
                      }`}>
                {v === 'charts' ? 'Charts' : v === 'tables' ? 'Tables' : 'Feature Importance'}
              </button>
            ))}
            <span className="text-xs text-gray-600 ml-2">
              {view === 'charts'
                ? 'Click any bar to drill down into matching trades'
                : view === 'tables'
                ? 'Sort/filter any column; click a run ID to open it'
                : 'Which entry/exit indicators actually predict WIN vs LOSS across all trades'}
            </span>
          </div>

          {!data && !loading && <div className="text-gray-500 text-sm py-4 text-center">No data loaded.</div>}

          {data && view === 'charts' && (
            <>
              {/* Stat boxes */}
              {stats && (
                <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                  <StatBox label="Total P&L" value={fmtUsd(stats.totalPnl)}
                           color={stats.totalPnl >= 0 ? 'green' : 'red'}
                           sub={`${data.run_count} runs`} />
                  <StatBox label="Total Trades" value={stats.totalTrades.toLocaleString()} />
                  <StatBox label="Overall Win%" value={`${stats.overallWR.toFixed(1)}%`}
                           color={stats.overallWR >= 50 ? 'green' : 'red'} />
                  <StatBox label="Best Strategy"
                           value={stats.bestStrat ? stats.bestStrat.strategy : '—'}
                           color="green"
                           sub={stats.bestStrat ? fmtUsd(stats.bestStrat.pnl) : undefined} />
                  <StatBox label="Best Ticker"
                           value={stats.bestTicker ? stats.bestTicker.ticker : '—'}
                           color="green"
                           sub={stats.bestTicker ? fmtUsd(stats.bestTicker.pnl) : undefined} />
                </div>
              )}

              {/* Row 1: By Strategy + Top Tickers */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <ChartCard title="P&L by Strategy" hint="click to filter runs table">
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={strategyChart}
                              onClick={(e: any) => {
                                const p = e?.activePayload?.[0]?.payload;
                                if (p?.name) onApplyFilter({ strategy: p.name });
                              }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
                      <XAxis dataKey="name" tick={{ fill: '#8b949e', fontSize: 11 }} />
                      <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
                      <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                               formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
                      <Bar dataKey="pnl" cursor="pointer">
                        {strategyChart.map((e, i) => (
                          <Cell key={i} fill={e.pnl >= 0 ? CHART_COLORS.green : CHART_COLORS.red} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </ChartCard>

                <ChartCard title="Top 20 Tickers by P&L" hint="click to filter runs table">
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={tickerChart}
                              onClick={(e: any) => {
                                const p = e?.activePayload?.[0]?.payload;
                                if (p?.name) onApplyFilter({ ticker: p.name });
                              }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
                      <XAxis dataKey="name" tick={{ fill: '#8b949e', fontSize: 10 }} angle={-45} textAnchor="end" height={60} />
                      <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
                      <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                               formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
                      <Bar dataKey="pnl" cursor="pointer">
                        {tickerChart.map((e, i) => (
                          <Cell key={i} fill={e.pnl >= 0 ? CHART_COLORS.green : CHART_COLORS.red} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </ChartCard>
              </div>

              {/* Row 2: Top (ticker × strategy) + Top Runs */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <ChartCard title="Top 20 (Ticker × Strategy)" hint="click to filter runs table">
                  <ResponsiveContainer width="100%" height={260}>
                    <BarChart data={tsChart} layout="vertical"
                              onClick={(e: any) => {
                                const p = e?.activePayload?.[0]?.payload;
                                if (p?.ticker && p?.strategy) onApplyFilter({
                                  ticker: p.ticker, strategy: p.strategy,
                                });
                              }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
                      <XAxis type="number" tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
                      <YAxis type="category" dataKey="name" tick={{ fill: '#8b949e', fontSize: 10 }} width={110} />
                      <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                               formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
                      <Bar dataKey="pnl" cursor="pointer">
                        {tsChart.map((e, i) => (
                          <Cell key={i} fill={e.pnl >= 0 ? CHART_COLORS.green : CHART_COLORS.red} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </ChartCard>

                <ChartCard title="Top 15 Individual Runs" hint="click to open run drill-down">
                  <ResponsiveContainer width="100%" height={260}>
                    <BarChart data={topRunsChart} layout="vertical"
                              onClick={(e: any) => {
                                const p = e?.activePayload?.[0]?.payload;
                                if (p?.id != null) onOpenRun(p.id);
                              }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
                      <XAxis type="number" tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
                      <YAxis type="category" dataKey="name" tick={{ fill: '#8b949e', fontSize: 10 }} width={110} />
                      <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                               formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
                      <Bar dataKey="pnl" fill={CHART_COLORS.blue} cursor="pointer" />
                    </BarChart>
                  </ResponsiveContainer>
                </ChartCard>
              </div>
            </>
          )}

          {data && view === 'tables' && (
            <div className="space-y-4">
              <div>
                <div className="text-xs text-gray-400 font-semibold mb-1">
                  Ticker × Strategy <span className="text-gray-600 font-normal">(click row → filter runs table)</span>
                </div>
                <AnalyticsTable rows={data.by_ticker_strategy} cols={tsCols} empty="No pairs."
                                onRowClick={r => onApplyFilter({ ticker: r.ticker, strategy: r.strategy })} />
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <div className="text-xs text-gray-400 font-semibold mb-1">
                    By Strategy <span className="text-gray-600 font-normal">(click → filter runs)</span>
                  </div>
                  <AnalyticsTable rows={data.by_strategy} cols={sCols} empty="No strategies."
                                  onRowClick={r => onApplyFilter({ strategy: r.strategy })} />
                </div>
                <div>
                  <div className="text-xs text-gray-400 font-semibold mb-1">
                    By Ticker <span className="text-gray-600 font-normal">(click → filter runs)</span>
                  </div>
                  <AnalyticsTable rows={data.by_ticker} cols={tCols} empty="No tickers."
                                  onRowClick={r => onApplyFilter({ ticker: r.ticker })} />
                </div>
              </div>
              <div>
                <div className="text-xs text-gray-400 font-semibold mb-1">Top 20 runs by P&L</div>
                <AnalyticsTable rows={data.top_runs} cols={rCols} empty="No winning runs." />
              </div>
              <div>
                <div className="text-xs text-gray-400 font-semibold mb-1">Bottom 20 runs by P&L</div>
                <AnalyticsTable rows={data.bottom_runs} cols={rCols} empty="No losing runs." />
              </div>
            </div>
          )}

          {view === 'features' && (
            <FeatureImportanceView filter={filter} />
          )}
        </div>
      )}
    </div>
  );
}


// ─────────────────────────────────────────────────────────
// FeatureImportanceView — #1: which indicators actually predict WIN?
// ─────────────────────────────────────────────────────────

function FeatureImportanceView({ filter }: { filter: RunsFilter }) {
  const [source, setSource] = useState<'entry' | 'exit'>('entry');
  const [minTrades, setMinTrades] = useState<number>(500);
  const [data, setData] = useState<{
    features: FeatureRow[];
    total_trades: number;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true); setErr(null);
    const qs = new URLSearchParams();
    qs.set('source', source);
    qs.set('min_trades', String(minTrades));
    qs.set('limit', '40');
    if (filter.strategy) qs.set('strategy', filter.strategy);
    if (filter.ticker)   qs.set('ticker',   filter.ticker);
    fetch(`/api/backtests/analytics/feature_importance?${qs.toString()}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { setData(d); setLoading(false); })
      .catch(e => { setErr(e.message); setLoading(false); });
  }, [source, minTrades, filter.strategy, filter.ticker]);

  return (
    <div className="space-y-3">
      {/* Controls */}
      <div className="flex items-center gap-3 flex-wrap bg-[#0d1117] border border-[#30363d] rounded p-2">
        <div className="flex items-center gap-1">
          <span className="text-xs text-gray-500 mr-1">Indicator source:</span>
          {(['entry', 'exit'] as const).map(s => (
            <button key={s} onClick={() => setSource(s)}
                    className={`px-2 py-0.5 text-xs rounded ${
                      source === s
                        ? 'bg-blue-500/20 text-blue-400 border border-blue-500/40'
                        : 'bg-[#21262d] text-gray-400 hover:text-white'
                    }`}>
              {s === 'entry' ? 'Entry indicators' : 'Exit indicators'}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-500">
          <span>Min trades/feature:</span>
          <input type="number" min={1} max={10000} value={minTrades}
                 onChange={e => setMinTrades(Math.max(1, parseInt(e.target.value) || 1))}
                 className="w-20 px-1.5 py-0.5 bg-[#21262d] border border-[#30363d] rounded text-gray-300" />
        </div>
        <span className="text-xs text-gray-600 ml-auto">
          {data ? `${data.total_trades.toLocaleString()} trades · ${data.features.length} informative features` : '—'}
        </span>
      </div>

      {err && <div className="text-red-400 text-xs">Error: {err}</div>}
      {loading && !data && <div className="text-gray-500 text-sm text-center py-6">Loading…</div>}

      {data && data.features.length === 0 && (
        <div className="text-gray-500 text-sm text-center py-6">
          No features pass the min_trades cutoff. Try lowering it, or switch indicator source.
        </div>
      )}

      {data && data.features.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs text-gray-500 px-2">
            Sorted by <b>quartile spread</b> (biggest gap between best and worst quartile's win rate).
            A spread &gt;10% means the feature is actionable.
          </div>
          {data.features.map(f => <FeatureRowCard key={f.feature} f={f} />)}
        </div>
      )}
    </div>
  );
}

function FeatureRowCard({ f }: { f: FeatureRow }) {
  const hasQuartiles = f.quartile_win_rates.length === 4;
  const spread = f.quartile_spread ?? 0;
  const spreadColor = spread >= 15 ? 'text-green-400'
                     : spread >= 8  ? 'text-yellow-400'
                     : 'text-gray-500';

  return (
    <div className="bg-[#0d1117] border border-[#30363d] rounded p-3">
      <div className="flex items-baseline gap-3 mb-2 flex-wrap">
        <div className="text-sm font-semibold text-gray-200">{f.feature}</div>
        <div className={`text-xs font-mono ${spreadColor}`}>
          spread = {f.quartile_spread != null ? `${f.quartile_spread.toFixed(1)}%` : '—'}
        </div>
        <div className="text-xs text-gray-500">
          n={f.n_total.toLocaleString()} · wins={f.n_wins} · losses={f.n_losses}
        </div>
        <div className="text-xs text-gray-500 ml-auto">
          WIN mean: <span className="text-gray-300 font-mono">{f.win_mean ?? '—'}</span>
          {' · '}
          LOSS mean: <span className="text-gray-300 font-mono">{f.loss_mean ?? '—'}</span>
        </div>
      </div>

      {hasQuartiles && (
        <div className="grid grid-cols-4 gap-2">
          {f.quartile_win_rates.map((b, i) => {
            const wr = b.win_rate;
            const barColor = wr >= 60 ? 'bg-green-500/40' : wr >= 50 ? 'bg-yellow-500/40' : 'bg-red-500/40';
            const textColor = wr >= 60 ? 'text-green-300' : wr >= 50 ? 'text-yellow-300' : 'text-red-300';
            return (
              <div key={i} className="bg-[#161b22] border border-[#30363d] rounded p-2">
                <div className="text-[10px] text-gray-500 font-mono truncate" title={b.label}>{b.label}</div>
                <div className={`text-lg font-bold ${textColor}`}>{wr.toFixed(1)}%</div>
                <div className="text-[10px] text-gray-600">n={b.count}</div>
                <div className={`h-1 mt-1 ${barColor} rounded`}
                     style={{ width: `${Math.min(100, wr)}%` }} />
              </div>
            );
          })}
        </div>
      )}
      {!hasQuartiles && (
        <div className="text-[11px] text-gray-600">
          Not enough samples for quartile breakdown (need ≥8).
        </div>
      )}
    </div>
  );
}

function AnalyticsTable<T extends { [k: string]: any }>({ rows, cols, empty, onRowClick }: {
  rows: T[]; cols: ColDef<T>[]; empty: string;
  onRowClick?: (row: T) => void;
}) {
  const { processed, sortKey, sortDir, toggleSort, filters, setFilter } =
    useSortableFilterable(rows, cols);
  const hasFilters = Object.values(filters).some(v => v?.trim());
  return (
    <div className="overflow-x-auto">
      {hasFilters && (
        <div className="px-2 py-1 text-xs text-gray-500">
          Showing <span className="text-gray-300">{processed.length}</span> of {rows.length}
          <button onClick={() => Object.keys(filters).forEach(k => setFilter(k, ''))}
                  className="ml-2 text-blue-400 hover:text-blue-300">(clear)</button>
        </div>
      )}
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-[#21262d]">
            {cols.map(c =>
              <SortHeader key={c.key} col={c} sortKey={sortKey} sortDir={sortDir}
                          onClick={() => toggleSort(c.key)} />
            )}
          </tr>
          <tr className="bg-[#161b22]">
            {cols.map(c =>
              <th key={c.key} className="px-2 py-1 border-b border-[#21262d]">
                {c.filterable ? (
                  <input value={filters[c.key] || ''}
                         onChange={e => setFilter(c.key, e.target.value)}
                         placeholder={c.filterType === 'number' ? '>0' : 'filter'}
                         className="w-full px-1.5 py-0.5 text-xs bg-[#0d1117] border border-[#30363d] rounded text-gray-300 placeholder:text-gray-600" />
                ) : null}
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {processed.map((r, i) => (
            <tr key={i}
                onClick={() => onRowClick?.(r)}
                className={`border-b border-[#21262d] hover:bg-[#1c2128] ${onRowClick ? 'cursor-pointer' : ''}`}>
              {cols.map(c =>
                <td key={c.key} className={`px-3 py-1.5 ${c.align === 'right' ? 'text-right' : ''}`}>
                  {c.render(r)}
                </td>
              )}
            </tr>
          ))}
          {processed.length === 0 && (
            <tr><td colSpan={cols.length} className="px-3 py-6 text-center text-gray-500">
              {rows.length === 0 ? empty : 'No rows match the filters.'}
            </td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}


// ─────────────────────────────────────────────────────────
// TradesModal — single unified drill-down modal used everywhere.
// Two modes:
//   • source = 'run'      → paginated, trades fetched per-page
//   • source = 'analytics' → in-memory trades (pre-loaded by chart click)
// Consistent header, close behavior, and TradesTable regardless of source.
// ─────────────────────────────────────────────────────────

interface TradesModalProps {
  title: string;
  subtitle?: React.ReactNode;
  onClose: () => void;
  // Run-mode
  runId?: number | null;
  // Analytics-mode
  analyticsFilters?: { strategy?: string; ticker?: string; run_id?: number; outcome?: string };
}

function TradesModal(props: TradesModalProps) {
  const { title, subtitle, onClose, runId, analyticsFilters } = props;
  const isRun = runId != null;
  const pageSize = 100;

  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState<'all' | 'WIN' | 'LOSS'>('all');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Server-side sort state (re-queries on change)
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);
  // Server-side column filters (debounced)
  const [columnFilters, setColumnFilters] = useState<Record<string, string>>({});
  const [debouncedCF, setDebouncedCF] = useState<Record<string, string>>({});
  useEffect(() => {
    const t = setTimeout(() => setDebouncedCF(columnFilters), 300);
    return () => clearTimeout(t);
  }, [columnFilters]);

  useEffect(() => {
    setLoading(true); setErr(null);
    const cfSpecs = Object.entries(debouncedCF)
      .filter(([, v]) => v?.trim())
      .map(([k, v]) => `${k}:${v.trim()}`);
    const qs = new URLSearchParams();
    if (sortKey && sortDir) { qs.set('sort', sortKey); qs.set('direction', sortDir); }
    if (filter !== 'all') qs.set('outcome', filter);
    for (const s of cfSpecs) qs.append('filter', s);

    let url: string;
    if (isRun) {
      qs.set('limit', String(pageSize));
      qs.set('offset', String(page * pageSize));
      url = `/api/backtests/${runId}/trades?${qs.toString()}`;
    } else {
      qs.set('limit', '500');
      if (analyticsFilters?.strategy) qs.set('strategy', analyticsFilters.strategy);
      if (analyticsFilters?.ticker) qs.set('ticker', analyticsFilters.ticker);
      if (analyticsFilters?.run_id != null) qs.set('run_id', String(analyticsFilters.run_id));
      url = `/api/backtests/analytics/trades?${qs.toString()}`;
    }
    fetch(url)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => {
        setTrades(d.trades || []);
        setTotal(d.total || 0);
        setLoading(false);
      })
      .catch(e => { setErr(e.message); setLoading(false); });
  }, [isRun, runId, page, filter, sortKey, sortDir,
      JSON.stringify(debouncedCF), JSON.stringify(analyticsFilters || {})]);

  // Reset pagination when filter changes
  useEffect(() => { setPage(0); }, [JSON.stringify(debouncedCF), filter]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  // Close on ESC
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
         onClick={onClose}>
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg w-[95vw] max-w-6xl max-h-[90vh] flex flex-col shadow-2xl"
           onClick={e => e.stopPropagation()}>
        <div className="px-4 py-3 border-b border-[#30363d] flex items-center justify-between">
          <div>
            <h3 className="text-base font-semibold text-gray-200">{title}</h3>
            {subtitle && <div className="text-xs text-gray-500 mt-0.5">{subtitle}</div>}
          </div>
          <button onClick={onClose}
                  className="text-gray-500 hover:text-white text-2xl leading-none px-2"
                  aria-label="Close">&times;</button>
        </div>

        <div className="px-4 py-2 border-b border-[#30363d] flex items-center gap-2 flex-wrap">
          <div className="flex items-center gap-1">
            <span className="text-xs text-gray-500 mr-1">Outcome:</span>
            {(['all', 'WIN', 'LOSS'] as const).map(f => (
              <button key={f}
                      onClick={() => { setFilter(f); setPage(0); }}
                      className={`px-2 py-0.5 text-xs rounded ${
                        filter === f
                          ? 'bg-blue-500/20 text-blue-400'
                          : 'bg-[#21262d] text-gray-400 hover:text-white'
                      }`}>
                {f === 'all' ? 'All' : f}
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2">
            {isRun ? (
              <>
                <button onClick={() => setPage(p => Math.max(0, p - 1))}
                        disabled={page === 0}
                        className="px-2 py-0.5 text-xs rounded bg-[#21262d] border border-[#30363d] text-gray-400 disabled:text-gray-700 disabled:cursor-not-allowed">Prev</button>
                <span className="text-xs text-gray-500">
                  Page {page + 1} / {totalPages}
                  {' · '}
                  {total > 0 ? `${page * pageSize + 1}–${Math.min((page + 1) * pageSize, total)} of ${total}` : '0 rows'}
                </span>
                <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
                        disabled={page >= totalPages - 1}
                        className="px-2 py-0.5 text-xs rounded bg-[#21262d] border border-[#30363d] text-gray-400 disabled:text-gray-700 disabled:cursor-not-allowed">Next</button>
              </>
            ) : (
              <span className="text-xs text-gray-500">
                Showing top {trades.length} of {total} {total > trades.length ? '(sorted by P&L)' : ''}
              </span>
            )}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {err && <div className="px-4 py-3 text-xs text-red-400">{err}</div>}
          {loading && !err && <div className="px-4 py-6 text-xs text-gray-500 text-center">Loading…</div>}
          {!loading && !err && <TradesTable trades={trades} serverSort={{
            sortKey, sortDir,
            onChange: (k, d) => { setSortKey(k); setSortDir(d); setPage(0); },
          }} serverFilter={{
            filters: columnFilters,
            onChange: setColumnFilters,
          }} />}
        </div>

        <div className="px-4 py-2 border-t border-[#30363d] text-xs text-gray-600 text-center">
          Click outside, press ESC, or [X] to close
        </div>
      </div>
    </div>
  );
}


function LaunchDialog({ onClose, onLaunched, strategies }: {
  onClose: () => void;
  onLaunched: () => void;
  strategies: Strategy[];
}) {
  const defStrategy = strategies.find(s => s.is_default) || strategies[0];
  const today = new Date();
  const sixty = new Date(today.getTime() - 60 * 86_400_000);

  const [name, setName] = useState(`${defStrategy?.name || 'ict'} ${isoDate(today)}`);
  const [strategyName, setStrategyName] = useState(defStrategy?.name || 'ict');
  const [tickers, setTickers] = useState('QQQ,SPY,IWM');
  const [startDate, setStartDate] = useState(isoDate(sixty));
  const [endDate, setEndDate] = useState(isoDate(today));
  const [pnlTarget, setPnlTarget] = useState('1.00');
  const [stopLoss, setStopLoss] = useState('0.60');
  const [optionDTE, setOptionDTE] = useState('7');
  const [optionVol, setOptionVol] = useState('0.20');
  const [baseInterval, setBaseInterval] = useState('5m');

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const launch = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch('/api/backtests/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          strategy: strategyName,
          tickers: tickers.split(',').map(t => t.trim()).filter(Boolean),
          start_date: startDate,
          end_date: endDate,
          config: {
            profit_target: parseFloat(pnlTarget),
            stop_loss: parseFloat(stopLoss),
            option_dte_days: parseFloat(optionDTE),
            option_vol: parseFloat(optionVol),
            base_interval: baseInterval,
          },
        }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        setError(j.detail || `HTTP ${res.status}`);
        setSubmitting(false);
        return;
      }
      onLaunched();
      onClose();
    } catch (e: any) {
      setError(e?.message || 'launch failed');
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6 w-[500px]" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-bold">Run Backtest</h3>
          <button onClick={onClose} className="text-gray-500 hover:text-white text-xl">&times;</button>
        </div>
        <div className="space-y-3 text-sm">
          <div>
            <label className="block text-xs text-gray-400 mb-1">Name</label>
            <input value={name} onChange={e => setName(e.target.value)}
                   className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded" />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Strategy</label>
            <select value={strategyName} onChange={e => setStrategyName(e.target.value)}
                    className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded">
              {strategies.map(s =>
                <option key={s.strategy_id} value={s.name}>{s.display_name} ({s.name})</option>
              )}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Tickers (comma-separated)</label>
            <input value={tickers} onChange={e => setTickers(e.target.value)}
                   className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-400 mb-1">Start Date</label>
              <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)}
                     className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded" />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">End Date</label>
              <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)}
                     className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-xs text-gray-400 mb-1">Profit Target</label>
              <input value={pnlTarget} onChange={e => setPnlTarget(e.target.value)}
                     className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded" />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Stop Loss</label>
              <input value={stopLoss} onChange={e => setStopLoss(e.target.value)}
                     className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded" />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Interval</label>
              <select value={baseInterval} onChange={e => setBaseInterval(e.target.value)}
                      className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded">
                <option value="1m">1m</option>
                <option value="5m">5m</option>
                <option value="15m">15m</option>
                <option value="1h">1h</option>
              </select>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-400 mb-1">Option DTE (days)</label>
              <input value={optionDTE} onChange={e => setOptionDTE(e.target.value)}
                     className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded" />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Option Vol</label>
              <input value={optionVol} onChange={e => setOptionVol(e.target.value)}
                     className="w-full px-2 py-1 bg-[#0d1117] border border-[#30363d] rounded" />
            </div>
          </div>
          {error && <div className="text-red-400 text-xs">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button onClick={onClose}
                    className="px-3 py-1.5 text-xs bg-[#21262d] border border-[#30363d] rounded">Cancel</button>
            <button onClick={launch} disabled={submitting}
                    className="px-3 py-1.5 text-xs bg-green-600 text-white rounded">
              {submitting ? 'Starting...' : 'Run'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


// ─────────────────────────────────────────────────────────
// Main tab — static, refresh button, two tables
// ─────────────────────────────────────────────────────────

export default function BacktestTab() {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [runsTotal, setRunsTotal] = useState(0);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [showLaunch, setShowLaunch] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Server-side sort state for the runs table
  const [runsSortKey, setRunsSortKey] = useState<string | null>(null);
  const [runsSortDir, setRunsSortDir] = useState<SortDir>(null);
  const [runsFilter, setRunsFilter] = useState<RunsFilter>({});
  // Server-side per-column filters on the runs table (debounced).
  const [runsColFilters, setRunsColFilters] = useState<Record<string, string>>({});
  const [debouncedRCF, setDebouncedRCF] = useState<Record<string, string>>({});
  useEffect(() => {
    const t = setTimeout(() => setDebouncedRCF(runsColFilters), 300);
    return () => clearTimeout(t);
  }, [runsColFilters]);
  const runsLimit = 100;

  const fetchRuns = () => {
    setErr(null);
    const qs = new URLSearchParams();
    qs.set('limit', String(runsLimit));
    if (runsSortKey && runsSortDir) {
      qs.set('sort', runsSortKey);
      qs.set('direction', runsSortDir);
    }
    if (runsFilter.strategy) qs.set('strategy', runsFilter.strategy);
    if (runsFilter.ticker) qs.set('ticker', runsFilter.ticker);
    for (const [k, v] of Object.entries(debouncedRCF)) {
      if (v?.trim()) qs.append('filter', `${k}:${v.trim()}`);
    }
    fetch(`/api/backtests?${qs.toString()}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { setRuns(d.runs || []); setRunsTotal(d.total || (d.runs?.length ?? 0)); })
      .catch(e => setErr(e.message));
  };

  const fetchStrategies = () => {
    fetch('/api/backtests/strategies')
      .then(r => r.ok ? r.json() : null)
      .then(d => setStrategies(d?.strategies || []))
      .catch(() => {});
  };

  useEffect(() => { fetchRuns(); },
            [runsSortKey, runsSortDir, runsFilter.strategy, runsFilter.ticker,
             JSON.stringify(debouncedRCF)]);
  useEffect(() => { fetchStrategies(); }, []);

  const applyFilter = (patch: RunsFilter) => {
    // Toggle semantics: clicking the already-applied value clears it.
    setRunsFilter(cur => {
      const next: RunsFilter = { ...cur };
      if (patch.strategy !== undefined) {
        next.strategy = cur.strategy === patch.strategy ? undefined : patch.strategy;
      }
      if (patch.ticker !== undefined) {
        next.ticker = cur.ticker === patch.ticker ? undefined : patch.ticker;
      }
      return next;
    });
  };

  const onRunClick = (id: number) => {
    setSelectedRunId(id);
  };

  const closeModal = () => {
    setSelectedRunId(null);
  };

  const deleteRun = async (id: number) => {
    if (!confirm(`Delete backtest run #${id}?`)) return;
    await fetch(`/api/backtests/${id}`, { method: 'DELETE' });
    if (selectedRunId === id) closeModal();
    fetchRuns();
  };

  const selectedRun = runs.find(r => r.id === selectedRunId);

  return (
    <div className="space-y-4">
      {/* Top bar: actions */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-3 flex items-center gap-2">
        <button onClick={() => setShowLaunch(true)}
                className="px-3 py-1.5 text-sm bg-green-600 text-white rounded font-medium">
          + Run Backtest
        </button>
        <button onClick={fetchRuns}
                className="px-3 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-300 rounded hover:text-white">
          Refresh
        </button>
        {err && <span className="text-red-400 text-xs ml-2">{err}</span>}
        <span className="ml-auto text-xs text-gray-500">{runs.length} runs</span>
      </div>

      {/* Cross-run Analytics — charts + tables with drill-down. */}
      <AnalyticsPanel onOpenRun={onRunClick} onApplyFilter={applyFilter}
                      filter={runsFilter} />

      {/* Active filter chips — drives the runs table query below */}
      <div id="backtest-runs-table">
        {(runsFilter.strategy || runsFilter.ticker) && (
          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 mb-2 flex items-center gap-2 flex-wrap">
            <span className="text-xs text-gray-500">Active filters:</span>
            {runsFilter.strategy && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-blue-500/20 border border-blue-500/40 text-blue-300 text-xs">
                strategy = <span className="font-semibold">{runsFilter.strategy}</span>
                <button onClick={() => setRunsFilter(f => ({ ...f, strategy: undefined }))}
                        className="ml-1 text-blue-300 hover:text-white">×</button>
              </span>
            )}
            {runsFilter.ticker && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-purple-500/20 border border-purple-500/40 text-purple-300 text-xs">
                ticker ∈ tickers = <span className="font-semibold">{runsFilter.ticker}</span>
                <button onClick={() => setRunsFilter(f => ({ ...f, ticker: undefined }))}
                        className="ml-1 text-purple-300 hover:text-white">×</button>
              </span>
            )}
            <button onClick={() => setRunsFilter({})}
                    className="text-xs text-gray-400 hover:text-white underline ml-auto">
              Clear all
            </button>
          </div>
        )}

        {/* Runs table — click any row to open the same TradesModal */}
        <RunsTable
          runs={runs}
          total={runsTotal}
          selectedRunId={selectedRunId}
          onRunClick={onRunClick}
          onDelete={deleteRun}
          sortKey={runsSortKey}
          sortDir={runsSortDir}
          onSortChange={(k, d) => { setRunsSortKey(k); setRunsSortDir(d); }}
          columnFilters={runsColFilters}
          onColumnFiltersChange={setRunsColFilters}
        />
      </div>

      {/* UNIFIED drill-down modal — one component for every entry point */}
      {selectedRunId != null && (
        <TradesModal
          runId={selectedRunId}
          title={`Run #${selectedRunId}${selectedRun?.name ? ` — ${selectedRun.name}` : ''}`}
          subtitle={selectedRun && (
            <>
              <span className="text-gray-300">{selectedRun.strategy_name || '—'}</span>
              {' · '}{selectedRun.tickers?.join(', ') || '—'}
              {' · '}{selectedRun.start_date} → {selectedRun.end_date}
              {' · '}<span className={pnlColor(selectedRun.total_pnl)}>{fmtUsd(selectedRun.total_pnl)}</span>
              {' · '}{Number(selectedRun.win_rate || 0).toFixed(1)}% win rate
            </>
          )}
          onClose={closeModal}
        />
      )}

      {showLaunch && (
        <LaunchDialog onClose={() => setShowLaunch(false)}
                      onLaunched={fetchRuns}
                      strategies={strategies} />
      )}
    </div>
  );
}
