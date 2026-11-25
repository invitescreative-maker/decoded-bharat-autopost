#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu Oracle VM:   bash setup.sh
# Secrets are typed here by you and stored only in ~/autopost.env on this VM (chmod 600).
set -e
REPO_SSH="git@github.com:invitescreative-maker/decoded-bharat-autopost.git"

sudo apt-get update -y
sudo apt-get install -y git python3 python3-venv
[ -d "$HOME/venv" ] || python3 -m venv "$HOME/venv"
"$HOME/venv/bin/pip" install -q requests

if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
  ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519" -C "oracle-autopost"
fi
echo
echo "=== Add this key at GitHub -> repo decoded-bharat-autopost -> Settings -> Deploy keys -> Add deploy key"
echo "=== Title: oracle   |   tick 'Allow write access'   |   Key:"
cat "$HOME/.ssh/id_ed25519.pub"
echo
read -r -p "Press Enter once the deploy key is added... " _
ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null

[ -d "$HOME/autopost" ] || git clone "$REPO_SSH" "$HOME/autopost"

if [ ! -f "$HOME/autopost.env" ]; then
  echo "Enter the 4 secrets (input is hidden). Same values as the GitHub Actions secrets."
  read -r -s -p "IG_ACCESS_TOKEN: " A; echo
  read -r -p "IG_USER_ID: " B
  read -r -s -p "TELEGRAM_BOT_TOKEN: " C; echo
  read -r -p "TELEGRAM_CHAT_ID: " D
  umask 077
  printf 'IG_ACCESS_TOKEN=%s\nIG_USER_ID=%s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$A" "$B" "$C" "$D" > "$HOME/autopost.env"
fi
chmod 600 "$HOME/autopost.env"

chmod +x "$HOME/autopost/oracle/loop.sh"
sudo tee /etc/systemd/system/decoded-bharat.service >/dev/null <<EOF
[Unit]
Description=Decoded Bharat autoposter
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
ExecStart=$HOME/autopost/oracle/loop.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now decoded-bharat.service
sleep 5
sudo systemctl --no-pager status decoded-bharat.service | head -12
echo "Logs:  journalctl -u decoded-bharat -f"
