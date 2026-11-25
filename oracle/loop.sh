#!/usr/bin/env bash
# Runs forever on the Oracle VM (started by systemd): every 30 s -> pull jobs, handle Telegram taps,
# post what is due, push the updated state back to GitHub.
cd "$HOME/autopost" || exit 1
while true; do
  if git pull --rebase -q; then
    set -a; . "$HOME/autopost.env"; set +a
    "$HOME/venv/bin/python" poster.py
    git add -A jobs videos state.json
    if ! git diff --cached --quiet; then
      git -c user.name=autopost-bot -c user.email=autopost-bot@users.noreply.github.com commit -q -m "autopost state"
      git push -q || echo "push failed, will retry next loop"
    fi
  else
    git rebase --abort 2>/dev/null   # never reset: local state may hold a claimed job
    echo "pull failed, skipping this loop"
  fi
  sleep 30
done
