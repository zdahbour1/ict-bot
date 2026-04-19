import { useEffect, useMemo, useState } from 'react';
import { useApi } from '../hooks/useApi';

// ─────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────

interface Strategy {
  strategy_id: number;
  name: string;
  display_name: string;
  enabled: boolean;
  is_default: boolean;
}

interface BacktestRun {
  id: number;
  name: string | null;
  status: string;
  strategy_id: number;
  strategy_name: string | null;
  tickers: string[];
  start_date: string | null;
  end_date: string | null;
  total_trades: number;
  wins: number;
  losses: number;
  scratches: number;
  total_pnl: number;
  win_rate: number;
  avg_win: number;
  avg_loss: number;
  max_drawdown: number;
  sharpe_ratio: number | null;
  profit_factor: number | null;
  avg_hold_min: number;
  duration_sec: number | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
  error_message: string | null;
}

interface BacktestTrade {
  id: number;
  ticker: string;
  symbol: string | null;
  direction: string;
  contracts: number;
  entry_price: number | null;
  exit_price: number | null;
  pnl_pct: number;
  pnl_usd: number;
  entry_time: string | null;
  exit_time: string | null;
  hold_minutes: number | null;
  signal_type: string | null;
  exit_reason: string | null;
  exit_result: string | null;
  tp_trailed: boolean;
  rolled: boolean;
  entry_indicators: Record<string, unknown>;
  exit_indicators: Record<string, unknown>;
  entry_context: Record<string, unknown>;
  signal_details: Record<string, unknown>;
}

interface Analytics {
  pnl_by_ticker: { ticker: string; trades: number; pnl: number; wins: number }[];
  by_reason: { reason: string; count: number; pnl: number }[];
  by_signal: { signal: string; count: number; wins: number; pnl: number }[];
  cum_pnl: { t: string; cum_pnl: number }[];
  hold_time_hist: { bucket_min: number; count: number }[];
  by_day_of_week: { dow: string; count: number; pnl: number; wins: number }[];
}

interface FeatureRow {
  feature: string;
  n_total: number;
  n_wins: number;
  n_losses: number;
  win_mean: number | null;
  loss_mean: number | null;
  edge: number | null;
  quartile_win_rates: { label: string; count: number; win_rate: number }[];
}

interface FeatureAnalysis {
  features: FeatureRow[];
  total_trades: number;
}

// ─────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────

function statusPillColor(s: string): string {
  const m: Record<string, string> = {
    pending: 'bg-yellow-500/20 text-yellow-400',
    running: 'bg-blue-500/20 text-blue-400 animate-pulse',
    completed: 'bg-green-500/20 text-green-400',
    failed: 'bg-red-500/20 text-red-400',
  };
  return m[s] || 'bg-gray-700 text-gray-400';
}

function fmtUsd(v: number | null | undefined): string {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : '';
  return `${s}$${v.toFixed(2)}`;
}

