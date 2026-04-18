# iOS Native Mobile Application — Design Document

## Purpose

Build a native iOS app that provides the same monitoring, control, and analytics 
capabilities as the web dashboard. The app connects to the existing FastAPI backend — 
zero business logic duplication. The server is the brain, the mobile app is a window.

---

## Architecture — Zero Logic Duplication

```
┌─────────────────────────────────────────────────────────────────┐
│                    MOBILE ARCHITECTURE                           │
│                                                                   │
│  ┌──────────────────┐          ┌──────────────────────────────┐  │
│  │  iOS App (Swift)  │          │  Server (existing)           │  │
│  │                    │          │                              │  │
│  │  ┌──────────┐     │  HTTPS   │  ┌──────────┐              │  │
│  │  │ SwiftUI  │     │◀────────▶│  │ FastAPI  │              │  │
│  │  │ Views    │     │  REST +  │  │ API      │              │  │
│  │  └────┬─────┘     │  WebSocket│  │ :8000    │              │  │
│  │       │           │          │  └────┬─────┘              │  │
│  │  ┌────▼─────┐     │          │       │                    │  │
│  │  │ ViewModel│     │          │  ┌────▼─────┐              │  │
│  │  │ (MVVM)   │     │          │  │PostgreSQL│              │  │
│  │  └────┬─────┘     │          │  │(all data)│              │  │
│  │       │           │          │  └──────────┘              │  │
│  │  ┌────▼─────┐     │          │                            │  │
│  │  │ API      │     │          │  Business logic lives HERE  │  │
│  │  │ Client   │─────┼──────────┤  ─ Signal detection         │  │
│  │  │ (URLSes) │     │          │  ─ Trade execution           │  │
│  │  └──────────┘     │          │  ─ Exit management           │  │
│  │                    │          │  ─ Reconciliation            │  │
│  │  NO business logic │          │  ─ P&L calculation          │  │
│  │  NO trading logic  │          │  ─ Analytics                │  │
│  │  Display + Control │          │                              │  │
│  │  only              │          │                              │  │
│  └──────────────────┘          └──────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Key Principle: Server Does Everything

The iOS app is a **thin client**. It:
- Calls the same REST API the web dashboard uses
- Displays data from API responses
- Sends commands (start/stop, close trade, reconcile) via API
- Receives real-time updates via WebSocket (Socket.IO)
- Does NOT contain any trading logic, P&L calculation, or strategy code

This means:
- Zero code duplication between web and mobile
- Bug fixes on the server automatically fix both platforms
- New features added to the API are immediately available to both UIs

---

## Technology Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| **UI** | SwiftUI | Native iOS, declarative, modern Apple standard |
| **Architecture** | MVVM | Clean separation, works great with SwiftUI |
| **Networking** | URLSession + async/await | Built-in, no dependencies |
| **Real-time** | WebSocket (native) | Socket.IO compatible |
| **Auth** | JWT in Keychain | Secure token storage |
| **Charts** | Swift Charts (iOS 16+) | Native Apple charting framework |
| **Push Notifications** | APNs | Trade alerts, error notifications |
| **Min iOS** | iOS 16+ | Swift Charts + modern async/await |

---

## Screen Designs

### 1. Login Screen

```
┌─────────────────────────┐
│                         │
│    ICT Trading Bot      │
│    ─────────────────    │
│                         │
│    Server URL:          │
│    ┌───────────────┐    │
│    │ https://...   │    │
│    └───────────────┘    │
│                         │
│    Username:            │
│    ┌───────────────┐    │
│    │ admin         │    │
│    └───────────────┘    │
│                         │
│    Password:            │
│    ┌───────────────┐    │
│    │ ••••••••      │    │
│    └───────────────┘    │
│                         │
│    [     Login      ]   │
│                         │
│    2FA Code (if needed):│
│    ┌───────────────┐    │
│    │ 428___        │    │
│    └───────────────┘    │
│                         │
└─────────────────────────┘
```

### 2. Dashboard (Home Tab)

```
┌─────────────────────────┐
│ ICT Bot          ● Live │
│─────────────────────────│
│                         │
│  Today's P&L            │
│  ┌─────────────────┐    │
│  │    +$2,340       │    │
│  │    54% Win Rate  │    │
│  │    12 Open / 28 Closed│
│  └─────────────────┘    │
│                         │
│  Bot Status             │
│  ┌─────────────────┐    │
│  │ ● Running       │    │
│  │ 19 Scanners     │    │
│  │ 4 IB Connections│    │
│  │                  │    │
│  │ [Stop Scans]     │    │
│  │ [Stop Bot]       │    │
│  └─────────────────┘    │
│                         │
│  Recent Trades          │
│  ├─ QQQ  +$120  TP  ✅ │
│  ├─ AMD  -$80   SL  ❌ │
│  ├─ SPY  +$240  ROLL✅ │
│  └─ AAPL +$45   TRAIL✅│
│                         │
│─────────────────────────│
│ [Home] [Trades] [Charts]│
│ [Threads] [Settings]    │
└─────────────────────────┘
```

### 3. Trades Tab

```
┌─────────────────────────┐
│ Trades     [Open][Closed]│
│─────────────────────────│
│                         │
│ ┌─ QQQ 640 Call ───────┐│
│ │ LONG  2x  $0.49      ││
│ │ P&L: +$120 (+122%)   ││
│ │ Peak: +145%  SL: -40% ││
│ │ TP: $0.98  Entry: 7:14││
│ │              [Close]  ││
│ └───────────────────────┘│
│                         │
│ ┌─ AMD 280 Call ───────┐│
│ │ LONG  2x  $0.97      ││
│ │ P&L: +$65 (+33%)     ││
│ │ Peak: +40%  SL: -60%  ││
│ │              [Close]  ││
│ └───────────────────────┘│
│                         │
│ ┌─ SPY 710 Put ────────┐│
│ │ SHORT  2x  $0.31     ││
│ │ P&L: -$22 (-35%)     ││
│ │              [Close]  ││
│ └───────────────────────┘│
│                         │
│─────────────────────────│
│ [Home] [Trades] [Charts]│
└─────────────────────────┘
```

### 4. Analytics Tab

```
┌─────────────────────────┐
│ Analytics   [Today][Week]│
│─────────────────────────│
│                         │
│  Cumulative P&L         │
│  ┌─────────────────┐    │
│  │    ╱──╲  ╱────   │    │
│  │   ╱    ╲╱        │    │
│  │  ╱                │    │
│  └─────────────────┘    │
│                         │
│  By Ticker              │
│  QQQ  ████████  +$890   │
│  AMD  ██████    +$510   │
│  AAPL ████      +$320   │
│  SPY  ███       +$240   │
│                         │
│  By Exit Reason         │
│  TP      ████  45       │
│  SL      ████  38       │
│  TRAIL   ███   22       │
│  ROLL    ██    18       │
│                         │
│─────────────────────────│
│ [Home] [Trades] [Charts]│
└─────────────────────────┘
```

### 5. Push Notifications

```
┌─────────────────────────┐
│ ICT Trading Bot         │
│─────────────────────────│
│                         │
│ 🔔 Trade Opened         │
│ QQQ LONG 2x $0.49      │
│ Signal: LONG_iFVG       │
│                         │
│ 🔔 Trade Closed — WIN   │
│ AMD +$120 (+71%) ROLL   │
│                         │
│ ⚠️ Thread STALE          │
│ scanner-NVDA no heartbeat│
│ for 3 minutes           │
│                         │
│ 🔴 CRITICAL ERROR        │
│ Direction mismatch      │
│ IB qty=-2 for LONG trade│
│                         │
└─────────────────────────┘
```

---

## API Consumption — Same Endpoints as Web

The iOS app calls the EXACT same API endpoints:

| Screen | API Calls |
|--------|-----------|
| Dashboard | `GET /api/bot/status`, `GET /api/summary`, `GET /api/trades?status=open` |
| Trades | `GET /api/trades?limit=50`, `POST /api/trades/{id}/close` |
| Analytics | `GET /api/analytics?start=&end=` |
| Threads | `GET /api/threads`, `GET /api/system-log` |
| Settings | `GET /api/settings`, `PUT /api/settings/{key}` |
| Auth | `POST /api/auth/login`, `POST /api/auth/verify-2fa` |
| Control | `POST /api/bot/start`, `POST /api/bot/stop`, `POST /api/bot/reconcile` |

### WebSocket for Real-Time Updates

```swift
// iOS WebSocket connection
let socket = URLSession.shared.webSocketTask(with: serverURL)
socket.receive { result in
    switch result {
    case .success(let message):
        // Parse trade update, thread status, P&L change
        // Update SwiftUI @Published properties → auto-refresh UI
    case .failure(let error):
        // Reconnect
    }
}
```

---

## Project Structure (Xcode)

```
ICTTradingBot/
├── App/
│   ├── ICTTradingBotApp.swift      — App entry point
│   └── ContentView.swift            — Tab bar root
│
├── Models/
│   ├── Trade.swift                  — Trade data model
│   ├── BotStatus.swift              — Bot state model
│   ├── ThreadStatus.swift           — Thread monitoring model
│   ├── Analytics.swift              — Chart data models
│   └── Settings.swift               — Config model
│
├── ViewModels/
│   ├── DashboardVM.swift            — Home screen logic
│   ├── TradesVM.swift               — Trade list + actions
│   ├── AnalyticsVM.swift            — Chart data loading
│   ├── ThreadsVM.swift              — Thread monitoring
│   ├── SettingsVM.swift             — Config management
│   └── AuthVM.swift                 — Login + 2FA
│
├── Views/
│   ├── Dashboard/
│   │   ├── DashboardView.swift
│   │   ├── PnLSummaryCard.swift
│   │   └── RecentTradesView.swift
│   ├── Trades/
│   │   ├── TradeListView.swift
│   │   ├── TradeRowView.swift
│   │   └── TradeDetailView.swift
│   ├── Analytics/
│   │   ├── AnalyticsView.swift
│   │   ├── CumulativePnLChart.swift
│   │   └── TickerBarChart.swift
│   ├── Threads/
│   │   ├── ThreadsView.swift
│   │   └── ThreadLogView.swift
│   ├── Settings/
│   │   └── SettingsView.swift
│   └── Auth/
│       ├── LoginView.swift
│       └── TwoFactorView.swift
│
├── Services/
│   ├── APIClient.swift              — HTTP client (URLSession)
│   ├── WebSocketService.swift       — Real-time updates
│   ├── AuthService.swift            — JWT + Keychain
│   └── NotificationService.swift    — Push notifications
│
├── Utils/
│   ├── OCCParser.swift              — Option symbol parsing
│   ├── DateFormatters.swift
│   └── Colors.swift                 — Theme colors
│
└── Resources/
    ├── Assets.xcassets
    └── Info.plist
