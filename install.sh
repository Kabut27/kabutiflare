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
echo "  ║   Cloudflare Manager + HA Auto-Failover   ║"
echo "  ╚═══════════════════════════════════════════╝"
echo -e "${NC}"

REPO="https://raw.githubusercontent.com/Kabut27/kabutiflare/main"
DIR="/opt/kabutiflare"

# ── Pre-configured Defaults ──────────────────────────────────────────────────
DEFAULT_ADMIN_ID="474008580"
DEFAULT_PRIVATE_MODE="false"
DEFAULT_CHECK_METHOD="tcp"
DEFAULT_CHECK_PORT="443"
DEFAULT_CHECK_SCHEME="https"
DEFAULT_HEALTH_PATH="/"
DEFAULT_CHECK_TIMEOUT="5"
DEFAULT_CHECK_INTERVAL="30"
DEFAULT_DNS_TTL="60"
DEFAULT_PROXIED="false"
DEFAULT_HEARTBEAT="30"
DEFAULT_LEADER_TTL="90"

# Safe input reader (works even with: curl -fsSL ... | bash)
ask() {
  local prompt="$1"
  local varname="$2"
  local default="${3:-}"
  local input=""
  if [ -r /dev/tty ]; then
    read -r -p "$prompt" input </dev/tty || true
  else
    read -r -p "$prompt" input || true
  fi
  if [ -z "$input" ]; then
    input="$default"
  fi
  printf -v "$varname" '%s' "$input"
}

gen_node_id() {
  local host_part rand_part
  host_part="$(hostname -s 2>/dev/null | tr -cd 'a-zA-Z0-9_-' || echo "vps")"
  rand_part="$(head -c 2 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n' || date +%S)"
  echo "${host_part}-${rand_part}"
}

# ── Stop legacy cf-failover-telegram service if present (prevents 409 Conflict)
if systemctl list-unit-files 2>/dev/null | grep -q "cf-failover-telegram.service"; then
  echo -e "${YELLOW}⚠️  Old cf-failover-telegram service detected — disabling to avoid 409 Conflict...${NC}"
  systemctl disable --now cf-failover-telegram 2>/dev/null || true
fi

# ── Install method ───────────────────────────────────────────────────────────
echo -e "${YELLOW}Install method:${NC}"
echo "  1) 🐳 Docker (recommended)"
echo "  2) 📦 Systemd (without Docker)"
ask "Choose [1/2] (default 1): " METHOD "1"

mkdir -p "$DIR"
cd "$DIR"

REUSE_ENV="n"
if [ -f "$DIR/.env" ]; then
  echo ""
  echo -e "${YELLOW}📄 Existing .env found in $DIR/.env${NC}"
  ask "Reuse existing .env and only update bot files? [Y/n]: " REUSE_ENV "y"
fi

if [[ ! "${REUSE_ENV,,}" =~ ^(y|yes)$ ]]; then
  # ── 1. Telegram Bot Token ──────────────────────────────────────────────────
  echo ""
  ask "🤖 Telegram Bot Token: " BOT_TOKEN ""
  if [ -z "$BOT_TOKEN" ]; then
    echo -e "${RED}❌ Bot token is required.${NC}"
    exit 1
  fi

  # ── 2. Cloudflare API Token (for 24/7 HA Failover + Leader Election) ───────
  echo ""
  echo -e "${YELLOW}⚡ Cloudflare API Token (kwa 24/7 Auto-Failover & Leader Election):${NC}"
  echo "   (Press Enter to skip and run in Standalone mode)"
  ask "🔑 Cloudflare API Token: " CF_API_TOKEN ""

  CF_ZONE_ID=""
  CONFIG_RECORD_NAME=""
  AUTO_NODE_ID="$(gen_node_id)"

  if [ -n "$CF_API_TOKEN" ]; then
    echo -e "\n${CYAN}🔍 Fetching your Cloudflare domains...${NC}"
    ZONES_JSON="$(curl -sS --connect-timeout 8 --max-time 15 --config - <<EOF 2>/dev/null || true
