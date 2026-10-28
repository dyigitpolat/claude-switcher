# claude-switcher

Keep Claude Code working when you hit a usage limit: claude-switcher rotates
between several Claude accounts you own, moving **every running Claude Code
session** to the account with the most room left before the current one runs
out, without restarting anything. It reports each change to Telegram, sends
you a sign-in link when a login expires, and brings Remote Control back in
the sessions that used it.

- Checks each account's 5-hour session and weekly usage every 5 minutes
  (every minute for the active account once it passes 80%).
- Switches when the active account reaches 95% of either limit, to the
  account with the most headroom, never at random.
- Swaps the login of all running sessions in place: they pick it up on their
  next request, mid-task.
- Cycles Remote Control off and on in sessions that had it on, so they
  reconnect under the new account.
- Refreshes logins in the background; when one dies, Telegram gets a sign-in
  button that works from your phone.
- Runs as a systemd user service: survives crashes and reboots.

> **Unofficial.** Not affiliated with Anthropic. It relies on how Claude Code
> stores and reloads logins, which is undocumented and may change in any
> release. Make sure your use complies with Anthropic's terms.

## Requirements

- Linux with systemd. (On macOS Claude Code keeps logins in the Keychain, which
  this tool does not handle.)
- [Claude Code](https://code.claude.com/docs/en/setup) 2.1.280 or newer,
  logged in with a Claude subscription (Pro or Max), using the default
  `~/.claude` folder.
- Python 3.10 or newer. Nothing to `pip install`: the standard library only.
- Two or more Claude accounts with subscriptions.
- A Telegram account, for notifications, sign-in links and remote commands.

## Install

```sh
git clone https://github.com/dyigitpolat/claude-switcher.git
cd claude-switcher
./install.sh
```

Keep the folder where you cloned it: the service and the Claude Code mod run
from it. `install.sh` adds:

- the `claude-switcher` command, in `~/.local/bin`;
- the `claude-switcher` systemd user service, started now and at every boot
  (it enables lingering for your user so it also runs while you are logged
  out; if that needs root it tells you the command to run);
- the **rc-keeper** mod, installed in Claude Code for your user. It is what
  brings Remote Control back after a switch.

The service runs idle until you finish the setup below.

## Setup

### 1. Create a Telegram bot

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Pick a name, then a username ending in `bot`. BotFather replies with a
   **token** like `123456789:AAH...`.
3. Run:

   ```sh
   claude-switcher setup-telegram
   ```

   Paste the token when asked (it is not shown on screen), then open your new
   bot in Telegram and send it `/start`. You get a confirmation message.

The bot answers **only** the chat that sent that `/start`; it ignores everyone
else.

### 2. Enroll your accounts

The account Claude Code is logged into now:

```sh
claude-switcher import-current work
```

Each other account (pick any label: lowercase letters, digits, `-`, `_`):

```sh
claude-switcher add personal --email you@example.com
```

`add` prints a sign-in link. Open it in any browser, on any device, sign in
to **that** account, and the page shows a code: paste it back into the
terminal (or send it to the bot). Your current Claude Code login is not
touched. You can also enroll from Telegram with `/add <label> [email]`.

### 3. Load rc-keeper in sessions that were already running

New Claude Code sessions load the mod by themselves. In each session that was
already open, type `/reload-plugins` once.

### 4. Check

```sh
claude-switcher status     # every account's usage, fetched now
claude-switcher sessions   # running sessions, Remote Control, rc-keeper
```

Or send `/status` to the bot. That's it: from here on it works by itself.

## Telegram

| Command | What it does |
|---|---|
| `/status` | Every account's session and weekly usage, reset times, which one is active, whether auto-switching is on |
| `/sessions` | Running Claude Code sessions: Remote Control on or off (with its link) and whether rc-keeper is loaded |
| `/switch <label>` | Move every session to that account now |
| `/auth <label>` | Send a fresh sign-in link for an account |
| `/add <label> [email]` | Enroll another account |
| `/pause`, `/resume` | Stop or restart automatic switching (usage checks and sign-in links continue) |
| `/help` | The list above |

After following a sign-in link, paste the code the page shows as a plain
message. The code works once, only together with a secret that never leaves
your machine, and the bot deletes your message after using it.

What you will be told about: every switch (why, and the numbers for each
account), Remote Control coming back in each session (with links), logins
that expired or are about to (with a sign-in button), and the moment no
account has room left.

## Command line

| Command | What it does |
|---|---|
| `claude-switcher setup-telegram` | Connect the bot and your chat |
| `claude-switcher import-current <label>` | Enroll the account Claude Code runs on now |
| `claude-switcher add <label> [--email E]` | Enroll an account, or sign an enrolled one in again |
| `claude-switcher status` | Usage of every account, fetched now |
| `claude-switcher sessions` | Running sessions and their Remote Control |
| `claude-switcher switch <label>` | Move every session to an account now |
| `claude-switcher remove <label>` | Forget an account |
| `claude-switcher pause` / `resume` | Stop or restart automatic switching |
| `claude-switcher test-telegram` | Send a test message |

## How it works

**Choosing the next account.** An account only qualifies at or below 85%
session *and* 90% weekly usage, so a switch buys real time. Among those, the
winner is the one whose tighter limit leaves the most headroom
(`100 − max(session %, weekly %)`); a tie goes to the account whose weekly
window resets first. When nothing qualifies, the best account below 95% is
used; when no account is below 95%, nothing moves and you get an alert.

**Switching running sessions.** Every Claude Code session re-reads
`~/.claude/.credentials.json` when the file changes. The switch writes the
new login there, inside the same lock files Claude Code uses for its own
token refreshes (so a session can never write the old account back), and
updates the account in `~/.claude.json`. Each session moves over on its next
request. The outgoing account's latest tokens go back to claude-switcher's
store, which keeps every inactive login refreshed.

**Remote Control.** After an account change a session's Remote Control
either drops for good or lingers, bound to the old account. So rc-keeper,
inside each session that had it on, turns it off and on again once the
session is idle (Claude Code runs a plugin's commands between turns): "off"
borrows the *Enable Remote Control for all sessions* setting, which applies
only to the session that changes it, and immediately puts your value back;
"on" is `/remote-control`. Nothing is typed into your terminals. The
reconnected sessions appear in the Claude app under the **new** account.

**Staying alive.** The service restarts on any crash, starts at boot, exits
for a clean restart if the Telegram bot stops listening, and has a systemd
watchdog for hangs. Each restart sends a silent "running" message.

## Configuration

Settings live in `~/.config/claude-switcher/config.json`. Change a value and
run `systemctl --user restart claude-switcher`.

| Key | Default | Meaning |
|---|---|---|
| `switch_at_percent` | 95 | Leave the active account when its session or weekly usage reaches this |
| `candidate_max_session` | 85 | An account qualifies as a target only at or below this session usage... |
| `candidate_max_weekly` | 90 | ...and this weekly usage |
| `poll_seconds` | 300 | How often every account's usage is checked |
| `fast_poll_above_percent` | 80 | Above this, the active account is checked more often... |
| `fast_poll_seconds` | 60 | ...this often |
| `min_seconds_between_switches` | 120 | No two automatic switches closer than this |
| `reauth_warn_hours` | 48 | Send a sign-in link this long before a login expires |
| `reauth_reminder_hours` | 12 | Repeat an unanswered sign-in link this often |
| `remote_control.watch_minutes` | 120 | How long to wait for each session to reconnect |

## Files and logs

- `~/.config/claude-switcher/config.json`: settings and the bot token.
- `~/.local/share/claude-switcher/accounts/`: one file per account, holding
  its OAuth tokens. Everything under `~/.local/share/claude-switcher` is
  readable by you alone (0600 files, 0700 folders).
- Logs: `journalctl --user -u claude-switcher -f`.

## Updating

```sh
git pull
./install.sh
```

Then `/reload-plugins` in running Claude Code sessions, so they load the new
rc-keeper. `claude-switcher sessions` shows any that still run an older one.

## Uninstalling

```sh
./uninstall.sh           # keeps your enrolled accounts and settings
./uninstall.sh --purge   # also deletes them (and the tokens they hold)
```

Claude Code stays logged into whichever account was active.

## Limitations

- A session that reaches 100% of a limit before the next check stops with a
  usage-limit error. With the 95% threshold and one-minute checks near the
  limit this is rare, but heavy parallel use can outrun it.
- Remote Control comes back when a session's current turn ends, not mid-turn.
- Sessions that were running before rc-keeper was installed need
  `/reload-plugins` before their Remote Control can be cycled; until then the
  bot tells you to do it by hand.
- Only claude.ai subscription logins; not API keys, Bedrock, Vertex or a
  custom `CLAUDE_CONFIG_DIR`.
- Tested with Claude Code 2.1.280 to 2.1.312. A future release can change the
  internals this depends on; the logs and the bot will show errors if so.

## Development

```sh
python3 -m unittest discover -s tests -t tests   # the service
claude plugin test mod/rc-keeper                  # the mod
```

`claude_switcher/` is the service (stdlib-only Python); `mod/rc-keeper/` is
the Claude Code mod; `systemd/` holds the unit template `install.sh` fills in.

## License

MIT
