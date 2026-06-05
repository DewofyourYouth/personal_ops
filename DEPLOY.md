# Deploy

CI + deploy pipeline (`.github/workflows/deploy.yml`): on every push/PR the tests run;
on a push to `main` that passes, a GitHub Actions runner **SSHes into the Hetzner VPS** and
rebuilds the container there. No registry, no agent — just SSH.

```
git push main ──▶ Actions: pytest ──▶ (if green) ssh deploy@<vps>
                                          git reset --hard origin/main
                                          docker compose up -d --build
                                          health-check the container
```

There are **two keypairs** (both one-time):
1. **Actions → VPS** — lets the runner SSH in. Private half in a GitHub secret.
2. **VPS → GitHub** — a read-only deploy key so the box can `git pull` the (private) repo.

---

## 1. VPS setup (one-time)

As root on the fresh Hetzner box:

```bash
# Docker
curl -fsSL https://get.docker.com | sh

# A non-root deploy user that can run docker
adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
```

### VPS → GitHub read key (so the box can pull the private repo)
```bash
sudo -u deploy ssh-keygen -t ed25519 -f /home/deploy/.ssh/github -N "" -C "vps-pull"
sudo -u deploy cat /home/deploy/.ssh/github.pub
```
Add that public key to the repo: **GitHub → repo → Settings → Deploy keys → Add** (read-only).
Then tell SSH to use it for github.com and clone:
```bash
sudo -u deploy bash -c '
  printf "Host github.com\n  IdentityFile ~/.ssh/github\n  IdentitiesOnly yes\n" >> ~/.ssh/config
  sudo mkdir -p /opt/personal_ops && sudo chown deploy /opt/personal_ops
  git clone git@github.com:DewofyourYouth/personal_ops.git /opt/personal_ops
'
```

### Runtime secrets + context (these live ONLY on the box, never in the repo)
```bash
cd /opt/personal_ops
# .env  (OPS_BOT_TOKEN, OPS_CHAT_ID, ANTHROPIC_API_KEY, OPENAI_API_KEY, OPS_CONTEXT_DIR, ...)
# credentials.json, token.json  — Google Calendar OAuth
# clone the private context repo somewhere, e.g. /opt/personal-ops-context
```
Point the context volume at the absolute path on the box — in `docker-compose.yml` change
`./ops/context` to `/opt/personal-ops-context` (or set `OPS_CONTEXT_DIR` accordingly).

> ⚠️ **Single Telegram instance.** Before first start on the VPS, **stop the Mac bot.** Only
> one process may poll Telegram at a time, or both flap with a `Conflict` error.

First start:
```bash
cd /opt/personal_ops && docker compose up -d --build
```

## 2. Actions → VPS deploy key + GitHub secrets

On your laptop:
```bash
ssh-keygen -t ed25519 -f deploy_key -N "" -C "github-actions-deploy"
```
- Put the **public** half on the VPS: append `deploy_key.pub` to `/home/deploy/.ssh/authorized_keys`.
- Put the **private** half + host into repo secrets — **GitHub → repo → Settings → Secrets and variables → Actions**:
  - `VPS_SSH_KEY` — contents of `deploy_key` (the private key)
  - `VPS_HOST` — the VPS IP
  - `VPS_USER` — `deploy`
- Delete the local `deploy_key`/`deploy_key.pub` once they're placed.

That's it — the next push to `main` deploys.

## Deploying

Just `git push` to `main`. The Actions tab shows test + deploy; the deploy step fails loudly
(and the bot keeps running the old image) if tests fail or the new container doesn't come up.

## Rollback

```bash
ssh deploy@<vps>
cd /opt/personal_ops
git reset --hard <previous-good-sha>   # from `git log` or the GitHub history
docker compose up -d --build
```

## Notes

- `git reset --hard origin/main` only touches **tracked** files, so `.env`, `ops/log/` (DBs),
  and the mounted context are never disturbed by a deploy.
- The container build happens on the VPS (~1–2 min). If that ever gets heavy, the upgrade is to
  build in CI and push to GHCR, then `docker compose pull` on the box — same SSH step otherwise.
- Hardening (later, optional): restrict the `deploy` user to key-auth only; pin its
  `authorized_keys` entry to a forced command so the Actions key can *only* run the deploy.