url = "https://api.cloudflare.com/client/v4/zones?per_page=50"
header = "Authorization: Bearer ${CF_API_TOKEN}"
header = "Content-Type: application/json"
EOF
)"

    ZONE_LINES=""
    if command -v python3 &>/dev/null && [ -n "$ZONES_JSON" ]; then
      ZONE_LINES="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    if d.get("success"):
        for z in d.get("result", []):
            print(f"{z[\"id\"]}|{z[\"name\"]}")
except Exception:
    pass
' <<< "$ZONES_JSON")"
    elif command -v jq &>/dev/null && [ -n "$ZONES_JSON" ]; then
      ZONE_LINES="$(jq -r 'if .success then .result[] | "\(.id)|\(.name)" else empty end' <<< "$ZONES_JSON" 2>/dev/null || true)"
    fi

    if [ -n "$ZONE_LINES" ]; then
      mapfile -t ZONE_ARR <<< "$ZONE_LINES"
      echo -e "${GREEN}✅ Domains found:${NC}"
      idx=1
      for item in "${ZONE_ARR[@]}"; do
        zname="${item#*|}"
        echo "  ${idx}) ${zname}"
        idx=$((idx + 1))
      done
      ask "Select Failover domain [1-${#ZONE_ARR[@]}] (default 1): " ZPICK "1"
      if ! [[ "$ZPICK" =~ ^[0-9]+$ ]] || [ "$ZPICK" -lt 1 ] || [ "$ZPICK" -gt "${#ZONE_ARR[@]}" ]; then
        ZPICK=1
      fi
      SELECTED="${ZONE_ARR[$((ZPICK - 1))]}"
      CF_ZONE_ID="${SELECTED%%|*}"
      ZONE_NAME="${SELECTED#*|}"
      DEFAULT_CFG_REC="_failover.${ZONE_NAME}"
      echo -e "${GREEN}✅ Selected:${NC} ${ZONE_NAME} (${CF_ZONE_ID})"
      ask "📝 Shared TXT Record Name (Enter = ${DEFAULT_CFG_REC}): " CONFIG_RECORD_NAME "$DEFAULT_CFG_REC"
    else
      echo -e "${YELLOW}⚠️  Could not auto-fetch domains (check token permissions or python3/jq). Enter manually:${NC}"
      ask "🆔 Cloudflare Zone ID: " CF_ZONE_ID ""
      ask "📝 Shared TXT Record Name (e.g. _failover.domain.com): " CONFIG_RECORD_NAME ""
    fi

    echo ""
    ask "🖥️  Node ID for this VPS (Enter = ${AUTO_NODE_ID}): " NODE_ID "$AUTO_NODE_ID"
  else
    NODE_ID="$AUTO_NODE_ID"
  fi

  # ── 3. Optional Webapp URL ─────────────────────────────────────────────────
  echo ""
  ask "🌐 Webapp URL (optional — press Enter to skip): " WEBAPP_URL ""

  # ── Write pre-configured .env ──────────────────────────────────────────────
  cat > "$DIR/.env" << EOF
# ── KabutiFlare Bot ──────────────────────────────────────────
BOT_TOKEN=${BOT_TOKEN}
WEBAPP_URL=${WEBAPP_URL}
ADMIN_ID=${DEFAULT_ADMIN_ID}
PRIVATE_MODE=${DEFAULT_PRIVATE_MODE}
ALLOWED_USERS=${DEFAULT_ADMIN_ID}

# ── Node Identity & HA Leader Election ───────────────────────
NODE_ID=${NODE_ID}
CF_TG_HEARTBEAT_INTERVAL=${DEFAULT_HEARTBEAT}
CF_TG_LEADER_TTL=${DEFAULT_LEADER_TTL}

