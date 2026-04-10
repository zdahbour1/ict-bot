import { useState, useCallback } from 'react';
import { useApi } from '../hooks/useApi';
import {
  BarChart, Bar, LineChart, Line, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from 'recharts';

const COLORS = {
  green: '#3fb950', red: '#f85149', blue: '#58a6ff', yellow: '#d29922',
  purple: '#bc8cff', cyan: '#39d2c0',
};

function ChartCard({ title, children, className = '' }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={`bg-[#161b22] border border-[#30363d] rounded-lg p-4 ${className}`}>
      <h3 className="text-xs text-gray-500 uppercase tracking-wide mb-3">{title}</h3>
      {children}
    </div>
  );
}

function StatCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  const clr = color === 'green' ? 'text-green-400' : color === 'red' ? 'text-red-400' : 'text-gray-200';
  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`text-lg font-bold ${clr}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
    </div>
  );
}

function DrilldownModal({ trades, title, onClose }: { trades: any[]; title: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-6 w-[900px] max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-bold">{title} <span className="text-gray-500 text-sm">({trades.length} trades)</span></h3>
          <button onClick={onClose} className="text-gray-500 hover:text-white text-xl">&times;</button>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr>
              {['Ticker', 'Type', 'Entry', 'Exit', 'P&L $', 'P&L %', 'Reason', 'Entry Time', 'Exit Time', 'Hold'].map(h => (
                <th key={h} className="bg-[#21262d] px-2 py-1.5 text-left text-xs text-gray-500 border-b border-[#30363d]">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.map((t: any, i: number) => (
              <tr key={i} className="hover:bg-[#1c2128]">
                <td className="px-2 py-1.5 border-b border-[#21262d] font-bold">{t.ticker}</td>
                <td className="px-2 py-1.5 border-b border-[#21262d]">
                  <span className={t.contract_type === 'Call' ? 'text-green-400' : 'text-red-400'}>{t.contract_type}</span>
                </td>
                <td className="px-2 py-1.5 border-b border-[#21262d]">${Number(t.entry_price).toFixed(2)}</td>
                <td className="px-2 py-1.5 border-b border-[#21262d]">{t.exit_price ? `$${Number(t.exit_price).toFixed(2)}` : '-'}</td>
                <td className={`px-2 py-1.5 border-b border-[#21262d] ${Number(t.pnl_usd) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  ${Number(t.pnl_usd).toFixed(0)}
                </td>
                <td className={`px-2 py-1.5 border-b border-[#21262d] ${Number(t.pnl_pct) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {Number(t.pnl_pct).toFixed(1)}%
                </td>
                <td className="px-2 py-1.5 border-b border-[#21262d] text-xs text-gray-400">{t.exit_reason || '-'}</td>
                <td className="px-2 py-1.5 border-b border-[#21262d] text-xs">{t.entry_time} PT</td>
                <td className="px-2 py-1.5 border-b border-[#21262d] text-xs">{t.exit_time || '-'} PT</td>
                <td className="px-2 py-1.5 border-b border-[#21262d] text-xs">{t.hold_min ? `${t.hold_min}m` : '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function AnalyticsTab() {
  const [startDate, setStartDate] = useState<string>('');
  const [endDate, setEndDate] = useState<string>('');
  const [drilldown, setDrilldown] = useState<{ trades: any[]; title: string } | null>(null);

  const params = [
    startDate ? `start=${startDate}` : '',
    endDate ? `end=${endDate}` : '',
  ].filter(Boolean).join('&');
  const endpoint = `/analytics${params ? '?' + params : ''}`;
  const { data, loading, refetch } = useApi<any>(endpoint, 60000);

  const fetchDrilldown = useCallback(async (filters: Record<string, any>, title: string) => {
    const qs = new URLSearchParams();
    if (startDate) qs.set('start', startDate);
    if (endDate) qs.set('end', endDate);
    Object.entries(filters).forEach(([k, v]) => { if (v != null) qs.set(k, String(v)); });
    try {
      const res = await fetch(`/api/analytics/drilldown?${qs.toString()}&_t=${Date.now()}`);
      const d = await res.json();
      setDrilldown({ trades: d.trades || [], title });
    } catch { /* ignore */ }
  }, [startDate, endDate]);

  if (loading) return <div className="text-gray-500 py-12 text-center">Loading analytics...</div>;
  if (!data || data.error) return <div className="text-gray-500 py-12 text-center">{data?.error || 'No data'}</div>;

  const s = data.summary || {};
  const best = data.best_trade;
  const worst = data.worst_trade;
  const streaks = data.streaks || {};
  const ctData = (data.contract_type || []).map((c: any) => ({ name: c.contract_type, value: Number(c.trades), pnl: Number(c.pnl) }));

  const setQuickRange = (days: number | 'all') => {
    if (days === 'all') {
      setStartDate(data.available_dates?.[data.available_dates.length - 1] || '');
      setEndDate(data.available_dates?.[0] || '');
    } else if (days === 0) {
      setStartDate(''); setEndDate('');
    } else {
      const end = data.available_dates?.[0] || '';
      const d = new Date(end);
      d.setDate(d.getDate() - days + 1);
      setStartDate(d.toISOString().split('T')[0]);
      setEndDate(end);
    }
  };

  const fmtHour = (h: any) => {
    const hr = Number(h);
    if (hr === 0) return '12 AM';
    if (hr < 12) return `${hr} AM`;
    if (hr === 12) return '12 PM';
    return `${hr - 12} PM`;
  };

  return (
    <div>
      {/* Date range filter */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <input type="date" value={startDate || data.start || ''} onChange={e => setStartDate(e.target.value)}
            className="px-2 py-1 text-sm bg-[#21262d] border border-[#30363d] text-gray-300 rounded" />
          <span className="text-gray-500">to</span>
          <input type="date" value={endDate || data.end || ''} onChange={e => setEndDate(e.target.value)}
            className="px-2 py-1 text-sm bg-[#21262d] border border-[#30363d] text-gray-300 rounded" />
          <div className="flex gap-1 ml-2">
            {[
              { label: 'Latest', fn: () => setQuickRange(0) },
              { label: '5 Days', fn: () => setQuickRange(5) },
              { label: 'All', fn: () => setQuickRange('all') },
            ].map(b => (
              <button key={b.label} onClick={b.fn}
                className="px-2 py-1 text-xs bg-[#21262d] border border-[#30363d] text-gray-400 rounded hover:text-white">
                {b.label}
              </button>
            ))}
          </div>
          <span className="text-sm text-gray-500 ml-3">
            {s.total_trades || 0} trades | {s.unique_tickers || 0} tickers | Median P&L: ${s.median_pnl || 0} | Std: ${s.pnl_stddev || 0}
          </span>
        </div>
        <button onClick={refetch} className="px-3 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-400 rounded-md hover:text-white">
          Refresh
        </button>
      </div>

      {/* Top stats */}
      <div className="grid grid-cols-6 gap-3 mb-4">
        <StatCard label="Best Trade" value={best ? `+$${Number(best.pnl_usd).toFixed(0)}` : '-'}
          sub={best ? `${best.ticker} ${best.pnl_pct}%` : ''} color="green" />
        <StatCard label="Worst Trade" value={worst ? `$${Number(worst.pnl_usd).toFixed(0)}` : '-'}
          sub={worst ? `${worst.ticker} ${worst.pnl_pct}%` : ''} color="red" />
        <StatCard label="Win Streak" value={`${streaks.max_win_streak || 0}`} sub="consecutive wins" color="green" />
        <StatCard label="Loss Streak" value={`${streaks.max_loss_streak || 0}`} sub="consecutive losses" color="red" />
        <StatCard label="Avg Hold" value={`${s.avg_hold || 0} min`} />
        <StatCard label="Risk Capital" value={`$${Number(s.total_risk_capital || 0).toFixed(0)}`} sub="total premium" />
      </div>

      {/* Row 1: Cumulative P&L + P&L by Ticker */}
      <div className="grid grid-cols-2 gap-4 mb-4">
        <ChartCard title="Cumulative P&L (Timeline PT)">
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={(data.cumulative_pnl || []).map((d: any) => ({ ...d, pnl: Number(d.cumulative_pnl) }))}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="time" tick={{ fill: '#8b949e', fontSize: 11 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
              <Line type="monotone" dataKey="pnl" stroke={COLORS.blue} strokeWidth={2} dot={{ r: 3 }} />
            </LineChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="P&L by Ticker (click to drill down)">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={(data.pnl_by_ticker || []).map((d: any) => ({ ...d, pnl: Number(d.pnl) }))}
              onClick={(e: any) => { if (e?.activeLabel) fetchDrilldown({ ticker: e.activeLabel }, `Trades: ${e.activeLabel}`); }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="ticker" tick={{ fill: '#8b949e', fontSize: 11 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
              <Bar dataKey="pnl" cursor="pointer">
                {(data.pnl_by_ticker || []).map((entry: any, i: number) => (
                  <Cell key={i} fill={Number(entry.pnl) >= 0 ? COLORS.green : COLORS.red} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Row 2: P&L by Hour */}
      <div className="grid grid-cols-2 gap-4 mb-4">
        <ChartCard title="P&L by Exit Hour PT (click to drill down)">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={(data.pnl_by_exit_hour || []).map((d: any) => ({ ...d, hour_label: fmtHour(d.hour), pnl: Number(d.pnl) }))}
              onClick={(e: any) => { const h = data.pnl_by_exit_hour?.[e?.activeTooltipIndex]?.hour; if (h != null) fetchDrilldown({ hour: h, hour_type: 'exit' }, `Exits at ${fmtHour(h)} PT`); }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="hour_label" tick={{ fill: '#8b949e', fontSize: 10 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }} />
              <Bar dataKey="pnl" cursor="pointer">
                {(data.pnl_by_exit_hour || []).map((e: any, i: number) => (
                  <Cell key={i} fill={Number(e.pnl) >= 0 ? COLORS.green : COLORS.red} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="P&L by Entry Hour PT (click to drill down)">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={(data.pnl_by_entry_hour || []).map((d: any) => ({ ...d, hour_label: fmtHour(d.hour), pnl: Number(d.pnl) }))}
              onClick={(e: any) => { const h = data.pnl_by_entry_hour?.[e?.activeTooltipIndex]?.hour; if (h != null) fetchDrilldown({ hour: h, hour_type: 'entry' }, `Entries at ${fmtHour(h)} PT`); }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="hour_label" tick={{ fill: '#8b949e', fontSize: 10 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }} />
              <Bar dataKey="pnl" cursor="pointer">
                {(data.pnl_by_entry_hour || []).map((e: any, i: number) => (
                  <Cell key={i} fill={Number(e.pnl) >= 0 ? COLORS.green : COLORS.red} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Row 3: Risk + Contract Type + Contracts */}
      <div className="grid grid-cols-3 gap-4 mb-4">
        <ChartCard title="Risk Capital by Hour PT">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={(data.risk_by_hour || []).map((d: any) => ({ ...d, hour_label: fmtHour(d.hour), capital: Number(d.capital) }))}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="hour_label" tick={{ fill: '#8b949e', fontSize: 10 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }} />
              <Bar dataKey="capital" fill={COLORS.yellow} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="P&L by Contract Type (click to drill down)">
          <ResponsiveContainer width="100%" height={180}>
            <PieChart>
              <Pie data={ctData} cx="50%" cy="50%" innerRadius={40} outerRadius={70}
                dataKey="value" nameKey="name" label={({ name, value }: any) => `${name}: ${value}`}
                onClick={(e: any) => { if (e?.name) fetchDrilldown({ contract_type: e.name }, `${e.name} Trades`); }}
                cursor="pointer">
                <Cell fill={COLORS.green} />
                <Cell fill={COLORS.red} />
              </Pie>
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                formatter={(v: any, name: any, props: any) => [
                  `${v} trades | P&L: $${props?.payload?.pnl?.toFixed(0) || 0}`, name
                ]} />
              <Legend />
            </PieChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Contracts by Hour PT">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={(data.contracts_by_hour || []).map((d: any) => ({ ...d, hour_label: fmtHour(d.hour), contracts: Number(d.contracts) }))}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="hour_label" tick={{ fill: '#8b949e', fontSize: 10 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }} />
              <Bar dataKey="contracts" fill={COLORS.cyan} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Row 4: Exit Reasons */}
      <ChartCard title="Exit Reasons (click to drill down)">
        <ResponsiveContainer width="100%" height={180}>
          <BarChart data={(data.exit_reasons || []).map((d: any) => ({ ...d, count: Number(d.count), pnl: Number(d.pnl) }))} layout="vertical"
            onClick={(e: any) => { if (e?.activeLabel) fetchDrilldown({ exit_reason: e.activeLabel }, `Exit: ${e.activeLabel}`); }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
            <XAxis type="number" tick={{ fill: '#8b949e', fontSize: 11 }} />
            <YAxis type="category" dataKey="reason" tick={{ fill: '#8b949e', fontSize: 10 }} width={140} />
            <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
              formatter={(v: any, name: any) => [name === 'count' ? `${v} trades` : `$${Number(v).toFixed(0)}`, name]} />
            <Bar dataKey="count" fill={COLORS.blue} name="Trades" cursor="pointer" />
            <Bar dataKey="pnl" fill={COLORS.green} name="P&L ($)" cursor="pointer" />
          </BarChart>
        </ResponsiveContainer>
      </ChartCard>

      {/* Drilldown Modal */}
      {drilldown && (
        <DrilldownModal trades={drilldown.trades} title={drilldown.title} onClose={() => setDrilldown(null)} />
      )}
    </div>
  );
}
