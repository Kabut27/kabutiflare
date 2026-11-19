#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "  ╔═══════════════════════════════════════════╗"
echo "  ║           KabutiFlare Installer           ║"
echo "  ║  Cloudflare Manager + Auto-Failover Bot   ║"
echo "  ╚═══════════════════════════════════════════╝"
echo -e "${NC}"

REPO="https://raw.githubusercontent.com/Kabut27/kabutiflare/main"
DIR="/opt/kabutiflare"

# Soma input hata script ikikimbizwa kupitia: curl -fsSL ... | bash
ask() {
  local prompt="$1"
  local var_name="$2"
  local default_val="${3:-}"
  local input=""
  if [ -r /dev/tty ]; then
    read -r -p "$prompt" input < /dev/tty
  else
    read -r -p "$prompt" input
  fi
  if [ -z "$input" ]; then
    eval "$var_name=\"\$default_val\""
  else
    eval "$var_name=\"\$input\""
  fi
}

# Pakia .env ya zamani kama ipo (kwa ajili ya update bila kupoteza config)
OLD_BOT_TOKEN=""
OLD_CF_TOKEN=""
OLD_WEBAPP_URL=""
OLD_ADMIN_ID="474008580"
if [ -f "$DIR/.env" ]; then
  echo -e "${YELLOW}ℹ️  Existing .env detected in $DIR — press Enter to keep saved values.${NC}\n"
  OLD_BOT_TOKEN=$(grep -E '^BOT_TOKEN=' "$DIR/.env" | cut -d'=' -f2- | tr -d '"' || true)
  OLD_CF_TOKEN=$(grep -E '^CF_API_TOKEN=' "$DIR/.env" | cut -d'=' -f2- | tr -d '"' || true)
  OLD_WEBAPP_URL=$(grep -E '^WEBAPP_URL=' "$DIR/.env" | cut -d'=' -f2- | tr -d '"' || true)
  _aid=$(grep -E '^ADMIN_ID=' "$DIR/.env" | cut -d'=' -f2- | tr -d '"' || true)
  [ -n "$_aid" ] && OLD_ADMIN_ID="$_aid"
fi

# ── 1. Install Method ─────────────────────────────────────────────────────────
echo -e "${YELLOW}Install method:${NC}"
echo "  1) 🐳 Docker (recommended)"
echo "  2) 📦 Systemd (without Docker)"
ask "Choose [1/2] (default 1): " METHOD "1"

# ── 2. Bot Token (Required) ───────────────────────────────────────────────────
echo ""
if [ -n "$OLD_BOT_TOKEN" ]; then
  ask "🤖 Telegram Bot Token [Enter to keep existing]: " BOT_TOKEN "$OLD_BOT_TOKEN"
else
  ask "🤖 Telegram Bot Token: " BOT_TOKEN ""
fi
if [ -z "$BOT_TOKEN" ]; then
  echo -e "${RED}❌ Bot token is required.${NC}"
  exit 1
fi

# ── 3. Cloudflare API Token (Optional — recommended for 24/7 Auto-Failover) ───
echo ""
echo -e "${YELLOW}☁️  Cloudflare API Token (optional):${NC}"
echo "   • Recommended if you want 24/7 Auto-Failover & Admin auto-login."
echo "   • Press Enter to skip (you can connect later inside Telegram)."
if [ -n "$OLD_CF_TOKEN" ]; then
  ask "CF API Token [Enter to keep existing]: " CF_API_TOKEN "$OLD_CF_TOKEN"
else
  ask "CF API Token: " CF_API_TOKEN ""
fi

# ── Zima cf-failover-telegram ya zamani kuzuia 409 Conflict ───────────────────
if systemctl list-unit-files 2>/dev/null | grep -q '^cf-failover-telegram\.service'; then
  echo -e "\n${YELLOW}⚠️  Stopping old cf-failover-telegram.service to prevent Telegram 409 Conflict...${NC}"
  systemctl stop cf-failover-telegram.service 2>/dev/null || true
  systemctl disable cf-failover-telegram.service 2>/dev/null || true
