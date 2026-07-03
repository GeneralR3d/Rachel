# Deploying Rachel to a VPS (fully Dockerized)

This walks through deploying Rachel on a **fresh Ubuntu/Debian VPS** with nothing
installed. Unlike [`DEPLOY.md`](./DEPLOY.md) (app on the host via uv + systemd),
here **Postgres, Neo4j, and the app all run in docker-compose**. nginx stays on
the host as the reverse proxy terminating HTTPS in front of the admin API.

Rachel needs **two** datastores, both defined in `docker-compose.yml`:

- **Postgres** — chat history, summaries, user profiles, prompts, traits, schedule.
- **Neo4j** — the Graphiti world-view / per-user-facts knowledge graph.

> **Four things people trip on:**
> 1. **The Telegram login is interactive** — `scripts.login` prompts for a phone
>    number and the SMS code. It cannot run unattended. Here we run it once in a
>    **one-off container** (`docker compose run`) over SSH.
> 2. **`DATABASE_URL` and `NEO4J_URI` hosts differ by run mode** — inside
>    docker-compose the app reaches Postgres at **`db:5432`** and Neo4j at
>    **`neo4j:7687`**, *not* the `localhost` loopback ports. The compose file
>    injects the right values for the `app` service; don't override them in `.env`.
> 3. **Session files must exist on the host before the container starts** — they
>    are bind-mounted. Bind-mounting a path that doesn't exist makes Docker create
>    a *directory*, which then breaks the session. `touch` them first.
> 4. **Neo4j is slow to become healthy** — it can take 20-40 s on first boot. The
>    `app` service waits on `neo4j`'s healthcheck via `depends_on`, so the first
>    `up` may sit for a bit before the app container starts. That's expected.

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
- `OPENROUTER_API_KEY` — drives Rachel's router/summarizer/responder.
- `OPENROUTER_GRAPHITI_API_KEY` — separate key billed for all Graphiti/world-view
  LLM + embedder + reranker calls. **Leave blank to reuse `OPENROUTER_API_KEY`.**
- `ADMIN_ID` — your user id from [@userinfobot](https://t.me/userinfobot)
- `DB_PASSWORD` — **change this** from the `rachel` default for any internet-facing VPS.
- `NEO4J_PASSWORD` — **set this.** docker-compose reads it for `NEO4J_AUTH`
  (`neo4j/<password>`) and injects the same value into the app. It falls back to
  `password` if left unset — change it for any internet-facing VPS.

> **Leave `DATABASE_URL` and `NEO4J_URI` alone.** The `app` service in
> `docker-compose.yml` sets `DATABASE_URL=...@db:5432/rachel` and
> `NEO4J_URI=bolt://neo4j:7687` via Compose env injection, which overrides whatever
> is in `.env`. The `localhost:5433` / `localhost:7687` lines in `template.env` are
> only for the host/uv workflow in `DEPLOY.md`.

## 6. Review the `app` service in docker-compose

`docker-compose.yml` ships with the `app` service **already enabled** and wired up
for a container deploy — no uncommenting needed. Confirm the block looks like this:

```yaml
  app:
    build: .
    image: rachel-app:latest
    container_name: rachel
    depends_on:
      db:
        condition: service_healthy
      neo4j:
        condition: service_healthy
    env_file: .env
    environment:
      DATABASE_URL: postgresql+asyncpg://rachel:${DB_PASSWORD:-rachel}@db:5432/rachel
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_PASSWORD: ${NEO4J_PASSWORD:-password}
      PYTHONUNBUFFERED: 1
    restart: always
    ports:
      # Bind to loopback only — nginx is the only thing that should reach uvicorn.
      - "127.0.0.1:8000:8000"
    volumes:
      - ./anon.session:/app/anon.session
      - ./bot.session:/app/bot.session
```

> Things already set for you (and why they matter):
> - **`ports: 127.0.0.1:8000:8000`** (not `8000:8000`) so uvicorn is never exposed
>   to the internet — the admin API has no auth of its own.
> - **`restart: always`** so the app comes back after crashes/reboots (this is what
>   replaces systemd in the host-based guide).
> - **`depends_on` both `db` and `neo4j` on `service_healthy`** so the app doesn't
>   start until both datastores accept connections.
> - **`DATABASE_URL` + `NEO4J_URI` injected here** point at the compose service
>   hosts, overriding the loopback URLs in `.env`.

The app `CMD` already runs `alembic upgrade head` before starting uvicorn, so there
is **no separate migration step** — migrations apply automatically on every
container start. (Neo4j has no migrations; Graphiti builds its schema/indices on
first connect.)

## 7. Create the session files on the host

Bind mounts need these to exist as files first:

```bash
touch anon.session bot.session
```

## 8. Start Postgres and Neo4j

```bash
docker compose up -d db neo4j
docker compose ps                  # confirm BOTH db and neo4j are healthy
```

Neo4j may report `starting` for 20-40 s before it flips to `healthy` — wait for it
before moving on, since the login container (next) depends on it too.

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
docker compose ps                  # app + db + neo4j all up
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

### (Optional) Seed the world-view graph

Rachel builds her world-view automatically from conversations, but you can
bulk-load facts up front once Neo4j is up:

```bash
docker compose run --rm app uv run python -m scripts.ingest_worldview_md facts.md
# or a single fact:
docker compose run --rm app uv run python -m scripts.add_worldview_fact "Chagee is a bubble tea brand"
```

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

Ports **8000** (uvicorn), **5433** (Postgres), and **7687/7474** (Neo4j bolt +
browser) stay closed to the internet — all are published only on `127.0.0.1`, and
nginx is the only public entry point.

---

## Order that matters (recap)

1. Docker, nginx installed
2. `.env` filled (set `DB_PASSWORD` **and** `NEO4J_PASSWORD`; **don't** touch
   `DATABASE_URL` / `NEO4J_URI`)
3. Confirm the `app` service is enabled (loopback port, `restart: always`,
   `depends_on` db + neo4j)
4. `touch anon.session bot.session`
5. `docker compose up -d db neo4j` ← wait for **both** healthy
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
docker compose logs -f neo4j        # Neo4j logs (Graphiti ingest/search errors surface here)
docker compose restart app          # restart just the app
docker compose down                 # stop everything (data survives in named volumes)
docker compose up -d                # bring db + neo4j + app back up
```

> Shutdown is graceful: `app/main.py`'s lifespan flushes all in-memory message
> buffers to Postgres before disconnecting, so `docker compose down`/`restart`
> won't lose buffered conversation history. Postgres data lives in the `pgdata`
> volume and the Neo4j graph in the `neo4jdata` volume — both survive `down`/`up`.
