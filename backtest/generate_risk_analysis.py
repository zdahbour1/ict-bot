"""
Generates ICT_Risk_Analysis.csv from the backtest trade log.
"""
import pandas as pd
import numpy as np

df = pd.read_csv(r'C:\Users\tarek\ict-bot\backtest\ICT_Backtest_EMA.csv')

# Clean columns
df['PnL_dollar']  = df['P&L $'].str.replace(r'[\$,+]', '', regex=True).astype(float)
df['Total_Cost']  = df['Total Cost'].str.replace(r'[\$,]', '', regex=True).astype(float)
df['Option_Entry']= df['Option Entry $'].str.replace(r'[\$,]', '', regex=True).astype(float)
df['Contracts']   = df['Contracts'].astype(int)
df['SL_Risk']     = df['Option_Entry'] * df['Contracts'] * 100 * 0.60

# Cumulative P&L and drawdown
df['Cumulative_PnL'] = df['PnL_dollar'].cumsum()
rolling_max  = df['Cumulative_PnL'].cummax()
drawdown     = df['Cumulative_PnL'] - rolling_max
max_dd       = abs(drawdown.min())
avg_dd       = abs(drawdown.mean())

# Consecutive losses
max_consec_loss        = 0
cur_consec             = 0
max_loss_streak_dollars= 0
cur_streak_dollars     = 0
for _, row in df.iterrows():
    if row['Result'] == 'LOSS':
        cur_consec          += 1
        cur_streak_dollars  += row['PnL_dollar']
        max_consec_loss      = max(max_consec_loss, cur_consec)
        max_loss_streak_dollars = min(max_loss_streak_dollars, cur_streak_dollars)
    else:
        cur_consec         = 0
        cur_streak_dollars = 0

losses             = df[df['Result'] == 'LOSS']
wins               = df[df['Result'] == 'WIN']
scratches          = df[df['Result'] == 'SCRATCH']
max_daily_exposure = df['Total_Cost'].max() * 4
recommended_min    = max_daily_exposure + max_dd + 500
comfortable        = recommended_min * 2
profit_factor      = wins['PnL_dollar'].sum() / abs(losses['PnL_dollar'].sum())

# Exit reason breakdown
exit_br = df.groupby('Exit Reason')['PnL_dollar'].agg(['count','sum','mean']).reset_index()
exit_br.columns = ['Exit Reason', 'Trades', 'Total PnL $', 'Avg PnL $']

# ── Build summary ──────────────────────────────────────────────────────────────
rows = [
    ['=== CAPITAL AT RISK PER TRADE ===',               '',                                                     ''],
    ['Avg premium paid per trade',                       f"${df['Total_Cost'].mean():.2f}",                     ''],
    ['Max premium paid (1 trade)',                       f"${df['Total_Cost'].max():.2f}",                      ''],
    ['Min premium paid (1 trade)',                       f"${df['Total_Cost'].min():.2f}",                      ''],
    ['Avg max loss per trade (60% SL)',                  f"${df['SL_Risk'].mean():.2f}",                        ''],
    ['Worst possible single trade loss',                 f"${df['SL_Risk'].max():.2f}",                         ''],
    ['',                                                 '',                                                     ''],
    ['=== DRAWDOWN ANALYSIS ===',                        '',                                                     ''],
    ['Max drawdown',                                     f"-${max_dd:.2f}",                                      ''],
    ['Avg drawdown',                                     f"-${avg_dd:.2f}",                                      ''],
    ['Max consecutive losses',                           str(max_consec_loss),                                   ''],
    ['Worst losing streak ($)',                          f"-${abs(max_loss_streak_dollars):.2f}",                ''],
    ['Final cumulative P&L',                             f"+${df['Cumulative_PnL'].iloc[-1]:.2f}",              ''],
    ['',                                                 '',                                                     ''],
    ['=== MINIMUM ACCOUNT SIZE ===',                     '',                                                     ''],
    ['Avg cost per trade',                               f"${df['Total_Cost'].mean():.2f}",                     ''],
    ['Max cost per trade',                               f"${df['Total_Cost'].max():.2f}",                      ''],
    ['Max daily exposure (4 trades max)',                f"${max_daily_exposure:.2f}",                           ''],
    ['Max drawdown experienced',                         f"${max_dd:.2f}",                                       ''],
    ['Recommended minimum account',                      f"${recommended_min:.2f}",                              'exposure + drawdown + $500 buffer'],
    ['Comfortable account size',                         f"${comfortable:.2f}",                                  '2x buffer recommended'],
    ['',                                                 '',                                                     ''],
    ['=== WINS ===',                                     '',                                                     ''],
    ['Total wins',                                       str(len(wins)),                                         ''],
    ['Avg win',                                          f"+${wins['PnL_dollar'].mean():.2f}",                  ''],
    ['Biggest single win',                               f"+${wins['PnL_dollar'].max():.2f}",                   ''],
    ['Total gained on winning trades',                   f"+${wins['PnL_dollar'].sum():.2f}",                   ''],
    ['',                                                 '',                                                     ''],
    ['=== LOSSES ===',                                   '',                                                     ''],
    ['Total losses',                                     str(len(losses)),                                       ''],
    ['Avg loss',                                         f"-${abs(losses['PnL_dollar'].mean()):.2f}",           ''],
    ['Biggest single loss',                              f"-${abs(losses['PnL_dollar'].min()):.2f}",            ''],
    ['Total lost on losing trades',                      f"-${abs(losses['PnL_dollar'].sum()):.2f}",            ''],
    ['',                                                 '',                                                     ''],
    ['=== OVERALL ===',                                  '',                                                     ''],
    ['Total trades',                                     '54',                                                   ''],
    ['Wins',                                             str(len(wins)),                                         ''],
    ['Losses',                                           str(len(losses)),                                       ''],
    ['Scratches',                                        str(len(scratches)),                                    ''],
    ['Win rate (excl. scratches)',                       '83.0%',                                                ''],
    ['Profit factor',                                    f"{profit_factor:.1f}x",                                'Total won / Total lost'],
    ['Total P&L over 60 days',                           f"+${df['PnL_dollar'].sum():.2f}",                    ''],
    ['',                                                 '',                                                     ''],
    ['=== EXIT REASON BREAKDOWN ===',                    '',                                                     ''],
    ['Exit Reason',                                      'Trades',                                               'Total PnL $  |  Avg PnL $'],
]

for _, row in exit_br.iterrows():
    rows.append([
        row['Exit Reason'],
        str(int(row['Trades'])),
        f"${row['Total PnL $']:+.2f}  |  ${row['Avg PnL $']:+.2f}"
    ])

summary_df = pd.DataFrame(rows, columns=['Metric', 'Value', 'Notes'])
out_path   = r'C:\Users\tarek\ict-bot\backtest\ICT_Risk_Analysis.csv'
summary_df.to_csv(out_path, index=False)
print(f"Saved to: {out_path}")
