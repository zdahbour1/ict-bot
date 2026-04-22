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
  notes: string | null;
  // Bracket health (reconcile PASS 4)
  ib_tp_perm_id: number | null;
  ib_sl_perm_id: number | null;
  ib_tp_status:  string | null;   // Submitted / PreSubmitted / Cancelled / MISSING / null
  ib_sl_status:  string | null;
  ib_tp_price:   number | null;
  ib_sl_price:   number | null;
  ib_tp_order_id: number | null;
  ib_sl_order_id: number | null;
  ib_brackets_checked_at: string | null;
  // Parent (entry) order IDs — shown in ID cell tooltip
  ib_order_id: number | null;
  ib_perm_id: number | null;
  ib_con_id: number | null;
  // Human-readable IB↔DB correlation (format: TICKER-YYMMDD-NN).
  // Displayed in the ID cell tooltip, not a standalone column.
  client_trade_id: string | null;
}

export interface ThreadStatus {
  id: number;
  thread_name: string;
  ticker: string | null;
  status: string;
  last_scan_time: string | null;
  pid: number | null;
  thread_id: number | null;
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
  // Multi-strategy v2: tickers are scoped per strategy (NOT NULL FK).
  strategy_id: number;
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
  // Multi-strategy v2: NULL = inherited global; non-NULL = override for
  // that strategy. The UI uses this to render a pill / muted styling.
  strategy_id: number | null;
  updated_at: string;
}

export interface StrategyRow {
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
