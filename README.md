# MiMo Grok Adapter

A local adapter for **MiMo-V2.6-Flash** and **MiMo-V2.6-Pro** in **Grok Build**.
It works around a confirmed nullable-type incompatibility in both Xiaomi MiMo
Token Plan and the ordinary pay-as-you-go API. The incompatibility can produce
truncated JSON arguments or raw XML tool calls when using `grep` and `read_file`.

```text
                         tool-schema normalization
Grok Build → 127.0.0.1:8320/v1/responses     → Singapore Token Plan
           → 127.0.0.1:8320/api/v1/responses → pay-as-you-go API
```

One user service handles both routes. Existing Token Plan configurations retain
their original local URL.

| Access method | Local Grok `base_url` | Fixed upstream | Client credential |
| --- | --- | --- | --- |
| Singapore Token Plan | `http://127.0.0.1:8320/v1` | `https://token-plan-sgp.xiaomimimo.com/v1` | Singapore Token Plan key |
| Pay-as-you-go API | `http://127.0.0.1:8320/api/v1` | `https://api.xiaomimimo.com/v1` | Ordinary API key |

Keep each key with its matching route. Ordinary API requests consume API credits;
Token Plan requests consume subscription quota.

The project uses the Python standard library and supports the OpenAI Responses
API. It includes the adapter, a compatibility checker, and a systemd user-service
installer.

## How the workaround works

Before forwarding a request, the adapter walks the `tools` tree and narrows type
arrays containing a single non-null type:

```json
{"type": ["string", "null"]}
```

to:

```json
{"type": "string"}
```

The transformation also applies to nested schemas and namespaced tools.
`required` fields are preserved, and optional parameters remain optional.
Prompts, message history, and reasoning parameters retain their original values.
Xiaomi response bodies, including streamed events, pass through unchanged and
are forwarded as they arrive.

### Scope and limitations

- Explicit `null` loses its schema allowance after normalization.
- Unions with multiple non-null types, such as `["string", "integer", "null"]`,
  remain unchanged. `anyOf`/`oneOf` constructs are preserved.
- The adapter supports POST `/v1/responses` and `/api/v1/responses` for the two
  models listed above. Both routes apply the same schema normalization.
- The request path selects one of the two fixed upstreams in the table above.
  Client-supplied upstream URLs and unknown paths are rejected.
- The workaround addresses a specific schema incompatibility. Compatibility
  with every possible tool and future Grok version requires separate testing.
- Limits: a 32 MiB request body, up to 16 concurrent handlers, and a 180-second
  upstream socket timeout.

## Requirements

- Linux with a systemd user manager and an available user session.
- Python **3.11+**, including `/usr/bin/python3`, which the service uses.
- Git for cloning and updates; Bash and curl for one-command installation.
- Grok Build and a key for your chosen route: Singapore Token Plan, ordinary
  Xiaomi API, or both.

On Ubuntu, install the dependencies with `apt`:

```bash
sudo apt update
sudo apt install python3 git curl
```

Check that your Ubuntu release provides Python 3.11 or newer. All Python code in
this project uses the standard library.

## Installation

### One-command installation with curl

```bash
curl -fsSL https://raw.githubusercontent.com/zinin/mimo-grok-adapter/master/install.sh | bash
```

This command downloads and runs `install.sh` from the **master** branch. The
script clones the repository into
`${XDG_DATA_HOME:-$HOME/.local/share}/mimo-grok-adapter` and runs `install.py`.
Running it again updates a clean checkout with `git pull --ff-only` and
reinstalls the service. The checkout is retained for future updates.

To use an existing local clone or another directory:

```bash
curl -fsSL https://raw.githubusercontent.com/zinin/mimo-grok-adapter/master/install.sh \
  | MIMO_GROK_REPO_DIR=/opt/github/zinin/mimo-grok-adapter bash
```

`MIMO_GROK_REPO_DIR` specifies an absolute path. An existing checkout must be on
`master`, point to this repository through `origin`, and have a clean working
tree. The script stops and preserves user files when it finds an unrelated
directory, a different branch, or uncommitted changes.

To install files only:

```bash
curl -fsSL https://raw.githubusercontent.com/zinin/mimo-grok-adapter/master/install.sh \
  | bash -s -- --no-start
```

The script requires Bash. Run it as your regular user. To review the code before
executing it, use the manual clone procedure below.

### Installation from Git

Clone the repository into your preferred directory. For a local mirror under
`/opt/github/zinin`:

```bash
git clone --branch master https://github.com/zinin/mimo-grok-adapter.git /opt/github/zinin/mimo-grok-adapter
cd /opt/github/zinin/mimo-grok-adapter
./install.py
```

You can also run `./install.sh` from a local checkout. It forwards arguments to
the Python installer.

Run the installer as your regular user. It installs these files:

| Source | Installed file |
| --- | --- |
| `mimo-grok-adapter` | `~/.local/bin/mimo-grok-adapter` |
| `check-mimo-grok` | `~/.local/bin/check-mimo-grok` |
| `systemd/mimo-grok-adapter.service` | `~/.config/systemd/user/mimo-grok-adapter.service` |

It then runs `systemctl --user daemon-reload`, enables automatic startup,
restarts `mimo-grok-adapter.service`, and checks its local `/health` endpoint.
The PID returned by `/health` must match `MainPID` of the active user unit. A
response from another process occupying the port causes the installer to roll
back. The service starts through the systemd user manager and restarts on
failure. The installer preserves `linger` settings and other services.

