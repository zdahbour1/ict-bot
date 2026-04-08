export interface Trade {
  id: number;
  account: string;
  ticker: string;
  symbol: string;
  direction: 'LONG' | 'SHORT';
  contracts_entered: number;
  contracts_open: number;
  contracts_closed: number;
  entry_price: number;
  exit_price: number | null;
  current_price: number | null;
  ib_fill_price: number | null;
  pnl_pct: number;
  pnl_usd: number;
  peak_pnl_pct: number;
  dynamic_sl_pct: number;
  profit_target: number | null;
  stop_loss_level: number | null;
  signal_type: string | null;
  entry_time: string;
  exit_time: string | null;
  status: 'open' | 'closed' | 'errored';
  exit_reason: string | null;
  exit_result: 'WIN' | 'LOSS' | 'SCRATCH' | null;
  error_message: string | null;
}

export interface ThreadStatus {
  id: number;
  thread_name: string;
  ticker: string | null;
  status: string;
  last_scan_time: string | null;
  last_message: string | null;
  scans_today: number;
  trades_today: number;
  alerts_today: number;
  error_count: number;
  updated_at: string | null;
}

export interface Ticker {
  id: number;
  symbol: string;
  name: string | null;
  is_active: boolean;
  contracts: number;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface Setting {
  id: number;
  category: string;
  key: string;
  value: string;
  data_type: string;
  description: string | null;
  is_secret: boolean;
  updated_at: string;
}

export interface Summary {
  date: string;
  total_trades: number;
  open_trades: number;
  closed_trades: number;
  errored_trades: number;
  open_pnl: number;
  closed_pnl: number;
  total_pnl: number;
  wins: number;
  losses: number;
  scratches: number;
  win_rate: number;
  avg_win: number;
  avg_loss: number;
}

export interface BotStatus {
  status: 'running' | 'stopped' | 'starting' | 'stopping' | 'crashed' | 'unknown';
  account: string | null;
  pid: number | null;
  total_tickers: number;
  started_at: string | null;
  db: boolean;
}
