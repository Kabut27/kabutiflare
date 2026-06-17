# 🔥 KabutiFlare

> Manage Cloudflare DNS, SSL, Workers, Email Routing and more — directly from Telegram.

## ⚡ One-Command Deploy

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Kabut27/kabutiflare/main/install.sh)
```

## ✨ Features
- 🌐 Manage Cloudflare DNS records
- 🔒 SSL/TLS settings
- ⚙️ Zone settings
- 👷 Workers and Routes
- 📧 Email Routing
- 📄 Page Rules
- 🔐 Admin-only access control

## 📋 Requirements
- VPS/Server running Ubuntu/Debian
- Docker installed (or the installer will install it for you)
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

## 🚀 Manual Deploy

```bash
git clone https://github.com/Kabut27/kabutiflare.git /opt/kabutiflare
cd /opt/kabutiflare
cp env.example .env
nano .env          # Fill in your BOT_TOKEN
echo '{"users":{},"cf_logins":{}}' > users.json
docker compose up -d --build
```

## 🔧 Useful Commands

| Action  | Command |
|---------|---------|
| Start   | `docker compose up -d` |
| Stop    | `docker compose down` |
| Restart | `docker restart kabutiflare` |
| Logs    | `docker logs kabutiflare --tail 20` |
| Update  | `cd /opt/kabutiflare && git pull && docker compose down && docker compose up -d --build` |

## 🔑 Update Bot Token

```bash
nano /opt/kabutiflare/.env
# Change BOT_TOKEN, save with CTRL+X → Y → Enter
docker compose down && docker compose up -d --build
```

## 📬 Bot Commands

| Command | Description |
|---------|-------------|
| /start | Main menu |
| /connect | Connect Cloudflare account |
| /domains | List your domains |
| /dns | Manage DNS records |
| /disconnect | Disconnect account |
| /help | Help and guide |
| /stats | Bot stats (admin only) |
| /broadcast | Broadcast message (admin only) |

## 🛡️ Security Tips

- ⚠️ Keep your GitHub repo **Private**
- Never commit `.env` (it's in `.gitignore`)
- Revoke bot token from @BotFather if exposed
- Only add trusted Telegram IDs to `ALLOWED_USERS`

> Built with ❤️ by Kabut27
