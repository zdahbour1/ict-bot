# ENH-025 Design — iOS Companion App

## 1. Goals + non-goals

**Goals**
- Real-time P&L and open positions visible on phone at a glance.
- Emergency controls: "close all positions" and "stop bot" one-tap (with confirm).
- Push notifications for new signals, fills, and error events.
- Biometric unlock (FaceID) so the app is safe to leave installed.

**Non-goals**
- Full strategy authoring, parameter editing, or backtest configuration — those stay on desktop web.
- Android parity in phase A (PWA will cover most Android cases for free).
- Offline trade entry. App is read-mostly with a few destructive action buttons.

## 2. Approach options

| Option | Dev effort | Native feel | Offline | Push | Maintenance | Cost |
|---|---|---|---|---|---|---|
| PWA (installable) | days | 3/5 | limited | web-push limited on iOS | low | $0 |
| Capacitor wrap of existing React app | ~1 week | 4/5 | yes | APNs via Capacitor plugin | low | $99/yr |
| React Native fresh app | 3-4 weeks | 4.5/5 | yes | first-class | medium | $99/yr |
| SwiftUI native | 6+ weeks | 5/5 | yes | first-class | high | $99/yr |

Key trade-offs:
- **PWA** reuses `dashboard/frontend/` verbatim. iOS Safari 16.4+ does support web push for installed PWAs, but reliability and background delivery are weaker than APNs. No App Store listing.
- **Capacitor** wraps the same React bundle in a WKWebView shell and gives access to native APNs, FaceID, share sheet, and background fetch. Minimal new code — mostly config and a handful of bridge plugins.
- **React Native** means rewriting the table/card UI. Value is low for a read-mostly ops tool the user opens a few times a day.
- **SwiftUI** is overkill; it triples maintenance for negligible UX gain on a data dashboard.

## 3. Recommendation

Two-phase rollout:

- **Phase A — PWA (3-5 days).** Add `manifest.json`, a service worker, install prompts, and a mobile-responsive pass on the existing React dashboard. Ship to the user's phone via "Add to Home Screen." Zero app store. Zero new backend.
- **Phase B — Capacitor shell (1-2 weeks).** Wrap the same build output, add APNs push, FaceID unlock, and submit to App Store under a personal Apple Developer account ($99/yr). Only triggered once Phase A proves the feature set.

SwiftUI or React Native are explicitly deferred unless usage patterns prove a PWA/Capacitor hybrid is insufficient (e.g. if sub-second chart interactions or complex gesture handling become core requirements).

## 4. Backend changes

Shared between Phase A and B where possible:

- **Device token registration** — `POST /api/devices` storing `{ user_id, platform, token, created_at }`. Used by APNs in Phase B; in Phase A only web-push subscription endpoints are needed.
- **Push dispatcher** — fan-out on signal / fill / error events. One module, pluggable transports (web-push, APNs).
- **Event stream** — WebSocket or SSE endpoint publishing position deltas and P&L updates. Useful for the web dashboard too, so cost is amortized across ENH-018/020.
- **Multi-tenancy** — device tokens scoped by `user_id` from the ENH-018 auth context. Push filtering enforces that each tenant only receives their own events.
- **Quiet hours** — per-user config (start/end, timezone) consulted by the dispatcher before enqueueing a push.

## 5. Feature scope per phase

**PWA v1 (Phase A)**
- Login reusing existing web auth (ENH-018).
- Positions list, P&L cards, recent trades view.
- "Close all" and "Stop bot" buttons with confirm modal and double-tap guard.
- Mobile-responsive layout pass on TanStack tables.

**PWA v2**
- Web push for new signals/fills (Android fully, iOS 16.4+ installed PWAs only).
- Pull-to-refresh.

**Capacitor v3 (Phase B)**
- APNs push with rich payloads (symbol, side, P&L delta).
- FaceID unlock on app resume.
- Native share sheet for trade screenshots.
- App Store submission.

## 6. File / code estimates

- **PWA:** `dashboard/frontend/public/manifest.json`, `dashboard/frontend/src/sw.ts`, install-prompt component, responsive tweaks — ~150 LOC.
- **Backend:** `devices` table migration, `apns_client.py`, `push_dispatcher.py`, event subscribers — ~400 LOC.
- **Capacitor:** `capacitor.config.ts`, iOS project scaffold, bridge plugin wiring (push, biometrics) — ~200 LOC plus generated Xcode project.

## 7. Multi-tenancy impact

- Device tokens carry `user_id` and are isolated on lookup.
- Push dispatcher resolves recipients by event → owning user → that user's active device tokens only.
- ENH-020 tenant scoping already enforces row-level filtering; the devices table and push events piggyback on that model without new primitives.

## 8. Effort

- Phase A PWA: **3-5 days** (front-end work dominates; backend only needs web-push subscription storage).
- Phase B Capacitor + APNs: **1-2 weeks** (Apple Developer enrollment, certificates, APNs integration, store review).
- Total to App Store: **~3 weeks of elapsed effort**, most of which is reusable in Phase A immediately.

## 9. Risks + open questions

- **iOS web push coverage** — Safari requires the PWA to be installed to the home screen and may silently drop background pushes. If Phase A push reliability is poor, Phase B may need to start earlier than planned.
- **App Store review** — Apple sometimes rejects apps described as "trading" without broker agreements or compliance disclosures. Mitigation: describe the app as a **monitoring/ops console for a personal automated system**, not a brokerage client. Do not offer order entry beyond emergency close/stop.
- **Alert fatigue** — without quiet hours and per-event-type toggles, users will mute the app within a week. Quiet-hours config must ship with v2 push, not be deferred.
- **Apple Developer cost** — $99/yr recurring; only justified once Phase B is committed. Phase A is free.
- **Open question:** do we want TestFlight-only distribution long-term (skips public review, limits to 100 testers) or full App Store listing? Recommend TestFlight for first 3 months, promote to public listing only if a second tenant joins.

---

Path: `C:\src\trading\ict-bot-strategies\docs\design_enh_025_ios_app.md`
