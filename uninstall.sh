#!/usr/bin/env bash
# Removes claude-switcher: the service, the command and the rc-keeper mod.
# Your enrolled accounts and settings are kept unless you pass --purge.
# Claude Code stays logged into whichever account was active.
set -uo pipefail

systemctl --user disable --now claude-switcher.service 2>/dev/null
rm -f ~/.config/systemd/user/claude-switcher.service
systemctl --user daemon-reload 2>/dev/null
rm -f ~/.local/bin/claude-switcher

if command -v claude >/dev/null; then
  claude plugin uninstall rc-keeper@claude-switcher 2>/dev/null
  claude plugin marketplace remove claude-switcher 2>/dev/null
fi

if [ "${1:-}" = "--purge" ]; then
  # These hold the OAuth tokens of every enrolled account and the bot token.
  rm -rf ~/.config/claude-switcher ~/.local/share/claude-switcher
  echo "Removed claude-switcher and its data."
else
  echo "Removed claude-switcher. Accounts and settings are kept in"
  echo "~/.local/share/claude-switcher and ~/.config/claude-switcher (./uninstall.sh --purge deletes them)."
fi
