import type { Summary } from '../types';

export default function PnlSummary({ summary }: { summary: Summary | null }) {
  if (!summary) return null;
  const fmt = (v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;
  const clr = (v: number) => v > 0 ? 'text-green-400' : v < 0 ? 'text-red-400' : 'text-gray-300';

  return (
    <div className="flex items-center gap-6 mb-4 text-sm">
      {/* Trade counts — prominent badges */}
      <div className="flex items-center gap-1.5">
        <span className="px-2 py-0.5 rounded bg-blue-500/20 text-blue-400 font-bold">{summary.total_trades}</span>
        <span className="text-gray-500">total</span>
        <span className="text-gray-700 mx-0.5">|</span>
        <span className="px-2 py-0.5 rounded bg-green-500/15 text-green-400 font-bold">{summary.open_trades}</span>
        <span className="text-gray-500">open</span>
        <span className="text-gray-700 mx-0.5">|</span>
        <span className="px-2 py-0.5 rounded bg-gray-500/15 text-gray-300 font-bold">{summary.closed_trades}</span>
        <span className="text-gray-500">closed</span>
        {summary.errored_trades > 0 && (
          <>
            <span className="text-gray-700 mx-0.5">|</span>
            <span className="px-2 py-0.5 rounded bg-red-500/15 text-red-400 font-bold">{summary.errored_trades}</span>
            <span className="text-gray-500">errored</span>
          </>
        )}
      </div>
      <span className="text-gray-700">|</span>
      <div className="flex items-center gap-2">
        <span className="text-gray-500">Open P&L:</span>
        <span className={`font-bold ${clr(summary.open_pnl)}`}>{fmt(summary.open_pnl)}</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-gray-500">Closed:</span>
        <span className={`font-bold ${clr(summary.closed_pnl)}`}>{fmt(summary.closed_pnl)}</span>
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
