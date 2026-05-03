# Khyra AI — DigitalOcean Droplet Deployment Guide

## Infrastructure Overview

| Component | Details |
|-----------|---------|
| Provider | DigitalOcean |
| Droplet IP | `168.144.91.95` |
| Region | Bangalore (`blr1`) |
| OS | Ubuntu 24.04 LTS |
| Domain | `api.khyraai.com` |
| App Port | `8000` |
| DB Port | `5432` |
| Adminer Port | `8080` |

---

## 1. SSH Into Droplet

```bash
ssh -i C:\Users\Lenovo\ssh_digital_ocean root@168.144.91.95
```

---

## 2. Install Docker & Docker Compose

```bash
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version
```

---

## 3. Clone Repository

```bash
mkdir -p /opt/khyra
cd /opt
git clone https://github.com/khyraai/Khyra_AI.git khyra
cd khyra
```

---

## 4. Configure .env

```bash
nano /opt/khyra/.env
```

Copy contents from local `.env` file. Then ensure these two values are set correctly for Docker:

```bash
# Must use service name 'postgres', not 'localhost'
DATABASE_URL=postgresql://khyra:khyra_secret@postgres:5432/khyra_db

# Must use HTTPS domain (not ngrok)
SERVER_BASE_URL=https://api.khyraai.com
```

To update after copying:
```bash
sed -i 's|SERVER_BASE_URL=.*|SERVER_BASE_URL=https://api.khyraai.com|' .env
sed -i 's|DATABASE_URL=.*|DATABASE_URL=postgresql://khyra:khyra_secret@postgres:5432/khyra_db|' .env
```

---

## 5. Open Firewall Ports

```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8000/tcp
ufw allow 8080/tcp
ufw --force enable
ufw status
```

---

## 6. Build and Start Services

```bash
cd /opt/khyra
docker compose up -d --build
```

Verify all containers are healthy:
```bash
docker compose ps
curl https://api.khyraai.com/health
```

Expected response: `{"message":"Vobiz Voice Assistant Running 🚀"}`

---

## 7. Nginx + SSL Setup

### Install
```bash
apt install -y nginx certbot python3-certbot-nginx
```

### Get SSL Certificate
```bash
certbot --nginx -d api.khyraai.com
```

### Configure Nginx
```bash
nano /etc/nginx/sites-enabled/default
```

Paste:
```nginx
server {
    listen 80;
    server_name api.khyraai.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name api.khyraai.com;

    ssl_certificate /etc/letsencrypt/live/api.khyraai.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.khyraai.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
    }
}
```

```bash
nginx -t
systemctl reload nginx
```

---

## 8. Vobiz Dashboard

Set Answer URL to:
```
https://api.khyraai.com/answer
```

---

## 9. Database UI (Adminer)

Access at: `http://168.144.91.95:8080`

| Field | Value |
|-------|-------|
| System | PostgreSQL |
| Server | `postgres` |
| Username | `khyra` |
| Password | `khyra_secret` |
| Database | `khyra_db` |

Adminer is defined in `docker-compose.yml` and starts automatically with `docker compose up`.

---

## 10. Useful Commands

```bash
# View live app logs
docker compose logs -f app

# Restart app (after .env change)
docker compose restart app

# Update after code push
cd /opt/khyra
git pull origin main
docker compose up -d --build app

# Stop all services
docker compose down

# Check container status
docker compose ps
```

---

## 11. SSL Certificate Renewal

Certbot auto-renews via a scheduled task. To manually renew:
```bash
certbot renew --dry-run
```

Certificate expiry: **2026-07-27** (auto-renews before expiry)
