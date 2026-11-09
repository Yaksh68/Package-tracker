# Deploying Packages Tracker on Oracle Cloud (Always Free)

This walks you from an empty Oracle account to Packages Tracker running at
`https://<your-domain>` with GitHub sign-in and nightly backups. Budget about an
hour, most of it waiting for the VM and the first image build.

What you'll have at the end:

```
Internet ──443──▶ Caddy (HTTPS, certificates, security headers)
                    │
                    ▼
                  app (API + UI, one container) ──▶ PostgreSQL (internal only)
```

All three run with Docker Compose on one Ampere A1 VM in Mumbai
(`ap-mumbai-1`). Everything here stays within Oracle's Always Free limits.

**You need:** your Oracle account (with the $1 budget alert), your GitHub
account, and a terminal with `ssh` on your computer.

---

## 1. Keep the VM from being reclaimed (recommended)

Oracle stops Always Free VMs it considers idle, judged over a 7-day window by
CPU, network and (on A1) memory use. A tracker that mostly waits between
scans can look idle. Converting the account to **Pay As You Go** stops that.
You're still not charged while you stay within the Always Free limits, and
your budget alert warns you if anything starts costing money.

*Billing & Cost Management → Upgrade and Manage Payment → Pay As You Go.*

If you'd rather not, the site still works. Just keep an eye out for Oracle's
"idle instance" email, which comes a week before it stops the VM.

## 2. Create the VM

*Compute → Instances → Create instance*:

- **Name:** `packages-tracker`.
- **Image:** *Change image → Ubuntu → Canonical Ubuntu 24.04* (not the "Minimal" one).
- **Shape:** *Change shape → Ampere → VM.Standard.A1.Flex*, **4 OCPUs, 24 GB**
  (the whole Always Free A1 allowance; 2 and 12 GB also work).
- **Networking:** create a new virtual cloud network with a **public subnet**,
  and keep *Assign a public IPv4 address* on.
- **SSH keys:** *Generate a key pair for me* and download the private key, or
  paste your own public key (`~/.ssh/id_ed25519.pub`).
- **Boot volume:** the default 50 GB is plenty (Always Free covers 200 GB in total).

*Create.* If Oracle says **"Out of capacity"**, A1 is busy in that
availability domain: try another one, or try again later.

## 3. Make its public IP permanent

The IP the VM gets is *ephemeral*: it survives restarts, but not a rebuilt VM,
and your domain will point at it. Reserve one instead:

1. *Networking → IP management → Reserved public IPs → Reserve public IP address.*
2. On the instance: *Attached VNICs → (the VNIC) → IPv4 addresses → ⋮ → Edit*.
   Choose *No public IP* and save, then edit again and choose *Reserved public IP*
   with the one you just reserved.

Write the IP down. It's `<IP>` below.

## 4. Open ports 80 and 443 in Oracle's network

*Networking → Virtual cloud networks → (your VCN) → Security lists → Default
security list → Add ingress rules*. Add three rules, each with source CIDR
`0.0.0.0/0`:

