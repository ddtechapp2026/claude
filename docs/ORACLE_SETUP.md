# Oracle Cloud VM setup — end to end

This walks you from nothing to a running bot + a dashboard you can open from
any other computer on any network. Follow it top to bottom the first time.

There are two firewalls between your laptop and the dashboard, and **both** must
allow the traffic. This trips up almost everyone on Oracle Cloud:

1. **Oracle Cloud's virtual firewall** — the VCN *Security List* (or *Network
   Security Group*). Configured in the web console. (Step 5)
2. **The VM's own firewall** — `iptables` inside Ubuntu. Oracle's Ubuntu image
   ships locked down. (Handled by `setup_vm.sh`, explained in step 6)

---

## 1. Create the VM

1. Sign in to the [Oracle Cloud console](https://cloud.oracle.com/).
2. **Menu → Compute → Instances → Create instance.**
3. Name it e.g. `stonks`.
4. **Image and shape:**
   - Image: **Canonical Ubuntu 22.04** (or 24.04).
   - Shape: an **Always Free** eligible shape is fine —
     `VM.Standard.A1.Flex` (Ampere ARM) with 1 OCPU / 6 GB is comfortable and
     free. The `VM.Standard.E2.1.Micro` (1 GB RAM) also works but is tight.
5. **Add SSH keys:** choose *Generate a key pair for me* and **download both
   keys**, or paste your own public key. You need this to log in.
6. **Networking:** let it create a new VCN + subnet, and make sure
   *Assign a public IPv4 address* is enabled.
7. Click **Create**. When it finishes, note the **Public IP address**.

## 2. Connect over SSH

From your laptop (macOS/Linux; on Windows use PowerShell or WSL):

```bash
chmod 600 /path/to/your-private-key.key          # first time only
ssh -i /path/to/your-private-key.key ubuntu@YOUR_VM_PUBLIC_IP
```

The default username on Oracle's Ubuntu images is `ubuntu`.

## 3. Get the code onto the VM

This repo is **public**, so the VM can pull it with no GitHub login. Pick either
option.

### Option A — git clone (simplest)

```bash
sudo apt-get update -y && sudo apt-get install -y git
git clone https://github.com/ddtechapp2026/claude.git Stonks
cd Stonks
```

### Option B — self-contained installer (one file, no git)

`install_stonks.sh` contains the entire project in one file. Pull just that file
and run it — it recreates the whole project in `~/Stonks`:

```bash
curl -fsSL https://raw.githubusercontent.com/ddtechapp2026/claude/main/install_stonks.sh -o install_stonks.sh
bash install_stonks.sh
cd ~/Stonks
```

No outbound internet on the VM at all? Copy the file up from your **local
computer** with `scp` instead, then run it:

```bash
scp -i /path/to/your-private-key.key install_stonks.sh ubuntu@YOUR_VM_PUBLIC_IP:~/
# then on the VM:
bash install_stonks.sh && cd ~/Stonks
```

No `scp`? You can instead paste it: on the VM run `cat > install_stonks.sh`,
paste the file's contents, press `Enter` then `Ctrl+D`, and run
`bash install_stonks.sh`.

## 4. Get your API keys

The bots trade **simulated wallets** priced off Alpaca crypto data, so the only
key you really need is for the **AI** features:

1. **OpenRouter (for the AI):** sign up at <https://openrouter.ai/>, then
   **Keys → Create Key**. This powers plain-English strategies and the learning
   loop. Without it, a built-in rule-based parser is used instead.
2. **Alpaca (optional):** the crypto price data works with no key. Adding free
   **paper** keys from <https://app.alpaca.markets/> just raises your rate
   limits.

Now create your `.env` on the VM:

```bash
cp .env.example .env
nano .env      # paste OPENROUTER_API_KEY (and Alpaca keys if you have them)
```

Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

> Note: nothing here ever places a real order — every bot uses a simulated
> wallet. It is safe to run before you've tuned anything; bots start **OFF** and
> you turn them on individually from the dashboard.

## 5. Open the port in Oracle Cloud (the virtual firewall)

This is the step people miss. In the Oracle Cloud console:

1. **Compute → Instances →** your instance → click the **Virtual Cloud
   Network** link (under "Primary VNIC").
2. Open the **Subnet** your instance uses, then its **Security List**
   (usually "Default Security List for ...").
3. **Add Ingress Rules → Add Ingress Rule:**
   - **Source Type:** CIDR
   - **Source CIDR:** `0.0.0.0/0` (any network — see the security note below)
   - **IP Protocol:** TCP
   - **Destination Port Range:** `80`
   - Save.

> **Security note:** `0.0.0.0/0` means "reachable from the whole internet",
> protected only by the nginx password. Because you asked for open-port access
> from another network, that is expected here — but tighten it when you can:
> if your remote computer has a stable IP, put *that* IP as the source CIDR
> instead (e.g. `203.0.113.5/32`). And add HTTPS as soon as practical (step 8).

## 6. Run the setup script

Back in your SSH session, from `~/Stonks`:

```bash
chmod +x deploy/setup_vm.sh
./deploy/setup_vm.sh
```

It will:
- install Python, nginx, and tooling,
- create the `.venv` virtualenv and install dependencies,
- install and start the `stonks-bot` and `stonks-dashboard` systemd services,
- configure the nginx reverse proxy,
- **prompt you to create the dashboard username + password**,
- open port 80 in the VM's `iptables` firewall and persist it.

## 7. Open the dashboard from your remote computer

From any computer on any network, browse to:

```
http://YOUR_VM_PUBLIC_IP/
```

Enter the username/password you created in step 6. You should see the dashboard
with your paper account equity. Done.

### If it doesn't load, check in this order
- **Services up?** `sudo systemctl status stonks-dashboard stonks-bot`
- **Bot logs:** `journalctl -u stonks-bot -f` (watch a cycle happen)
- **nginx up?** `sudo systemctl status nginx` and `sudo nginx -t`
- **VM firewall?** `sudo iptables -L INPUT -n --line-numbers | grep 80`
- **Oracle firewall?** re-check the ingress rule in step 5 — this is the most
  common culprit.
- **Local test on the VM:** `curl -s localhost:8000/healthz` should return
  `{"ok":true}`.

## 8. (Recommended) Add HTTPS

Plain HTTP sends your dashboard password in a form anyone on the network path
can read. Two easy fixes:

- **Own a domain?** Point it at the VM's IP, put the domain in
  `deploy/nginx-stonks.conf` (`server_name`), then:
  ```bash
  sudo apt-get install -y certbot python3-certbot-nginx
  sudo certbot --nginx -d yourdomain.com
  ```
  Certbot gets a free Let's Encrypt certificate and switches nginx to HTTPS.

- **No domain?** Use a **Cloudflare Tunnel** — it gives you a public HTTPS URL
  with *no open inbound ports at all* (you could even remove the port-80 rule):
  ```bash
  # after creating a free Cloudflare account + adding a domain there
  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
  sudo install cloudflared /usr/local/bin/
  cloudflared tunnel login
  cloudflared tunnel --url http://localhost:8000
  ```

## Day-to-day operations

```bash
# Restart after you change code or .env
sudo systemctl restart stonks-bot stonks-dashboard

# Follow the bot live
journalctl -u stonks-bot -f

# Update to the latest code
cd ~/Stonks && git pull && ./.venv/bin/pip install -r requirements.txt
sudo systemctl restart stonks-bot stonks-dashboard
```
