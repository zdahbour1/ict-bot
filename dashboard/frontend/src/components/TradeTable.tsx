import { useState, useMemo, useEffect } from 'react';
import {
  useReactTable, getCoreRowModel, getSortedRowModel, getFilteredRowModel,
  flexRender, createColumnHelper, type SortingState, type ColumnFiltersState,
} from '@tanstack/react-table';
import type { Trade, TradeLeg } from '../types';
import { apiPost } from '../hooks/useApi';

const col = createColumnHelper<Trade>();

function Badge({ text, variant }: { text: string; variant: string }) {
  const colors: Record<string, string> = {
    open: 'bg-blue-500/20 text-blue-400',
    closed: 'bg-gray-700 text-gray-400',
    errored: 'bg-red-500/20 text-red-400',
    WIN: 'bg-green-500/20 text-green-400',
    LOSS: 'bg-red-500/20 text-red-400',
    SCRATCH: 'bg-yellow-500/20 text-yellow-400',
  };
  return <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${colors[variant] || 'bg-gray-700 text-gray-400'}`}>{text}</span>;
}

function PnlCell({ value }: { value: number }) {
  const color = value > 0 ? 'text-green-400' : value < 0 ? 'text-red-400' : 'text-gray-500';
  return <span className={color}>{value > 0 ? '+' : ''}{value.toFixed(2)}</span>;
}


// ── BracketStatusCell ───────────────────────────────────────
// Shows the TP + SL bracket orders per trade with their current IB
// state (updated by reconcile PASS 4 every ~60s). Colors:
//   green  — both TP + SL Submitted/PreSubmitted (healthy, protected)
//   red    — at least one is Cancelled/Inactive/MISSING (UNPROTECTED)
//   gray   — never known / status not yet refreshed
// Hover tooltip reveals permId + orderId + checked timestamp.
const _ACTIVE_STATUSES = new Set([
  "Submitted", "PreSubmitted", "PendingSubmit",
]);
const _BAD_STATUSES = new Set([
  "Cancelled", "ApiCancelled", "Inactive", "MISSING",
]);

function _legStatusColor(status: string | null): string {
  if (!status) return "text-gray-500";
  if (_ACTIVE_STATUSES.has(status)) return "text-green-400";
  if (_BAD_STATUSES.has(status)) return "text-red-400";
  if (status === "Filled") return "text-blue-400";
  return "text-gray-400";
}

function _legAbbr(status: string | null): string {
  if (!status) return "—";
  if (status === "Submitted" || status === "PreSubmitted") return "OK";
  if (status === "Cancelled" || status === "ApiCancelled") return "CXL";
  if (status === "Inactive") return "INACT";
  if (status === "Filled") return "FILL";
  if (status === "MISSING") return "GONE";
  return status.slice(0, 5);
}

function BracketStatusCell({ trade }: { trade: Trade }) {
  if (trade.status !== 'open') {
    // Closed trades: brackets are irrelevant; show a dash.
    return <span className="text-gray-600">—</span>;
  }

  const tp = trade.ib_tp_status;
  const sl = trade.ib_sl_status;
  const tpBad = tp === null || _BAD_STATUSES.has(tp);
  const slBad = sl === null || _BAD_STATUSES.has(sl);
  const unprotected = tpBad && slBad;

  const tooltip = [
    `TP  perm=${trade.ib_tp_perm_id ?? '-'}  order=${trade.ib_tp_order_id ?? '-'}  status=${tp ?? 'unknown'}  price=$${trade.ib_tp_price ?? '-'}`,
    `SL  perm=${trade.ib_sl_perm_id ?? '-'}  order=${trade.ib_sl_order_id ?? '-'}  status=${sl ?? 'unknown'}  price=$${trade.ib_sl_price ?? '-'}`,
    `last check: ${trade.ib_brackets_checked_at ?? 'never'}`,
  ].join('\n');

  return (
    <div
      title={tooltip}
      className={`text-[11px] font-mono whitespace-nowrap ${unprotected ? 'bg-red-500/20 px-1 rounded' : ''}`}>
      <span className="text-gray-500">TP </span>
      <span className={_legStatusColor(tp)}>{_legAbbr(tp)}</span>
      {trade.ib_tp_price != null && (
        <span className="text-gray-500"> ${trade.ib_tp_price.toFixed(2)}</span>
      )}
      <span className="text-gray-600"> · </span>
      <span className="text-gray-500">SL </span>
      <span className={_legStatusColor(sl)}>{_legAbbr(sl)}</span>
      {trade.ib_sl_price != null && (
        <span className="text-gray-500"> ${trade.ib_sl_price.toFixed(2)}</span>
      )}
      {unprotected && <span className="ml-1 text-red-400 font-bold">⚠</span>}
    </div>
  );
}


// ── Trade details modal — opens on ID-cell click.
// Shows every unique reference (DB / IB / TWS / contract) plus the
// full TP/SL bracket detail that was previously only accessible via
// hover tooltip. Single-glance debug page: if a trade misbehaves,
// this is the first thing to open.
function TradeDetailsModal({ trade, onClose }: {
  trade: Trade; onClose: () => void;
}) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);

  const Row = ({ label, value, mono = true }: {
    label: string; value: React.ReactNode; mono?: boolean;
  }) => (
    <div className="flex items-baseline gap-3 py-1 border-b border-[#21262d] last:border-0">
      <div className="text-xs text-gray-500 w-44 shrink-0">{label}</div>
      <div className={`text-xs text-gray-200 ${mono ? 'font-mono' : ''} break-all`}>
        {value ?? <span className="text-gray-600">—</span>}
      </div>
    </div>
  );

  const legStatusColor = (s: string | null) => _legStatusColor(s);
  const fmt$ = (v: number | null | undefined) =>
    v == null ? <span className="text-gray-600">—</span> : `$${v.toFixed(2)}`;

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
         onClick={onClose}>
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg w-[95vw] max-w-2xl max-h-[85vh] flex flex-col shadow-2xl"
           onClick={e => e.stopPropagation()}>
        <div className="px-4 py-3 border-b border-[#30363d] flex items-center justify-between">
          <div>
            <h3 className="text-base font-semibold text-gray-200">
              Trade {trade.id} — {trade.ticker}{' '}
              <span className="text-gray-500 font-normal text-sm">
                ({trade.symbol})
              </span>
            </h3>
            <div className="text-xs text-gray-500 mt-0.5">
              Unique references for cross-system troubleshooting
            </div>
          </div>
          <button onClick={onClose}
                  className="text-gray-500 hover:text-white text-2xl leading-none px-2"
                  aria-label="Close">&times;</button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {/* Identifiers */}
          <div className="mb-5">
            <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-2">
              Identifiers
            </div>
            <Row label="DB id" value={trade.id} />
            <Row label="Ref (IB orderRef)" value={trade.client_trade_id} />
            <Row label="Entry orderId" value={trade.ib_order_id} />
            <Row label="Entry permId" value={
              <>
                {trade.ib_perm_id ?? '—'}
                <span className="text-gray-600 ml-2">(globally unique)</span>
              </>
            } />
            <Row label="Contract conId" value={trade.ib_con_id} />
            <Row label="Account" value={trade.account} />
          </div>

          {/* Take-profit leg */}
          <div className="mb-5">
            <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-2">
              Take-profit leg
            </div>
            <Row label="Status" value={
              <span className={legStatusColor(trade.ib_tp_status)}>
                {trade.ib_tp_status ?? 'unknown'}
              </span>
            } />
            <Row label="orderId" value={trade.ib_tp_order_id} />
            <Row label="permId" value={trade.ib_tp_perm_id} />
            <Row label="Price" value={fmt$(trade.ib_tp_price)} />
          </div>

          {/* Stop-loss leg */}
          <div className="mb-5">
            <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-2">
              Stop-loss leg
            </div>
            <Row label="Status" value={
              <span className={legStatusColor(trade.ib_sl_status)}>
                {trade.ib_sl_status ?? 'unknown'}
              </span>
            } />
            <Row label="orderId" value={trade.ib_sl_order_id} />
            <Row label="permId" value={trade.ib_sl_perm_id} />
            <Row label="Price" value={fmt$(trade.ib_sl_price)} />
            <Row label="Last bracket check"
                 value={trade.ib_brackets_checked_at
                   ? new Date(trade.ib_brackets_checked_at).toLocaleString()
                   : null}
                 mono={false} />
          </div>

          {/* Trade state */}
          <div>
            <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-2">
              State
            </div>
            <Row label="Status" value={trade.status.toUpperCase()} mono={false} />
            <Row label="Direction" value={trade.direction} mono={false} />
            <Row label="Contracts (open/entered)"
                 value={`${trade.contracts_open} / ${trade.contracts_entered}`} />
            <Row label="Signal" value={trade.signal_type} mono={false} />
            {trade.error_message && (
              <Row label="Error"
                   value={<span className="text-red-400">{trade.error_message}</span>}
                   mono={false} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}


// ── Audit trail modal — shows every system_log row touching this trade.
//    Uses the append-only trail written by strategy.audit.log_trade_action.
interface AuditEntry {
  id: number;
  component: string;
  level: string;
  message: string;
  details: Record<string, any>;
  created_at: string | null;
}

function fmtPT(iso: string | null): string {
  if (!iso) return '-';
  const d = new Date(iso);
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Los_Angeles',
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const get = (t: string) => parts.find(p => p.type === t)?.value ?? '';
  return `${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')} PT`;
}

function AuditModal({ tradeId, ticker, onClose }: {
  tradeId: number; ticker: string; onClose: () => void;
}) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true); setErr(null);
    fetch(`/api/trades/${tradeId}/audit`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { setEntries(d.entries || []); setLoading(false); })
      .catch(e => { setErr(e.message); setLoading(false); });
  }, [tradeId]);

  // ESC to close
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);

  const levelColors: Record<string, string> = {
    error: 'bg-red-500/20 text-red-300',
    warn:  'bg-yellow-500/20 text-yellow-300',
    info:  'bg-blue-500/20 text-blue-300',
    debug: 'bg-gray-500/20 text-gray-400',
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
         onClick={onClose}>
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg w-[95vw] max-w-5xl max-h-[85vh] flex flex-col shadow-2xl"
           onClick={e => e.stopPropagation()}>
        <div className="px-4 py-3 border-b border-[#30363d] flex items-center justify-between">
          <div>
            <h3 className="text-base font-semibold text-gray-200">
              Audit Trail — {ticker} <span className="text-gray-500 font-normal">(db_id={tradeId})</span>
            </h3>
            <div className="text-xs text-gray-500 mt-0.5">
              Every thread action on this trade, oldest first · times in Pacific Time
            </div>
          </div>
          <button onClick={onClose}
                  className="text-gray-500 hover:text-white text-2xl leading-none px-2"
                  aria-label="Close">&times;</button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {err && <div className="px-4 py-3 text-sm text-red-400">Error: {err}</div>}
          {loading && <div className="px-4 py-6 text-sm text-gray-500 text-center">Loading…</div>}
          {!loading && !err && entries.length === 0 && (
            <div className="px-4 py-6 text-sm text-gray-500 text-center">
              No audit entries found for this trade.
            </div>
          )}
          {!loading && !err && entries.length > 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-[#21262d] sticky top-0">
                  <th className="px-3 py-2 text-left text-gray-500 border-b border-[#30363d]">Time (PT)</th>
                  <th className="px-3 py-2 text-left text-gray-500 border-b border-[#30363d]">Thread / actor</th>
                  <th className="px-3 py-2 text-left text-gray-500 border-b border-[#30363d]">Action</th>
                  <th className="px-3 py-2 text-left text-gray-500 border-b border-[#30363d]">Message</th>
                </tr>
              </thead>
              <tbody>
                {entries.map(e => {
                  const action = e.details?.action ?? '—';
                  const pyt = e.details?.py_thread;
                  return (
                    <tr key={e.id} className="border-b border-[#21262d] hover:bg-[#1c2128] align-top">
                      <td className="px-3 py-1.5 whitespace-nowrap text-gray-400 font-mono">
                        {fmtPT(e.created_at)}
                      </td>
                      <td className="px-3 py-1.5">
                        <div className="text-gray-300 font-mono">{e.component}</div>
                        {pyt && <div className="text-[10px] text-gray-600">py:{pyt}</div>}
                      </td>
                      <td className="px-3 py-1.5">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${levelColors[e.level] || 'text-gray-400'}`}>
                          {action}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 text-gray-300">
                        <div>{e.message}</div>
                        {Object.keys(e.details || {}).filter(k => !['trade_id','action','actor','py_thread'].includes(k)).length > 0 && (
                          <details className="mt-1">
                            <summary className="cursor-pointer text-[10px] text-gray-500 hover:text-gray-300">details</summary>
                            <pre className="text-[10px] text-gray-500 mt-1 bg-[#0d1117] p-2 rounded overflow-x-auto">
                              {JSON.stringify(
                                Object.fromEntries(
                                  Object.entries(e.details || {})
                                    .filter(([k]) => !['trade_id','action','actor','py_thread'].includes(k))
                                ), null, 2)}
                            </pre>
                          </details>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <div className="px-4 py-2 border-t border-[#30363d] text-xs text-gray-600 text-center">
          {entries.length} entr{entries.length === 1 ? 'y' : 'ies'} · ESC or click outside to close
        </div>
      </div>
    </div>
  );
}

// ── LegsPanel ───────────────────────────────────────────────
// Renders each leg of a multi-leg trade (iron condor, spread, hedged
// position). Fetched lazily from /api/trades/{id}/legs on expand.
// ENH-047.
function LegsPanel({ tradeId }: { tradeId: number }) {
  const [legs, setLegs] = useState<TradeLeg[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/trades/${tradeId}/legs`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) setLegs(data.legs || []);
      } catch (e: any) {
        if (!cancelled) setError(e.message || 'Failed to load legs');
      }
    })();
    return () => { cancelled = true; };
  }, [tradeId]);

  if (error) return (
    <div className="px-4 py-3 text-xs text-red-400">Error: {error}</div>
  );
  if (legs === null) return (
    <div className="px-4 py-3 text-xs text-gray-500">Loading legs…</div>
  );
  if (legs.length === 0) return (
    <div className="px-4 py-3 text-xs text-gray-600">No legs found.</div>
  );

  return (
    <div className="px-4 py-3 bg-[#0d1117] border-l-2 border-blue-500/40">
      <table className="w-full text-[11px] font-mono">
        <thead>
          <tr className="text-gray-500 border-b border-[#21262d]">
            <th className="text-left py-1 pr-3">#</th>
            <th className="text-left py-1 pr-3">Role</th>
            <th className="text-left py-1 pr-3">Symbol</th>
            <th className="text-left py-1 pr-3">Dir</th>
            <th className="text-right py-1 pr-3">Strike</th>
            <th className="text-right py-1 pr-3">Qty</th>
            <th className="text-right py-1 pr-3">Entry</th>
            <th className="text-right py-1 pr-3">Exit / Cur</th>
            <th className="text-right py-1 pr-3">P&amp;L $</th>
            <th className="text-left py-1 pr-3">Status</th>
            <th className="text-left py-1">IB ids</th>
          </tr>
        </thead>
        <tbody>
          {legs.map(l => {
            const dirColor = l.direction === 'LONG' ? 'text-green-400' : 'text-red-400';
            const pnl = l.pnl_usd;
            const pnlColor = pnl == null ? 'text-gray-600'
              : pnl > 0 ? 'text-green-400' : pnl < 0 ? 'text-red-400' : 'text-gray-500';
            const closePx = l.exit_price ?? l.current_price;
            return (
              <tr key={l.leg_id} className="border-b border-[#21262d]/60">
                <td className="py-1 pr-3 text-gray-500">{l.leg_index}</td>
                <td className="py-1 pr-3 text-gray-300">{l.leg_role ?? '—'}</td>
                <td className="py-1 pr-3 text-blue-300">{l.symbol}</td>
                <td className={`py-1 pr-3 ${dirColor}`}>{l.direction}</td>
                <td className="py-1 pr-3 text-right text-gray-300">
                  {l.strike != null ? `$${l.strike.toFixed(2)}` : '—'}
                  {l.right ? <span className="ml-1 text-gray-500">{l.right}</span> : null}
                </td>
                <td className="py-1 pr-3 text-right text-gray-300">
                  {l.contracts_entered}
                  {l.contracts_open !== l.contracts_entered && (
                    <span className="text-gray-500"> ({l.contracts_open} open)</span>
                  )}
                </td>
                <td className="py-1 pr-3 text-right">
                  {l.entry_price != null ? `$${l.entry_price.toFixed(2)}` : '—'}
                  {l.price_source && l.price_source !== 'exec' && l.price_source !== 'mkt_single' && (
                    <span
                      title={
                        l.price_source === 'quote'
                          ? 'ESTIMATE — mid-quote used after IB did not report per-leg fill price'
                          : l.price_source === 'proportional'
                          ? 'ESTIMATE — proportional split of combo net_fill_price (last-resort fallback)'
                          : `price_source=${l.price_source}`
                      }
                      className={`ml-1 px-1 py-0.5 rounded text-[9px] font-mono uppercase ${
                        l.price_source === 'quote'
                          ? 'bg-amber-500/20 text-amber-400'
                          : 'bg-red-500/20 text-red-400'
                      }`}
                    >
                      est
                    </span>
                  )}
                </td>
                <td className="py-1 pr-3 text-right">
                  {closePx != null ? `$${closePx.toFixed(2)}` : '—'}
                </td>
                <td className={`py-1 pr-3 text-right ${pnlColor}`}>
                  {pnl != null ? `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}` : '—'}
                </td>
                <td className="py-1 pr-3">
                  <span className={l.leg_status === 'open' ? 'text-blue-400' : 'text-gray-500'}>
                    {l.leg_status}
                  </span>
                </td>
                <td className="py-1 text-gray-500 text-[10px]">
                  ord={l.ib_order_id ?? '—'} sl={l.ib_sl_order_id ?? '—'}
                  {l.ib_tp_order_id ? ` tp=${l.ib_tp_order_id}` : ''}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}


export default function TradeTable({ trades, onRefresh, lastUpdated }: { trades: Trade[]; onRefresh: () => void; lastUpdated?: Date | null }) {
  const [sorting, setSorting] = useState<SortingState>([{ id: 'entry_time', desc: true }]);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [tickerFilter, setTickerFilter] = useState<string>('');
  const [strategyFilter, setStrategyFilter] = useState<string>('');
  const [periodFilter, setPeriodFilter] = useState<string>('today');
  const [refreshing, setRefreshing] = useState(false);
  const [auditTrade, setAuditTrade] = useState<{ id: number; ticker: string } | null>(null);
  const [detailsTrade, setDetailsTrade] = useState<Trade | null>(null);
  // ENH-047: track which multi-leg trades are expanded to show legs.
  // ENH-040 per-strategy summary cards
  const [strategySummary, setStrategySummary] = useState<any[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const p = periodFilter || 'today';
        const res = await fetch(`/api/trades/strategy-summary?period=${p}`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setStrategySummary(data.strategies || []);
      } catch {}
    })();
    return () => { cancelled = true; };
  }, [periodFilter, trades]);

  // ENH-010 compact density toggle — persisted in localStorage
  const [compact, setCompact] = useState<boolean>(() => {
    try { return localStorage.getItem('tradeTable.compact') === 'true'; }
    catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem('tradeTable.compact', String(compact)); }
    catch {}
  }, [compact]);

  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const toggleExpanded = (tid: number) => {
    setExpandedIds(prev => {
      const next = new Set(prev);
      if (next.has(tid)) next.delete(tid); else next.add(tid);
      return next;
    });
  };

  const handleRefresh = () => {
    setRefreshing(true);
    onRefresh();
    setTimeout(() => setRefreshing(false), 500);
  };

  const filteredTrades = useMemo(() => {
    let result = trades;

    // Date period filter
    if (periodFilter) {
      const now = new Date();
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      let cutoff: Date | null = null;
      if (periodFilter === 'today') cutoff = today;
      else if (periodFilter === 'week') { cutoff = new Date(today); cutoff.setDate(cutoff.getDate() - 7); }
      else if (periodFilter === 'month') { cutoff = new Date(today); cutoff.setDate(cutoff.getDate() - 30); }
      if (cutoff) {
        result = result.filter(t => t.entry_time && new Date(t.entry_time) >= cutoff!);
      }
    }

    if (statusFilter) result = result.filter(t => t.status === statusFilter);
    if (tickerFilter) result = result.filter(t => t.ticker === tickerFilter);
    if (strategyFilter) result = result.filter(t => (t as any).strategy_name === strategyFilter);
    return result;
  }, [trades, statusFilter, tickerFilter, strategyFilter, periodFilter]);

  const tickers = useMemo(() => [...new Set(trades.map(t => t.ticker))].sort(), [trades]);
  const strategyNames = useMemo(
    () => [...new Set(trades.map(t => (t as any).strategy_name).filter(Boolean))].sort(),
    [trades]
  );

  const columns = useMemo(() => [
    col.display({
      id: 'expander',
      header: '',
      cell: info => {
        const t = info.row.original;
        const nLegs = (t as any).n_legs ?? 1;
        if (nLegs <= 1) return <span className="text-gray-700">·</span>;
        const open = expandedIds.has(t.id);
        return (
          <button
            onClick={(e) => { e.stopPropagation(); toggleExpanded(t.id); }}
            title={open ? 'Collapse legs' : `Expand ${nLegs} legs`}
            className="text-blue-400 hover:text-blue-300 font-mono text-xs"
          >
            {open ? '▼' : '▶'} <span className="text-gray-500">{nLegs}</span>
          </button>
        );
      },
    }),
    col.accessor('status', {
      header: 'Status',
      cell: info => <Badge text={info.getValue().toUpperCase()} variant={info.getValue()} />,
    }),
    col.accessor('id', {
      header: 'ID',
      // Click opens TradeDetailsModal with every unique reference
      // (DB / IB / TWS / contract) + full TP/SL bracket detail.
      cell: info => {
        const t = info.row.original;
        return (
          <button
            onClick={() => setDetailsTrade(t)}
            className="text-[11px] font-mono text-blue-400 hover:text-blue-300 hover:underline"
            title="Click for unique references + bracket detail"
          >
            {t.id}
          </button>
        );
      },
    }),
    col.accessor('ticker', { header: 'Ticker', cell: info => <strong>{info.getValue()}</strong> }),
    col.accessor('strategy_name' as any, {
      id: 'strategy',
      header: 'Strategy',
      cell: info => {
        const name = info.getValue() as string | null;
        const display = (info.row.original.strategy_display_name as string | null) || name;
        if (!name) return <span className="text-gray-600">—</span>;
        const palette: Record<string, string> = {
          ict: 'bg-blue-500/20 text-blue-400',
          orb: 'bg-purple-500/20 text-purple-400',
          vwap_revert: 'bg-amber-500/20 text-amber-400',
          delta_neutral: 'bg-emerald-500/20 text-emerald-400',
        };
        const cls = palette[name] || 'bg-gray-700 text-gray-400';
        return (
          <span className={`px-2 py-0.5 rounded text-[11px] font-mono ${cls}`}
                title={display ?? name}>
            {name}
          </span>
        );
      },
    }),
    col.accessor('direction', {
      header: 'Type',
      cell: info => {
        const dir = info.getValue();
        const isCall = dir === 'LONG';
        return <span className={isCall ? 'text-green-400' : 'text-red-400'}>{isCall ? 'Call' : 'Put'}</span>;
      },
    }),
    col.accessor('symbol', {
      header: 'Expiry / Strike',
      cell: info => {
        const sym = (info.getValue() || '').replace(/\s+/g, ''); // Strip ALL spaces first
        // Parse OCC: TICKER YYMMDD C/P SSSSSSSS
        const match = sym.match(/^[A-Z]+(\d{6})([CP])(\d{8})$/);
        if (!match) return <span className="text-xs text-gray-400">{info.getValue()}</span>;
        const expStr = match[1]; // YYMMDD
        const cp = match[2]; // C or P
        const strike = parseInt(match[3]) / 1000;
        const expDate = new Date(2000 + parseInt(expStr.slice(0,2)), parseInt(expStr.slice(2,4)) - 1, parseInt(expStr.slice(4,6)));
        const expFmt = expDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        return <span className="text-xs">{expFmt} <span className="text-gray-400">${strike} {cp === 'C' ? 'Call' : 'Put'}</span></span>;
      },
    }),
    col.display({
      id: 'contracts',
      header: 'Contracts',
      cell: ({ row }) => {
        const t = row.original;
        if (t.status === 'closed') return `${t.contracts_closed} / ${t.contracts_entered}`;
        return `${t.contracts_open} / ${t.contracts_entered}`;
      },
    }),
    col.accessor('entry_price', { header: 'Entry', cell: info => `$${info.getValue()?.toFixed(2) || '-'}` }),
    col.display({
      id: 'price_now',
      header: 'Current / Exit',
      cell: ({ row }) => {
        const t = row.original;
        if (t.status === 'closed' && t.exit_price) {
          return <span className="text-gray-400">${t.exit_price.toFixed(2)}</span>;
        }
        return t.current_price ? `$${t.current_price.toFixed(2)}` : '-';
      },
    }),
    col.accessor('pnl_pct', { header: 'P&L %', cell: info => <PnlCell value={info.getValue() * 100} /> }),
    col.accessor('pnl_usd', { header: 'P&L $', cell: info => <PnlCell value={info.getValue()} /> }),
    col.accessor('peak_pnl_pct', { header: 'Peak', cell: info => `${(info.getValue() * 100).toFixed(1)}%` }),
    col.accessor('dynamic_sl_pct', { header: 'Trail SL', cell: info => `${(info.getValue() * 100).toFixed(0)}%` }),
    col.display({
      id: 'brackets',
      header: 'Brackets',
      cell: ({ row }) => <BracketStatusCell trade={row.original} />,
    }),
    col.accessor('entry_time', {
      header: 'Entry Time',
      cell: info => info.getValue() ? new Date(info.getValue()).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }) : '-',
    }),
    col.accessor('exit_time', {
      header: 'Exit Time',
      cell: info => info.getValue() ? new Date(info.getValue()!).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }) : '-',
    }),
    col.accessor('exit_reason', {
      header: 'Exit Reason',
      cell: info => {
        const reason = info.getValue();
        const result = info.row.original.exit_result;
        if (!reason) return '-';
        return <Badge text={reason} variant={result || 'closed'} />;
      },
    }),
    col.accessor('error_message', {
      header: 'Error',
      cell: info => info.getValue() ? <span className="text-red-400 text-xs truncate max-w-32 block">{info.getValue()}</span> : '-',
    }),
    col.accessor('notes' as any, {
      header: 'Notes',
      cell: info => {
        const tradeId = info.row.original.id;
        const currentNotes = (info.getValue() as string) || '';
        return (
          <input
            type="text"
            defaultValue={currentNotes}
            placeholder="..."
            className="bg-transparent border-b border-transparent hover:border-[#30363d] focus:border-blue-500
                       text-xs text-gray-400 w-24 px-1 py-0.5 outline-none"
            onBlur={async (e) => {
              const newNotes = e.target.value;
              if (newNotes !== currentNotes) {
                try {
                  await fetch(`/api/trades/${tradeId}/notes`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ notes: newNotes }),
                  });
                } catch { /* silent */ }
              }
            }}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
          />
        );
      },
    }),
    col.display({
      id: 'actions',
      header: 'Actions',
      cell: ({ row }) => (
        <div className="flex items-center gap-1">
          <button
            onClick={() => setAuditTrade({ id: row.original.id, ticker: row.original.ticker })}
            title="Audit trail: every thread action on this trade"
            className="px-2 py-1 text-xs bg-[#21262d] border border-[#30363d] text-gray-400 rounded hover:text-blue-400 hover:border-blue-400"
          >
            Audit
          </button>
          {row.original.status === 'open' && (
            <button
              onClick={async () => {
                if (confirm(`Close ${row.original.ticker} (${row.original.contracts_open} contracts)?`)) {
                  try {
                    await apiPost(`/trades/${row.original.id}/close`);
                    onRefresh();
                  } catch (e: any) {
                    alert(`Close failed: ${e.message}`);
                  }
                }
              }}
              className="px-2 py-1 text-xs bg-[#21262d] border border-[#30363d] text-gray-400 rounded hover:text-red-400 hover:border-red-400"
            >
              Close
            </button>
          )}
        </div>
      ),
    }),
  ], [onRefresh, expandedIds]);

  const table = useReactTable({
    data: filteredTrades,
    columns,
    state: { sorting, columnFilters },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  return (
    <div>
      {auditTrade && (
        <AuditModal tradeId={auditTrade.id} ticker={auditTrade.ticker}
                    onClose={() => setAuditTrade(null)} />
      )}
      {detailsTrade && (
        <TradeDetailsModal trade={detailsTrade}
                           onClose={() => setDetailsTrade(null)} />
      )}

      {/* Controls */}
      <div className="flex items-center gap-3 mb-4">
        <button onClick={async () => { if (confirm('Close ALL open trades?')) { try { await apiPost('/trades/close-all'); onRefresh(); } catch(e:any) { alert(`Failed: ${e.message}`); } } }}
          className="px-3 py-1.5 text-sm bg-red-600 text-white rounded-md hover:bg-red-700">
          Close All Trades
        </button>
        <button onClick={handleRefresh}
          className={`px-3 py-1.5 text-sm border rounded-md transition-colors ${
            refreshing ? 'bg-blue-600 border-blue-600 text-white' : 'bg-[#21262d] border-[#30363d] text-gray-400 hover:text-white'
          }`}>
          {refreshing ? 'Refreshing...' : 'Refresh'}
        </button>
        <button onClick={() => {
          const url = `/api/trades/export?status=${statusFilter || ''}`;
          window.open(url, '_blank');
        }}
          className="px-3 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-400 rounded-md hover:text-white">
          Export Excel
        </button>
        {lastUpdated && <span className="text-xs text-gray-500">Updated: {lastUpdated.toLocaleTimeString()}</span>}
        <button onClick={() => setCompact(!compact)}
          title="Toggle compact row height (ENH-010)"
          className={`px-2 py-1.5 text-xs border rounded-md ${compact ? 'bg-blue-500/20 border-blue-500/30 text-blue-400' : 'bg-[#21262d] border-[#30363d] text-gray-400'} hover:text-white`}>
          {compact ? 'Compact' : 'Normal'}
        </button>
        <select value={periodFilter} onChange={e => setPeriodFilter(e.target.value)}
          className="px-2 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-300 rounded-md">
          <option value="today">Today</option>
          <option value="week">Last 7 Days</option>
          <option value="month">Last 30 Days</option>
          <option value="">All Time</option>
        </select>
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
          className="px-2 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-300 rounded-md">
          <option value="">All statuses</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
          <option value="errored">Errored</option>
        </select>
        <select value={tickerFilter} onChange={e => setTickerFilter(e.target.value)}
          className="px-2 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-300 rounded-md">
          <option value="">All tickers</option>
          {tickers.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={strategyFilter} onChange={e => setStrategyFilter(e.target.value)}
          className="px-2 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-300 rounded-md">
          <option value="">All strategies</option>
          {strategyNames.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      {/* ENH-040 Per-strategy P&L summary cards */}
      {strategySummary && strategySummary.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
          {strategySummary.map(s => {
            const palette: Record<string, string> = {
              ict: 'border-blue-500/30 bg-blue-500/5',
              orb: 'border-purple-500/30 bg-purple-500/5',
              vwap_revert: 'border-amber-500/30 bg-amber-500/5',
              delta_neutral: 'border-emerald-500/30 bg-emerald-500/5',
            };
            const cls = palette[s.strategy] || 'border-[#30363d] bg-[#161b22]';
            const pnlColor = s.net_pnl > 0 ? 'text-green-400'
              : s.net_pnl < 0 ? 'text-red-400' : 'text-gray-400';
            return (
              <div key={s.strategy}
                onClick={() => setStrategyFilter(strategyFilter === s.strategy ? '' : s.strategy)}
                className={`rounded-lg border ${cls} p-3 cursor-pointer hover:brightness-125 ${strategyFilter === s.strategy ? 'ring-2 ring-blue-500/50' : ''}`}
                title={s.display_name}>
                <div className="flex items-baseline justify-between mb-1">
                  <span className="text-xs font-mono text-gray-400">{s.strategy}</span>
                  <span className="text-[10px] text-gray-600">
                    {s.trade_count} trade{s.trade_count === 1 ? '' : 's'}
                    {s.open_count > 0 && <span className="text-blue-400 ml-1">· {s.open_count} open</span>}
                  </span>
                </div>
                <div className={`text-xl font-semibold ${pnlColor}`}>
                  {s.net_pnl >= 0 ? '+' : ''}${s.net_pnl.toFixed(2)}
                </div>
                <div className="text-[10px] text-gray-500 mt-1">
                  P&L ${s.total_pnl.toFixed(2)} − comm ${s.total_commission.toFixed(2)} ·
                  win {s.win_rate.toFixed(0)}% ({s.wins}/{s.wins + s.losses})
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Table */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            {table.getHeaderGroups().map(hg => (
              <tr key={hg.id}>
                {hg.headers.map(h => (
                  <th key={h.id} onClick={h.column.getToggleSortingHandler()}
                    className="bg-[#21262d] px-3 py-2.5 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d] cursor-pointer hover:text-gray-300 whitespace-nowrap select-none">
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {{ asc: ' ▲', desc: ' ▼' }[h.column.getIsSorted() as string] ?? ''}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map(row => {
              const t = row.original;
              const open = expandedIds.has(t.id);
              const colSpan = row.getVisibleCells().length;
              return (
                <>
                  <tr key={row.id} className={`hover:bg-[#1c2128] ${t.status === 'closed' ? 'opacity-60' : ''}`}>
                    {row.getVisibleCells().map(cell => (
                      <td key={cell.id} className={`${compact ? 'px-2 py-1 text-xs' : 'px-3 py-2.5'} border-b border-[#21262d] whitespace-nowrap`}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                  {open && (
                    <tr key={`${row.id}-legs`} className="bg-[#0d1117]">
                      <td colSpan={colSpan} className="p-0 border-b border-[#21262d]">
                        <LegsPanel tradeId={t.id} />
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
        {filteredTrades.length === 0 && (
          <div className="text-center py-12 text-gray-500">No trades found</div>
        )}
      </div>
    </div>
  );
}
