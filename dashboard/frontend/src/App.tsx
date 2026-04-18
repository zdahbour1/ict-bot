import { useState } from 'react';
import { useApi, apiPost } from './hooks/useApi';
import type { Summary, Trade, BotStatus } from './types';
import PnlSummary from './components/PnlSummary';
import TradeTable from './components/TradeTable';
import ThreadsTab from './components/ThreadsTab';
import TickersTab from './components/TickersTab';
import SettingsTab from './components/SettingsTab';
import AnalyticsTab from './components/AnalyticsTab';
import TestsTab from './components/TestsTab';

type Tab = 'trades' | 'threads' | 'tickers' | 'settings' | 'analytics' | 'tests';

function BotStatusDot({ status }: { status: string }) {
  const color = status === 'running' ? 'bg-green-400 shadow-[0_0_6px_#3fb950]'
    : status === 'stopped' ? 'bg-red-400'
    : 'bg-yellow-400';
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${color}`} />;
}

export default function App() {
  const [tab, setTab] = useState<Tab>('trades');
  const [refreshInterval, setRefreshInterval] = useState(15000);
  const [botBusy, setBotBusy] = useState<'starting' | 'stopping' | null>(null);
  const [scanBusy, setScanBusy] = useState(false);

  const { data: botStatus, refetch: refetchBot } = useApi<BotStatus>('/bot/status', 10000);
  const { data: summaryData, refetch: refetchSummary } = useApi<Summary>('/summary', refreshInterval);
  const { data: tradesData, refetch: refetchTrades, lastUpdated: tradesLastUpdated } = useApi<{ trades: Trade[] }>('/trades?limit=200', refreshInterval);

  const trades = tradesData?.trades || [];
  const bot = botStatus || { status: 'unknown', account: null, total_tickers: 0, db: false } as BotStatus;

  const scansActive = (botStatus as any)?.scans_active || false;

  const handleStartStop = async () => {
    if (bot.status === 'running') {
      if (confirm('Stop the bot entirely? This stops IB connection, all monitoring, and all threads.')) {
        setBotBusy('stopping');
        try {
          await apiPost('/bot/stop');
        } finally {
          setTimeout(() => { refetchBot(); setBotBusy(null); }, 3000);
        }
      }
    } else {
      setBotBusy('starting');
      try {
        await apiPost('/bot/start');
        setTimeout(() => { refetchBot(); setBotBusy(null); }, 5000);
      } catch (e: any) {
        setBotBusy(null);
        alert(`Failed to start bot:\n\n${e.message}\n\nMake sure IB TWS/Gateway is running.`);
        refetchBot();
      }
    }
  };

  const handleScanToggle = async () => {
    setScanBusy(true);
    if (scansActive) {
      await apiPost('/bot/pause-scans');
    } else {
      await apiPost('/bot/resume-scans');
    }
    setTimeout(() => { refetchBot(); setScanBusy(false); }, 2000);
  };

  const refetchAll = () => { refetchTrades(); refetchSummary(); };

  return (
    <div className="min-h-screen">
      {/* Nav */}
      <nav className="bg-[#161b22] border-b border-[#30363d] px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-6">
          <span className="text-lg font-bold text-blue-400">ICT Trading Bot</span>
          <div className="flex gap-1">
            {(['trades', 'analytics', 'threads', 'tickers', 'tests', 'settings'] as Tab[]).map(t => (
              <button key={t} onClick={() => setTab(t)}
                className={`px-4 py-2 rounded-md text-sm capitalize ${tab === t ? 'bg-[#21262d] text-gray-200' : 'text-gray-500 hover:text-gray-300'}`}>
                {t}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 text-sm">
            <BotStatusDot status={bot.status} />
            <span className={bot.status === 'running' ? 'text-gray-200' : 'text-red-400'}>
              {bot.status === 'running' ? (scansActive ? 'Trading' : 'Monitoring') : bot.status}
            </span>
            {bot.account && <span className="text-gray-500">| {bot.account}</span>}
            {bot.total_tickers > 0 && <span className="text-gray-500">| {bot.total_tickers} tickers</span>}
          </div>
          <select value={refreshInterval} onChange={e => setRefreshInterval(Number(e.target.value))}
            className="px-2 py-1 text-xs bg-[#21262d] border border-[#30363d] text-gray-400 rounded">
            <option value={15000}>15s</option>
            <option value={30000}>30s</option>
            <option value={60000}>1 min</option>
            <option value={300000}>5 min</option>
          </select>
          {bot.status === 'running' && (
            <button onClick={handleScanToggle} disabled={scanBusy || !!botBusy}
              className={`px-3 py-1.5 text-xs rounded-md font-medium transition-all ${
                scanBusy
                  ? 'bg-blue-600 text-white animate-pulse cursor-wait'
                  : scansActive
                    ? 'bg-yellow-600 text-white hover:bg-yellow-700'
                    : 'bg-green-600 text-white hover:bg-green-700'
              }`}>
              {scanBusy ? (scansActive ? 'Stopping Scans...' : 'Starting Scans...') : scansActive ? 'Stop Scans' : 'Start Scans'}
            </button>
          )}
          <button onClick={handleStartStop} disabled={!!botBusy}
            className={`px-3 py-1.5 text-xs rounded-md font-medium transition-all ${
              botBusy
                ? 'bg-blue-600 text-white animate-pulse cursor-wait'
                : bot.status === 'running'
                  ? 'bg-red-600 text-white hover:bg-red-700'
                  : 'bg-green-600 text-white hover:bg-green-700'
            }`}>
            {botBusy === 'stopping' ? 'Stopping...' : botBusy === 'starting' ? 'Starting...' : bot.status === 'running' ? 'Stop Bot' : 'Start Bot'}
          </button>
        </div>
      </nav>

      {/* Content */}
      <div className="p-6">
        {tab === 'trades' && (
          <>
            <PnlSummary summary={summaryData || null} />
            <TradeTable trades={trades} onRefresh={refetchAll} lastUpdated={tradesLastUpdated} />
          </>
        )}
        {tab === 'threads' && <ThreadsTab />}
        {tab === 'tickers' && <TickersTab />}
        {tab === 'settings' && <SettingsTab />}
        {tab === 'analytics' && <AnalyticsTab />}
        {tab === 'tests' && <TestsTab />}
      </div>
    </div>
  );
}
