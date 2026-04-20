import { useEffect, useState } from 'react';

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

      {/* Runs table — capped height so the trades panel stays in view
          when user clicks a row. With 800+ historical runs the table
          pushed the drill-down below the fold (which was the bug!). */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto max-h-[400px] overflow-y-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-[#21262d]">
              {['Name', 'Strategy', 'Period', 'Status', 'Trades', 'Win%', 'P&L', 'PF', 'Max DD', ''].map(h =>
                <th key={h} className="px-3 py-2 text-left text-xs text-gray-500 border-b border-[#30363d]">{h}</th>
              )}
            </tr>
          </thead>
          <tbody>
            {runs.map(r => {
              const isSelected = r.id === selectedRunId;
              return (
                <tr key={r.id}
                    onClick={() => onRunClick(r.id)}
                    className={`cursor-pointer border-b border-[#21262d] hover:bg-[#1c2128] ${isSelected ? 'bg-[#1c2128]' : ''}`}>
                  <td className="px-3 py-2 text-xs text-gray-200">{r.name || `run-${r.id}`} <span className="text-gray-600">#{r.id}</span></td>
                  <td className="px-3 py-2 text-xs text-gray-400">{r.strategy_name || '—'}</td>
                  <td className="px-3 py-2 text-xs text-gray-500">{r.start_date} → {r.end_date}</td>
                  <td className="px-3 py-2 text-xs">
                    <span className={`px-2 py-0.5 rounded text-xs ${
                      r.status === 'completed' ? 'bg-green-500/20 text-green-400' :
                      r.status === 'running'   ? 'bg-blue-500/20 text-blue-400 animate-pulse' :
                      r.status === 'failed'    ? 'bg-red-500/20 text-red-400' :
                      'bg-gray-700 text-gray-400'
                    }`}>{r.status}</span>
                  </td>
                  <td className="px-3 py-2 text-xs">{r.total_trades}</td>
                  <td className="px-3 py-2 text-xs">{Number(r.win_rate || 0).toFixed(1)}%</td>
                  <td className={`px-3 py-2 text-xs font-mono ${pnlColor(r.total_pnl)}`}>{fmtUsd(r.total_pnl)}</td>
                  <td className="px-3 py-2 text-xs text-gray-400">{r.profit_factor != null ? Number(r.profit_factor).toFixed(2) : '—'}</td>
                  <td className={`px-3 py-2 text-xs font-mono ${pnlColor(r.max_drawdown)}`}>{fmtUsd(r.max_drawdown)}</td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={e => { e.stopPropagation(); deleteRun(r.id); }}
                            className="text-xs text-gray-500 hover:text-red-400">Delete</button>
                  </td>
                </tr>
              );
            })}
            {runs.length === 0 && (
              <tr><td colSpan={10} className="px-3 py-6 text-center text-sm text-gray-500">
                No runs yet. Click <b>+ Run Backtest</b> above.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

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

            {/* Modal body — scrollable trades table */}
            <div className="flex-1 overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-[#21262d]">
                  <tr>
                    {['Ticker', 'Dir', 'Entry $', 'Exit $', 'P&L', 'Hold', 'Signal', 'Reason', 'Result'].map(h =>
                      <th key={h} className="px-3 py-2 text-left text-gray-500 border-b border-[#30363d]">{h}</th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {trades.map(t => (
                    <tr key={t.id} className="border-b border-[#21262d] hover:bg-[#1c2128]">
                      <td className="px-3 py-1.5 text-gray-200">{t.ticker}</td>
                      <td className="px-3 py-1.5 text-gray-400">{t.direction}</td>
                      <td className="px-3 py-1.5 font-mono text-gray-400">{t.entry_price != null ? Number(t.entry_price).toFixed(2) : '—'}</td>
                      <td className="px-3 py-1.5 font-mono text-gray-400">{t.exit_price != null ? Number(t.exit_price).toFixed(2) : '—'}</td>
                      <td className={`px-3 py-1.5 font-mono ${pnlColor(t.pnl_usd)}`}>{fmtUsd(t.pnl_usd)}</td>
                      <td className="px-3 py-1.5 text-gray-500">{t.hold_minutes != null ? `${Math.round(t.hold_minutes)}m` : '—'}</td>
                      <td className="px-3 py-1.5 text-gray-400">{t.signal_type || '—'}</td>
                      <td className="px-3 py-1.5 text-gray-400">{t.exit_reason || '—'}</td>
                      <td className={`px-3 py-1.5 ${
                        t.exit_result === 'WIN' ? 'text-green-400' :
                        t.exit_result === 'LOSS' ? 'text-red-400' :
                        'text-gray-500'
                      }`}>{t.exit_result || '—'}</td>
                    </tr>
                  ))}
                  {trades.length === 0 && (
                    <tr><td colSpan={9} className="px-3 py-8 text-center text-gray-500">
                      {tradesTotal > 0 ? 'Loading trades…' : 'No trades in this run.'}
                    </td></tr>
                  )}
                </tbody>
              </table>
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