```

---

## Push Notification Integration

### Server Side (add to FastAPI)

```python
# dashboard/routes/notifications.py
@router.post("/notifications/register")
def register_device(device_token: str, user_id: int):
    """Register iOS device for push notifications."""
    # Store device_token in DB
    
# Called from exit_manager when trade opens/closes:
def send_push(title, body, device_tokens):
    """Send APNs push notification."""
    # Use PyAPNs2 library
```

### What Triggers Notifications

| Event | Notification |
|-------|-------------|
| Trade opened | "QQQ LONG 2x @ $0.49 — LONG_iFVG" |
| Trade closed WIN | "AMD +$120 (+71%) — ROLL" |
| Trade closed LOSS | "SPY -$80 (-60%) — SL" |
| Thread stale/dead | "scanner-NVDA no heartbeat for 3m" |
| Critical error | "Direction mismatch — IB qty=-2" |
| Bot stopped | "Bot stopped — IB connection lost" |
| Reconciliation | "Reconcile: closed 2, adopted 1" |

---

## Implementation Order

1. **Xcode project setup** — SwiftUI app, MVVM structure
2. **API client** — URLSession async/await wrapper
3. **Auth flow** — Login + JWT in Keychain + 2FA
4. **Dashboard** — Bot status, P&L summary, recent trades
5. **Trades tab** — Trade list, close actions, trade detail
6. **Analytics** — Swift Charts for P&L visualization
7. **Threads** — Thread status, log viewer
8. **Settings** — Config viewing/editing
9. **WebSocket** — Real-time updates
10. **Push notifications** — APNs integration
11. **TestFlight** — Beta testing distribution

---

## Benefits

1. **Monitor anywhere** — Check trades, P&L, thread health from your phone
2. **Instant alerts** — Push notifications for trade opens, closes, errors
3. **Quick actions** — Close trades, stop bot, reconcile from phone
4. **Zero duplication** — Same API, same data, different UI
5. **Native performance** — SwiftUI + Swift Charts = smooth 60fps
6. **Offline awareness** — Shows last known state when disconnected
