import { useState, useMemo } from 'react';
import {
  useReactTable, getCoreRowModel, getSortedRowModel, getFilteredRowModel,
  flexRender, createColumnHelper, type SortingState, type ColumnFiltersState,
} from '@tanstack/react-table';
import type { Trade } from '../types';
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

export default function TradeTable({ trades, onRefresh, lastUpdated }: { trades: Trade[]; onRefresh: () => void; lastUpdated?: Date | null }) {
  const [sorting, setSorting] = useState<SortingState>([{ id: 'entry_time', desc: true }]);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [tickerFilter, setTickerFilter] = useState<string>('');
  const [refreshing, setRefreshing] = useState(false);

  const handleRefresh = () => {
    setRefreshing(true);
    onRefresh();
    setTimeout(() => setRefreshing(false), 500);
  };

  const filteredTrades = useMemo(() => {
    let result = trades;
    if (statusFilter) result = result.filter(t => t.status === statusFilter);
    if (tickerFilter) result = result.filter(t => t.ticker === tickerFilter);
    return result;
  }, [trades, statusFilter, tickerFilter]);

  const tickers = useMemo(() => [...new Set(trades.map(t => t.ticker))].sort(), [trades]);

  const columns = useMemo(() => [
    col.accessor('status', {
      header: 'Status',
      cell: info => <Badge text={info.getValue().toUpperCase()} variant={info.getValue()} />,
    }),
    col.accessor('ticker', { header: 'Ticker', cell: info => <strong>{info.getValue()}</strong> }),
    col.accessor('direction', { header: 'Dir' }),
    col.accessor('symbol', { header: 'Symbol', cell: info => <span className="text-xs text-gray-400">{info.getValue()}</span> }),
    col.display({
      id: 'contracts',
      header: 'Contracts',
      cell: ({ row }) => `${row.original.contracts_open} / ${row.original.contracts_entered}`,
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
    col.display({
      id: 'actions',
      header: 'Actions',
      cell: ({ row }) => {
        if (row.original.status !== 'open') return null;
        return (
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
        );
      },
    }),
  ], [onRefresh]);

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
        {lastUpdated && <span className="text-xs text-gray-500">Updated: {lastUpdated.toLocaleTimeString()}</span>}
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
      </div>

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
            {table.getRowModel().rows.map(row => (
              <tr key={row.id} className={`hover:bg-[#1c2128] ${row.original.status === 'closed' ? 'opacity-60' : ''}`}>
                {row.getVisibleCells().map(cell => (
                  <td key={cell.id} className="px-3 py-2.5 border-b border-[#21262d] whitespace-nowrap">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {filteredTrades.length === 0 && (
          <div className="text-center py-12 text-gray-500">No trades found</div>
        )}
      </div>
    </div>
  );
}
