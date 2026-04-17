# Cloud Deployment — Installable Image from GitHub

## Purpose

Create a deployable package from the GitHub repository that can be installed
on any cloud machine (AWS, GCP, Azure, or dedicated server) with minimal
configuration. The deployment should be reproducible, secure, and automated.

---

## Architecture — Deployment Topology

```
┌─────────────────────────────────────────────────────────────────┐
│                    CLOUD DEPLOYMENT                              │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Docker Compose Stack                                     │    │
│  │                                                           │    │
│  │  ┌───────────┐  ┌──────────┐  ┌──────────┐              │    │
│  │  │ PostgreSQL │  │ FastAPI  │  │  React   │              │    │
│  │  │ :5432      │  │ API :8000│  │ nginx:443│              │    │
│  │  │ (volume)   │  │          │  │ (SSL)    │              │    │
│  │  └───────────┘  └──────────┘  └──────────┘              │    │
│  │                                                           │    │
│  │  ┌───────────┐  ┌──────────┐                             │    │
│  │  │ pgAdmin   │  │ Bot Mgr  │                             │    │
│  │  │ :5050     │  │ Sidecar  │                             │    │
│  │  │           │  │ :9000    │                             │    │
│  │  └───────────┘  └──────────┘                             │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Trading Bot Process (host)                               │    │
│  │  ├── 4 IB connections (pool)                              │    │
│  │  ├── 17+ scanner threads                                  │    │
│  │  ├── Exit manager                                         │    │
│  │  └── Reconciliation (2min cycle)                          │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  IB Gateway (headless, Docker or host)                    │    │
│  │  ├── Port 4001 (Gateway API)                              │    │
│  │  └── Auto-restart on disconnect                           │    │
│  └──────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Deployment Options

### Option A: Full Docker (Recommended for Cloud)

Everything in Docker including the trading bot and IB Gateway.

```yaml
# docker-compose.cloud.yml
services:
  postgres:
    image: postgres:16-alpine
    volumes: [pgdata:/var/lib/postgresql/data]
    environment:
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ${DB_NAME}

  api:
    build: { context: ., dockerfile: Dockerfile.api }
    depends_on: [postgres]
    environment:
      DATABASE_URL: postgresql://${DB_USER}:${DB_PASSWORD}@postgres:5432/${DB_NAME}

  frontend:
    build: { context: ., dockerfile: Dockerfile.frontend }
    ports: ['443:443']  # HTTPS
    volumes: [./ssl:/etc/nginx/ssl:ro]

  ib-gateway:
    image: ghcr.io/extrange/ibkr:stable
    ports: ['4001:4001']
    environment:
      TRADING_MODE: paper  # or 'live'
      TWS_USERID: ${IB_USER}
      TWS_PASSWORD: ${IB_PASSWORD}

  bot:
    build: { context: ., dockerfile: Dockerfile.bot }
    depends_on: [postgres, ib-gateway]
    environment:
      DATABASE_URL: postgresql://${DB_USER}:${DB_PASSWORD}@postgres:5432/${DB_NAME}
      IB_HOST: ib-gateway
      IB_PORT: 4001

volumes:
  pgdata:
```

### Option B: GitHub Actions Deployment

Auto-deploy on push to main branch.

```yaml
# .github/workflows/deploy.yml
name: Deploy to Cloud
on:
  push:
    branches: [main]
    
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Build Docker images
        run: docker compose -f docker-compose.cloud.yml build
      
      - name: Push to Container Registry
        run: |
          echo ${{ secrets.REGISTRY_TOKEN }} | docker login ghcr.io -u ${{ github.actor }} --password-stdin
          docker compose -f docker-compose.cloud.yml push
      
      - name: Deploy to server
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.DEPLOY_HOST }}
          username: ${{ secrets.DEPLOY_USER }}
          key: ${{ secrets.DEPLOY_KEY }}
          script: |
            cd /opt/ict-bot
            git pull
            docker compose -f docker-compose.cloud.yml pull
            docker compose -f docker-compose.cloud.yml up -d
```

---

## Installation Script

```bash
#!/bin/bash
# install.sh — One-command cloud deployment
set -e

echo "=== ICT Trading Bot — Cloud Installation ==="

# Prerequisites check
command -v docker >/dev/null 2>&1 || { echo "Docker required"; exit 1; }
command -v docker compose >/dev/null 2>&1 || { echo "Docker Compose required"; exit 1; }

# Clone repository
git clone https://github.com/zdahbour1/ict-bot.git /opt/ict-bot
cd /opt/ict-bot

# Create .env from template
if [ ! -f .env ]; then
    cp .env.cloud.example .env
    echo "Edit .env with your IB credentials and DB passwords"
    echo "Then run: docker compose -f docker-compose.cloud.yml up -d"
    exit 0
fi

# Build and start
docker compose -f docker-compose.cloud.yml up -d --build

# Wait for services
echo "Waiting for services to start..."
sleep 10

# Initialize database
docker compose exec api python -c "from db.connection import init_db; init_db()"
docker compose exec postgres psql -U $DB_USER -d $DB_NAME -f /docker-entrypoint-initdb.d/analytics_views.sql

echo "=== Installation Complete ==="
echo "Dashboard: https://$(hostname):443"
echo "API: https://$(hostname):8000"
echo "pgAdmin: http://$(hostname):5050"
```

---

## Security Considerations

| Area | Implementation |
|------|---------------|
| HTTPS | nginx with Let's Encrypt SSL or self-signed cert |
| DB passwords | .env file, never in git |
| IB credentials | .env file, never in git |
| API authentication | JWT tokens (ENH-018: login screen) |
| 2FA | TOTP (Google Authenticator) for dashboard login |
| Firewall | Only expose ports 443 (HTTPS) externally |
| DB access | Only internal Docker network, no external port |

---

## Environment Variables (.env.cloud.example)

```bash
# Database
DB_USER=ict_bot
DB_PASSWORD=<generate-strong-password>
DB_NAME=ict_bot

# IB Gateway
IB_HOST=ib-gateway
IB_PORT=4001
IB_CLIENT_ID=1
IB_ACCOUNT=<your-account>
IB_USER=<ib-username>
IB_PASSWORD=<ib-password>
TRADING_MODE=paper  # or 'live'

# Security
JWT_SECRET=<generate-random-key>
ADMIN_USER=admin
ADMIN_PASSWORD=<generate-strong-password>

# SSL
SSL_CERT_PATH=/etc/nginx/ssl/cert.pem
SSL_KEY_PATH=/etc/nginx/ssl/key.pem
```