Existing versions of these three files are saved with private permissions under
`~/.local/state/mimo-grok-adapter/backups/<timestamp-id>/`. If installation fails,
the installer restores the previous files and attempts to restore its service
to its previous state. Symbolic links at destination file paths are rejected.
Backups remain available for manual recovery.

The installer preserves `~/.grok/config.toml`, API keys, permissions, hooks, and
global Grok settings. Make sure `~/.local/bin` is included in `PATH`.

To install files only and leave systemd unchanged:

```bash
./install.py --no-start
```

Check the service:

```bash
systemctl --user status mimo-grok-adapter.service
curl --noproxy '*' http://127.0.0.1:8320/health
journalctl --user -u mimo-grok-adapter.service -f
```

`/health` checks the local process. Use the compatibility checker below to test
model behavior.

## Grok configuration

Add the missing sections from
[`examples/grok-models.toml`](examples/grok-models.toml) to
`~/.grok/config.toml`. Update the fields of any model section that already
exists: declaring the same TOML table twice is invalid. Preserve your
`[models]` defaults and existing direct-model entries.

The example uses separate variables for the two credentials:

- `MIMO_API_KEY`: Singapore Token Plan key, preserving the existing example.
- `MIMO_PAYG_API_KEY`: ordinary pay-as-you-go API key.

Supply the variables for the routes you use before starting Grok, for example
with interactive prompts:

```bash
read -rsp 'Singapore Xiaomi Token Plan key: ' MIMO_API_KEY
printf '\n'
export MIMO_API_KEY

read -rsp 'Xiaomi pay-as-you-go API key: ' MIMO_PAYG_API_KEY
printf '\n'
export MIMO_PAYG_API_KEY
```

If your keys are already configured in Grok, retain that authentication method.
The adapter receives `Authorization` from each client request and forwards it to
the upstream selected by that request path. Keys stay in the client
configuration; the service itself stores none.

Start Token Plan models through the adapter:

```bash
grok -m mimo-v2.6-flash-adapted --effort high
grok -m mimo-v2.6-pro-adapted --effort high
```

Start ordinary API models through the adapter:

```bash
grok -m mimo-v2.6-flash-api-adapted --effort high
grok -m mimo-v2.6-pro-api-adapted --effort high
```

The example also includes direct entries: `mimo-v2.6-flash` / `mimo-v2.6-pro`
for Token Plan and `mimo-v2.6-flash-api` / `mimo-v2.6-pro-api` for the ordinary
API. Keep them to check upstream compatibility with the original schemas.
All entries send the original model IDs, `mimo-v2.6-flash` and `mimo-v2.6-pro`.
The `-api-adapted` suffix selects the local API route, while `-adapted` selects
the local Token Plan route.

## Checking whether Xiaomi has fixed the server

```bash
check-mimo-grok                         # direct Token Plan
check-mimo-grok --adapted               # Token Plan through the adapter
check-mimo-grok --api                   # direct ordinary API
check-mimo-grok --api --adapted         # ordinary API through the adapter
check-mimo-grok --api --adapted --model pro --timeout 90 --json
```

The default remains the original Token Plan entries. `--api` chooses the
pay-as-you-go entries; `--adapted` chooses the matching local adapter entries.
For direct checks, retain the direct `base_url` values from the example.
The JSON report identifies the provider as `token_plan` or `api` and the route
as `upstream` or `adapter`.

Each model receives three checks:

1. A native `list_dir` call at `high` as a control request.
2. A sequence of native `grep` and `read_file` calls at `high`.
3. A text request with global reasoning effort set to `max`.

The checker validates actual tool results. It creates a private temporary
configuration copy and separate sessions, disables discovered MCP servers in
that copy, preserves the original rules and permissions, and cleans up its own
test processes and files. Inconclusive results are reported as `ERROR`.
Token Plan checks consume subscription quota. `--api` checks incur ordinary
API usage charges, including when combined with `--adapted`.

| Exit code | Meaning |
| --- | --- |
| `0` | All checks passed (`OK`) |
| `1` | A known failure was reproduced (`BUG`) |
| `2` | Startup error or inconclusive result (`ERROR`) |
| `130` | The check was interrupted |

Tested on **2026-10-05** with Grok Build **1.0.46**: both models reproduced the
`grep/read_file` failure through the direct Token Plan and ordinary API
endpoints. Each of the four adapted model entries successfully completed
`list_dir/high`, `grep` followed by `read_file/high`, and text/global-max in live
checks. Later runs report the current state of the server and client.

## Updating the local installation

For curl installations, repeat your chosen command from the installation
section. For a checkout at `/opt/github/zinin/mimo-grok-adapter`:

```bash
cd /opt/github/zinin/mimo-grok-adapter
git pull --ff-only
./install.py
```

The installer copies the current checkout into the same installed paths,
backs up the previous version, and restarts the service. The running service
uses the installed copy and operates independently of the checkout location.
Update between requests: restarting the service interrupts active connections.

Verify the update:

```bash
check-mimo-grok --adapted
check-mimo-grok --api --adapted
```

## Security

- The server binds exclusively to `127.0.0.1`.
- Adapter logs contain route labels, HTTP status codes, and normalized-field
  counts. Request bodies, model output, and API keys are kept out of the logs.
- Authorization is forwarded only to the fixed upstream selected by the exact
  request path. HTTP redirects are disabled.
- Keep keys, user configurations, and backups outside the repository.
- The installer manages its own user unit and the three files listed above.

## Development and tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
```

Tests use temporary HOME directories, local HTTP fixtures, and isolated
`systemctl` and Git stubs. They run without Xiaomi API keys or a real systemd
user manager. Live model requests are made separately by the compatibility
checker.

## License

[MIT](LICENSE).
