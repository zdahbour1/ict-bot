import { useEffect, useMemo, useState } from 'react';
import type { Setting, StrategyRow } from '../types';
import { useApi, apiPut, apiDelete } from '../hooks/useApi';

// Multi-strategy v2 (Phase 3): the Settings tab scopes reads/writes to
// a selected strategy. The strategy list comes from /api/strategies,
// filtered to enabled=TRUE. Globals (strategy_id=NULL) are shown with a
// muted "inherited" pill; scoped overrides render normally with a
// "Reset to global" link that DELETEs the scoped row.

export default function SettingsTab() {
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

  const endpoint = selectedStrategyId !== null
    ? `/settings?strategy_id=${selectedStrategyId}`
    : '/settings';
  const { data, refetch } = useApi<{ settings: Record<string, Setting[]> }>(endpoint, 30000);
  const grouped = data?.settings || {};

  const [editKey, setEditKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [showSecrets, setShowSecrets] = useState<Set<string>>(new Set());

  const categories = Object.keys(grouped).sort();

  // Corner-case note: when the dropdown changes we drop any in-progress
  // edit without prompting. Unsaved edits are quick to redo and the
  // prompt-vs-discard trade-off isn't worth a modal. (Chosen: discard.)
  const handleStrategyChange = (sid: number) => {
    setEditKey(null);
    setSelectedStrategyId(sid);
  };

  const handleSave = async (s: Setting) => {
    // Always write scoped to the currently selected strategy — editing
    // an inherited row creates a new scoped override rather than
    // mutating the global.
    await apiPut(`/settings/${s.key}`, {
      value: editValue,
      strategy_id: selectedStrategyId,
    });
    setEditKey(null);
    refetch();
  };

  const handleResetToGlobal = async (s: Setting) => {
    if (s.strategy_id === null) return;
    if (!confirm(`Delete the "${s.key}" override for this strategy? The global value will take effect again.`)) return;
    await apiDelete(`/settings/${s.key}?strategy_id=${s.strategy_id}`);
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

  const selectedStrategy = enabledStrategies.find(s => s.strategy_id === selectedStrategyId);

  return (
    <div>
      {/* Strategy scope selector */}
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
            Showing settings for <span className="text-gray-300 font-mono">{selectedStrategy.name}</span>;
            inherited globals are marked.
          </span>
        )}
      </div>

      <div className="bg-blue-500/10 border border-blue-500/30 rounded-lg p-3 mb-4 text-sm text-blue-400">
        Settings are saved to the database. Editing an inherited value creates an override scoped to the selected strategy.
        Click "Reload Bot Settings" for the running bot to pick up changes.
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
                  {['Key', 'Value', 'Scope', 'Type', 'Description', ''].map(h => (
                    <th key={h} className="bg-[#21262d] px-3 py-2 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d]">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {grouped[cat]?.map(s => {
                  const inherited = s.strategy_id === null && selectedStrategyId !== null;
                  const rowCls = inherited
                    ? 'hover:bg-[#1c2128] text-gray-500 italic'
                    : 'hover:bg-[#1c2128]';
                  return (
                    <tr key={`${s.key}-${s.strategy_id ?? 'g'}`} className={rowCls}>
                      <td className="px-3 py-2 border-b border-[#21262d] font-mono text-xs text-blue-400">{s.key}</td>
                      <td className="px-3 py-2 border-b border-[#21262d]">
                        {editKey === s.key ? (
                          <div className="flex gap-2">
                            <input value={editValue} onChange={e => setEditValue(e.target.value)}
                              className="px-2 py-1 bg-[#21262d] border border-blue-500 text-gray-200 rounded text-xs w-48"
                              autoFocus onKeyDown={e => e.key === 'Enter' && handleSave(s)} />
                            <button onClick={() => handleSave(s)} className="text-xs text-green-400 hover:underline">Save</button>
                            <button onClick={() => setEditKey(null)} className="text-xs text-gray-500 hover:underline">Cancel</button>
                          </div>
                        ) : (
                          <div className="flex items-center gap-2">
                            <span className={inherited ? 'text-xs text-gray-500' : 'text-xs text-gray-300'}>
                              {s.is_secret && !showSecrets.has(s.key) ? '********' : s.value || '(empty)'}
                            </span>
                            {s.is_secret && (
                              <button onClick={() => toggleSecret(s.key)} className="text-xs text-gray-500 hover:text-gray-300">
                                {showSecrets.has(s.key) ? 'hide' : 'show'}
                              </button>
                            )}
                            <button onClick={() => { setEditKey(s.key); setEditValue(s.value === '********' ? '' : s.value); }}
                              className="text-xs text-blue-400 hover:underline">
                              {inherited ? 'override' : 'edit'}
                            </button>
                            {!inherited && s.strategy_id !== null && (
                              <button onClick={() => handleResetToGlobal(s)}
                                className="text-xs text-amber-400 hover:underline">
                                reset to global
                              </button>
                            )}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2 border-b border-[#21262d] text-xs">
                        {inherited ? (
                          <span className="inline-block px-2 py-0.5 rounded-full bg-gray-700/40 text-gray-400 text-[10px] uppercase tracking-wide">
                            inherited
                          </span>
                        ) : s.strategy_id !== null ? (
                          <span className="inline-block px-2 py-0.5 rounded-full bg-blue-700/40 text-blue-300 text-[10px] uppercase tracking-wide">
                            override
                          </span>
                        ) : (
                          <span className="text-gray-600 text-[10px] uppercase tracking-wide">global</span>
                        )}
                      </td>
                      <td className="px-3 py-2 border-b border-[#21262d] text-xs text-gray-500">{s.data_type}</td>
                      <td className="px-3 py-2 border-b border-[#21262d] text-xs text-gray-500 max-w-xs">{s.description || '-'}</td>
                      <td className="px-3 py-2 border-b border-[#21262d] text-xs text-gray-500">
                        {s.updated_at ? new Date(s.updated_at).toLocaleDateString() : ''}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}
