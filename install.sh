#!/usr/bin/env bash
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

if [ "$(id -u)" -ne 0 ]; then
  echo -e "${RED}❌ Run as root (sudo).${NC}"
  exit 1
fi

REPO="https://raw.githubusercontent.com/Kabut27/kabutiflare/main"
DIR="/opt/kabutiflare"

# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_ADMIN_ID="474008580"
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

# Safe input reader (works with: curl -fsSL ... | bash)
ask() {
  local prompt="$1" varname="$2" default="${3:-}" input=""
  if [ -r /dev/tty ]; then
    read -r -p "$prompt" input </dev/tty || true
  else
    read -r -p "$prompt" input || true
  fi
  [ -z "$input" ] && input="$default"
  printf -v "$varname" '%s' "$input"
}

gen_node_id() {
  local host_part rand_part
  host_part="$(hostname -s 2>/dev/null | tr -cd 'a-zA-Z0-9_-')"
  [ -z "$host_part" ] && host_part="vps"
  rand_part="$(head -c 2 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "${host_part}-${rand_part}"
}

# Token never appears in `ps`: the secret goes through stdin to curl --config
tg_getme() {
  printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$1" |
    curl -sS --connect-timeout 8 --max-time 15 --config - 2>/dev/null || true
}
cf_get() {
  printf 'url = "%s"\nheader = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "$1" "$CF_API_TOKEN" |
    curl -sS --connect-timeout 8 --max-time 15 --config - 2>/dev/null || true
}

# ── Smart Docker check: install Docker and/or Compose only if missing ───────
DC=""
ensure_docker() {
  if command -v docker &>/dev/null; then
    echo -e "${GREEN}✅ Docker found: $(docker --version 2>/dev/null)${NC}"
  else
    echo -e "${YELLOW}📦 Docker not found — installing...${NC}"
    if ! curl -fsSL https://get.docker.com | sh; then
      echo -e "${RED}❌ Docker installation failed. Install it manually and run again.${NC}"
      exit 1
    fi
  fi

  # make sure the daemon is running
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do docker info >/dev/null 2>&1 && break; sleep 2; done
  if ! docker info >/dev/null 2>&1; then
    echo -e "${RED}❌ Docker daemon is not running. Try: systemctl start docker${NC}"
    exit 1
  fi

  # Compose: plugin (v2) → standalone → apt → direct download
  if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
  elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    DC="docker-compose"
  else
    echo -e "${YELLOW}📦 Docker Compose not found — installing...${NC}"
    apt-get update -qq >/dev/null 2>&1 || true
    for pkg in docker-compose-plugin docker-compose-v2; do
      apt-get install -y -qq "$pkg" >/dev/null 2>&1 || true
      docker compose version >/dev/null 2>&1 && break
    done
    if ! docker compose version >/dev/null 2>&1; then
      case "$(uname -m)" in
        x86_64|amd64)  CARCH="x86_64" ;;
        aarch64|arm64) CARCH="aarch64" ;;
        armv7l)        CARCH="armv7" ;;
        *)             CARCH="" ;;
      esac
      if [ -n "$CARCH" ]; then
        mkdir -p /usr/local/lib/docker/cli-plugins
        if curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${CARCH}" \
             -o /usr/local/lib/docker/cli-plugins/docker-compose; then
          chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
        fi
      fi
    fi
    if docker compose version >/dev/null 2>&1; then
      DC="docker compose"
    else
      echo -e "${RED}❌ Could not install Docker Compose. Use the Systemd method (2) instead.${NC}"
      exit 1
    fi
  fi
  echo -e "${GREEN}✅ Compose ready: ${DC}${NC}"
}

# ── Stop legacy service (prevents 409 Conflict) ─────────────────────────────
if systemctl list-unit-files 2>/dev/null | grep -q "cf-failover-telegram.service"; then
  echo -e "${YELLOW}⚠️  Old cf-failover-telegram service found — disabling...${NC}"
  systemctl disable --now cf-failover-telegram 2>/dev/null || true
