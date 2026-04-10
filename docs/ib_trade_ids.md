# IB Trade ID Integration — One-to-One Mapping

## Date: 2026-04-10
## Status: Next to implement
## Priority: Critical — needed for reliable reconciliation

## Problem
Currently no reliable way to match a DB trade to its IB position/order.
The reconciliation uses symbol matching which is fragile (same symbol
can have multiple positions if bot re-enters after false close).

## Solution: Store IB Permanent IDs

### IB Identifiers
| Field | Description | Survives Restart | Unique |
|-------|-------------|-----------------|--------|
| orderId | Client-assigned, per session | NO | Per session |
| **permId** | IB-assigned, permanent | **YES** | **Globally** |
| **conId** | Contract identifier | YES | Per contract |
| execId | Execution/fill identifier | YES | Per fill |

### New DB Columns on trades table
```sql
ALTER TABLE trades ADD COLUMN IF NOT EXISTS ib_perm_id INT;          -- permanent order ID (parent)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS ib_tp_perm_id INT;       -- TP leg permanent ID
ALTER TABLE trades ADD COLUMN IF NOT EXISTS ib_sl_perm_id INT;       -- SL leg permanent ID
ALTER TABLE trades ADD COLUMN IF NOT EXISTS ib_con_id INT;           -- contract ID
```

### Where to capture
1. **On order placement** (`_ib_place_order`): 
   - After fill: `trade.order.permId` → store as `ib_perm_id`
   - Contract: `contract.conId` → store as `ib_con_id`
   
2. **On bracket placement** (`_ib_place_bracket`):
   - Parent: `parent_trade.order.permId` → `ib_perm_id`
   - TP: `tp_trade.order.permId` → `ib_tp_perm_id`
   - SL: `sl_trade.order.permId` → `ib_sl_perm_id`

3. **Store in trade dict** → flows to DB via `insert_trade()`

### Reconciliation using IDs
```python
# Match DB trade to IB position by conId (exact match)
for db_trade in open_trades:
    ib_match = None
    for ib_pos in ib_positions:
        if ib_pos.contract.conId == db_trade.ib_con_id:
            ib_match = ib_pos
            break
    
    if ib_match:
        # Position exists — verify quantities
        pass
    else:
        # Position closed — check fills by permId
        for fill in ib.fills():
            if fill.execution.permId == db_trade.ib_perm_id:
                # Found the close — get exit price and time
                pass
```

### Benefits
1. **Exact matching** — no symbol string comparison issues
2. **Fill tracking** — know exactly when and at what price a trade closed
3. **Bracket tracking** — know if TP or SL leg fired (by permId)
4. **Survives restarts** — permId is permanent across sessions
5. **No false closes** — can verify a trade is truly closed vs timeout