| Protocol | Destination port | For |
|---|---|---|
| TCP | 80 | HTTP (Caddy redirects it to HTTPS, and Let's Encrypt checks it) |
| TCP | 443 | HTTPS |
| UDP | 443 | HTTP/3 (optional) |

## 5. Log in and update

```bash
ssh -i <path-to-private-key> ubuntu@<IP>
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

Log in again after a minute.

## 6. Open ports 80 and 443 in the VM's own firewall

Oracle's Ubuntu images also block everything but SSH *inside* the VM
(iptables). New rules must go **above** the image's `REJECT` rule, so look at the
list first:

```bash
sudo iptables -L INPUT -n --line-numbers
```

Find the line with `REJECT ... reject-with icmp-host-prohibited` (often line 6)
and insert above it, using that line number:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p udp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Do this **before** installing Docker, so the saved rules don't include Docker's.

## 7. Install Docker

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker ubuntu
exit
```

Log in again (so the group applies), then check: `docker run --rm hello-world`.

## 8. Point a domain at the VM

The easiest free option is **DuckDNS**:

1. Sign in at [duckdns.org](https://www.duckdns.org) (GitHub sign-in works).
2. Add a subdomain, e.g. `packages-tracker`, and set its IP to `<IP>`.
   The IP is reserved, so it never needs updating.

Your domain is then `packages-tracker.duckdns.org`. (With a domain of your own,
add an `A` record for it pointing at `<IP>` instead.) Check from the VM:

```bash
getent hosts <your-domain>     # should print <IP>
```

## 9. Create the GitHub sign-in app

*GitHub → Settings → Developer settings → OAuth Apps → New OAuth App*:

- **Homepage URL:** `https://<your-domain>`
- **Authorization callback URL:** `https://<your-domain>/api/auth/github/callback`

*Register application*, then *Generate a new client secret*. Keep the
**Client ID** and the **client secret** for step 11. (This is separate from any
app you made for local testing: the callback URL differs.)

## 10. Get the code onto the VM

The repo is private, so the VM gets a read-only **deploy key**:

```bash
ssh-keygen -t ed25519 -C "packages-tracker VM" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

Copy the printed key to *GitHub → the repo → Settings → Deploy keys → Add deploy
key* (leave *Allow write access* off). Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
git clone git@github.com:Yaksh68/Package-tracker.git ~/Package-tracker
```

## 11. Fill in the settings

```bash
cd ~/Package-tracker
cp deploy/.env.cloud.example deploy/.env.cloud
chmod 600 deploy/.env.cloud
python3 -c "import secrets; print('APP_SECRET_KEY=' + secrets.token_urlsafe(48))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_hex(24))"
nano deploy/.env.cloud
```

Fill in `DOMAIN`, `ACME_EMAIL`, the two generated secrets, the GitHub
**Client ID** and **secret**, `PLATFORM_ADMINS=Yaksh68`, and `OPERATOR_NAME` and
`CONTACT_EMAIL` (shown on the privacy and terms pages).

**Save `APP_SECRET_KEY` and `POSTGRES_PASSWORD` in your password manager now.**
`APP_SECRET_KEY` encrypts the API keys users save. Without it, a backup can be
restored but those keys can't be read.

## 12. Start it

```bash
docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud up -d --build
```

The first build takes several minutes. Then:

```bash
docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud ps     # all "running", app "healthy"
docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud logs caddy | grep -i certificate
```

Open `https://<your-domain>`. You should see the sign-in page with a valid
certificate.

## 13. First sign-in

1. *Sign in with GitHub*. As a platform admin, you see **Admin** in the menu.
2. Add a public repository (e.g. `https://github.com/psf/requests`) and wait for
   its scan. It shows up in your personal workspace.
3. Look over *Admin → Limits*. The defaults are in the README.
4. Signed-out visitors can try one free scan from the sign-in page. Its limits
   are in *Admin → Limits* (setting free scans per day to 0 turns them off).
   Optionally, give free scans' GitHub lookups a token: `TRIAL_GITHUB_TOKEN` in
   `deploy/.env.cloud`, a token with no permissions (see the example file).
5. *Admin → Analytics* shows how the service is doing: sign-ups, activity,
   scans, AI use and the server's health. The health charts fill in from the
   first hourly reading.

That's the site live. The rest keeps it healthy.

## 14. Nightly backups, copied off the VM

`deploy/backup.sh` dumps the database and the uploaded zips, keeps 14 days on
the VM, and copies them to **Object Storage** (20 GB is Always Free) with rclone.

1. *Storage → Buckets → Create bucket*: name `packages-tracker-backups`, *Standard*,
   private.
2. Your **namespace**: *Profile → Tenancy → Object storage namespace*.
3. An S3-style key: *Profile → User settings → Customer secret keys → Generate
   secret key*. Copy the secret (shown once) and the **access key**.
4. On the VM:

   ```bash
   sudo apt install -y rclone
   rclone config create oci s3 provider=Other \
     access_key_id=<access key> secret_access_key=<secret> \
     endpoint=https://<namespace>.compat.objectstorage.ap-mumbai-1.oraclecloud.com \
     region=ap-mumbai-1 no_check_bucket=true
   rclone lsd oci:                                  # lists packages-tracker-backups
   mkdir -p ~/backups
   BACKUP_REMOTE=oci:packages-tracker-backups ~/Package-tracker/deploy/backup.sh
   ```

5. Schedule it every night (`crontab -e`, then add):

   ```
   15 3 * * *  BACKUP_REMOTE=oci:packages-tracker-backups /home/ubuntu/Package-tracker/deploy/backup.sh >> /home/ubuntu/backups/backup.log 2>&1
   ```

### Test a restore (now, then every few months)

A backup you haven't restored is a hope, not a backup. This restores the newest
one into a throwaway database and counts what's in it, without touching the
live site:

```bash
docker run -d --name restore-test -e POSTGRES_PASSWORD=test postgres:16-alpine
sleep 10
gunzip -c "$(ls -t ~/backups/db-*.sql.gz | head -1)" | docker exec -i restore-test psql -q -U postgres
docker exec restore-test psql -U postgres -c "select count(*) as users from users" -c "select count(*) as repos from repos"
docker rm -f restore-test
```

To restore the live site from a backup (after losing the database):

```bash
cd ~/Package-tracker
docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud stop app
gunzip -c ~/backups/db-<time>.sql.gz | docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud exec -T postgres psql -q -U packages -d packages_tracker
docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud start app
```

On a new VM, also set the same `APP_SECRET_KEY`, and unpack the uploads:
`docker compose ... exec -T app tar -C /data -xzf - < ~/backups/uploads-<time>.tar.gz`.

## 15. Updating

Merge to `main` and wait for CI to pass, then on the VM:

```bash
~/Package-tracker/deploy/update.sh
```

It pulls, rebuilds, restarts what changed, and cleans up the old image.
Migrations run by themselves when the app starts.

## 16. Automatic OS updates and an uptime check

Security updates install by themselves on Ubuntu (`unattended-upgrades`). Let
them reboot the VM at night when one needs it:

```bash
echo 'Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";' | sudo tee /etc/apt/apt.conf.d/51auto-reboot
```

The containers come back after a reboot by themselves (`restart: unless-stopped`).

For an uptime check, a free [UptimeRobot](https://uptimerobot.com) monitor works:
an *HTTP(s)* monitor on `https://<your-domain>/api/health` every 5 minutes, with
email alerts.

---

## Troubleshooting

| Symptom | Look at |
|---|---|
| The site doesn't load at all | Steps 4 and 6 (both firewalls). From your computer: `curl -I http://<IP>` should answer. Then `docker compose ... ps`. |
| No certificate / browser warning | `docker compose ... logs caddy`. Usually the domain doesn't point at `<IP>` yet, or port 80 is closed. Let's Encrypt limits retries, so after fixing it give it a few minutes. |
| GitHub says "redirect_uri is not associated" | The OAuth app's callback URL (step 9) must be exactly `https://<DOMAIN>/api/auth/github/callback`. |
| Sign-in loops back to the sign-in page | `DOMAIN` in `deploy/.env.cloud` must match the address you open. |
| The app container isn't healthy | `docker compose ... logs app`. A missing `APP_SECRET_KEY` stops it at startup on purpose. |
| "Out of capacity" when creating the VM | Another availability domain, or try later. |

`docker compose ...` is short for
`docker compose -f deploy/compose.cloud.yml --env-file deploy/.env.cloud`, run from
`~/Package-tracker`.

## Changing secrets

- **`APP_SECRET_KEY`:** put the current key in `APP_SECRET_KEY_PREVIOUS`, a new
  one in `APP_SECRET_KEY`, and restart (`update.sh`). Keys and tokens saved
  earlier stay readable through the previous key, so keep it set.
- **GitHub client secret:** generate a new one in the OAuth app, update
  `deploy/.env.cloud`, restart.
- **`POSTGRES_PASSWORD`:** it's set when the database is first created; changing
  it later needs `ALTER USER packages PASSWORD '...'` inside the database as well.
