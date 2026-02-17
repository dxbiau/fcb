#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# deploy.sh — Deploy FCB Bot to a fresh DigitalOcean Droplet
# ═══════════════════════════════════════════════════════════════
#
# Prerequisites:
#   1. A DigitalOcean droplet (Ubuntu 22.04+, 1GB RAM minimum)
#   2. SSH access configured (ssh root@your-server-ip)
#   3. Your Bybit API key and secret ready
#
# Usage (run from your LOCAL machine):
#   chmod +x deploy.sh
#   ./deploy.sh <server-ip> <bybit-api-key> <bybit-api-secret>
#
# What it does:
#   1. Installs Docker + Docker Compose on the server
#   2. Uploads the project (excluding data/analysis/backtest)
#   3. Creates .env with your API keys
#   4. Builds and starts the bot + dashboard
#   5. Verifies health
#
# After deployment:
#   Dashboard: http://<server-ip>:8080/
#   Logs:      ssh root@<server-ip> "docker-compose -f /opt/fcb-bot/docker-compose.yml logs -f"
#   Stop:      ssh root@<server-ip> "docker-compose -f /opt/fcb-bot/docker-compose.yml down"
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ── Args ──
if [ $# -lt 3 ]; then
    echo "Usage: ./deploy.sh <server-ip> <bybit-api-key> <bybit-api-secret>"
    echo ""
    echo "Example:"
    echo "  ./deploy.sh 164.92.105.42 myApiKey myApiSecret"
    exit 1
fi

SERVER_IP="$1"
API_KEY="$2"
API_SECRET="$3"
REMOTE_DIR="/opt/fcb-bot"
SSH_USER="${SSH_USER:-root}"

echo "═══════════════════════════════════════════════════"
echo "  FCB Bot — Deploying to $SERVER_IP"
echo "═══════════════════════════════════════════════════"

# ── Step 1: Install Docker on remote ──
echo ""
echo "[1/6] Installing Docker on $SERVER_IP..."
ssh -o StrictHostKeyChecking=no "$SSH_USER@$SERVER_IP" bash <<'INSTALL_DOCKER'
set -e
if command -v docker &> /dev/null; then
    echo "Docker already installed: $(docker --version)"
else
    echo "Installing Docker..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    echo "Docker installed: $(docker --version)"
fi

# Enable and start Docker
systemctl enable docker
systemctl start docker
INSTALL_DOCKER

# ── Step 2: Create remote directory ──
echo ""
echo "[2/6] Creating project directory..."
ssh "$SSH_USER@$SERVER_IP" "mkdir -p $REMOTE_DIR/live/logs"

# ── Step 3: Upload project files ──
echo ""
echo "[3/6] Uploading project files..."

# Create a temporary archive excluding unnecessary files
TMPFILE=$(mktemp /tmp/fcb-bot-XXXXXX.tar.gz)
tar czf "$TMPFILE" \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='data' \
    --exclude='analysis' \
    --exclude='backtest' \
    --exclude='strategy' \
    --exclude='tests' \
    --exclude='results' \
    --exclude='config' \
    --exclude='.env' \
    --exclude='*.pyc' \
    --exclude='live/state.json' \
    --exclude='live/trades.csv' \
    --exclude='live/logs/*' \
    --exclude='live/skipped_trades.csv' \
    --exclude='live/bot.lock' \
    --exclude='sweep_*.py' \
    --exclude='mega_sweep.py' \
    --exclude='analyze_*.py' \
    --exclude='run_backtest.py' \
    --exclude='run_analysis.py' \
    --exclude='screen_*.py' \
    --exclude='quick_scan.py' \
    --exclude='x1000_*.py' \
    -C "$(dirname "$0")" .

scp "$TMPFILE" "$SSH_USER@$SERVER_IP:/tmp/fcb-bot.tar.gz"
ssh "$SSH_USER@$SERVER_IP" "cd $REMOTE_DIR && tar xzf /tmp/fcb-bot.tar.gz && rm /tmp/fcb-bot.tar.gz"
rm "$TMPFILE"

echo "  Files uploaded ✓"

# ── Step 4: Create .env ──
echo ""
echo "[4/6] Creating .env with API keys..."
ssh "$SSH_USER@$SERVER_IP" bash <<ENV_SCRIPT
cat > $REMOTE_DIR/.env <<EOF
BYBIT_API_KEY=$API_KEY
BYBIT_API_SECRET=$API_SECRET
EOF
chmod 600 $REMOTE_DIR/.env
echo "  .env created ✓"
ENV_SCRIPT

# ── Step 5: Build and start ──
echo ""
echo "[5/6] Building and starting containers..."
ssh "$SSH_USER@$SERVER_IP" bash <<START_SCRIPT
set -e
cd $REMOTE_DIR

# Create empty state/trade files if they don't exist (bind mounts need them)
touch live/state.json 2>/dev/null || true
touch live/trades.csv 2>/dev/null || true

# Build and start
docker compose build --no-cache
docker compose up -d

echo "  Containers started ✓"
docker compose ps
START_SCRIPT

# ── Step 6: Verify health ──
echo ""
echo "[6/6] Verifying deployment..."
sleep 10

ssh "$SSH_USER@$SERVER_IP" bash <<VERIFY
set -e
cd $REMOTE_DIR

# Check bot container
BOT_STATUS=\$(docker compose ps --format json fcb-bot 2>/dev/null | head -1)
echo "Bot container: \$(docker compose ps fcb-bot --format '{{.Status}}' 2>/dev/null || echo 'checking...')"

# Check dashboard
DASH_STATUS=\$(curl -sf http://localhost:8080/api/health 2>/dev/null || echo '{"status":"starting"}')
echo "Dashboard: \$DASH_STATUS"

# Show last few log lines
echo ""
echo "── Recent bot logs ──"
docker compose logs --tail 20 fcb-bot 2>/dev/null || true
VERIFY

echo ""
echo "═══════════════════════════════════════════════════"
echo "  DEPLOYMENT COMPLETE"
echo "═══════════════════════════════════════════════════"
echo ""
echo "  Dashboard: http://$SERVER_IP:8080/"
echo "  Bot logs:  ssh $SSH_USER@$SERVER_IP 'cd $REMOTE_DIR && docker compose logs -f fcb-bot'"
echo "  Stop:      ssh $SSH_USER@$SERVER_IP 'cd $REMOTE_DIR && docker compose down'"
echo "  Restart:   ssh $SSH_USER@$SERVER_IP 'cd $REMOTE_DIR && docker compose restart'"
echo ""
echo "  Trade data: ssh $SSH_USER@$SERVER_IP 'cat $REMOTE_DIR/live/logs/trades.jsonl'"
echo ""