fi

# ── Download files ────────────────────────────────────────────────────────────
echo -e "\n${CYAN}📥 Downloading KabutiFlare files...${NC}"
mkdir -p "$DIR"
cd "$DIR"

curl -fsSL "$REPO/bot.py"             -o bot.py
curl -fsSL "$REPO/requirements.txt"   -o requirements.txt
curl -fsSL "$REPO/Dockerfile"         -o Dockerfile
curl -fsSL "$REPO/docker-compose.yml" -o docker-compose.yml

# ── Create Pre-configured .env ────────────────────────────────────────────────
cat > .env << EOF
# Core Telegram & Admin
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${OLD_ADMIN_ID}
PRIVATE_MODE=false
WEBAPP_URL=${OLD_WEBAPP_URL}

# Cloudflare Auto-Login & 24/7 Failover (Admin)
CF_API_TOKEN=${CF_API_TOKEN}

# Failover Engine Defaults (can also be edited inside Telegram)
FAILOVER_CHECK_METHOD=tcp
FAILOVER_CHECK_PORT=443
FAILOVER_CHECK_TIMEOUT=5
FAILOVER_INTERVAL=30
FAILOVER_DNS_TTL=60
FAILOVER_HEALTH_PATH=/
EOF
chmod 600 .env

# ── Init users.json ───────────────────────────────────────────────────────────
if [ ! -f users.json ]; then
  echo '{"users":{},"cf_logins":{},"banned_users":[],"failover":{}}' > users.json
fi

# ═════════════════════════════════════════════════════════════════════════════
if [ "$METHOD" = "1" ]; then
  echo -e "\n${CYAN}🐳 Installing with Docker...${NC}"

  if ! command -v docker &>/dev/null; then
    echo -e "${YELLOW}📦 Installing Docker...${NC}"
    curl -fsSL https://get.docker.com | sh
  fi

  if ! docker compose version &>/dev/null 2>&1; then
    echo -e "${YELLOW}📦 Installing Docker Compose plugin...${NC}"
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin 2>/dev/null || true
  fi

  docker compose down 2>/dev/null || true
  docker compose up -d --build

  echo -e "\n${GREEN}✅ KabutiFlare deployed with Docker!${NC}"
  echo -e "📁 Directory : ${CYAN}$DIR${NC}"
  echo -e "🔧 Logs      : ${CYAN}docker compose -f $DIR/docker-compose.yml logs -f${NC}"
  echo -e "🔄 Restart   : ${CYAN}docker compose -f $DIR/docker-compose.yml restart${NC}"
  echo -e "🛑 Stop      : ${CYAN}docker compose -f $DIR/docker-compose.yml down${NC}"
  echo -e "⬆️  Update    : ${CYAN}curl -fsSL $REPO/install.sh | bash${NC}"

else
  echo -e "\n${CYAN}📦 Installing with Systemd...${NC}"

  apt-get update -qq
  apt-get install -y -qq python3 python3-pip >/dev/null 2>&1

  pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt

  cat > /etc/systemd/system/kabutiflare.service << EOF
[Unit]
Description=KabutiFlare Telegram Bot (Cloudflare + Auto-Failover)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
ExecStart=/usr/bin/python3 $DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable kabutiflare
  systemctl restart kabutiflare

  echo -e "\n${GREEN}✅ KabutiFlare deployed with Systemd!${NC}"
  echo -e "📁 Directory : ${CYAN}$DIR${NC}"
  echo -e "🔧 Logs      : ${CYAN}journalctl -u kabutiflare -f${NC}"
  echo -e "🔄 Restart   : ${CYAN}systemctl restart kabutiflare${NC}"
  echo -e "🛑 Stop      : ${CYAN}systemctl stop kabutiflare${NC}"
  echo -e "⬆️  Update    : ${CYAN}curl -fsSL $REPO/install.sh | bash${NC}"
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  KabutiFlare is running! 🚀${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "Built with ❤️  by ${CYAN}Kabut27${NC}"
