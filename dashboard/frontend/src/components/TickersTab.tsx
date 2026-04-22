import { useEffect, useMemo, useState } from 'react';
import type { Ticker, StrategyRow } from '../types';
import { useApi, apiPost, apiPut, apiDelete } from '../hooks/useApi';
import {
  useReactTable, getCoreRowModel, getSortedRowModel,
  flexRender, createColumnHelper, type SortingState,
} from '@tanstack/react-table';

// Multi-strategy v2 (Phase 3): the Tickers tab scopes reads/writes to a
// selected strategy. Strategy list comes from /api/strategies, filtered
// to enabled=TRUE. The `tickers` table has a NOT NULL strategy_id FK and
// a UNIQUE(symbol, strategy_id) so the same symbol can exist under
// multiple strategies independently.

const col = createColumnHelper<Ticker>();

export default function TickersTab() {
  const { data: stratData } = useApi<{ strategies: StrategyRow[] }>('/strategies', 60000);
  const allStrategies = stratData?.strategies || [];
  const enabledStrategies = useMemo(
    () => allStrategies.filter(s => s.enabled)
                       .sort((a, b) => Number(b.is_default) - Number(a.is_default)
                                       || a.name.localeCompare(b.name)),
    [allStrategies]
  );

  const [selectedStrategyId, setSelectedStrategyId] = useState<number | null>(null);

  // Pre-select default (or first enabled) once the strategy list loads.
  useEffect(() => {
    if (selectedStrategyId !== null) return;
    if (enabledStrategies.length === 0) return;
    const def = enabledStrategies.find(s => s.is_default) || enabledStrategies[0];
    setSelectedStrategyId(def.strategy_id);
  }, [enabledStrategies, selectedStrategyId]);

  // When no strategy is selected yet (first render / no enabled strategies),
  // hit the unscoped endpoint rather than passing null — matches SettingsTab.
  const endpoint = selectedStrategyId !== null
    ? `/tickers?strategy_id=${selectedStrategyId}`
    : '/tickers';
  const { data, refetch } = useApi<{ tickers: Ticker[]; active: number }>(endpoint, 30000);
  const tickers = data?.tickers || [];
  const [sorting, setSorting] = useState<SortingState>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [newSymbol, setNewSymbol] = useState('');
  const [newName, setNewName] = useState('');
  const [newContracts, setNewContracts] = useState(2);

  const selectedStrategy = enabledStrategies.find(s => s.strategy_id === selectedStrategyId);

  const handleStrategyChange = (sid: number) => {
    setShowAdd(false);
    setSelectedStrategyId(sid);
  };

  const handleToggle = async (t: Ticker) => {
    await apiPut(`/tickers/${t.id}`, { is_active: !t.is_active });
    refetch();
  };

  const handleAdd = async () => {
    if (!newSymbol.trim() || selectedStrategyId === null) return;
    await apiPost('/tickers', {
      symbol: newSymbol.trim(),
      name: newName.trim() || null,
      contracts: newContracts,
      strategy_id: selectedStrategyId,
    });
    setNewSymbol(''); setNewName(''); setNewContracts(2); setShowAdd(false);
    refetch();
  };

  const handleDelete = async (t: Ticker) => {
    if (confirm(`Delete ticker ${t.symbol}?`)) {
      await apiDelete(`/tickers/${t.id}`);
      refetch();
    }
  };

  const columns = useMemo(() => [
    col.accessor('is_active', {
      header: 'Active',
      cell: info => {
        const t = info.row.original;
        return (
          <button onClick={() => handleToggle(t)}
            className={`w-10 h-5 rounded-full relative transition-colors ${t.is_active ? 'bg-green-500' : 'bg-gray-600'}`}>
            <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full transition-transform ${t.is_active ? 'left-5' : 'left-0.5'}`} />
          </button>
        );
      },
    }),
    col.accessor('symbol', { header: 'Symbol', cell: info => <strong>{info.getValue()}</strong> }),
    col.accessor('name', { header: 'Name', cell: info => <span className="text-gray-400">{info.getValue() || '-'}</span> }),
    col.accessor('contracts', { header: 'Contracts' }),
    col.accessor('notes', { header: 'Notes', cell: info => <span className="text-xs text-gray-500 max-w-32 truncate block">{info.getValue() || '-'}</span> }),
    col.accessor('created_at', {
      header: 'Created',
      cell: info => <span className="text-xs text-gray-500">{info.getValue() ? new Date(info.getValue()).toLocaleDateString() : '-'}</span>,
    }),
    col.accessor('updated_at', {
      header: 'Updated',
      cell: info => <span className="text-xs text-gray-500">{info.getValue() ? new Date(info.getValue()).toLocaleDateString() : '-'}</span>,
    }),
    col.display({
      id: 'actions',
      header: '',
      cell: ({ row }) => (
        <button onClick={() => handleDelete(row.original)}
          className="px-2 py-0.5 text-xs bg-[#21262d] border border-[#30363d] text-gray-500 rounded hover:text-red-400 hover:border-red-400">
          Delete
        </button>
      ),
    }),
  ], []);

  const table = useReactTable({
    data: tickers,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div>
      {/* Strategy scope selector — mirrors SettingsTab pattern. */}
      <div className="mb-4 flex items-center gap-3">
        <label className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          Strategy scope
        </label>
        <select
          value={selectedStrategyId ?? ''}
          onChange={e => handleStrategyChange(Number(e.target.value))}
          className="px-2 py-1 bg-[#21262d] border border-[#30363d] text-gray-200 rounded text-sm"
        >
          {enabledStrategies.length === 0 && <option value="">(no enabled strategies)</option>}
          {enabledStrategies.map(s => (
            <option key={s.strategy_id} value={s.strategy_id}>
              {s.display_name}{s.is_default ? ' (default)' : ''}
            </option>
          ))}
        </select>
        {selectedStrategy && (
          <span className="text-xs text-gray-500">
            Tickers for <span className="text-gray-300 font-mono">{selectedStrategy.name}</span>
          </span>
        )}
      </div>

      <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-2 mb-3 text-xs text-yellow-400">
        Changes take effect on next bot restart. Active tickers get scanner threads.
        Tickers are scoped to the selected strategy — the same symbol can exist independently under different strategies.
      </div>

      <h3 className="text-sm font-semibold text-gray-300 mb-2">
        {selectedStrategy
          ? <>Tickers for <span className="text-blue-400">{selectedStrategy.display_name}</span></>
          : 'Tickers'}
      </h3>

      <div className="flex items-center gap-3 mb-3">
        <button onClick={() => setShowAdd(true)}
          disabled={selectedStrategyId === null}
          className="px-3 py-1.5 text-sm bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-40 disabled:cursor-not-allowed">
          + Add Ticker
        </button>
        <button onClick={refetch} className="px-3 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-400 rounded-md hover:text-white">Refresh</button>
        <span className="text-sm text-gray-500">{data?.active || 0} active / {tickers.length} total</span>
      </div>

      {showAdd && (
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-3 mb-3 flex items-end gap-3">
          <div>
            <label className="text-xs text-gray-500 block mb-1">Symbol</label>
            <input value={newSymbol} onChange={e => setNewSymbol(e.target.value.toUpperCase())}
              className="px-2 py-1 bg-[#21262d] border border-[#30363d] text-gray-200 rounded text-sm w-20" placeholder="AAPL" />
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">Name</label>
            <input value={newName} onChange={e => setNewName(e.target.value)}
              className="px-2 py-1 bg-[#21262d] border border-[#30363d] text-gray-200 rounded text-sm w-40" placeholder="Apple Inc." />
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">Contracts</label>
            <input type="number" value={newContracts} onChange={e => setNewContracts(Number(e.target.value))}
              className="px-2 py-1 bg-[#21262d] border border-[#30363d] text-gray-200 rounded text-sm w-16" min={1} />
          </div>
          <span className="text-xs text-gray-500 pb-1">
            → <span className="text-gray-300 font-mono">{selectedStrategy?.name ?? '(none)'}</span>
          </span>
          <button onClick={handleAdd} className="px-3 py-1 text-sm bg-blue-600 text-white rounded">Save</button>
          <button onClick={() => setShowAdd(false)} className="px-3 py-1 text-sm bg-[#21262d] border border-[#30363d] text-gray-400 rounded">Cancel</button>
        </div>
      )}

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
              <tr key={row.id} className={`hover:bg-[#1c2128] ${!row.original.is_active ? 'opacity-40' : ''}`}>
                {row.getVisibleCells().map(cell => (
                  <td key={cell.id} className="px-3 py-2 border-b border-[#21262d] whitespace-nowrap">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
