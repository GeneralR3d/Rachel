# Deploying Rachel to a VPS

This walks through deploying Rachel on a **fresh Ubuntu/Debian VPS** with nothing
installed. Setup: **Postgres in Docker, the app on the host via uv, managed by
systemd, with nginx as a reverse proxy** terminating HTTPS in front of the admin API.

> **Two things people trip on:**
> 1. **The Telegram login is interactive** — `scripts.login` prompts for a phone
>    number and the SMS code. It cannot be automated inside systemd or Docker; run
>    it by hand once over SSH.
> 2. **`DATABASE_URL` host differs by run mode** — use `localhost:5433` when the app
>    runs on the host (this guide), or `db:5432` only when the app runs *inside*
>    docker-compose.

---

## 1. Initial server setup

```bash
ssh root@YOUR_VPS_IP

apt update && apt upgrade -y
apt install -y git curl ca-certificates
```

## 2. Install Docker + Compose

```bash
curl -fsSL https://get.docker.com | sh
docker --version
docker compose version
```

## 3. Install nginx + certbot + auth tools

```bash
apt install -y nginx certbot python3-certbot-nginx apache2-utils
```

## 4. Get the code

```bash
cd /opt
git clone YOUR_REPO_URL rachel
cd rachel
```

`.env`, `anon.session`, and `bot.session` are gitignored and will **not** come with
a clone — you create them on the server in the steps below.

## 5. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env        # or re-open the shell

cd /opt/rachel
uv sync                            # installs deps into .venv
```

## 6. Configure `.env`

```bash
cp template.env .env
nano .env
```

Fill in:

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — from [my.telegram.org](https://my.telegram.org)
- `TELEGRAM_BOT_TOKEN` — admin bot from [@BotFather](https://t.me/botfather)
- `OPENROUTER_API_KEY`
- `ADMIN_ID` — your user id from [@userinfobot](https://t.me/userinfobot)
- `DATABASE_URL` — keep the **localhost:5433** form (default in `template.env`):

  ```
  DATABASE_URL=postgresql+asyncpg://rachel:rachel@localhost:5433/rachel
  ```

> ⚠️ Change the default `rachel:rachel` Postgres password in both
> `docker-compose.yml` and `DATABASE_URL` for any internet-facing VPS. Port 5433
> stays closed to the world (see firewall section).

## 7. Start Postgres

```bash
docker compose up -d db
docker compose ps                  # confirm healthy
```

## 8. Run migrations

```bash
uv run alembic upgrade head
```

## 9. One-time interactive Telegram login

Creates `anon.session` for Rachel's account. Prompts for her phone number and the
login code Telegram sends — do this in your interactive SSH session:

```bash
uv run python -m scripts.login
```

You only repeat this if the session is invalidated.

## 10. Smoke test

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Watch the logs: lifespan seeds prompts/traits/schedule and connects both Telegram
clients. From another shell, `curl localhost:8000/health`. Then `Ctrl-C`.

> Bind to `127.0.0.1` (not `0.0.0.0`) — nginx is the only thing that should talk to
> uvicorn. The admin API has **no auth of its own**, so it must never be reachable
> directly from the internet.

## 11. Run persistently with systemd

Create `/etc/systemd/system/rachel.service`:

```ini
[Unit]
Description=Rachel Telegram bot
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/rachel
ExecStart=/root/.local/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
ExecStartPre=/usr/bin/docker compose -f /opt/rachel/docker-compose.yml up -d db

[Install]
WantedBy=multi-user.target
```

Adjust the `uv` path if you didn't install as root (`which uv`). Then:

```bash
systemctl daemon-reload
systemctl enable --now rachel
systemctl status rachel
journalctl -u rachel -f            # live logs
```

## 12. Configure nginx as the reverse proxy

Point a **DNS A record** for `rachel.yourdomain.com` at the VPS IP before continuing.

Create `/etc/nginx/sites-available/rachel`:

```nginx
server {
    server_name rachel.yourdomain.com;

    # Admin API has no auth of its own — gate it here.
    auth_basic "Rachel admin";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    listen 80;
}
```

Create the basic-auth user, enable the site, and reload:

```bash
htpasswd -c /etc/nginx/.htpasswd admin     # prompts for a password

ln -s /etc/nginx/sites-available/rachel /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

## 13. Enable HTTPS with certbot

```bash
certbot --nginx -d rachel.yourdomain.com   # rewrites the config to 443 + HTTPS
```

Certbot auto-renews via a systemd timer; test with `certbot renew --dry-run`.

## 14. Firewall

```bash
apt install -y ufw
ufw allow OpenSSH
ufw allow 'Nginx Full'             # opens 80 + 443
ufw enable
```

Ports **8000** (uvicorn) and **5433** (Postgres) stay closed to the internet —
uvicorn binds to `127.0.0.1` and nginx is the only public entry point.

---

## Order that matters (recap)

1. Docker, nginx, uv installed
2. `.env` filled (`localhost:5433` DB URL)
3. `docker compose up -d db`
4. `uv run alembic upgrade head`
5. `uv run python -m scripts.login` ← interactive, by hand
6. systemd unit → `systemctl enable --now rachel` (uvicorn on `127.0.0.1`)
7. nginx site + basic auth → `certbot --nginx` for HTTPS
8. ufw: SSH + Nginx Full only

Browse the admin API at `https://rachel.yourdomain.com/docs` (basic-auth prompt).

## Updating a deployment

```bash
cd /opt/rachel
git pull
uv sync
uv run alembic upgrade head
systemctl restart rachel
```
