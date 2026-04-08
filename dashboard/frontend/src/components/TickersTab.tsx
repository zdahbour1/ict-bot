import { useState } from 'react';
import type { Ticker } from '../types';
import { useApi, apiPost, apiPut, apiDelete } from '../hooks/useApi';

export default function TickersTab() {
  const { data, refetch } = useApi<{ tickers: Ticker[]; active: number }>('/tickers', 30000);
  const tickers = data?.tickers || [];
  const [showAdd, setShowAdd] = useState(false);
  const [newSymbol, setNewSymbol] = useState('');
  const [newName, setNewName] = useState('');
  const [newContracts, setNewContracts] = useState(2);

  const handleToggle = async (t: Ticker) => {
    await apiPut(`/tickers/${t.id}`, { is_active: !t.is_active });
    refetch();
  };

  const handleAdd = async () => {
    if (!newSymbol.trim()) return;
    await apiPost('/tickers', { symbol: newSymbol.trim(), name: newName.trim() || null, contracts: newContracts });
    setNewSymbol(''); setNewName(''); setNewContracts(2); setShowAdd(false);
    refetch();
  };

  const handleDelete = async (t: Ticker) => {
    if (confirm(`Delete ticker ${t.symbol}? This cannot be undone.`)) {
      await apiDelete(`/tickers/${t.id}`);
      refetch();
    }
  };

  return (
    <div>
      <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-3 mb-4 text-sm text-yellow-400">
        Changes take effect on next bot restart. Active tickers will get scanner threads.
      </div>

      <div className="flex items-center gap-3 mb-4">
        <button onClick={() => setShowAdd(true)} className="px-3 py-1.5 text-sm bg-green-600 text-white rounded-md hover:bg-green-700">
          + Add Ticker
        </button>
        <span className="text-sm text-gray-500">{data?.active || 0} active / {tickers.length} total</span>
      </div>

      {showAdd && (
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4 mb-4 flex items-end gap-3">
          <div>
            <label className="text-xs text-gray-500 block mb-1">Symbol</label>
            <input value={newSymbol} onChange={e => setNewSymbol(e.target.value.toUpperCase())}
              className="px-3 py-1.5 bg-[#21262d] border border-[#30363d] text-gray-200 rounded-md text-sm w-24" placeholder="AAPL" />
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">Name</label>
            <input value={newName} onChange={e => setNewName(e.target.value)}
              className="px-3 py-1.5 bg-[#21262d] border border-[#30363d] text-gray-200 rounded-md text-sm w-48" placeholder="Apple Inc." />
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">Contracts</label>
            <input type="number" value={newContracts} onChange={e => setNewContracts(Number(e.target.value))}
              className="px-3 py-1.5 bg-[#21262d] border border-[#30363d] text-gray-200 rounded-md text-sm w-20" min={1} />
          </div>
          <button onClick={handleAdd} className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md">Save</button>
          <button onClick={() => setShowAdd(false)} className="px-3 py-1.5 text-sm bg-[#21262d] border border-[#30363d] text-gray-400 rounded-md">Cancel</button>
        </div>
      )}

      <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr>
              {['Active', 'Symbol', 'Name', 'Contracts', 'Notes', 'Created', 'Updated', 'Actions'].map(h => (
                <th key={h} className="bg-[#21262d] px-3 py-2.5 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d]">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tickers.map(t => (
              <tr key={t.id} className={`hover:bg-[#1c2128] ${!t.is_active ? 'opacity-40' : ''}`}>
                <td className="px-3 py-2.5 border-b border-[#21262d]">
                  <button onClick={() => handleToggle(t)}
                    className={`w-10 h-5 rounded-full relative transition-colors ${t.is_active ? 'bg-green-500' : 'bg-gray-600'}`}>
                    <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full transition-transform ${t.is_active ? 'left-5' : 'left-0.5'}`} />
                  </button>
                </td>
                <td className="px-3 py-2.5 border-b border-[#21262d] font-bold">{t.symbol}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d] text-gray-400">{t.name || '-'}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d]">{t.contracts}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d] text-xs text-gray-500 max-w-32 truncate">{t.notes || '-'}</td>
                <td className="px-3 py-2.5 border-b border-[#21262d] text-xs text-gray-500">
                  {t.created_at ? new Date(t.created_at).toLocaleDateString() : '-'}
                </td>
                <td className="px-3 py-2.5 border-b border-[#21262d] text-xs text-gray-500">
                  {t.updated_at ? new Date(t.updated_at).toLocaleDateString() : '-'}
                </td>
                <td className="px-3 py-2.5 border-b border-[#21262d]">
                  <button onClick={() => handleDelete(t)}
                    className="px-2 py-0.5 text-xs bg-[#21262d] border border-[#30363d] text-gray-500 rounded hover:text-red-400 hover:border-red-400">
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