fi

# ── Install method ──────────────────────────────────────────────────────────
echo -e "${YELLOW}Install method:${NC}"
echo "  1) 🐳 Docker (recommended)"
echo "  2) 📦 Systemd (no Docker)"
ask "Choose [1/2] (default 1): " METHOD "1"

mkdir -p "$DIR/data"
cd "$DIR"

# Migrate legacy users.json (old layout) into data/
if [ -f "$DIR/users.json" ] && [ ! -f "$DIR/data/users.json" ]; then
  mv "$DIR/users.json" "$DIR/data/users.json"
  echo -e "${GREEN}✅ Migrated users.json → data/users.json${NC}"
elif [ -d "$DIR/users.json" ]; then
  rmdir "$DIR/users.json" 2>/dev/null || true
fi
[ -f "$DIR/data/users.json" ] || echo '{"users":{},"cf_logins":{},"banned_users":[]}' > "$DIR/data/users.json"

REUSE_ENV="n"
if [ -f "$DIR/.env" ]; then
  echo ""
  echo -e "${YELLOW}📄 Existing .env found${NC}"
  ask "Reuse it and only update bot files? [Y/n]: " REUSE_ENV "y"
fi

if [[ ! "${REUSE_ENV,,}" =~ ^(y|yes)$ ]]; then
  # ── 1. Telegram bot token (validated) ─────────────────────────────────────
  echo ""
  while true; do
    ask "🤖 Telegram Bot Token: " BOT_TOKEN ""
    if [ -z "$BOT_TOKEN" ]; then
      echo -e "${RED}❌ Bot token is required.${NC}"
      continue
    fi
    if tg_getme "$BOT_TOKEN" | grep -q '"ok":true'; then
      echo -e "${GREEN}✅ Token is valid.${NC}"
      break
    fi
    echo -e "${RED}❌ Telegram rejected this token. Try again.${NC}"
  done

  # ── 2. Admin ID & access mode ─────────────────────────────────────────────
  echo ""
  while true; do
    ask "👑 Your Telegram ID — admin (Enter = ${DEFAULT_ADMIN_ID}): " ADMIN_ID "$DEFAULT_ADMIN_ID"
    [[ "$ADMIN_ID" =~ ^[0-9]+$ ]] && break
    echo -e "${RED}❌ Must be numeric.${NC}"
  done
  ask "🔒 Private mode — only you can use the bot? [y/N]: " PRIV "n"
  if [[ "${PRIV,,}" =~ ^(y|yes)$ ]]; then PRIVATE_MODE="true"; else PRIVATE_MODE="false"; fi

  # ── 3. Cloudflare API token (admin failover) ──────────────────────────────
  echo ""
  echo -e "${YELLOW}⚡ Cloudflare API Token for 24/7 Auto-Failover (admin only)${NC}"
  echo "   Needs: Zone · DNS · Edit.  Press Enter to skip (standalone mode)."
  ask "🔑 Cloudflare API Token: " CF_API_TOKEN ""

  CF_ZONE_ID=""
  CONFIG_RECORD_NAME=""
  AUTO_NODE_ID="$(gen_node_id)"

  if [ -n "$CF_API_TOKEN" ]; then
    if ! cf_get "https://api.cloudflare.com/client/v4/user/tokens/verify" | grep -q '"success":true'; then
      echo -e "${YELLOW}⚠️  Cloudflare did not verify this token (check it later in .env).${NC}"
    fi

    echo -e "\n${CYAN}🔍 Fetching your Cloudflare domains...${NC}"
    ZONES_JSON="$(cf_get "https://api.cloudflare.com/client/v4/zones?per_page=50")"

    if ! command -v python3 &>/dev/null && ! command -v jq &>/dev/null; then
      apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq jq >/dev/null 2>&1 || true
    fi

    ZONE_LINES=""
    if [ -n "$ZONES_JSON" ]; then
      if command -v python3 &>/dev/null; then
        ZONE_LINES="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    if d.get("success"):
        for z in d.get("result", []):
            print(z["id"] + "|" + z["name"])
