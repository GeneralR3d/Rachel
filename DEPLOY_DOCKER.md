# Deploying Rachel to a VPS (fully Dockerized)

This walks through deploying Rachel on a **fresh Ubuntu/Debian VPS** with nothing
installed. Unlike [`DEPLOY.md`](./DEPLOY.md) (app on the host via uv + systemd),
here **both Postgres and the app run in docker-compose**. nginx stays on the host
as the reverse proxy terminating HTTPS in front of the admin API.

> **Three things people trip on:**
> 1. **The Telegram login is interactive** — `scripts.login` prompts for a phone
>    number and the SMS code. It cannot run unattended. Here we run it once in a
>    **one-off container** (`docker compose run`) over SSH.
> 2. **`DATABASE_URL` host differs by run mode** — inside docker-compose the app
>    reaches Postgres at **`db:5432`**, *not* `localhost:5433`. The compose file
>    injects the right value for the `app` service; don't override it in `.env`.
> 3. **Session files must exist on the host before the container starts** — they
>    are bind-mounted. Bind-mounting a path that doesn't exist makes Docker create
>    a *directory*, which then breaks the session. `touch` them first.

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

## 5. Configure `.env`

```bash
cp template.env .env
nano .env
```

Fill in:

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — from [my.telegram.org](https://my.telegram.org)
- `TELEGRAM_BOT_TOKEN` — admin bot from [@BotFather](https://t.me/botfather)
- `OPENROUTER_API_KEY`
- `ADMIN_ID` — your user id from [@userinfobot](https://t.me/userinfobot)
- `DB_PASSWORD` — **change this** from the `rachel` default for any internet-facing VPS.

> **Leave `DATABASE_URL` alone.** The `app` service in `docker-compose.yml` sets
> `DATABASE_URL=...@db:5432/rachel` via Compose env injection, which overrides
> whatever is in `.env`. The `localhost:5433` line in `template.env` is only for
> the host/uv workflow in `DEPLOY.md`.

## 6. Enable the `app` service in docker-compose

`docker-compose.yml` ships with the `app` service commented out (the default dev
setup runs only `db`). Uncomment it. Open the file and remove the leading `# ` from
the `app:` block so it reads:

```yaml
  app:
    build: .
    depends_on:
      db:
        condition: service_healthy
    env_file: .env
    environment:
      DATABASE_URL: postgresql+asyncpg://rachel:${DB_PASSWORD:-rachel}@db:5432/rachel
    restart: always
    ports:
      # Bind to loopback only — nginx is the only thing that should reach uvicorn.
      - "127.0.0.1:8000:8000"
    volumes:
      - ./anon.session:/app/anon.session
      - ./bot.session:/app/bot.session
```

> Two edits vs. the shipped comment block:
> - **`ports: 127.0.0.1:8000:8000`** (not `8000:8000`) so uvicorn is never exposed
>   to the internet — the admin API has no auth of its own.
> - **`restart: always`** so the app comes back after crashes/reboots (this is what
>   replaces systemd in the host-based guide).

The app `CMD` already runs `alembic upgrade head` before starting uvicorn, so there
is **no separate migration step** — migrations apply automatically on every
container start.

## 7. Create the session files on the host

Bind mounts need these to exist as files first:

```bash
touch anon.session bot.session
```

## 8. Start Postgres

```bash
docker compose up -d db
docker compose ps                  # confirm db is healthy
```

## 9. One-time interactive Telegram login (in a one-off container)

This creates `anon.session` for Rachel's account. It prompts for her phone number
and the login code Telegram sends — do it in your interactive SSH session. We use
`docker compose run` so it shares the same image/env and writes the session through
the bind mount onto the host:

```bash
docker compose run --rm app uv run python -m scripts.login
```

`--rm` discards the throwaway container afterwards; the resulting `anon.session`
persists on the host via the volume mount. You only repeat this if the session is
invalidated.

## 10. Build and start the app

```bash
docker compose up -d --build app
docker compose ps                  # app + db both up
docker compose logs -f app         # watch lifespan seed prompts/traits/schedule
                                   # and connect both Telegram clients
```

Smoke test from the host (uvicorn is bound to loopback):

```bash
curl localhost:8000/health
```

> Because the port is published as `127.0.0.1:8000:8000`, this works from the VPS
> itself but is unreachable from the internet — exactly what we want. nginx (next)
> is the only public entry point.

## 11. Configure nginx as the reverse proxy

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

## 12. Enable HTTPS with certbot

```bash
certbot --nginx -d rachel.yourdomain.com   # rewrites the config to 443 + HTTPS
```

Certbot auto-renews via a systemd timer; test with `certbot renew --dry-run`.

## 13. Firewall

```bash
apt install -y ufw
ufw allow OpenSSH
ufw allow 'Nginx Full'             # opens 80 + 443
ufw enable
```

Ports **8000** (uvicorn) and **5433** (Postgres) stay closed to the internet — both
are published only on `127.0.0.1`, and nginx is the only public entry point.

---

## Order that matters (recap)

1. Docker, nginx installed
2. `.env` filled (set `DB_PASSWORD`; **don't** touch `DATABASE_URL`)
3. Uncomment + edit the `app` service (loopback port, `restart: always`)
4. `touch anon.session bot.session`
5. `docker compose up -d db`
6. `docker compose run --rm app uv run python -m scripts.login` ← interactive, by hand
7. `docker compose up -d --build app` (migrations run automatically on start)
8. nginx site + basic auth → `certbot --nginx` for HTTPS
9. ufw: SSH + Nginx Full only

Browse the admin API at `https://rachel.yourdomain.com/docs` (basic-auth prompt).

## Updating a deployment

```bash
cd /opt/rachel
git pull
docker compose up -d --build app   # rebuilds image; CMD re-runs alembic on start
```

Check it came back up:

```bash
docker compose ps
docker compose logs -f app
```

## Operating the containers

```bash
docker compose logs -f app          # live app logs (replaces journalctl)
docker compose restart app          # restart just the app
docker compose down                 # stop everything (data survives in pgdata volume)
docker compose up -d                # bring db + app back up
```

> Shutdown is graceful: `app/main.py`'s lifespan flushes all in-memory message
> buffers to Postgres before disconnecting, so `docker compose down`/`restart`
> won't lose buffered conversation history.
