# PlayPalace V11

PlayPalace is an accessible online gaming platform. This repository contains both the server (v11, modern) and client (ported from v10).

## Contact

If you have questions, want realtime information, or plan to develop for the project, you can join the [Play Palace discord server](https://discord.gg/e9cSpwJ5mR) here.

This is the primary place for discussion about the project.

## Quick Start

You need Python 3.13 or later. We use [uv](https://docs.astral.sh/uv/) for dependency management on the server and client.

### Developing with nix

If you have nix installed, the repository ships a ready-to-use flake dev shell:

```bash
nix --extra-experimental-features 'nix-command flakes' develop .
```

There are also task-oriented shells:

- `nix --extra-experimental-features 'nix-command flakes' develop .#server`
- `nix --extra-experimental-features 'nix-command flakes' develop .#client`

Once inside the shell, use the helper scripts under `scripts/` (documented below) for repeatable test runs without additional setup.

### Running the Server

PlayPalace always reads configuration from `server/config.toml` when run from source. On Windows installs produced by the MSI, the server instead looks for `%PROGRAMDATA%\PlayPalace\config.toml`. The installer copies `config.example.toml` into that directory the first time and future updates preserve any edits. See [Server Configuration](#server-configuration) for descriptions of each knob.

```bash
cd server
cp config.example.toml config.toml  # first run only
```

TLS is required unless you explicitly allow plaintext websockets. For production, leave `[network].allow_insecure_ws = false` and pass `--ssl-cert/--ssl-key`. For trusted local development, set `allow_insecure_ws = true` in `config.toml` so you can run without certificates.

After the config is in place, install dependencies and launch the server:

```bash
uv sync
uv run python main.py
```

You can also double-click `run_server.bat` (Windows) or use the `scripts/run_server.sh` helper as a shortcut.

The server starts on port 8000 by default. Use `--help` to see all options:

```bash
uv run python main.py --help
```

Common options:
- `--port PORT` - Port number (default: 8000)
- `--host HOST` - Host address (default: 0.0.0.0)
- `--ssl-cert PATH` - SSL certificate for WSS (secure WebSocket)
- `--ssl-key PATH` - SSL private key for WSS
- `--preload-locales` - Block startup until every Fluent bundle compiles, instead of warming them in the background.

Compiled localization bundles are cached under `server/.cache/locales`, so only the first start (or the first start after editing `.ftl` files) pays the compile cost. Delete that directory, or set `PLAYPALACE_DISABLE_LOCALE_CACHE=1`, to force a full rebuild.

On the first launch, the server copies `config.example.toml` to `config.toml`, prints a reminder to edit it, and exits so you can review the settings. When a config file exists but the database is empty, the server (only when attached to a TTY) prompts you to create the initial owner account; in headless environments, run `uv run python -m server.cli bootstrap-owner --username <name>` instead.

### Running with SSL/WSS (Secure WebSocket)

To run the server with SSL encryption (required for production deployments):

**Using Let's Encrypt certificates:**
```bash
uv run python main.py --port 8000 \
  --ssl-cert /etc/letsencrypt/live/yourdomain.com/fullchain.pem \
  --ssl-key /etc/letsencrypt/live/yourdomain.com/privkey.pem
```

**Using self-signed certificates (for testing):**
```bash
# Generate self-signed certificate
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"

# Run server with self-signed certificate
uv run python main.py --port 8443 --ssl-cert cert.pem --ssl-key key.pem
```

When SSL is enabled, the server will report `wss://` instead of `ws://` in its startup message. Clients connecting to a WSS server must use the `wss://` protocol in their connection URL.

### Running the Client

```bash
cd clients/desktop
uv sync
uv run python client.py
```

You can launch the "run_client.bat" file as a shortcut.

The client requires wxPython and a few other dependencies from v10.

The client supports both `ws://` and `wss://` connections. When connecting to a server with SSL enabled, enter the server address with the `wss://` prefix (e.g., `wss://example.com`). The client will handle SSL certificate validation automatically.
Use the **Server Manager** button on the login screen to add/edit servers (name, host, port, notes) and manage saved accounts for each server. You can add `localhost` for local testing.

### Windows Packaging (Work in Progress)

To produce a single MSI that ships both the PyInstaller-built client and server, run `clients/desktop/build.ps1` and `server/build.ps1`, then follow the WiX v4 instructions in `packaging/installers/windows/README.md`. When it is time to build, change into `packaging/installers/windows/wix/` (or pass the `.wxs/.wixproj` path) before invoking `wix build` so the relative `ClientSource`/`ServerSource` values resolve correctly. The MSI installs binaries under `Program Files\PlayPalace`, copies configuration into `%PROGRAMDATA%\PlayPalace\config.toml`, and registers the server as a Windows service with a post-install PowerShell helper that applies the installer’s host/port/SSL inputs. Branding assets and the final configuration wizard are still placeholders—see the installer README for current gaps.

Linux packaging work has begun too: run the repo-relative helpers (`./packaging/installers/linux/scripts/build_deb.sh`, `build_rpm.sh`, `build_arch.sh`) to produce basic `.deb`, `.rpm`, and Arch packages (client and server separately) from any working directory. See `packaging/installers/linux/README.md` for details.

### Packet Schema Validation

Packet contracts are defined once in `server/network/packet_models.py` using Pydantic. Whenever you add or edit packet fields, regenerate the mirrored JSON schema files (used by both the server and client validators) with:

```bash
cd server
uv run python tools/export_packet_schema.py
```

This command rewrites `server/packet_schema.json` and `clients/desktop/packet_schema.json`; make sure both artifacts are committed alongside any packet changes. The server validates incoming packets before handing them to gameplay handlers, and the client validates both outgoing and incoming packets before they are sent or dispatched.

#### TLS Verification

PlayPalace now enforces TLS hostname and certificate verification for all `wss://` connections. When the server presents an unknown or self-signed certificate, the client shows the certificate details (CN, SANs, issuer, validity window, and SHA-256 fingerprint) and lets you explicitly trust it. Trusted certificates are pinned per server entry—subsequent connections will only succeed if the fingerprint matches, and you can remove a stored certificate from the Server Manager dialog at any time.

#### Token-Based Auth Flow

PlayPalace uses short-lived access tokens (1 hour by default) and refresh tokens to avoid sending passwords after the initial login. The login flow is:

1. The client sends `authorize` with username + password (or an existing access token if it is still valid).
2. The server responds with `authorize_success`, including the access token and a refresh token.
3. On reconnect, if the access token is expired, the client sends `refresh_session` with the stored refresh token.
4. The server rotates the refresh token and returns `refresh_session_success` with new tokens.

Refresh tokens are stored in the client identities file (same storage used for saved accounts). Access tokens remain in memory only.

### Server Configuration

After the first server launch creates `server/config.toml` (or `%PROGRAMDATA%\PlayPalace\config.toml` on Windows installs), edit that file (or re-copy `config.example.toml` if you need a fresh baseline) to adjust behavior. Alongside the existing `[virtual_bots]` settings, the `[auth]` section lets you clamp username and password lengths that the server will accept:

```toml
[auth]
username_min_length = 3
username_max_length = 32
password_min_length = 8
password_max_length = 128
refresh_token_ttl_seconds = 2592000

[auth.rate_limits]
login_per_minute = 5
login_failures_per_minute = 3
registration_per_minute = 2
refresh_per_minute = 10
refresh_window_seconds = 60
# Optional sliding-window overrides (defaults: 60s each)
# login_window_seconds = 60
# login_failure_window_seconds = 60
# registration_window_seconds = 60
# refresh_window_seconds = 60
```

If the `[auth]` table is omitted, PlayPalace falls back to the defaults shown above. Adjust these values to match your policies (for example, force longer passwords on public deployments).

To limit the maximum inbound websocket payload size (guarding against giant packets), add a `[network]` section:

```toml
[network]
max_message_bytes = 1_048_576  # 1 MB default
allow_insecure_ws = false      # force TLS by default
```

Values are in bytes and map directly to the `max_size` setting used by the underlying websockets server.
Set `allow_insecure_ws` to `true` only for trusted development setups where TLS certificates are unavailable; the server will refuse to start without TLS when this flag is `false`, and it will print a loud warning whenever it runs in plaintext mode.
You cannot combine `allow_insecure_ws = true` with `--ssl-cert/--ssl-key`; pick either plaintext development mode or full TLS.
`[auth.rate_limits]` caps how many login attempts each IP can make per minute, how many failed attempts a specific username can accrue, how many registrations are allowed per minute from the same IP, and how often refresh tokens may be exchanged. Setting any of the limits (or the corresponding optional `*_window_seconds` overrides) to `0` disables that particular throttle.

### Email Verification and Password Recovery

PlayPalace can verify new accounts and recover forgotten passwords by email. Configure a secure SMTP relay in `server/config.toml`:

```toml
[smtp]
host = "smtp.example.com"
port = 587                    # 587 for STARTTLS, 465 for implicit TLS
security = "starttls"         # "starttls" or "ssl"
from_address = "PlayPalace <no-reply@example.com>"
username = "smtp-user"
password = "smtp-password"
timeout_seconds = 15
```

STARTTLS and implicit TLS use the system CA store with hostname verification enabled. Plaintext SMTP is rejected unless `allow_plaintext = true` is set explicitly, and should only be used against a local development relay. Keep real SMTP credentials out of version control; prefer an environment-managed config file or a provider-specific app password.

Set a stable `[auth].email_token_secret` so outstanding codes survive a server restart. Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Codes are stored only as keyed hashes, expire after `email_code_ttl_seconds` (15 minutes by default), are consumed once, and are discarded after `email_code_max_attempts` (5 by default). Login, registration, verification-resend, and password-reset requests are rate limited. The server always replies generically to password-reset requests so it does not reveal whether an account exists.

When SMTP is configured, unknown usernames are no longer registered implicitly on first login: clients must send the `register` packet with an email address, then verify the emailed code before the account can log in. Existing accounts without an email address remain usable, but cannot use email recovery; a server administrator can still reset their password with the CLI:

```bash
cd server
uv run python -m server.cli reset-password --username "<name>"
```

The desktop client exposes **Create Account**, **Forgot Password**, and **Advanced → Verify Account Email** on the login dialog. The web client exposes equivalent Create account and Forgot password controls.

#### Guided Virtual Bots

The `server/core/virtual_bots.py` manager reads the `[virtual_bots]` block to run deterministic “guided tables” that stage named bot groups into specific games.

##### Profiles
Define `[virtual_bots.profiles.<name>]` to override any timing/behavior knob (idle/online/offline windows, join/create/offline probabilities, logout delays, plus `min_bots_per_table`, `max_bots_per_table`, and `waiting_*` guards) per persona—e.g., `host`, `patron`, `mixer`. Unspecified fields inherit from the top-level `[virtual_bots]` block.

##### Bot Groups
Use `[virtual_bots.bot_groups.<tag>]` to enumerate bot usernames and optionally pin them to a profile. Guided tables reference these tags instead of raw usernames so you can reshuffle bots without editing every rule.

##### Guided Tables
Each `[[virtual_bots.guided_tables]]` entry describes a “channel”: set the unique `table` label, the allowed `game`, deterministic `priority`, desired seat counts (`min_bots`/`max_bots`), and the `bot_groups` allowed to fill the seats. Optional `profile` overrides force all bots in that rule to adopt one profile, and optional `cycle_ticks` plus `active_ticks = [start, end]` windows provide tick-based scheduling with no wall-clock dependency. `fallback_behavior` controls whether unassigned bots keep using probabilistic matchmaking (`"default"`) or stay offline until a guided slot needs them (`"disabled"`). `allocation_mode` (`"best_effort"` vs `"strict"`) dictates what happens when the bot pool cannot satisfy all `min_bots` requirements.

##### Admin Monitoring
Server owners can review the live guided-table plan from the admin → Virtual Bots menu: **Guided Tables** shows rule health (active, shortages, current table IDs), **Bot Groups** lists inventory per tag, and **Profiles** dumps the effective overrides so you can audit behavior without opening `config.toml`.

See `server/config.example.toml` for an annotated sample, and refer to [Appendix A](#appendix-a-single-game-stress-test-with-bots) for a minimal stress-test configuration.

## Project Structure

The server and clients are separate codebases with different philosophies.

### Server

The server is a complete rewrite for v11. It uses modern Python practices: dataclasses for all state, Mashumaro for serialization, websockets for networking, and Mozilla Fluent for localization.

We hold the view that game simulations should be entirely state-based. If a game can't be saved and loaded without custom save/load code, something has gone wrong. This is why everything is a dataclass, and why games never touch the network directly.

Key directories:
- `server/core/` - Server infrastructure, websocket handling, tick scheduler
- `server/games/` - Game implementations (Pig, Scopa, Threes, Light Turret, etc.)
- `server/game_utils/` - Shared utilities for games (cards, dice, teams, turn order)
- `server/auth/` - Authentication and session management
- `server/persistence/` - SQLite database for users and tables
- `server/messages/` - Localization system
- `docs/design/` - Design documents explaining architectural decisions

### Clients

The GUI client lives under `clients/desktop/` and is ported from v10. It works, but it carries some technical debt from the older codebase. You may encounter rough edges.

The desktop app is built with wxPython and designed for accessibility. It communicates with the server over websockets using JSON packets.

The browser client (`clients/web/`) shares the packet schema and focuses on quick testing or lightweight deployments.

### Packaging

`packaging/` contains container build contexts (under `packaging/containers/`) and cross-platform installers (`packaging/installers/`). Refer to the README files inside each subdirectory for distro-specific instructions.

### Runtime Artifacts

Local-only files (SQLite database, logs, collected build outputs) live under `var/`. The server now defaults to writing `var/server/playpalace.db` and `var/server/errors.log`, keeping the repo root clean; delete this directory if you want a fresh start.

## Running Tests

The server has comprehensive tests. We run them frequently during development.

```bash
cd server
uv run pytest
```

For verbose output:

```bash
uv run pytest -v
```

The test suite includes unit tests, integration tests, and "play tests" that run complete games with bots. Play tests save and reload game state periodically to verify persistence works correctly.

See also: CLI tool.

### Server tests inside `nix develop`

Inside any of the dev shells you can run the server tests without remembering the `uv` incantation:

```bash
./scripts/nix_server_pytest.sh            # from repo root
# or pass extra arguments
./scripts/nix_server_pytest.sh -k network
```

Under the hood the script changes into `server/` and runs `uv run pytest`, ensuring the correct environment variables from the nix shell are honored.
If you want to spawn the shell, run, and exit in one command, let nix wrap the helper:

```bash
nix --extra-experimental-features 'nix-command flakes' develop .#server --command ./scripts/nix_server_pytest.sh
```

### Client tests inside `nix develop`

Running the wxPython-based client tests under `nix develop` needs a small amount of extra Python tooling (pytest, pydantic, jsonschema). To make that repeatable, use the helper script below from the repo root:

```bash
nix --extra-experimental-features 'nix-command flakes' develop . --command ./scripts/nix_client_pytest.sh
```

Already inside `nix develop`, you can also call `./scripts/nix_client_pytest.sh` directly; the helper takes care of the rest either way. It installs the required Python packages under `.nix-python/` (per-repo, not system-wide), exports `PYTHONPATH` so that site-packages and `clients/desktop/` itself are importable, and executes:

```
clients/desktop/tests/test_network_manager.py
clients/desktop/tests/test_main_window_packets.py
clients/desktop/tests/test_main_window_startup.py
```

Pass additional pytest arguments to target different files, e.g.:

```bash
nix --extra-experimental-features 'nix-command flakes' develop . --command ./scripts/nix_client_pytest.sh -k network
```

The helper will reuse the cached `.nix-python/` prefix on subsequent runs, so once the packages download the first time, later test runs are instant.

If you ever want to reclaim disk space, just remove the prefix:

```bash
rm -rf .nix-python
```

#### Cache space tips

Building wxPython pulls in a substantial toolchain. If your default `$HOME` volume is small, point uv’s cache and temporary files at a larger mount before invoking the helper:

```bash
export UV_CACHE_DIR=/podman/uv-cache
export TMPDIR=/podman
nix --extra-experimental-features 'nix-command flakes' develop .#client --command ./scripts/nix_client_pytest.sh
```

Those environment variables persist only for the current command, so you can tailor them per machine without editing repo files.

#### Bootstrapping the First Admin

Fresh databases contain zero users. The server still allows the first remote registration for backwards compatibility, but production deployments should explicitly seed the owner account before exposing the port. Use the CLI helper:

```bash
cd server
uv run python -m server.cli bootstrap-owner --username admin
```

The command prompts for a password (or accept `--password-file/--password-stdin`) and creates an approved `SERVER_OWNER` user. Passing `--force` lets you update an existing account’s password/trust level if you’re repairing a database.
Verify the owner account with:

```bash
cd server
uv run python -m server.cli list-users
```

Look for the `SERVER_OWNER` trust level in the output before exposing the port.

If a user forgets their password, or you need to recover any account (including the owner), set a new one from the server console:

```bash
cd server
uv run python -m server.cli reset-password --username <name>
```

The command prompts for the new password (or accepts `--password-file/--password-stdin`), revokes the account's refresh tokens, and prints a confirmation. Use `uv run python -m server.cli list-users` to see accounts and trust levels if you need to look up a username. Both commands target `var/server/playpalace.db` by default; pass `--db-path` for a custom database.

When the server starts and finds zero users, it now prints a warning reminding you to run the bootstrap command. Automated test environments can silence the message by setting `PLAYPALACE_SUPPRESS_BOOTSTRAP_WARNING=1`, but this is not recommended for real deployments.

## Available Games

Note: many games are still works in progress.

- **Pig** - A push-your-luck dice game
- **Threes** - Another push-your-luck game, with a little more complexity
- **Scopa** - A complex game about collecting cards
- **Light Turret** - A dice game from the RB Play Center
- **Chaos Bear** - Another RB Play Center game about getting away from a bear
- **Mile by Mile** - A racing card game
- **Farkle** - A dice game somewhat reminiscent of Yahtzee
- **Yahtzee** - Classic dice game with 13 scoring categories
- **Ninety Nine** - Card game about keeping the running total under 99
- **Pirates of the Lost Seas** - RPG adventure with sailing, combat, and leveling
- **Tradeoff** - Dice trading game with set-based scoring
- **Toss Up** - Push-your-luck dice game with green/yellow/red dice
- **1-4-24** - Dice game where you keep 1 and 4, score the rest
- **Left Right Center** - Dice-and-chip elimination game
- **Age of Heroes** - Civilization-building card game (cities, monument, or last standing)

Each implementation lives under `server/games/<game>/` (see `server/games/registry.py` for the registration map), so you can inspect or extend the rules directly in code after finding the title here.

## CLI Tool

The server also includes a CLI (`server/cli.py`) for simulating games without running the full server. This is useful for testing and for AI agents. It does not supercede play tests, but works alongside them, and allows you to very quickly test specific scenarios.

```bash
cd server

# List available games
uv run python -m server.cli list-games

# Simulate a game with bots
uv run python -m server.cli simulate pig --bots 2

# Simulate with specific bot names
uv run python -m server.cli simulate lightturret --bots Alice,Bob,Charlie

# Output as JSON
uv run python -m server.cli simulate pig --bots 3 --json

# Test serialization (save/restore each tick)
uv run python -m server.cli simulate threes --bots 2 --test-serialization
```

## Architecture Notes

A few things worth understanding about how the server works:

**Tick-based simulation.** The server runs a tick every 50 milliseconds. Games don't use coroutines or async/await internally. All game logic is synchronous and state-based. This makes testing straightforward and persistence trivial.

**User abstraction.** Games never send network packets directly. They receive objects implementing the `User` interface and call methods like `speak()` and `show_menu()`. The actual user might be a real network client, a test mock, or a bot. Games don't need to know or care.

**Actions, not events.** There's a layer between "event received from network" and "action executed in game". Bots call actions directly on tick. Human players trigger actions through network events. The game logic is the same either way.

**Imperative state changes.** We recommend changing game state imperatively, not declaratively. Actions should directly end turns and send messages, not return results describing what should happen.

For more details, see the design documents in `docs/design/`.

## Known Issues

The client is a port from v10 and may have compatibility issues with some v11 features. If something doesn't work as expected, the server is likely fine and the client needs adjustment.

## Development

The server uses uv for dependency management. To add a dependency:

```bash
cd server
uv add <package>
```

For development dependencies:

```bash
uv add --dev <package>
```

When writing new games, look at existing implementations in `server/games/` for patterns. Pig is a good simple example. Scopa demonstrates card games with team support.

## Appendix A: Single-Game Stress Test With Bots

To hammer a single game with an always-on crew of bots (useful for load testing rules, UI, or translations), drop a minimal config like this into `server/config.toml`:

```toml
[virtual_bots]
names = ["Alex", "Jordan", "Taylor", "Morgan"]
min_idle_ticks = 20      # 1s
max_idle_ticks = 60      # 3s
min_online_ticks = 1000000
max_online_ticks = 1000000
go_offline_chance = 0.0
logout_after_game_chance = 0.0
max_tables_per_game = 1
fallback_behavior = "disabled"

[virtual_bots.profiles.default]
min_bots_per_table = 0
max_bots_per_table = 4

[virtual_bots.bot_groups.all_bots]
bots = ["Alex", "Jordan", "Taylor", "Morgan"]

[[virtual_bots.guided_tables]]
table = "Crazy Table"
game = "crazyeights"
bot_groups = ["all_bots"]
min_bots = 4
max_bots = 4
priority = 10
```

Key behaviors:
- Bots make a decision every 1–3 seconds and never voluntarily log off, so the lobby and table stay hot.
- `max_tables_per_game = 1` plus a single guided-table entry pins every bot to Crazy Eights; nothing else can spawn.
- `fallback_behavior = "disabled"` keeps any unassigned bots offline, eliminating random tables when testing.

This setup is ideal when you want to observe repeated starts/finishes of one game (e.g., Crazy Eights) without human supervision.