# ── Admin 24/7 Auto-Failover ─────────────────────────────────
CF_API_TOKEN=${CF_API_TOKEN}
CF_ZONE_ID=${CF_ZONE_ID}
CONFIG_RECORD_NAME=${CONFIG_RECORD_NAME}
TELEGRAM_CHAT_ID=${DEFAULT_ADMIN_ID}

# Initial fallback (only used if Cloudflare TXT is still empty)
NODE_PRIORITY=
TARGET_RECORDS=

# Health check settings (pre-configured)
CHECK_METHOD=${DEFAULT_CHECK_METHOD}
CHECK_PORT=${DEFAULT_CHECK_PORT}
CHECK_SCHEME=${DEFAULT_CHECK_SCHEME}
HEALTH_PATH=${DEFAULT_HEALTH_PATH}
CHECK_TIMEOUT=${DEFAULT_CHECK_TIMEOUT}
CHECK_INTERVAL=${DEFAULT_CHECK_INTERVAL}
DNS_TTL=${DEFAULT_DNS_TTL}
DEFAULT_PROXIED=${DEFAULT_PROXIED}
EOF
  chmod 600 "$DIR/.env"
  echo -e "${GREEN}✅ .env created (chmod 600).${NC}"
fi

# ── Download latest files from GitHub ────────────────────────────────────────
echo -e "\n${CYAN}📥 Downloading latest files from GitHub...${NC}"
curl -fsSL "$REPO/bot.py"             -o bot.py
curl -fsSL "$REPO/requirements.txt"   -o requirements.txt
curl -fsSL "$REPO/Dockerfile"         -o Dockerfile
curl -fsSL "$REPO/docker-compose.yml" -o docker-compose.yml

# ── Init users.json ──────────────────────────────────────────────────────────
[ -f users.json ] || echo '{"users":{},"cf_logins":{},"banned_users":[]}' > users.json

# ── Check for old cf-failover cron job ───────────────────────────────────────
if crontab -l 2>/dev/null | grep -q "cf-failover.sh"; then
  echo -e "${YELLOW}⚠️  Note: Old cron job for cf-failover.sh found in root crontab.${NC}"
  echo -e "   You can remove it with: ${CYAN}crontab -e${NC} (bot.py now handles failover 24/7)."
fi

# ═════════════════════════════════════════════════════════════════════════════
if [ "$METHOD" = "1" ]; then
  echo -e "\n${CYAN}🐳 Building & starting with Docker...${NC}"

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
  echo -e "⚙️  Config    : ${CYAN}nano $DIR/.env${NC}"
  echo -e "🔧 Logs      : ${CYAN}docker compose -f $DIR/docker-compose.yml logs -f${NC}"
  echo -e "🔄 Restart   : ${CYAN}docker compose -f $DIR/docker-compose.yml restart${NC}"
  echo -e "🛑 Stop      : ${CYAN}docker compose -f $DIR/docker-compose.yml down${NC}"
  echo -e "⬆️  Update    : ${CYAN}curl -fsSL $REPO/install.sh | bash${NC}"

else
  echo -e "\n${CYAN}📦 Installing with Systemd...${NC}"

  apt-get update -qq
  apt-get install -y -qq python3 python3-pip 2>/dev/null

  pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt

  cat > /etc/systemd/system/kabutiflare.service << EOF
[Unit]
Description=KabutiFlare Telegram Bot & HA Failover
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
  echo -e "⚙️  Config    : ${CYAN}nano $DIR/.env${NC}"
  echo -e "🔧 Logs      : ${CYAN}journalctl -u kabutiflare -f${NC}"
  echo -e "🔄 Restart   : ${CYAN}systemctl restart kabutiflare${NC}"
  echo -e "🛑 Stop      : ${CYAN}systemctl stop kabutiflare${NC}"
  echo -e "⬆️  Update    : ${CYAN}curl -fsSL $REPO/install.sh | bash${NC}"
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  KabutiFlare is running! 🚀${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "Built with ❤️  by ${CYAN}Kabut27${NC}"