except Exception:
    pass
' <<< "$ZONES_JSON")"
      elif command -v jq &>/dev/null; then
        ZONE_LINES="$(jq -r 'if .success then .result[] | "\(.id)|\(.name)" else empty end' <<< "$ZONES_JSON" 2>/dev/null || true)"
      fi
    fi

    if [ -n "$ZONE_LINES" ]; then
      mapfile -t ZONE_ARR <<< "$ZONE_LINES"
      echo -e "${GREEN}✅ Domains found:${NC}"
      idx=1
      for item in "${ZONE_ARR[@]}"; do
        echo "  ${idx}) ${item#*|}"
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
      echo -e "${GREEN}✅ Selected:${NC} ${ZONE_NAME}"
      ask "📝 Shared TXT record name (Enter = ${DEFAULT_CFG_REC}): " CONFIG_RECORD_NAME "$DEFAULT_CFG_REC"
    else
      echo -e "${YELLOW}⚠️  Could not auto-fetch domains. Enter manually:${NC}"
      ask "🆔 Cloudflare Zone ID: " CF_ZONE_ID ""
      ask "📝 Shared TXT record name (e.g. _failover.domain.com): " CONFIG_RECORD_NAME ""
    fi

    echo ""
    ask "🖥️  Node ID for this VPS (Enter = ${AUTO_NODE_ID}): " NODE_ID "$AUTO_NODE_ID"
  else
    NODE_ID="$AUTO_NODE_ID"
  fi

  # ── 4. Optional Webapp URL ────────────────────────────────────────────────
  echo ""
  ask "🌐 Webapp URL (optional — Enter to skip): " WEBAPP_URL ""

  cat > "$DIR/.env" << EOF
# ── KabutiFlare Bot ──────────────────────────────────────────
BOT_TOKEN=${BOT_TOKEN}
WEBAPP_URL=${WEBAPP_URL}
ADMIN_ID=${ADMIN_ID}
PRIVATE_MODE=${PRIVATE_MODE}
ALLOWED_USERS=${ADMIN_ID}

# ── Node Identity & HA Leader Election ───────────────────────
NODE_ID=${NODE_ID}
CF_TG_HEARTBEAT_INTERVAL=${DEFAULT_HEARTBEAT}
CF_TG_LEADER_TTL=${DEFAULT_LEADER_TTL}

# ── Admin 24/7 Auto-Failover (admin only) ────────────────────
CF_API_TOKEN=${CF_API_TOKEN}
CF_ZONE_ID=${CF_ZONE_ID}
CONFIG_RECORD_NAME=${CONFIG_RECORD_NAME}
TELEGRAM_CHAT_ID=${ADMIN_ID}

# Initial fallback (only used while the Cloudflare TXT is empty)
NODE_PRIORITY=
TARGET_RECORDS=

# Health check
CHECK_METHOD=${DEFAULT_CHECK_METHOD}
CHECK_PORT=${DEFAULT_CHECK_PORT}
CHECK_SCHEME=${DEFAULT_CHECK_SCHEME}
HEALTH_PATH=${DEFAULT_HEALTH_PATH}
CHECK_TIMEOUT=${DEFAULT_CHECK_TIMEOUT}
CHECK_INTERVAL=${DEFAULT_CHECK_INTERVAL}
DNS_TTL=${DEFAULT_DNS_TTL}
DEFAULT_PROXIED=${DEFAULT_PROXIED}

# Set to false on an IPv6-only VPS
FORCE_IPV4=true
EOF
  chmod 600 "$DIR/.env"
  echo -e "${GREEN}✅ .env created (chmod 600).${NC}"
