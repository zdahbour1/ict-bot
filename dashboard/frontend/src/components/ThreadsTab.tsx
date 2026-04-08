import type { ThreadStatus } from '../types';
import { useApi } from '../hooks/useApi';

function StatusBadge({ status }: { status: string }) {
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

export default function ThreadsTab() {
  const { data, loading } = useApi<{ threads: ThreadStatus[] }>('/threads', 10000);
  const threads = data?.threads || [];

  if (loading) return <div className="text-gray-500 py-12 text-center">Loading threads...</div>;

  const active = threads.filter(t => ['running', 'scanning', 'idle'].includes(t.status)).length;
  const errors = threads.filter(t => t.status === 'error').length;
  const totalScans = threads.reduce((s, t) => s + t.scans_today, 0);

  return (
    <div>
      {/* Summary cards */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase">Total Threads</div>
          <div className="text-2xl font-bold mt-1">{threads.length}</div>
        </div>
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase">Active</div>
          <div className="text-2xl font-bold mt-1 text-green-400">{active}</div>
        </div>
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase">Errors</div>
          <div className="text-2xl font-bold mt-1 text-red-400">{errors}</div>
        </div>
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase">Total Scans Today</div>
          <div className="text-2xl font-bold mt-1">{totalScans.toLocaleString()}</div>
        </div>
      </div>

      {/* Thread table */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr>
              {['Status', 'Thread Name', 'Ticker', 'Last Scan', 'Scans', 'Trades', 'Alerts', 'Errors', 'Last Message'].map(h => (
                <th key={h} className="bg-[#21262d] px-3 py-2.5 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d]">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {threads.map(t => (
              <tr key={t.thread_name} className="hover:bg-[#1c2128]">
                <td className="px-3 py-2.5 border-b border-[#21262d]"><StatusBadge status={t.status} /></td>
                <td className="px-3 py-2.5 border-b border-[#21262d] text-xs">{t.thread_name}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d] font-bold">{t.ticker || '-'}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d] text-xs text-gray-400">
                  {t.last_scan_time ? new Date(t.last_scan_time).toLocaleTimeString() : '-'}
                </td>
                <td className="px-3 py-2.5 border-b border-[#21262d]">{t.scans_today}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d]">{t.trades_today}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d]">{t.alerts_today}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d]">
                  <span className={t.error_count > 0 ? 'text-red-400' : ''}>{t.error_count}</span>
                </td>
                <td className="px-3 py-2.5 border-b border-[#21262d] text-xs text-gray-500 max-w-xs truncate">{t.last_message || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {threads.length === 0 && <div className="text-center py-12 text-gray-500">No threads running</div>}
      </div>
    </div>
  );
}
