# Deploying to AWS on self-hosted Kubernetes (k3s)

A single EC2 instance running [k3s](https://k3s.io) — real Kubernetes, control plane
included, operated by you. Not EKS: EKS is *managed*, so it would not be
self-hosted, and its control plane alone costs more per month than this entire
deployment.

Everything runs on one node: Postgres, Redis, the API, the ingestion worker, the
frontend, and Ollama.

---

## What this costs

| Item | Monthly |
|---|---|
| `t3.medium` EC2 (2 vCPU, 4 GB), on-demand | ~$30 |
| 30 GB gp3 EBS volume | ~$2.50 |
| Elastic IP (while attached to a running instance) | $0 |
| Data transfer out (100 GB/mo included) | ~$0 |
| DNS via `sslip.io` | $0 |
| **Total** | **~$33** |

**Stopped instances cost nothing for compute.** If the demo only needs to be live
for interviews, stop it in between and you pay only the ~$2.50 for the disk.

### Cheaper: ARM

`t4g.medium` is the same 2 vCPU / 4 GB on ARM Graviton for **~$24/mo**. It works,
and Ollama supports ARM natively. The cost is that container images must be built
for `linux/arm64` — cheap now that GitHub provides free arm64 runners for public
repositories, but it is one more thing to go wrong on a first deploy. Choose it by
picking the `t4g.medium` instance type and the **arm64** Ubuntu AMI in Step 4;
everything else in this runbook is identical.

---

## Step 0 — Set a billing alarm first

AWS has no spending cap by default. Do this before launching anything.

**Billing and Cost Management → Budgets → Create budget**

- Type: *Cost budget*, monthly, amount **$50**
- Alert thresholds at **50%** and **90%**, to your email

This is how you learn about a mistake in hours rather than at the end of the month.

---

## Step 1 — Pick a region

Choose the region closest to whoever will be viewing the demo. `us-east-1` is the
cheapest and the default in this guide. **Stay in one region throughout** — key
pairs, security groups, and Elastic IPs are all regional and will not appear
elsewhere.

---

## Step 2 — Import your SSH key

You already have a keypair at `~/.ssh/hetzner`. Import the public half rather than
letting AWS generate a new one.

**EC2 → Key Pairs → Actions → Import key pair**

- Name: `llm-demo`
- Paste the contents of `~/.ssh/hetzner.pub`:

```bash
cat ~/.ssh/hetzner.pub
```

---

## Step 3 — Create a security group

This is your real firewall — it sits outside the instance, so a misconfiguration
inside cannot expose anything.

**EC2 → Security Groups → Create security group**

- Name: `llm-demo-sg`
- Inbound rules:

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | **My IP** | Administration. Not `0.0.0.0/0` |
| HTTP | 80 | `0.0.0.0/0` | The site, and Let's Encrypt's HTTP-01 challenge |
| HTTPS | 443 | `0.0.0.0/0` | The site |

- Outbound: leave the default (allow all) — the app calls Groq and Gemini, and
  the node pulls container images.

**Deliberately absent: 5432 (Postgres), 6379 (Redis), 6443 (Kubernetes API).**
Nothing outside the node needs them. An unauthenticated Redis on a public IP is
one of the most reliably exploited things on the internet, and the safest way to
protect it is for the port never to be reachable. You will run `kubectl` on the
instance over SSH, which is also why the Kubernetes API stays closed.

---

## Step 4 — Launch the instance

**EC2 → Instances → Launch an instance**

| Field | Value |
|---|---|
| Name | `llm-inference-logger` |
| AMI | **Ubuntu Server 24.04 LTS**, architecture **64-bit (x86)** |
| Instance type | **t3.medium** |
| Key pair | `llm-demo` |
| Network settings | *Select existing security group* → `llm-demo-sg` |
| Storage | **30 GiB, gp3** (the free-tier allowance, and enough for images + the Ollama model) |

Launch it.

> For ARM: instance type `t4g.medium`, and switch the AMI architecture to
> **64-bit (Arm)**. The AMI must match the instance family or it will not boot.

---

## Step 5 — Allocate an Elastic IP

Without one, the public IP changes every time you stop and start the instance —
which breaks DNS and invalidates your TLS certificate.

**EC2 → Elastic IPs → Allocate Elastic IP address** → then **Actions → Associate**
→ select your instance.

Note the address. It is referred to below as `YOUR_IP`.

> An Elastic IP is free while attached to a *running* instance, and billed at
> roughly $3.60/mo while unattached or while the instance is stopped. If you stop
> the instance for long periods, release the IP and take a new one later.

---

## Step 6 — Get a hostname

TLS needs a hostname; a bare IP cannot have a public certificate. Three options:

**a. `sslip.io` — free, instant, recommended.** It resolves any IP embedded in the
hostname, and Let's Encrypt issues certificates for it. Replace dots with dashes:

```
IP 3.15.22.99   →   3-15-22-99.sslip.io
```

Nothing to configure. Verify:

```bash
dig +short 3-15-22-99.sslip.io      # should print your IP
```

Both the dash and dot forms work (`3.15.22.99.sslip.io` resolves identically), and
`nip.io` is an equivalent fallback if `sslip.io` is ever down. All three were
checked against a live resolver while writing this.

**b. A domain you own** — add an `A` record pointing at `YOUR_IP`. If the DNS is
behind Cloudflare, set the record to **DNS only (grey cloud)**: the proxy buffers
responses, which would break the token-by-token streaming this demo exists to show.

**c. Route 53** — only worth it if you want AWS to hold the domain. A hosted zone
is $0.50/mo plus ~$12/yr registration.

**The EC2 public DNS name will not work.** Let's Encrypt refuses to issue for
`*.amazonaws.com`, so `ec2-3-15-22-99.compute-1.amazonaws.com` cannot get a
certificate.

---

## Step 7 — Connect

Your key is not at a default filename, so it must be named explicitly:

```bash
ssh -i ~/.ssh/hetzner ubuntu@YOUR_IP
```

Note the user is **`ubuntu`**, not `root`, on Ubuntu AMIs.

To make this `ssh llm-demo` instead, add to `~/.ssh/config` on your Mac:

```
Host llm-demo
  HostName YOUR_IP
  User ubuntu
  IdentityFile ~/.ssh/hetzner
  IdentitiesOnly yes
```

---

## Step 8 — Prepare the server

```bash
sudo apt update && sudo apt upgrade -y

# Swap. The box has 4 GB; loading a model briefly spikes memory, and swap turns a
# potential OOM-kill into a slow moment. Skip this and Ollama will eventually get
# killed mid-generation.
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h                      # confirm 4 GB of swap
```

The AWS security group is already the firewall, so `ufw` is optional. If you want
defence in depth, note that k3s needs its internal traffic allowed:

```bash
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw allow in on cni0                 # k3s pod network — omit this and DNS inside the cluster breaks
sudo ufw --force enable
```

---

## Step 9 — Install k3s

```bash
curl -sfL https://get.k3s.io | sh -
```

Make `kubectl` usable without `sudo`:

```bash
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown ubuntu:ubuntu ~/.kube/config
chmod 600 ~/.kube/config
echo 'export KUBECONFIG=~/.kube/config' >> ~/.bashrc
export KUBECONFIG=~/.kube/config
```

### Verify before going further

```bash
kubectl get nodes                 # Ready
kubectl get pods -A               # coredns, traefik, metrics-server, local-path-provisioner
kubectl top nodes                 # must return numbers, not an error
kubectl get storageclass          # local-path (default)
```

Those last two matter for this project specifically:

- **`kubectl top nodes` working** means metrics-server is present, so the
  HorizontalPodAutoscalers will actually scale. On kind they report
  `cpu: <unknown>` because kind ships no metrics-server — k3s bundles one, so
  autoscaling becomes real here rather than merely correct on paper.
- **`local-path` StorageClass** is what backs the Postgres, Redis, and Ollama
  volumes. No EBS CSI driver setup required.

k3s also bundles **Traefik**, already wired to ports 80 and 443, which is why this
guide does not install ingress-nginx.

---

## Step 10 — Push the repository

The container images build from GitHub, so this has to exist first. On your Mac:

```bash
cd /Users/manush/Desktop/llm-inference-logger

# Confirm secrets are not about to become public. Both checks must pass.
git check-ignore -v .env
git log --all -p | grep -cE "gsk_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,}"   # must be 0

git remote add origin https://github.com/YOUR_USERNAME/llm-inference-logger.git
git branch -M main
git push -u origin main
```

Make the repository **public** so GitHub's free arm64 runners and unauthenticated
image pulls work.

---

## Step 11 — Have these ready

For the next stage:

- `GROQ_API_KEY`, `GEMINI_API_KEY`
- A Postgres password and a Redis password — `openssl rand -base64 24` for each
- A username and password for the site's basic auth, to share with reviewers
- Your hostname from Step 6, and an email address for Let's Encrypt

---

## Step 12 — Publish the images

The cluster pulls from GHCR, so the images must exist before you deploy. Pushing to
`main` triggers `.github/workflows/images.yml`, which builds both and publishes
them.

Watch it under the repository's **Actions** tab. Then make the two packages
**public** (repository → Packages → each package → Package settings → Change
visibility), otherwise the cluster gets a 401 pulling them and the pods sit in
`ImagePullBackOff`.

Confirm they are pullable without credentials:

```bash
sudo k3s ctr images pull ghcr.io/manush2312/llm-inference-logger-backend:main
sudo k3s ctr images pull ghcr.io/manush2312/llm-inference-logger-frontend:main
```

## Step 13 — Install cert-manager

This issues and renews the TLS certificate.

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=180s
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=180s
```

Wait for the **webhook** specifically. Applying a `ClusterIssuer` before it is
serving fails with a connection-refused error that reads like a broken manifest.

## Step 14 — Create the secrets

These are created once, by hand, and no `apply` ever overwrites them — the overlay
deliberately removes the base's placeholder Secret for exactly this reason.

```bash
kubectl create namespace llm-logger --dry-run=client -o yaml | kubectl apply -f -

kubectl -n llm-logger create secret generic app-secrets \
  --from-literal=POSTGRES_USER=llm \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=REDIS_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=GROQ_API_KEY="your-groq-key" \
  --from-literal=GEMINI_API_KEY="your-gemini-key" \
  --from-literal=ANTHROPIC_API_KEY="" \
  --from-literal=OPENAI_API_KEY=""
```

The generated passwords are never printed. Nothing needs to read them back — the
pods get them from the Secret — but if you ever want them:

```bash
kubectl -n llm-logger get secret app-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d
```

> Changing `POSTGRES_PASSWORD` after Postgres has initialised does **not** change
> the database's password — it is set only on first boot, from an empty data
> directory. Rotating it means either an `ALTER USER` inside Postgres or deleting
> the PVC and starting over.

## Step 15 — Configure the hostname and deploy

```bash
cd ~/llm-inference-logger
./infra/k8s-aws/configure.sh YOUR_HOSTNAME your@email.com
kubectl apply -k infra/k8s-aws
```

Then watch it come up:

```bash
kubectl -n llm-logger get pods -w
```

Expect this order: `migrate` runs to `Completed`, then `postgres` and `redis` go
`Running`, then `backend`, `worker` and `frontend`. `ollama` starts quickly but
`ollama-pull` takes several minutes downloading the model — the site works before
it finishes, just without the Ollama provider.

```bash
kubectl -n llm-logger logs job/ollama-pull -f      # download progress
kubectl get certificate -n llm-logger              # READY should become True
```

The certificate usually takes 1–3 minutes. If it stays `False`:

```bash
kubectl -n llm-logger describe certificate llm-logger-tls
kubectl -n llm-logger get challenge
```

The usual causes are DNS not yet resolving to this host, or port 80 unreachable —
Let's Encrypt validates over HTTP before it will issue.

> If issuance fails repeatedly, switch the Ingress annotation to
> `letsencrypt-staging`, get it working there, then switch back. Production
> Let's Encrypt allows only 5 failures per hostname per week, and once that is
> exhausted there is nothing to do but wait for the window to roll.

## Verification checklist

Once deployed, all of these should hold:

- [ ] `https://YOUR_HOST` loads with a valid certificate
- [ ] A reply streams token by token rather than arriving in one block
- [ ] Stop mid-stream keeps the partial text and records the call as `cancelled`
- [ ] The dashboard shows the call a few seconds later — the lag is by design
- [ ] `kubectl get pods -n llm-logger` shows no restarts
- [ ] `kubectl get hpa -n llm-logger` shows real CPU percentages, not `<unknown>`
- [ ] From your Mac: `nmap -Pn -p 22,80,443,5432,6379,6443 YOUR_IP` shows only
      22, 80, and 443 open
