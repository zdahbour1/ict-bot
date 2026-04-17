import { useState, useMemo, useEffect } from 'react';
import type { ThreadStatus } from '../types';
import { useApi } from '../hooks/useApi';
import {
  useReactTable, getCoreRowModel, getSortedRowModel,
  flexRender, createColumnHelper, type SortingState,
} from '@tanstack/react-table';

// ── Stale/dead thresholds (seconds) ──
const STALE_THRESHOLD = 120;  // 2 minutes
const DEAD_THRESHOLD  = 300;  // 5 minutes

function getHealthState(updatedAt: string | null): 'ok' | 'stale' | 'dead' {
  if (!updatedAt) return 'dead';
  const age = (Date.now() - new Date(updatedAt).getTime()) / 1000;
  if (age > DEAD_THRESHOLD) return 'dead';
  if (age > STALE_THRESHOLD) return 'stale';
  return 'ok';
}

function relativeTime(ts: string | null): string {
  if (!ts) return '-';
  const secs = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (secs < 0) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s ago`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m ago`;
}

function StatusBadge({ status, health }: { status: string; health: 'ok' | 'stale' | 'dead' }) {
  // Override status display if stale or dead
  if (health === 'dead' && status !== 'stopped') {
    return <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-red-500/20 text-red-400">DEAD</span>;
  }
  if (health === 'stale' && status !== 'stopped') {
    return <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-yellow-500/20 text-yellow-400">STALE</span>;
  }
  const colors: Record<string, string> = {
    scanning: 'bg-blue-500/20 text-blue-400',
    running: 'bg-green-500/20 text-green-400',
    idle: 'bg-gray-700 text-gray-400',
    error: 'bg-red-500/20 text-red-400',
    stopped: 'bg-gray-800 text-gray-500',
    starting: 'bg-yellow-500/20 text-yellow-400',
  };
  return <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${colors[status] || 'bg-gray-700 text-gray-400'}`}>{status.toUpperCase()}</span>;
}

function HealthDot({ health }: { health: 'ok' | 'stale' | 'dead' }) {
  const color = health === 'ok' ? 'bg-green-400' : health === 'stale' ? 'bg-yellow-400' : 'bg-red-400';
  return <span className={`inline-block w-2 h-2 rounded-full ${color} mr-1`} />;
}

interface ErrorLog {
  id: number; thread_name: string; ticker: string; error_type: string;
  message: string; traceback: string | null; created_at: string;
}

interface SystemLogEntry {
  id: number; component: string; level: string;
  message: string; details: Record<string, unknown>; created_at: string;
}

const col = createColumnHelper<ThreadStatus>();

export default function ThreadsTab() {
  const { data, loading, refetch } = useApi<{ threads: ThreadStatus[] }>('/threads', 10000);
  const threads = data?.threads || [];
  const [sorting, setSorting] = useState<SortingState>([]);
  const [errorPopup, setErrorPopup] = useState<{ ticker: string; threadName: string } | null>(null);
  const [errors, setErrors] = useState<ErrorLog[]>([]);
  const [loadingErrors, setLoadingErrors] = useState(false);
  // System log state
  const [showSysLog, setShowSysLog] = useState(false);
  const [sysLogs, setSysLogs] = useState<SystemLogEntry[]>([]);
  const [sysLogLevel, setSysLogLevel] = useState<string>('all');
  const [loadingSysLog, setLoadingSysLog] = useState(false);
  // Force re-render every 5s for relative timestamps
  const [, setTick] = useState(0);
  useEffect(() => {
    const iv = setInterval(() => setTick(t => t + 1), 5000);
    return () => clearInterval(iv);
  }, []);

  const active = threads.filter(t => ['running', 'scanning', 'idle'].includes(t.status)).length;
  const staleCount = threads.filter(t => getHealthState(t.updated_at) === 'stale' && t.status !== 'stopped').length;
  const deadCount = threads.filter(t => getHealthState(t.updated_at) === 'dead' && t.status !== 'stopped').length;
  const totalErrors = threads.reduce((s, t) => s + t.error_count, 0);
  const totalScans = threads.reduce((s, t) => s + t.scans_today, 0);
  const totalTrades = threads.reduce((s, t) => s + t.trades_today, 0);

  const showErrors = async (ticker: string, threadName: string) => {
    setErrorPopup({ ticker, threadName });
    setLoadingErrors(true);
    try {
      const res = await fetch(`/api/errors?ticker=${ticker}&thread_name=${threadName}&limit=20`);
      const data = await res.json();
      setErrors(data.errors || []);
    } catch { setErrors([]); }
    setLoadingErrors(false);
  };

  // Per-thread log viewer
  const [threadLogPopup, setThreadLogPopup] = useState<{ threadName: string; ticker: string } | null>(null);
  const [threadLogs, setThreadLogs] = useState<SystemLogEntry[]>([]);
  const [loadingThreadLogs, setLoadingThreadLogs] = useState(false);
  const [threadLogFilter, setThreadLogFilter] = useState<string>('all');

  const showThreadLogs = async (threadName: string, ticker: string) => {
    setThreadLogPopup({ threadName, ticker });
    setLoadingThreadLogs(true);
    try {
      // Build list of component names to search for this thread
      const components = [threadName];

      // Map thread names to their system_log component names
      if (threadName === 'bot-main') {
        components.push('bot');
      } else if (threadName === 'exit_manager') {
        components.push('exit_manager');
      } else if (threadName === 'reconciliation') {
        // already correct
      }

      // For ticker-based threads, search all related components
      if (ticker) {
        components.push(`scanner-${ticker}`, `exit_executor-${ticker}`,
          `option_selector-${ticker}`, `trade_entry-${ticker}`);
      }
      const allLogs: SystemLogEntry[] = [];
      for (const comp of components) {
        const levelParam = threadLogFilter !== 'all' ? `&level=${threadLogFilter}` : '';
        const res = await fetch(`/api/system-log?component=${comp}&limit=30${levelParam}`);
        const data = await res.json();
        allLogs.push(...(data.logs || []));
      }
      // Sort by time descending, dedup by id
      const seen = new Set<number>();
      const deduped = allLogs.filter(l => { if (seen.has(l.id)) return false; seen.add(l.id); return true; });
      deduped.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
      setThreadLogs(deduped.slice(0, 50));
    } catch { setThreadLogs([]); }
    setLoadingThreadLogs(false);
  };

  // Refresh thread logs when filter changes
  useEffect(() => {
    if (threadLogPopup) showThreadLogs(threadLogPopup.threadName, threadLogPopup.ticker);
  }, [threadLogFilter]);

  const [reconciling, setReconciling] = useState(false);
  const triggerReconcile = async () => {
    setReconciling(true);
    try {
      await fetch('/api/bot/reconcile', { method: 'POST' });
    } catch { /* ignore */ }
    setTimeout(() => setReconciling(false), 3000);
  };

  const fetchSysLogs = async () => {
    setLoadingSysLog(true);
    try {
      const levelParam = sysLogLevel !== 'all' ? `&level=${sysLogLevel}` : '';
      const res = await fetch(`/api/system-log?limit=50${levelParam}`);
      const data = await res.json();
      setSysLogs(data.logs || []);
    } catch { setSysLogs([]); }
    setLoadingSysLog(false);
  };

  // Auto-refresh system log when visible
  useEffect(() => {
    if (showSysLog) {
      fetchSysLogs();
      const iv = setInterval(fetchSysLogs, 10000);
      return () => clearInterval(iv);
    }
  }, [showSysLog, sysLogLevel]);

  const columns = useMemo(() => [
    col.accessor('status', {
      header: 'Status',
      cell: info => {
        const health = getHealthState(info.row.original.updated_at);
        return <StatusBadge status={info.getValue()} health={health} />;
      },
    }),
    col.accessor('thread_name', { header: 'Thread', cell: info => <span className="text-xs">{info.getValue()}</span> }),
    col.accessor('ticker', { header: 'Ticker', cell: info => <strong>{info.getValue() || '-'}</strong> }),
    col.accessor('pid', { header: 'PID', cell: info => <span className="font-mono text-xs text-gray-400">{info.getValue() || '-'}</span> }),
    col.accessor('updated_at', {
      header: 'Last Heartbeat',
      cell: info => {
        const health = getHealthState(info.getValue());
        return (
          <span className="text-xs">
            <HealthDot health={health} />
            <span className={health === 'dead' ? 'text-red-400' : health === 'stale' ? 'text-yellow-400' : 'text-gray-400'}>
              {relativeTime(info.getValue())}
            </span>
          </span>
        );
      },
    }),
    col.accessor('scans_today', { header: 'Scans' }),
    col.accessor('trades_today', { header: 'Trades' }),
    col.accessor('alerts_today', { header: 'Alerts' }),
    col.accessor('error_count', {
      header: 'Errors',
      cell: info => {
        const count = info.getValue();
        const ticker = info.row.original.ticker || '';
        const threadName = info.row.original.thread_name;
        if (count > 0) {
          return (
            <button onClick={() => showErrors(ticker, threadName)}
              className="text-red-400 underline cursor-pointer hover:text-red-300 font-semibold">
              {count}
            </button>
          );
        }
        return <span>0</span>;
      },
    }),
    col.display({
      id: 'logs',
      header: 'Logs',
      cell: ({ row }) => {
        const threadName = row.original.thread_name;
        const ticker = row.original.ticker || '';
        return (
          <button onClick={() => showThreadLogs(threadName, ticker)}
            className="text-blue-400 underline cursor-pointer hover:text-blue-300 text-xs">
            View
          </button>
        );
      },
    }),
    col.accessor('last_message', {
      header: 'Last Message',
      cell: info => <span className="text-xs text-gray-500 max-w-xs truncate block">{info.getValue() || '-'}</span>,
    }),
  ], []);

  const table = useReactTable({
    data: threads,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  if (loading) return <div className="text-gray-500 py-12 text-center">Loading threads...</div>;

  return (
    <div>
      {/* Stale/Dead Alert Banner */}
      {(staleCount > 0 || deadCount > 0) && (
        <div className={`mb-4 px-4 py-2 rounded-lg text-sm font-semibold ${deadCount > 0 ? 'bg-red-500/10 border border-red-500/30 text-red-400' : 'bg-yellow-500/10 border border-yellow-500/30 text-yellow-400'}`}>
          {deadCount > 0 && <span>{deadCount} thread{deadCount > 1 ? 's' : ''} DEAD (no heartbeat &gt;{DEAD_THRESHOLD / 60}m)</span>}
          {deadCount > 0 && staleCount > 0 && <span className="mx-2">|</span>}
          {staleCount > 0 && <span>{staleCount} thread{staleCount > 1 ? 's' : ''} STALE (no heartbeat &gt;{STALE_THRESHOLD / 60}m)</span>}
        </div>
      )}

      {/* Controls + compact KPIs */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-4">
          <button onClick={refetch} className="px-3 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-400 rounded-md hover:text-white">
            Refresh
          </button>
          <button onClick={triggerReconcile} disabled={reconciling}
            className={`px-3 py-1.5 text-sm border rounded-md ${reconciling ? 'bg-green-500/20 border-green-500/30 text-green-400' : 'bg-[#21262d] border-[#30363d] text-gray-400 hover:text-white'}`}>
            {reconciling ? 'Reconciling...' : 'Reconcile Now'}
          </button>
          <button onClick={() => setShowSysLog(!showSysLog)}
            className={`px-3 py-1.5 text-sm border rounded-md ${showSysLog ? 'bg-blue-500/20 border-blue-500/30 text-blue-400' : 'bg-[#21262d] border-[#30363d] text-gray-400 hover:text-white'}`}>
            System Log
          </button>
          <span className="text-sm text-gray-400">
            <strong className="text-green-400">{active}</strong> active
            <span className="text-gray-600 mx-1">|</span>
            <strong className={totalErrors > 0 ? 'text-red-400' : ''}>{totalErrors}</strong> errors
            <span className="text-gray-600 mx-1">|</span>
            <strong>{totalScans.toLocaleString()}</strong> scans
            <span className="text-gray-600 mx-1">|</span>
            <strong>{totalTrades}</strong> trades
            <span className="text-gray-600 mx-1">|</span>
            {threads.length} threads
          </span>
        </div>
        <span className="text-xs text-gray-500">Auto-refreshes 10s</span>
      </div>

      {/* Sortable thread table */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            {table.getHeaderGroups().map(hg => (
              <tr key={hg.id}>
                {hg.headers.map(h => (
                  <th key={h.id} onClick={h.column.getToggleSortingHandler()}
                    className="bg-[#21262d] px-3 py-2 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d] cursor-pointer hover:text-gray-300 whitespace-nowrap select-none">
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {{ asc: ' ▲', desc: ' ▼' }[h.column.getIsSorted() as string] ?? ''}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map(row => (
              <tr key={row.id} className="hover:bg-[#1c2128]">
                {row.getVisibleCells().map(cell => (
                  <td key={cell.id} className="px-3 py-2 border-b border-[#21262d] whitespace-nowrap">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {threads.length === 0 && <div className="text-center py-8 text-gray-500">No threads</div>}
      </div>

      {/* System Log Panel */}
      {showSysLog && (
        <div className="mt-4 bg-[#161b22] border border-[#30363d] rounded-lg">
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
            <h3 className="text-sm font-semibold text-gray-300">System Log</h3>
            <div className="flex items-center gap-2">
              {['all', 'error', 'warn'].map(level => (
                <button key={level} onClick={() => setSysLogLevel(level)}
                  className={`px-2 py-0.5 text-xs rounded ${sysLogLevel === level ? 'bg-blue-500/20 text-blue-400' : 'text-gray-500 hover:text-gray-300'}`}>
                  {level === 'all' ? 'All' : level === 'error' ? 'Errors' : 'Warnings'}
                </button>
              ))}
              <button onClick={fetchSysLogs} className="text-xs text-gray-500 hover:text-white ml-2">Refresh</button>
            </div>
          </div>
          <div className="max-h-80 overflow-y-auto">
            {loadingSysLog ? (
              <div className="text-gray-500 py-4 text-center text-sm">Loading...</div>
            ) : sysLogs.length === 0 ? (
              <div className="text-gray-500 py-4 text-center text-sm">No log entries</div>
            ) : (
              <table className="w-full text-xs">
                <thead>
                  <tr>
                    <th className="px-3 py-1.5 text-left text-gray-500 font-semibold">Time</th>
                    <th className="px-3 py-1.5 text-left text-gray-500 font-semibold">Level</th>
                    <th className="px-3 py-1.5 text-left text-gray-500 font-semibold">Component</th>
                    <th className="px-3 py-1.5 text-left text-gray-500 font-semibold">Message</th>
                  </tr>
                </thead>
                <tbody>
                  {sysLogs.map(log => {
                    const levelColors: Record<string, string> = {
                      error: 'text-red-400 bg-red-500/10',
                      warn: 'text-yellow-400 bg-yellow-500/10',
                      info: 'text-blue-400 bg-blue-500/10',
                      debug: 'text-gray-500 bg-gray-500/10',
                    };
                    return (
                      <tr key={log.id} className="hover:bg-[#1c2128] border-b border-[#21262d]">
                        <td className="px-3 py-1.5 text-gray-500 whitespace-nowrap">
                          {log.created_at ? new Date(log.created_at).toLocaleTimeString() : '-'}
                        </td>
                        <td className="px-3 py-1.5">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${levelColors[log.level] || 'text-gray-400'}`}>
                            {log.level.toUpperCase()}
                          </span>
                        </td>
                        <td className="px-3 py-1.5 text-gray-400 font-mono">{log.component}</td>
                        <td className="px-3 py-1.5 text-gray-300 max-w-md truncate">{log.message}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {/* Thread Log Popup */}
      {threadLogPopup && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setThreadLogPopup(null)}>
          <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-6 w-[900px] max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold">
                Thread Log — <span className="text-blue-400">{threadLogPopup.ticker || threadLogPopup.threadName}</span>
              </h3>
              <div className="flex items-center gap-2">
                {['all', 'error', 'warn', 'info'].map(level => (
                  <button key={level} onClick={() => setThreadLogFilter(level)}
                    className={`px-2 py-0.5 text-xs rounded ${threadLogFilter === level ? 'bg-blue-500/20 text-blue-400' : 'text-gray-500 hover:text-gray-300'}`}>
                    {level === 'all' ? 'All' : level.charAt(0).toUpperCase() + level.slice(1)}
                  </button>
                ))}
                <button onClick={() => showThreadLogs(threadLogPopup.threadName, threadLogPopup.ticker)}
                  className="text-xs text-gray-500 hover:text-white ml-2">Refresh</button>
                <button onClick={() => setThreadLogPopup(null)} className="text-gray-500 hover:text-white text-xl ml-2">&times;</button>
              </div>
            </div>
            {loadingThreadLogs ? (
              <div className="text-gray-500 py-4 text-center">Loading logs...</div>
            ) : threadLogs.length === 0 ? (
              <div className="text-gray-500 py-4 text-center">No log entries found</div>
            ) : (
              <div className="space-y-1">
                {threadLogs.map(log => {
                  const levelColors: Record<string, string> = {
                    error: 'text-red-400 bg-red-500/10',
                    warn: 'text-yellow-400 bg-yellow-500/10',
                    info: 'text-blue-400 bg-blue-500/10',
                    debug: 'text-gray-500 bg-gray-500/10',
                  };
                  return (
                    <div key={log.id} className="bg-[#0d1117] border border-[#21262d] rounded px-3 py-2">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs text-gray-500">
                          {log.created_at ? new Date(log.created_at).toLocaleTimeString() : '-'}
                        </span>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${levelColors[log.level] || 'text-gray-400'}`}>
                          {log.level.toUpperCase()}
                        </span>
                        <span className="text-xs text-gray-500 font-mono">{log.component}</span>
                      </div>
                      <div className="text-sm text-gray-300">{log.message}</div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Error Log Popup */}
      {errorPopup && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setErrorPopup(null)}>
          <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-6 w-[700px] max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold">
                Error Log — <span className="text-red-400">{errorPopup.ticker || errorPopup.threadName}</span>
              </h3>
              <button onClick={() => setErrorPopup(null)} className="text-gray-500 hover:text-white text-xl">&times;</button>
            </div>
            {loadingErrors ? (
              <div className="text-gray-500 py-4 text-center">Loading errors...</div>
            ) : errors.length === 0 ? (
              <div className="text-gray-500 py-4 text-center">No errors found</div>
            ) : (
              <div className="space-y-3">
                {errors.map(e => (
                  <div key={e.id} className="bg-[#0d1117] border border-[#21262d] rounded-lg p-3">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs font-semibold text-red-400">{e.error_type}</span>
                      <span className="text-xs text-gray-500">{new Date(e.created_at).toLocaleString()}</span>
                    </div>
                    <div className="text-sm text-gray-300 mb-1">{e.message}</div>
                    {e.traceback && (
                      <pre className="text-xs text-gray-500 bg-[#161b22] p-2 rounded mt-1 overflow-x-auto max-h-32">{e.traceback}</pre>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
