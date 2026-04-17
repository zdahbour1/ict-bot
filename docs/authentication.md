# Authentication & Security — Design Document

## Purpose

Secure the dashboard with user authentication (login screen) and
two-factor authentication (2FA). Prevent unauthorized access to
trading controls, trade data, and system configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    AUTHENTICATION FLOW                            │
│                                                                   │
│  ┌──────────┐     ┌──────────┐     ┌──────────┐                │
│  │  Browser  │────▶│  Login   │────▶│  2FA     │                │
│  │           │     │  Screen  │     │  Screen  │                │
│  └──────────┘     └────┬─────┘     └────┬─────┘                │
│                         │                │                       │
│                    ┌────▼────────────────▼─────┐                │
│                    │  POST /api/auth/login      │                │
│                    │  POST /api/auth/verify-2fa │                │
│                    └────────────┬──────────────┘                │
│                                 │                                │
│                    ┌────────────▼──────────────┐                │
│                    │  JWT Token Issued          │                │
│                    │  ─ Stored in httpOnly cookie│               │
│                    │  ─ 8 hour expiry            │               │
│                    │  ─ Refresh token (24h)      │               │
│                    └────────────┬──────────────┘                │
│                                 │                                │
│                    ┌────────────▼──────────────┐                │
│                    │  All API calls require     │                │
│                    │  valid JWT token           │                │
│                    │  ─ Authorization header    │                │
│                    │  ─ Or httpOnly cookie      │                │
│                    └───────────────────────────┘                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Database Design

### Table: `users`

```sql
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(50) UNIQUE NOT NULL,
    email           VARCHAR(100),
    password_hash   VARCHAR(255) NOT NULL,   -- bcrypt hash
    
    -- 2FA
    totp_secret     VARCHAR(100),            -- TOTP secret key
    totp_enabled    BOOLEAN DEFAULT FALSE,
    backup_codes    TEXT[],                   -- one-time recovery codes
    
    -- Session
    is_active       BOOLEAN DEFAULT TRUE,
    last_login      TIMESTAMPTZ,
    failed_attempts INT DEFAULT 0,
    locked_until    TIMESTAMPTZ,             -- account lockout
    
    -- Metadata
    role            VARCHAR(20) DEFAULT 'trader',  -- admin, trader, viewer
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Table: `auth_sessions`

```sql
CREATE TABLE auth_sessions (
    id              SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(id),
    token_hash      VARCHAR(255) NOT NULL,   -- hashed JWT
    refresh_token   VARCHAR(255),
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## Login Screen Wireframe

```
┌─────────────────────────────────────────────────────────────┐
│                                                              │
│                                                              │
│              ┌────────────────────────────┐                  │
│              │                            │                  │
│              │    ICT Trading Bot         │                  │
│              │    ─────────────────       │                  │
│              │                            │                  │
│              │    Username:               │                  │
│              │    ┌──────────────────┐    │                  │
│              │    │ admin            │    │                  │
│              │    └──────────────────┘    │                  │
│              │                            │                  │
│              │    Password:               │                  │
│              │    ┌──────────────────┐    │                  │
│              │    │ ••••••••         │    │                  │
│              │    └──────────────────┘    │                  │
│              │                            │                  │
│              │    [       Login       ]   │                  │
│              │                            │                  │
│              └────────────────────────────┘                  │
│                                                              │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 2FA Screen (after login)

```
┌─────────────────────────────────────────────────────────────┐
│                                                              │
│              ┌────────────────────────────┐                  │
│              │                            │                  │
│              │    Two-Factor Auth         │                  │
│              │    ─────────────────       │                  │
│              │                            │                  │
│              │    Enter the 6-digit code  │                  │
│              │    from your authenticator: │                 │
│              │                            │                  │
│              │    ┌──┐┌──┐┌──┐┌──┐┌──┐┌──┐│                │
│              │    │4 ││2 ││8 ││  ││  ││  ││                 │
│              │    └──┘└──┘└──┘└──┘└──┘└──┘│                 │
│              │                            │                  │
│              │    [      Verify       ]   │                  │
│              │                            │                  │
│              │    Use backup code instead │                  │
│              │                            │                  │
│              └────────────────────────────┘                  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/auth/login` | Username + password → JWT or 2FA required |
| `POST` | `/api/auth/verify-2fa` | TOTP code → JWT token |
| `POST` | `/api/auth/logout` | Invalidate session |
| `POST` | `/api/auth/refresh` | Refresh expired JWT |
| `GET` | `/api/auth/me` | Get current user info |
| `POST` | `/api/auth/setup-2fa` | Generate TOTP secret + QR code |
| `POST` | `/api/auth/enable-2fa` | Verify TOTP code and enable 2FA |
| `PUT` | `/api/auth/change-password` | Update password |

## Role-Based Access

| Role | Can Do |
|------|--------|
| **admin** | Everything + user management + settings |
| **trader** | View trades, start/stop bot, close trades, run backtests |
| **viewer** | View only — trades, analytics, threads (read-only) |

---

## Implementation Dependencies

```
# requirements-auth.txt
PyJWT>=2.8
bcrypt>=4.1
pyotp>=2.9        # TOTP 2FA
qrcode>=7.4       # QR code generation for 2FA setup
python-multipart   # form data parsing
```
