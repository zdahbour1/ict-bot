import { useState } from 'react';
import type { Setting } from '../types';
import { useApi, apiPut } from '../hooks/useApi';

export default function SettingsTab() {
  const { data, refetch } = useApi<{ settings: Record<string, Setting[]> }>('/settings', 30000);
  const grouped = data?.settings || {};
  const [editKey, setEditKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [showSecrets, setShowSecrets] = useState<Set<string>>(new Set());

  const categories = Object.keys(grouped).sort();

  const handleSave = async (key: string) => {
    await apiPut(`/settings/${key}`, { value: editValue });
    setEditKey(null);
    refetch();
  };

  const toggleSecret = (key: string) => {
    const next = new Set(showSecrets);
    if (next.has(key)) next.delete(key); else next.add(key);
    setShowSecrets(next);
  };

  const categoryLabels: Record<string, string> = {
    broker: 'Broker Configuration',
    strategy: 'ICT Strategy Parameters',
    exit_rules: 'Exit Rules',
    trade_window: 'Trade Window',
    email: 'Email Alerts',
    webhook: 'Webhook Server',
    general: 'General',
  };

  return (
    <div>
      <div className="bg-blue-500/10 border border-blue-500/30 rounded-lg p-3 mb-4 text-sm text-blue-400">
        Settings are saved to the database. Click "Reload Bot Settings" for the running bot to pick up changes.
      </div>

      {categories.map(cat => (
        <div key={cat} className="mb-6">
          <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wide mb-2">
            {categoryLabels[cat] || cat}
          </h3>
          <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr>
                  {['Key', 'Value', 'Type', 'Description', ''].map(h => (
                    <th key={h} className="bg-[#21262d] px-3 py-2 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d]">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {grouped[cat]?.map(s => (
                  <tr key={s.key} className="hover:bg-[#1c2128]">
                    <td className="px-3 py-2 border-b border-[#21262d] font-mono text-xs text-blue-400">{s.key}</td>
                    <td className="px-3 py-2 border-b border-[#21262d]">
                      {editKey === s.key ? (
                        <div className="flex gap-2">
                          <input value={editValue} onChange={e => setEditValue(e.target.value)}
                            className="px-2 py-1 bg-[#21262d] border border-blue-500 text-gray-200 rounded text-xs w-48"
                            autoFocus onKeyDown={e => e.key === 'Enter' && handleSave(s.key)} />
                          <button onClick={() => handleSave(s.key)} className="text-xs text-green-400 hover:underline">Save</button>
                          <button onClick={() => setEditKey(null)} className="text-xs text-gray-500 hover:underline">Cancel</button>
                        </div>
                      ) : (
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-gray-300">
                            {s.is_secret && !showSecrets.has(s.key) ? '********' : s.value || '(empty)'}
                          </span>
                          {s.is_secret && (
                            <button onClick={() => toggleSecret(s.key)} className="text-xs text-gray-500 hover:text-gray-300">
                              {showSecrets.has(s.key) ? 'hide' : 'show'}
                            </button>
                          )}
                          <button onClick={() => { setEditKey(s.key); setEditValue(s.value === '********' ? '' : s.value); }}
                            className="text-xs text-blue-400 hover:underline">edit</button>
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2 border-b border-[#21262d] text-xs text-gray-500">{s.data_type}</td>
                    <td className="px-3 py-2 border-b border-[#21262d] text-xs text-gray-500 max-w-xs">{s.description || '-'}</td>
                    <td className="px-3 py-2 border-b border-[#21262d] text-xs text-gray-500">
                      {s.updated_at ? new Date(s.updated_at).toLocaleDateString() : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}
