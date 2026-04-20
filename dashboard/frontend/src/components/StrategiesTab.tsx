import { useState } from 'react';
import { useApi } from '../hooks/useApi';

interface Strategy {
  strategy_id: number;
  name: string;
  display_name: string;
  description: string | null;
  class_path: string;
  enabled: boolean;
  is_default: boolean;
  is_active: boolean;
  created_at: string | null;
  updated_at: string | null;
}

interface ListResp {
  strategies: Strategy[];
  active: string | null;
  total: number;
}

function Pill({ color, children }: { color: string; children: React.ReactNode }) {
  const colors: Record<string, string> = {
    green: 'bg-green-500/20 text-green-400',
    blue: 'bg-blue-500/20 text-blue-400',
    gray: 'bg-gray-700 text-gray-400',
    yellow: 'bg-yellow-500/20 text-yellow-400',
  };
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${colors[color]}`}>
      {children}
    </span>
  );
}

export default function StrategiesTab() {
  const { data, refetch } = useApi<ListResp>('/strategies', 15000);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [banner, setBanner] = useState<{ type: 'info' | 'error'; text: string } | null>(null);

  const strategies = data?.strategies || [];
  const activeName = data?.active || null;

  const call = async (
    url: string,
    successMsg: string,
    strategyId: number,
  ) => {
    setBusyId(strategyId);
    setBanner(null);
    try {
      const res = await fetch(url, { method: 'POST' });
      const json = await res.json();
      if (!res.ok) {
        setBanner({ type: 'error', text: json.detail || `HTTP ${res.status}` });
      } else {
        setBanner({ type: 'info', text: successMsg });
        refetch();
      }
    } catch (e: any) {
      setBanner({ type: 'error', text: e?.message || 'request failed' });
    } finally {
      setTimeout(() => setBusyId(null), 500);
    }
  };

  const activate = (s: Strategy) => {
    if (!s.enabled) {
      setBanner({ type: 'error', text: `'${s.name}' is disabled — enable it first` });
      return;
    }
    if (!confirm(
      `Activate '${s.display_name}' as the live strategy?\n\n` +
      `• Backtests will default to it immediately\n` +
      `• Live bot will use it on the NEXT START\n` +
      `• You must stop + restart the bot to apply to live trading`
    )) return;
    call(`/api/strategies/${s.strategy_id}/activate`,
         `Activated '${s.name}'. Restart the bot to apply to live trading.`,
         s.strategy_id);
  };

  const enable = (s: Strategy) => {
    call(`/api/strategies/${s.strategy_id}/enable`, `Enabled '${s.name}'`, s.strategy_id);
  };

  const disable = (s: Strategy) => {
    if (s.is_active) {
      setBanner({
        type: 'error',
        text: `Cannot disable '${s.name}' — it is the active strategy. Activate another first.`
      });
      return;
    }
    call(`/api/strategies/${s.strategy_id}/disable`, `Disabled '${s.name}'`, s.strategy_id);
  };

  return (
    <div className="space-y-6">
      {/* Top explainer */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold mb-1">Trading Strategies</h2>
            <div className="text-xs text-gray-400">
              Active: {' '}
              {activeName ? (
                <span className="text-green-400 font-semibold">{activeName}</span>
              ) : (
                <span className="text-red-400">none</span>
              )}
              <span className="mx-2 text-gray-600">•</span>
              <span className="text-gray-500">
                Activation change requires a bot restart to take effect on live trading.
                Backtests pick up the active strategy immediately.
              </span>
            </div>
          </div>
          <button onClick={refetch}
            className="px-3 py-1.5 text-xs bg-[#21262d] border border-[#30363d] text-gray-400 rounded hover:text-white">
            Refresh
          </button>
        </div>
      </div>

      {/* Banner */}
      {banner && (
        <div className={`px-4 py-2 rounded-lg text-sm ${
          banner.type === 'error'
            ? 'bg-red-500/10 border border-red-500/30 text-red-400'
            : 'bg-blue-500/10 border border-blue-500/30 text-blue-400'
        }`}>
          {banner.text}
        </div>
      )}

      {/* Strategies table */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-[#21262d]">
              {['Name', 'Display Name', 'Status', 'Default', 'Class Path', 'Actions'].map(h => (
                <th key={h} className="px-3 py-2 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d]">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {strategies.map(s => (
              <tr key={s.strategy_id} className="border-b border-[#21262d] hover:bg-[#1c2128]">
                <td className="px-3 py-3">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm">{s.name}</span>
                    {s.is_active && <Pill color="green">ACTIVE</Pill>}
                  </div>
                </td>
                <td className="px-3 py-3 text-gray-300">{s.display_name}</td>
                <td className="px-3 py-3">
                  {s.enabled ? (
                    <Pill color="blue">enabled</Pill>
                  ) : (
                    <Pill color="gray">disabled</Pill>
                  )}
                </td>
                <td className="px-3 py-3">
                  {s.is_default && <Pill color="yellow">default</Pill>}
                </td>
                <td className="px-3 py-3 text-xs text-gray-500 font-mono">{s.class_path}</td>
                <td className="px-3 py-3">
                  <div className="flex items-center gap-2">
                    {!s.is_active && s.enabled && (
                      <button onClick={() => activate(s)}
                        disabled={busyId === s.strategy_id}
                        className={`px-3 py-1 text-xs rounded font-medium ${
                          busyId === s.strategy_id
                            ? 'bg-blue-600 text-white animate-pulse cursor-wait'
                            : 'bg-green-600 text-white hover:bg-green-700'
                        }`}>
                        Activate
                      </button>
                    )}
                    {s.is_active && (
                      <span className="text-xs text-green-400 px-3 py-1">— active —</span>
                    )}
                    {s.enabled ? (
                      <button onClick={() => disable(s)}
                        disabled={busyId === s.strategy_id || s.is_active}
                        title={s.is_active ? "Can't disable the active strategy" : "Disable this strategy"}
                        className={`px-3 py-1 text-xs border border-[#30363d] rounded ${
                          s.is_active ? 'text-gray-600 cursor-not-allowed' : 'text-gray-400 hover:text-white'
                        }`}>
                        Disable
                      </button>
                    ) : (
                      <button onClick={() => enable(s)}
                        disabled={busyId === s.strategy_id}
                        className="px-3 py-1 text-xs border border-[#30363d] rounded text-gray-300 hover:text-white bg-[#21262d]">
                        Enable
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {strategies.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-sm text-gray-500">
                  No strategies configured. (The migration should have seeded at least ICT.)
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Per-strategy descriptions */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Descriptions</h3>
        <div className="space-y-3 text-xs">
          {strategies.map(s => (
            <div key={s.strategy_id} className="border-l-2 border-[#30363d] pl-3">
              <div className="font-mono text-gray-200 mb-1">
                {s.name}
                {s.is_active && <span className="ml-2 text-green-400">(active)</span>}
              </div>
              <div className="text-gray-500">{s.description || '—'}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
