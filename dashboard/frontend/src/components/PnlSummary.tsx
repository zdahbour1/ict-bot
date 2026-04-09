import type { Summary } from '../types';

export default function PnlSummary({ summary }: { summary: Summary | null }) {
  if (!summary) return null;
  const fmt = (v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;
  const clr = (v: number) => v > 0 ? 'text-green-400' : v < 0 ? 'text-red-400' : 'text-gray-300';

  return (
    <div className="flex items-center gap-6 mb-4 text-sm">
      <div className="flex items-center gap-2">
        <span className="text-gray-500">Open P&L:</span>
        <span className={`font-bold ${clr(summary.open_pnl)}`}>{fmt(summary.open_pnl)}</span>
        <span className="text-gray-600">({summary.open_trades} open)</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-gray-500">Closed:</span>
        <span className={`font-bold ${clr(summary.closed_pnl)}`}>{fmt(summary.closed_pnl)}</span>
        <span className="text-gray-600">({summary.closed_trades} closed)</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-gray-500">Day Total:</span>
        <span className={`font-bold text-lg ${clr(summary.total_pnl)}`}>{fmt(summary.total_pnl)}</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-gray-500">Win Rate:</span>
        <span className="font-bold">{summary.win_rate}%</span>
        <span className="text-gray-600">{summary.wins}W/{summary.losses}L/{summary.scratches}S</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-gray-500">Avg W/L:</span>
        <span className="text-green-400">{fmt(summary.avg_win)}</span>
        <span className="text-gray-600">/</span>
        <span className="text-red-400">{fmt(summary.avg_loss)}</span>
      </div>
    </div>
  );
}
