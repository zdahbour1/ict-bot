import type { Summary } from '../types';

function Card({ label, value, sub, color }: { label: string; value: string; sub: string; color?: string }) {
  const colorClass = color === 'green' ? 'text-green-400' : color === 'red' ? 'text-red-400' : 'text-gray-200';
  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
      <div className="text-xs text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${colorClass}`}>{value}</div>
      <div className="text-xs text-gray-500 mt-1">{sub}</div>
    </div>
  );
}

export default function PnlSummary({ summary }: { summary: Summary | null }) {
  if (!summary) return null;
  const pnlColor = (v: number) => v > 0 ? 'green' : v < 0 ? 'red' : undefined;
  const fmt = (v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;

  return (
    <div className="grid grid-cols-6 gap-4 mb-6">
      <Card label="Open P&L" value={fmt(summary.open_pnl)} sub={`${summary.open_trades} open trades`} color={pnlColor(summary.open_pnl)} />
      <Card label="Closed P&L" value={fmt(summary.closed_pnl)} sub={`${summary.closed_trades} closed trades`} color={pnlColor(summary.closed_pnl)} />
      <Card label="Day Total" value={fmt(summary.total_pnl)} sub={`${summary.total_trades} total trades`} color={pnlColor(summary.total_pnl)} />
      <Card label="Win Rate" value={`${summary.win_rate}%`} sub={`${summary.wins}W / ${summary.losses}L / ${summary.scratches}S`} />
      <Card label="Avg Win" value={fmt(summary.avg_win)} sub={`${summary.wins} wins`} color="green" />
      <Card label="Avg Loss" value={fmt(summary.avg_loss)} sub={`${summary.losses} losses`} color="red" />
    </div>
  );
}
