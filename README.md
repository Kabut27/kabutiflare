<div align="center">

# ☁️ KabutiFlare ⚡

### Cloudflare Manager + 24/7 HA Auto-Failover — right inside Telegram

![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Cloudflare](https://img.shields.io/badge/Cloudflare-API-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)
![HA](https://img.shields.io/badge/High%20Availability-Multi--Node-22C55E?style=for-the-badge)

**🌐 Domains · 🛠️ DNS · 📊 Analytics · 🔄 Auto-Failover**

</div>

---

## ✨ Features

| | Feature |
|---|---|
| 🌐 | Manage your Cloudflare domains from Telegram |
| 🛠️ | Add, edit and delete DNS records |
| 📊 | Traffic analytics |
| ⚡ | Automatic DNS failover between your VPS nodes |
| 👑 | Admin panel, user list, ban and broadcast |
| 🔒 | Optional private mode |

---

## 🚀 Quick Install

Run as **root** on your VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/Kabut27/kabutiflare/main/install.sh | bash
```

The installer asks for your bot token, admin ID and (optionally) a Cloudflare token, then builds and starts everything with Docker. Open your bot and send `/start` 🎉

---

## ⚙️ Configuration

Edit `/opt/kabutiflare/.env`, then restart.

| 🔑 Variable | 📝 Description |
|---|---|
| `BOT_TOKEN` | 🤖 Telegram bot token (from @BotFather) |
| `ADMIN_ID` | 👑 Your numeric Telegram ID |
| `PRIVATE_MODE` | 🔒 `true` = only allowed users |
| `CF_API_TOKEN` | ☁️ Cloudflare token: **Zone · DNS · Edit** |
| `CF_ZONE_ID` | 🌍 Zone used for failover |
| `CONFIG_RECORD_NAME` | 📌 Shared TXT record, e.g. `fail.example.com` |
| `NODE_ID` | 🖥️ Unique name for each VPS |

---

## 🖥️ Multiple Nodes (HA)

Install on every VPS, then:

- ✅ Keep `BOT_TOKEN`, `ADMIN_ID`, `CF_API_TOKEN`, `CF_ZONE_ID`, `CONFIG_RECORD_NAME` **identical**
- ✅ Give each node a **unique** `NODE_ID`
- 👑 One node is elected **leader** and answers Telegram; the others stand by 😴
- 🔁 If the leader goes down, another node takes over automatically

> [!NOTE]
> `data/users.json` is stored per node. Copy it between nodes if you want the same users everywhere.

---

## 🧰 Useful Commands

```bash
cd /opt/kabutiflare
docker compose logs -f     # 📜 logs
docker compose restart     # 🔄 restart
docker compose down        # 🛑 stop
```

⬆️ **Update:** run the install command again and choose to reuse your `.env`.

---

## 🩺 Troubleshooting

<details>
<summary><b>🤐 Bot does not reply</b></summary>

<br>

- `Leader check failed` → Cloudflare token or zone ID is wrong or incomplete
- `409 Conflict` → another instance uses the same bot token
- Check logs: `docker compose logs --tail 50`

</details>

> [!WARNING]
> Never share your `.env` or tokens. If one leaks, regenerate it immediately.

---

<div align="center">

Built with ❤️ by [**Kabut27**](https://github.com/Kabut27)

</div>