else
  # Upgrading from the old version: make sure the new required keys exist
  if ! grep -q '^ADMIN_ID=' "$DIR/.env"; then
    echo "ADMIN_ID=${DEFAULT_ADMIN_ID}" >> "$DIR/.env"
    echo -e "${YELLOW}ℹ️  ADMIN_ID was missing — added ${DEFAULT_ADMIN_ID}.${NC}"
  fi
  chmod 600 "$DIR/.env"
fi

# ── Download latest files ───────────────────────────────────────────────────
echo -e "\n${CYAN}📥 Downloading latest files from GitHub...${NC}"
for f in bot.py requirements.txt Dockerfile docker-compose.yml; do
  curl -fsSL "$REPO/$f" -o "$DIR/$f.new"
  mv "$DIR/$f.new" "$DIR/$f"
done

if crontab -l 2>/dev/null | grep -q "cf-failover.sh"; then
  echo -e "${YELLOW}⚠️  Old cron job for cf-failover.sh found. Remove it: ${CYAN}crontab -e${NC} (bot.py handles failover now)."
fi

# ═════════════════════════════════════════════════════════════════════════════
if [ "$METHOD" = "1" ]; then
  echo -e "\n${CYAN}🐳 Building & starting with Docker...${NC}"

  ensure_docker

  systemctl disable --now kabutiflare 2>/dev/null || true   # avoid double polling if migrating
  $DC down 2>/dev/null || true
  $DC up -d --build

  sleep 5
  echo -e "\n${CYAN}📜 Last logs:${NC}"
  $DC logs --tail 12 2>/dev/null || true

  echo -e "\n${GREEN}✅ KabutiFlare deployed with Docker!${NC}"
  echo -e "📁 Directory : ${CYAN}$DIR${NC}"
  echo -e "⚙️  Config    : ${CYAN}nano $DIR/.env${NC}  (then restart)"
  echo -e "🔧 Logs      : ${CYAN}docker compose -f $DIR/docker-compose.yml logs -f${NC}"
  echo -e "🔄 Restart   : ${CYAN}docker compose -f $DIR/docker-compose.yml restart${NC}"
  echo -e "🛑 Stop      : ${CYAN}docker compose -f $DIR/docker-compose.yml down${NC}"
  echo -e "⬆️  Update    : ${CYAN}curl -fsSL $REPO/install.sh | bash${NC}"
else
  echo -e "\n${CYAN}📦 Installing with Systemd...${NC}"

  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip >/dev/null

  [ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
  "$DIR/venv/bin/pip" install -q --upgrade pip
  "$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

  cat > /etc/systemd/system/kabutiflare.service << EOF
[Unit]
Description=KabutiFlare Telegram Bot & HA Failover
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
Environment=DATA_DIR=$DIR/data
ExecStart=$DIR/venv/bin/python $DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
    (cd "$DIR" && docker compose down 2>/dev/null) || true   # avoid double polling if migrating
  fi
  systemctl daemon-reload
  systemctl enable kabutiflare
  systemctl restart kabutiflare

  sleep 4
  echo -e "\n${CYAN}📜 Last logs:${NC}"
  journalctl -u kabutiflare -n 12 --no-pager 2>/dev/null || true

  echo -e "\n${GREEN}✅ KabutiFlare deployed with Systemd!${NC}"
  echo -e "📁 Directory : ${CYAN}$DIR${NC}"
  echo -e "⚙️  Config    : ${CYAN}nano $DIR/.env${NC}  (then restart)"
  echo -e "🔧 Logs      : ${CYAN}journalctl -u kabutiflare -f${NC}"
  echo -e "🔄 Restart   : ${CYAN}systemctl restart kabutiflare${NC}"
  echo -e "🛑 Stop      : ${CYAN}systemctl stop kabutiflare${NC}"
  echo -e "⬆️  Update    : ${CYAN}curl -fsSL $REPO/install.sh | bash${NC}"
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  KabutiFlare is running! 🚀  Open the bot → /start${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "Built with ❤️  by ${CYAN}Kabut27${NC}"
