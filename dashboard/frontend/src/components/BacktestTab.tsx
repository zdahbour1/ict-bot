import { useEffect, useMemo, useState } from 'react';

// Dead simple: one refresh button, static data, direct queries from the API.
// No polling. No auto-select. No charts. Just the data.

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

function useSortableFilterable<T>(rows: T[], cols: ColDef<T>[]) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);
  const [filters, setFilters] = useState<Record<string, string>>({});

  const toggleSort = (key: string) => {
    if (sortKey !== key) { setSortKey(key); setSortDir('asc'); return; }
    if (sortDir === 'asc') { setSortDir('desc'); return; }
    if (sortDir === 'desc') { setSortKey(null); setSortDir(null); return; }
    setSortDir('asc');
  };

  const setFilter = (key: string, value: string) => {
    setFilters(f => ({ ...f, [key]: value }));
  };

  const processed = useMemo(() => {
    let out = rows;
    // Filter
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
    // Sort
    if (sortKey && sortDir) {
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
  }, [rows, cols, filters, sortKey, sortDir]);

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

function RunsTable({ runs, selectedRunId, onRunClick, onDelete }: {
  runs: RunRow[];
  selectedRunId: number | null;
  onRunClick: (id: number) => void;
  onDelete: (id: number) => void;
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

  const { processed, sortKey, sortDir, toggleSort, filters, setFilter } =
    useSortableFilterable(runs, cols);
  const hasFilters = Object.values(filters).some(v => v?.trim());

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
      <div className="px-4 py-2 border-b border-[#30363d] flex items-center justify-between text-xs">
        <span className="text-gray-500">
          Showing <span className="text-gray-300">{processed.length}</span> of {runs.length} runs
          {hasFilters && <button onClick={() => Object.keys(filters).forEach(k => setFilter(k, ''))}
            className="ml-2 text-blue-400 hover:text-blue-300">(clear filters)</button>}
        </span>
        <span className="text-gray-600">Click a header to sort · Type in the filter row to filter (numbers accept &gt;N, &lt;N, &gt;=N, &lt;=N, =N)</span>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-[#21262d]">
            {cols.map(c => (
              <SortHeader key={c.key} col={c} sortKey={sortKey} sortDir={sortDir}
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

function TradesTable({ trades }: { trades: TradeRow[] }) {
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
    useSortableFilterable(trades, cols);
  const hasFilters = Object.values(filters).some(v => v?.trim());

  return (
    <>
      {hasFilters && (
        <div className="px-4 py-1.5 text-xs text-gray-500 bg-[#0d1117] border-b border-[#21262d]">
          Showing <span className="text-gray-300">{processed.length}</span> of {trades.length} trades on this page
          <button onClick={() => Object.keys(filters).forEach(k => setFilter(k, ''))}
                  className="ml-2 text-blue-400 hover:text-blue-300">(clear)</button>
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
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [tradesTotal, setTradesTotal] = useState(0);
  const [tradesPage, setTradesPage] = useState(0);
  const [tradesFilter, setTradesFilter] = useState<'all' | 'WIN' | 'LOSS'>('all');
  const [showLaunch, setShowLaunch] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pageSize = 100;

  const fetchRuns = () => {
    setErr(null);
    fetch('/api/backtests?limit=100')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => setRuns(d.runs || []))
      .catch(e => setErr(e.message));
  };

  const fetchStrategies = () => {
    fetch('/api/backtests/strategies')
      .then(r => r.ok ? r.json() : null)
      .then(d => setStrategies(d?.strategies || []))
      .catch(() => {});
  };

  const fetchTrades = (runId: number, page: number, filter: string) => {
    const outcomeParam = filter === 'all' ? '' : `&outcome=${filter}`;
    const url = `/api/backtests/${runId}/trades?limit=${pageSize}&offset=${page * pageSize}${outcomeParam}`;
    console.log('[BacktestTab] fetch trades:', url);
    fetch(url)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => {
        console.log(`[BacktestTab] trades ok: ${d?.trades?.length} rows / ${d?.total} total`);
        setTrades(d.trades || []);
        setTradesTotal(d.total || 0);
      })
      .catch(e => {
        console.error('[BacktestTab] trades fetch failed:', e);
        setErr(`Trades fetch failed: ${e.message}`);
      });
  };

  // Load on mount, only.
  useEffect(() => {
    fetchRuns();
    fetchStrategies();
  }, []);

  // When user clicks a row, fetch its trades
  useEffect(() => {
    if (selectedRunId != null) {
      fetchTrades(selectedRunId, tradesPage, tradesFilter);
    }
  }, [selectedRunId, tradesPage, tradesFilter]);

  const onRunClick = (id: number) => {
    console.log('[BacktestTab] row click runId=', id);
    setSelectedRunId(id);
    setTradesPage(0);
    setTradesFilter('all');
  };

  const deleteRun = async (id: number) => {
    if (!confirm(`Delete backtest run #${id}?`)) return;
    await fetch(`/api/backtests/${id}`, { method: 'DELETE' });
    if (selectedRunId === id) {
      setSelectedRunId(null);
      setTrades([]);
    }
    fetchRuns();
  };

  const totalPages = Math.max(1, Math.ceil(tradesTotal / pageSize));
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

      {/* Runs table: sortable columns + per-column filters. Sort on
          header click (toggle asc/desc). Filter inputs in a second
          row below the headers. Page scrolls naturally — no
          internal scroll container. */}
      <RunsTable
        runs={runs}
        selectedRunId={selectedRunId}
        onRunClick={onRunClick}
        onDelete={deleteRun}
      />

      {/* Trades modal — centered overlay popup, not below-the-fold.
          Opens on row click, close with [X] / ESC / backdrop click. */}
      {selectedRunId != null && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
             onClick={() => setSelectedRunId(null)}>
          <div className="bg-[#161b22] border border-[#30363d] rounded-lg w-[95vw] max-w-6xl max-h-[90vh] flex flex-col shadow-2xl"
               onClick={e => e.stopPropagation()}>
            {/* Modal header */}
            <div className="px-4 py-3 border-b border-[#30363d] flex items-center justify-between">
              <div>
                <h3 className="text-base font-semibold text-gray-200">
                  Run #{selectedRunId}{selectedRun?.name ? ` — ${selectedRun.name}` : ''}
                </h3>
                <div className="text-xs text-gray-500 mt-0.5">
                  {selectedRun && (
                    <>
                      <span className="text-gray-300">{selectedRun.strategy_name || '—'}</span>
                      {' · '}
                      {selectedRun.tickers?.join(', ') || '—'}
                      {' · '}
                      {selectedRun.start_date} → {selectedRun.end_date}
                      {' · '}
                      <span className={pnlColor(selectedRun.total_pnl)}>{fmtUsd(selectedRun.total_pnl)}</span>
                      {' · '}
                      {Number(selectedRun.win_rate || 0).toFixed(1)}% win rate
                      {' · '}
                      {tradesTotal} trades
                    </>
                  )}
                </div>
              </div>
              <button onClick={() => setSelectedRunId(null)}
                      className="text-gray-500 hover:text-white text-2xl leading-none px-2"
                      aria-label="Close">&times;</button>
            </div>

            {/* Modal toolbar: filter + pagination */}
            <div className="px-4 py-2 border-b border-[#30363d] flex items-center gap-2 flex-wrap">
              <div className="flex items-center gap-1">
                <span className="text-xs text-gray-500 mr-1">Filter:</span>
                {(['all', 'WIN', 'LOSS'] as const).map(f => (
                  <button key={f}
                          onClick={() => { setTradesFilter(f); setTradesPage(0); }}
                          className={`px-2 py-0.5 text-xs rounded ${
                            tradesFilter === f
                              ? 'bg-blue-500/20 text-blue-400'
                              : 'bg-[#21262d] text-gray-400 hover:text-white'
                          }`}>
                    {f === 'all' ? 'All' : f}
                  </button>
                ))}
              </div>
              <div className="ml-auto flex items-center gap-2">
                <button onClick={() => setTradesPage(p => Math.max(0, p - 1))}
                        disabled={tradesPage === 0}
                        className="px-2 py-0.5 text-xs rounded bg-[#21262d] border border-[#30363d] text-gray-400 disabled:text-gray-700 disabled:cursor-not-allowed">Prev</button>
                <span className="text-xs text-gray-500">
                  Page {tradesPage + 1} / {totalPages}
                  {' · '}
                  {tradesTotal > 0 ? `${tradesPage * pageSize + 1}–${Math.min((tradesPage + 1) * pageSize, tradesTotal)} of ${tradesTotal}` : '0 rows'}
                </span>
                <button onClick={() => setTradesPage(p => Math.min(totalPages - 1, p + 1))}
                        disabled={tradesPage >= totalPages - 1}
                        className="px-2 py-0.5 text-xs rounded bg-[#21262d] border border-[#30363d] text-gray-400 disabled:text-gray-700 disabled:cursor-not-allowed">Next</button>
              </div>
            </div>

            {/* Modal body — sortable + filterable trades table */}
            <div className="flex-1 overflow-y-auto">
              <TradesTable trades={trades} />
            </div>

            {/* Modal footer hint */}
            <div className="px-4 py-2 border-t border-[#30363d] text-xs text-gray-600 text-center">
              Click outside or press [X] to close
            </div>
          </div>
        </div>
      )}

      {showLaunch && (
        <LaunchDialog onClose={() => setShowLaunch(false)}
                      onLaunched={fetchRuns}
                      strategies={strategies} />
      )}
    </div>
  );
}
