# home/ and home.local/

`agent-platform sync <env>` copies two directories onto your box's home
directory, in this order:

1. `home/` (this directory, committed): shared defaults for every developer.
2. `home.local/` (gitignored, yours only): personal config and tokens.

Paths are relative to your home directory: `home.local/.gitconfig` lands at
`~/.gitconfig`. Files in the repo always overwrite the box's copy. Nothing on
the box is ever deleted, and paths absent from these directories are never
touched. This README itself is not synced.

**Never put tokens in `home/`.** `sync` refuses to run if it finds
credential-looking files there (`.credentials.json`, `data.sqlite3`,
`*token*`, `*.pem`).

## Tokens

The simplest option is environment variables in `home.local/.agent-platform/env`.
The box sources this file at the top of `~/.bashrc`, so interactive shells,
herdr panes and `ssh box cmd` all see it:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token`
export KIRO_API_KEY=...
```

Run `chmod 600 home.local/.agent-platform/env`; rsync keeps the mode.

Token files also work instead:

| Path in home.local/ | What |
|---|---|
| `.claude/.credentials.json` | Claude Code OAuth token |
| `.local/share/kiro-cli/data.sqlite3` | Kiro auth (the whole file; it also holds history) |

A copied OAuth refresh token can be rotated by either copy, which logs the
other one out. The env-var tokens above avoid that.

**Never sync the rest of `~/.local/share/kiro-cli/`.** It is about 200 MB of
bundled `bun`, `node` and `tui.js` runtimes that the box installs itself.

## Personal config

| Path in home.local/ | What |
|---|---|
| `.claude/settings.json`, `.claude/CLAUDE.md`, `.claude/skills/`, `.claude/plugins/` | Claude Code config |
| `.kiro/agents/`, `.kiro/settings/`, `.kiro/skills/`, `.kiro/steering/` | Kiro config |
| `.gitconfig` | git identity |
| `.config/herdr/config.toml` | herdr config |
| `.agent-platform/env` | exported env vars (see above) |

Paths are not rewritten, so avoid `/mnt/c/...` (WSL) paths in synced files.
`doctor` warns if it finds any.

`sync` refuses to run while `claude` or `kiro-cli` is running on the box,
because Kiro's SQLite file would race the live process. Quit them first.
