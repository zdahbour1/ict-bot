import { useState } from 'react';
import { useApi } from '../hooks/useApi';
import {
  BarChart, Bar, LineChart, Line, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from 'recharts';

const COLORS = {
  green: '#3fb950', red: '#f85149', blue: '#58a6ff', yellow: '#d29922',
  purple: '#bc8cff', cyan: '#39d2c0', gray: '#8b949e',
};
// const CHART_COLORS = ['#58a6ff', '#3fb950', '#f85149', '#d29922', '#bc8cff', '#39d2c0', '#ff7b72', '#79c0ff'];

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

export default function AnalyticsTab() {
  const [selectedDate, setSelectedDate] = useState<string>('');
  const endpoint = selectedDate ? `/analytics?date=${selectedDate}` : '/analytics';
  const { data, loading, refetch } = useApi<any>(endpoint, 60000);

  if (loading) return <div className="text-gray-500 py-12 text-center">Loading analytics...</div>;
  if (!data) return <div className="text-gray-500 py-12 text-center">No data available</div>;

  const best = data.best_trade;
  const worst = data.worst_trade;
  const ct = data.contract_type || { calls: { pnl: 0, count: 0 }, puts: { pnl: 0, count: 0 } };
  const pieData = [
    { name: 'Calls', value: ct.calls.count, pnl: ct.calls.pnl },
    { name: 'Puts', value: ct.puts.count, pnl: ct.puts.pnl },
  ];

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <select value={selectedDate} onChange={e => setSelectedDate(e.target.value)}
            className="px-2 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-300 rounded-md">
            <option value="">Latest ({data.date})</option>
            {(data.available_dates || []).map((d: string) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
          <span className="text-sm text-gray-400">
            {data.total_trades} trades ({data.total_closed} closed, {data.total_open} open) | Avg hold: {data.avg_hold_minutes} min
          </span>
        </div>
        <button onClick={refetch} className="px-3 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-400 rounded-md hover:text-white">
          Refresh
        </button>
      </div>

      {/* Top stats */}
      <div className="grid grid-cols-5 gap-3 mb-4">
        <StatCard label="Best Trade" value={best ? `+$${best.pnl_usd.toFixed(0)}` : '-'}
          sub={best ? `${best.ticker} ${best.direction === 'LONG' ? 'Call' : 'Put'} (${best.pnl_pct}%)` : ''} color="green" />
        <StatCard label="Worst Trade" value={worst ? `$${worst.pnl_usd.toFixed(0)}` : '-'}
          sub={worst ? `${worst.ticker} ${worst.direction === 'LONG' ? 'Call' : 'Put'} (${worst.pnl_pct}%)` : ''} color="red" />
        <StatCard label="Calls P&L" value={`${ct.calls.pnl >= 0 ? '+' : ''}$${ct.calls.pnl.toFixed(0)}`}
          sub={`${ct.calls.count} trades`} color={ct.calls.pnl >= 0 ? 'green' : 'red'} />
        <StatCard label="Puts P&L" value={`${ct.puts.pnl >= 0 ? '+' : ''}$${ct.puts.pnl.toFixed(0)}`}
          sub={`${ct.puts.count} trades`} color={ct.puts.pnl >= 0 ? 'green' : 'red'} />
        <StatCard label="Avg Hold Time" value={`${data.avg_hold_minutes} min`} />
      </div>

      {/* Row 1: Cumulative P&L + P&L by Ticker */}
      <div className="grid grid-cols-2 gap-4 mb-4">
        <ChartCard title="Cumulative P&L (Timeline)">
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={data.cumulative_pnl || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="time" tick={{ fill: '#8b949e', fontSize: 11 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
              <Line type="monotone" dataKey="pnl" stroke={COLORS.blue} strokeWidth={2} dot={{ r: 3 }} />
            </LineChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="P&L by Ticker">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={data.pnl_by_ticker || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="ticker" tick={{ fill: '#8b949e', fontSize: 11 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
                formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
              <Bar dataKey="pnl" fill={COLORS.blue}>
                {(data.pnl_by_ticker || []).map((entry: any, i: number) => (
                  <Cell key={i} fill={entry.pnl >= 0 ? COLORS.green : COLORS.red} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Row 2: P&L by Hour (exit) + P&L by Hour (entry) */}
      <div className="grid grid-cols-2 gap-4 mb-4">
        <ChartCard title="P&L by Hour (Exit Time)">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={data.pnl_by_exit_hour || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="hour" tick={{ fill: '#8b949e', fontSize: 11 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }} />
              <Bar dataKey="pnl" fill={COLORS.blue}>
                {(data.pnl_by_exit_hour || []).map((entry: any, i: number) => (
                  <Cell key={i} fill={entry.pnl >= 0 ? COLORS.green : COLORS.red} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="P&L by Hour (Entry Time)">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={data.pnl_by_entry_hour || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="hour" tick={{ fill: '#8b949e', fontSize: 11 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }} />
              <Bar dataKey="pnl" fill={COLORS.purple}>
                {(data.pnl_by_entry_hour || []).map((entry: any, i: number) => (
                  <Cell key={i} fill={entry.pnl >= 0 ? COLORS.green : COLORS.red} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Row 3: Risk Capital + Contract Type Pie + Contracts by Hour */}
      <div className="grid grid-cols-3 gap-4 mb-4">
        <ChartCard title="Risk Capital by Hour (Premium Deployed)">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={data.risk_by_hour || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="hour" tick={{ fill: '#8b949e', fontSize: 11 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={v => `$${v}`} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }} />
              <Bar dataKey="capital" fill={COLORS.yellow} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Profit by Contract Type">
          <ResponsiveContainer width="100%" height={180}>
            <PieChart>
              <Pie data={pieData} cx="50%" cy="50%" innerRadius={40} outerRadius={70}
                dataKey="value" nameKey="name" label={({ name, value }) => `${name}: ${value}`}>
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

        <ChartCard title="Contracts Opened by Hour">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={data.contracts_by_hour || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis dataKey="hour" tick={{ fill: '#8b949e', fontSize: 11 }} />
              <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} />
              <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }} />
              <Bar dataKey="contracts" fill={COLORS.cyan} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Row 4: Exit Reasons */}
      <ChartCard title="Exit Reasons Breakdown">
        <ResponsiveContainer width="100%" height={180}>
          <BarChart data={data.exit_reasons || []} layout="vertical">
            <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
            <XAxis type="number" tick={{ fill: '#8b949e', fontSize: 11 }} />
            <YAxis type="category" dataKey="reason" tick={{ fill: '#8b949e', fontSize: 10 }} width={120} />
            <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #30363d', color: '#e1e4e8' }}
              formatter={(v: any, name: any) => [name === 'count' ? `${v} trades` : `$${Number(v).toFixed(0)}`, name]} />
            <Bar dataKey="count" fill={COLORS.blue} name="Trades" />
            <Bar dataKey="pnl" fill={COLORS.green} name="P&L ($)" />
          </BarChart>
        </ResponsiveContainer>
      </ChartCard>
    </div>
  );
}
