#!/usr/bin/env bash
# Installs claude-switcher for the current user: the `claude-switcher`
# command, a systemd user service (started now and at every boot), and the
# rc-keeper mod in Claude Code. Safe to run again, e.g. after `git pull`.
#
#   ./install.sh                 # uses the system python3 (3.10 or newer)
#   PYTHON=/path/to/python3 ./install.sh
set -euo pipefail

repo="$(cd "$(dirname "$0")" && pwd)"
fail() { echo "install.sh: $*" >&2; exit 1; }

# Prerequisites.
[ "$(uname -s)" = "Linux" ] || fail "Linux only: Claude Code keeps logins in the macOS Keychain, which this tool cannot swap."
command -v systemctl >/dev/null || fail "systemd is required (systemctl not found)."
systemctl --user show-environment >/dev/null 2>&1 || fail "no systemd user session; log in normally (not only via su/sudo) and try again."
command -v claude >/dev/null || fail "Claude Code is not on PATH: https://code.claude.com/docs/en/setup"
[ -z "${CLAUDE_CONFIG_DIR:-}" ] || fail "CLAUDE_CONFIG_DIR is set; only the default ~/.claude is supported."

if [ -z "${PYTHON:-}" ]; then
  # Prefer the system interpreter: a virtualenv's may be gone tomorrow.
  for candidate in /usr/bin/python3 "$(command -v python3 || true)"; do
    if [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
      PYTHON="$candidate"
      break
    fi
  done
fi
[ -n "${PYTHON:-}" ] || fail "Python 3.10 or newer is required; set PYTHON=/path/to/python3."
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "$PYTHON is older than 3.10."

# The command.
mkdir -p ~/.local/bin
cat > ~/.local/bin/claude-switcher <<EOF
#!/bin/sh
PYTHONPATH="$repo" exec "$PYTHON" -m claude_switcher "\$@"
EOF
chmod +x ~/.local/bin/claude-switcher
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "Note: add ~/.local/bin to your PATH to run claude-switcher by name." ;;
esac

# The service.
mkdir -p ~/.config/systemd/user
sed -e "s#@REPO@#$repo#g" -e "s#@PYTHON@#$PYTHON#g" "$repo/systemd/claude-switcher.service" \
  > ~/.config/systemd/user/claude-switcher.service
systemctl --user daemon-reload
systemctl --user enable claude-switcher.service >/dev/null
systemctl --user restart claude-switcher.service

# A user service runs at boot, and while you are logged out, only with lingering.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  if ! loginctl enable-linger "$USER" 2>/dev/null; then
    echo "Note: run 'sudo loginctl enable-linger $USER' so the service also runs at boot and while you are logged out."
  fi
fi

# The rc-keeper mod, read straight from this folder.
if ! claude plugin marketplace list 2>/dev/null | grep -q "$repo/mod"; then
  claude plugin marketplace add "$repo/mod"
fi
if ! claude plugin list 2>/dev/null | grep -q "rc-keeper@claude-switcher"; then
  claude plugin install rc-keeper@claude-switcher --scope user
fi

cat <<EOF

claude-switcher is installed and running (python: $PYTHON).
Next:
  claude-switcher setup-telegram          # connect your Telegram bot
  claude-switcher import-current <label>  # the account Claude Code is logged into now
  claude-switcher add <label> --email ... # each other account
In Claude Code sessions that were already running, type /reload-plugins once.
EOF