function pnlColor(v: number): string {
  return v > 0 ? 'text-green-400' : v < 0 ? 'text-red-400' : 'text-gray-400';
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

// ─────────────────────────────────────────────────────────
// Inline SVG charts (dependency-free)
// ─────────────────────────────────────────────────────────

function CumulativePnlChart({ data }: { data: Analytics['cum_pnl'] }) {
  if (!data.length) return <div className="text-sm text-gray-500 p-4">No trade timeline.</div>;
  const W = 520, H = 140, PAD = 20;
  const values = data.map(d => d.cum_pnl);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const step = (W - 2 * PAD) / Math.max(1, data.length - 1);
  const y = (v: number) => PAD + (H - 2 * PAD) * (1 - (v - min) / span);
  const points = data.map((d, i) => `${PAD + i * step},${y(d.cum_pnl)}`).join(' ');
  const zeroY = y(0);
  const finalColor = values[values.length - 1] >= 0 ? '#3fb950' : '#f85149';
  return (
    <svg width={W} height={H} className="overflow-visible">
      {min < 0 && max > 0 && (
        <line x1={PAD} x2={W - PAD} y1={zeroY} y2={zeroY}
              stroke="#30363d" strokeDasharray="3,3" />
      )}
      <polyline fill="none" stroke={finalColor} strokeWidth="2" points={points} />
      <text x={W - PAD} y={12} textAnchor="end" fontSize="10" fill="#8b949e">
        Final: {fmtUsd(values[values.length - 1])}
      </text>
    </svg>
  );
}

function BarRow({ label, value, max, count, color }:
  { label: string; value: number; max: number; count?: number; color?: string }) {
  const pct = max > 0 ? Math.abs(value) / max : 0;
  const fill = color || (value >= 0 ? '#3fb950' : '#f85149');
  return (
    <div className="flex items-center gap-2 text-xs py-1">
      <div className="w-20 text-gray-400 truncate">{label}</div>
      <div className="flex-1 bg-[#21262d] h-3 rounded-sm relative overflow-hidden">
        <div className="absolute left-0 top-0 h-full"
             style={{ width: `${pct * 100}%`, background: fill }} />
      </div>
      <div className={`w-24 text-right font-mono ${pnlColor(value)}`}>
        {fmtUsd(value)}{count != null && <span className="text-gray-500 ml-1">({count})</span>}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────
// Launch dialog
// ─────────────────────────────────────────────────────────

function LaunchDialog({ onClose, onLaunched, strategies }:
  { onClose: () => void; onLaunched: () => void; strategies: Strategy[] }) {
  const defaultStrategy = strategies.find(s => s.is_default) || strategies[0];
  const today = new Date();
  const sixtyDaysAgo = new Date(today.getTime() - 60 * 86_400_000);

  const [name, setName] = useState(
    `${defaultStrategy?.name || 'ict'} ${isoDate(today)}`
  );
  const [strategyName, setStrategyName] = useState(defaultStrategy?.name || 'ict');
  const [tickers, setTickers] = useState('QQQ,SPY');
  const [startDate, setStartDate] = useState(isoDate(sixtyDaysAgo));
  const [endDate, setEndDate] = useState(isoDate(today));
  const [pnlTarget, setPnlTarget] = useState('1.00');
  const [stopLoss, setStopLoss] = useState('0.60');
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
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
         onClick={onClose}>
      <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-6 w-[480px]"
           onClick={e => e.stopPropagation()}>
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
                <option key={s.strategy_id} value={s.name}>
                  {s.display_name} ({s.name})
                </option>
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
                <option value="1m">1m (last 7 days)</option>
                <option value="5m">5m (last 60 days)</option>
                <option value="15m">15m (last 60 days)</option>
                <option value="1h">1h (last 2 years)</option>
              </select>
            </div>
          </div>
          {error && <div className="text-red-400 text-xs">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button onClick={onClose}
                    className="px-3 py-1.5 text-xs bg-[#21262d] border border-[#30363d] rounded hover:text-white">
              Cancel
            </button>
            <button onClick={launch} disabled={submitting}
                    className={`px-3 py-1.5 text-xs rounded font-medium ${
                      submitting
                        ? 'bg-blue-600 text-white animate-pulse cursor-wait'
                        : 'bg-green-600 text-white hover:bg-green-700'
                    }`}>
              {submitting ? 'Starting...' : 'Run'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────
// Main tab
// ─────────────────────────────────────────────────────────

export default function BacktestTab() {
  const { data: runsResp, refetch: refetchRuns } =
    useApi<{ runs: BacktestRun[] }>('/backtests?limit=50', 10000);
  const { data: stratsResp } =
    useApi<{ strategies: Strategy[] }>('/backtests/strategies');

  const runs = runsResp?.runs || [];
  const strategies = stratsResp?.strategies || [];

  const [showLaunch, setShowLaunch] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);

  // Auto-select the most recent completed run on first load
  useEffect(() => {
    if (selectedRunId == null && runs.length > 0) {
      const firstCompleted = runs.find(r => r.status === 'completed');
      if (firstCompleted) setSelectedRunId(firstCompleted.id);
    }
  }, [runs, selectedRunId]);

  const deleteRun = async (id: number) => {
    if (!confirm(`Delete backtest run #${id}?`)) return;
    await fetch(`/api/backtests/${id}`, { method: 'DELETE' });
    if (selectedRunId === id) setSelectedRunId(null);
    refetchRuns();
  };

  return (
    <div className="space-y-6">
      {/* ── Control bar ── */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4 flex items-center gap-3">
        <button onClick={() => setShowLaunch(true)}
                className="px-4 py-1.5 text-sm bg-green-600 text-white rounded hover:bg-green-700 font-medium">
          + Run Backtest
        </button>
        <button onClick={refetchRuns}
                className="px-3 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-400 rounded hover:text-white">
          Refresh
        </button>
        <span className="text-xs text-gray-500 ml-auto">
          Runs on the host via <code className="text-gray-400">bot_manager</code>
        </span>
      </div>

      {/* ── Runs list ── */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-[#21262d]">
              {['Name', 'Strategy', 'Tickers', 'Period', 'Status',
                'Trades', 'Win%', 'P&L', 'PF', 'DD', ''].map(h => (
                <th key={h} className="px-3 py-2 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d]">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id}
                  onClick={() => setSelectedRunId(r.id)}
                  className={`cursor-pointer hover:bg-[#1c2128] border-b border-[#21262d] ${selectedRunId === r.id ? 'bg-[#1c2128]' : ''}`}>
                <td className="px-3 py-2 text-xs text-gray-200">{r.name || `run-${r.id}`}</td>
                <td className="px-3 py-2 text-xs text-gray-400">{r.strategy_name || '—'}</td>
                <td className="px-3 py-2 text-xs text-gray-400">{r.tickers.join(', ')}</td>
                <td className="px-3 py-2 text-xs text-gray-500">
                  {r.start_date} → {r.end_date}
                </td>
                <td className="px-3 py-2">
                  <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${statusPillColor(r.status)}`}>
                    {r.status}
                  </span>
                </td>
                <td className="px-3 py-2 text-xs">{r.total_trades}</td>
                <td className="px-3 py-2 text-xs">{r.win_rate.toFixed(1)}%</td>
                <td className={`px-3 py-2 text-xs font-mono ${pnlColor(r.total_pnl)}`}>
                  {fmtUsd(r.total_pnl)}
                </td>
                <td className="px-3 py-2 text-xs text-gray-400">
                  {r.profit_factor != null ? r.profit_factor.toFixed(2) : '—'}
                </td>
                <td className={`px-3 py-2 text-xs font-mono ${pnlColor(r.max_drawdown)}`}>
                  {fmtUsd(r.max_drawdown)}
                </td>
                <td className="px-3 py-2 text-right">
                  <button onClick={e => { e.stopPropagation(); deleteRun(r.id); }}
                          className="text-xs text-gray-500 hover:text-red-400">
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td colSpan={11} className="px-3 py-8 text-center text-sm text-gray-500">
                  No backtest runs yet. Click <strong>Run Backtest</strong> to start one.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* ── Drill-down ── */}
      {selectedRunId != null && <RunDetail runId={selectedRunId} />}

      {showLaunch && (
        <LaunchDialog
          onClose={() => setShowLaunch(false)}
          onLaunched={() => { refetchRuns(); }}
          strategies={strategies}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────
// Run detail — analytics + trades + feature analysis
// ─────────────────────────────────────────────────────────

function RunDetail({ runId }: { runId: number }) {
  const { data: detail } = useApi<{ run: BacktestRun; trades: BacktestTrade[] }>(
    `/backtests/${runId}`, 10000
  );
  const { data: analytics } = useApi<Analytics>(
    `/backtests/${runId}/analytics`, 30000
  );
  const { data: features } = useApi<FeatureAnalysis>(
    `/backtests/${runId}/feature_analysis`, 30000
  );

  const run = detail?.run;
  const trades = detail?.trades || [];

  const [tradeFilter, setTradeFilter] = useState<'all' | 'WIN' | 'LOSS'>('all');
  const filteredTrades = useMemo(() => {
    if (tradeFilter === 'all') return trades;
    return trades.filter(t => t.exit_result === tradeFilter);
  }, [trades, tradeFilter]);

  if (!run) return <div className="text-gray-500 py-8 text-center">Loading run…</div>;

  const maxTickerPnl = Math.max(1, ...(analytics?.pnl_by_ticker || []).map(t => Math.abs(t.pnl)));
  const maxReasonCount = Math.max(1, ...(analytics?.by_reason || []).map(t => t.count));
  const maxDowPnl = Math.max(1, ...(analytics?.by_day_of_week || []).map(d => Math.abs(d.pnl)));

  return (
    <div className="space-y-4">
      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
        <SummaryCard label="Trades" value={String(run.total_trades)} sub={`${run.wins}W / ${run.losses}L`} />
        <SummaryCard label="Win Rate" value={`${run.win_rate.toFixed(1)}%`} />
        <SummaryCard label="Total P&L" value={fmtUsd(run.total_pnl)} color={pnlColor(run.total_pnl)} />
        <SummaryCard label="Profit Factor" value={run.profit_factor != null ? run.profit_factor.toFixed(2) : '—'} />
        <SummaryCard label="Max DD" value={fmtUsd(run.max_drawdown)} color="text-red-400" />
        <SummaryCard label="Avg Hold" value={`${run.avg_hold_min.toFixed(0)}m`} />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Panel title="Cumulative P&L">
          {analytics && <CumulativePnlChart data={analytics.cum_pnl} />}
        </Panel>

        <Panel title="P&L by Ticker">
          {(analytics?.pnl_by_ticker || []).map(row => (
            <BarRow key={row.ticker} label={row.ticker} value={row.pnl}
                    max={maxTickerPnl} count={row.trades} />
          ))}
        </Panel>

        <Panel title="Exit Reasons">
          {(analytics?.by_reason || []).map(row => (
            <BarRow key={row.reason} label={row.reason} value={row.pnl}
                    max={maxReasonCount * 10} count={row.count}
                    color={row.reason === 'TP' || row.reason === 'TRAIL_STOP' ? '#3fb950' : '#f85149'} />
          ))}
        </Panel>

        <Panel title="By Day of Week">
          {(analytics?.by_day_of_week || []).map(row => (
            <BarRow key={row.dow} label={row.dow} value={row.pnl}
                    max={maxDowPnl} count={row.count} />
          ))}
        </Panel>
      </div>

      {/* Feature analysis */}
      <Panel title={`Feature Analysis (data-science view of ${features?.total_trades || 0} trades)`}>
        {(features?.features || []).length === 0 ? (
          <div className="text-xs text-gray-500">No numeric indicators found in entry_indicators.</div>
        ) : (
          <div className="space-y-3 max-h-96 overflow-y-auto">
            {features!.features.slice(0, 12).map(f => (
              <div key={f.feature} className="bg-[#0d1117] border border-[#21262d] rounded p-2">
                <div className="flex items-center gap-3 text-xs">
                  <span className="font-mono text-gray-200 w-32">{f.feature}</span>
                  <span className="text-gray-500">wins: {f.n_wins}</span>
                  <span className="text-gray-500">losses: {f.n_losses}</span>
                  <span className="text-green-400">WIN μ: {f.win_mean != null ? f.win_mean.toFixed(3) : '—'}</span>
                  <span className="text-red-400">LOSS μ: {f.loss_mean != null ? f.loss_mean.toFixed(3) : '—'}</span>
                  {f.edge != null && (
                    <span className={`ml-auto font-mono ${f.edge > 0 ? 'text-green-400' : 'text-red-400'}`}>
                      edge: {f.edge > 0 ? '+' : ''}{f.edge.toFixed(3)}
                    </span>
                  )}
                </div>
                {f.quartile_win_rates.length > 0 && (
                  <div className="mt-1 flex items-center gap-1 text-[11px]">
                    {f.quartile_win_rates.map(q => (
                      <div key={q.label} className="flex-1 bg-[#21262d] px-1 py-0.5 rounded text-gray-400">
                        <div className="truncate">{q.label}</div>
                        <div className={q.win_rate >= 50 ? 'text-green-400' : 'text-red-400'}>
                          {q.win_rate.toFixed(0)}% ({q.count})
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Panel>

      {/* Trades table */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
          <h3 className="text-sm font-semibold text-gray-300">Trades ({filteredTrades.length})</h3>
          <div className="flex items-center gap-2">
            {(['all', 'WIN', 'LOSS'] as const).map(f => (
              <button key={f} onClick={() => setTradeFilter(f)}
                      className={`px-2 py-0.5 text-xs rounded ${tradeFilter === f ? 'bg-blue-500/20 text-blue-400' : 'text-gray-500 hover:text-gray-300'}`}>
                {f === 'all' ? 'All' : f}
              </button>
            ))}
          </div>
        </div>
        <div className="max-h-[60vh] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[#21262d]">
              <tr>
                {['Ticker', 'Dir', 'Entry', 'Exit', 'P&L', 'Hold',
                  'Signal', 'Reason', 'Result'].map(h =>
                  <th key={h} className="px-2 py-2 text-left text-gray-500 font-semibold">{h}</th>
                )}
              </tr>
            </thead>
            <tbody>
              {filteredTrades.map(t => (
                <tr key={t.id} className="border-b border-[#21262d] hover:bg-[#1c2128]">
                  <td className="px-2 py-1 text-gray-200">{t.ticker}</td>
                  <td className="px-2 py-1 text-gray-400">{t.direction}</td>
                  <td className="px-2 py-1 font-mono text-gray-400">
                    {t.entry_price != null ? t.entry_price.toFixed(2) : '—'}
                  </td>
                  <td className="px-2 py-1 font-mono text-gray-400">
                    {t.exit_price != null ? t.exit_price.toFixed(2) : '—'}
                  </td>
                  <td className={`px-2 py-1 font-mono ${pnlColor(t.pnl_usd)}`}>
                    {fmtUsd(t.pnl_usd)}
                  </td>
                  <td className="px-2 py-1 text-gray-500">
                    {t.hold_minutes != null ? `${t.hold_minutes.toFixed(0)}m` : '—'}
                  </td>
                  <td className="px-2 py-1 text-gray-400">{t.signal_type || '—'}</td>
                  <td className="px-2 py-1 text-gray-400">{t.exit_reason || '—'}</td>
                  <td className={`px-2 py-1 ${t.exit_result === 'WIN' ? 'text-green-400' :
                                               t.exit_result === 'LOSS' ? 'text-red-400' :
                                               'text-gray-500'}`}>
                    {t.exit_result || '—'}
                  </td>
                </tr>
              ))}
              {filteredTrades.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-2 py-4 text-center text-gray-500">No trades.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {run.error_message && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-400 rounded p-3 text-xs font-mono whitespace-pre-wrap">
          {run.error_message}
        </div>
      )}
    </div>
  );
}

function SummaryCard({ label, value, sub, color }:
  { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-3">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className={`text-lg font-bold ${color || ''}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-3">
      <div className="text-xs text-gray-500 mb-2 font-semibold">{title}</div>
      {children}
    </div>
  );
}
