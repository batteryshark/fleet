# Fleet

<p><a href="assets/workstack-mark-full.png"><img src="assets/workstack-mark.png" alt="Fleet captain holding a tugboat" width="160"></a></p>

Fleet is a macOS registry and CLI for the background services your coding agents
create. It gives you and your agents one place to see registered services, check
what's running or stopped, and control them.

## Why Fleet

An agent can build a service that starts at login or keeps running long after
the session that created it. After a few projects, it's easy to lose track of
those services across one-off scripts, LaunchAgent labels and log locations.
Leaving autonomous services unattended indefinitely carries reliability and
security risks.

Fleet keeps service names, groups, commands, logs and URLs together so you and
your agents can return to that inventory, understand what's there and decide
what to keep or stop. macOS launchd continues to run and supervise the jobs.

Fleet runs on demand. It has no daemon, HTTP API or web UI. It manages jobs in
your user's GUI domain. It doesn't manage system daemons or other users' jobs.
Never run Fleet with `sudo`.

## Set up from source

You need macOS with a logged-in GUI user session and Python 3.9 or newer available
as `python3`. Fleet uses only the Python standard library. No package manager,
compiler, Node runtime or downloaded build tools are required for Fleet itself.
Managed programs have their own prerequisites.

From the root of this checkout:

```sh
python3 --version
python3 bin/fleet help
```

You can continue using `python3 bin/fleet`. For a `fleet` command on PATH, keep
the checkout at a stable location and create a symlink only if the destination
doesn't already exist:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/bin/fleet" "$HOME/.local/bin/fleet"
```

Add `$HOME/.local/bin` to PATH in your shell configuration. Review an existing
command before replacing it. Moving the checkout breaks the symlink. Removing
the symlink removes the command; it leaves registered jobs and their files alone.

## Register services deliberately

The first command that needs configuration creates
`~/.config/fleet/services.json` with an empty registry:

```json
{"version": 1, "services": []}
```

Have agents explicitly add or adopt services under the machine owner's existing
authority as part of setting them up. Fleet doesn't discover or import
services automatically. Check the existing registry before adding a duplicate.
Registration records how a service is managed; permission to register doesn't
imply permission to install, start or change it. Agree those actions separately
with the machine's owner.

The following names and commands are fictional examples. `add` records a command
without executing it or creating a plist:

```sh
fleet ls
fleet add example-greeting --group examples --no-keep-alive -- /bin/echo hello
fleet ls examples
```

For an existing user LaunchAgent, `fleet adopt local.example.worker --name
example-worker` records its label and, when available, log path. The label must
already have a plist in the configured LaunchAgents directory or appear in
launchctl's job list. Adoption doesn't bootstrap or restart the job. It doesn't
infer a port or an application health check. Supply those fields explicitly.

Use `fleet edit` to edit and validate the registry. A service has a unique `name`
and `label`, and exactly one of:

- `run`: an `argv` array, with optional `cwd`, `env` and `keepAlive`. Fleet can
  generate its plist. `keepAlive` defaults to true, so use `--no-keep-alive` for
  one-shot commands.
- `adopt`: an existing plist, optionally with an `install` shell command used
  when the plist is missing. The JSON form also accepts a custom `plist` path.

Optional service fields are `group`, `port`, `probe`, `log`, `url` and
`url_from_log`. Run `fleet help` for CLI flags. `FLEET_CONFIG` changes the registry
path; `FLEET_AGENTS_DIR` changes the plist directory. These overrides still use
the current user's launchd domain. `FLEET_LAUNCHCTL` selects the launchctl
executable; `FLEET_SETTLE` controls the delay before status after starting or
restarting jobs (default: two seconds).

`FLEET_CONFIG` and `FLEET_AGENTS_DIR` expand `~` only. Log paths, custom adopted
plist paths, `cwd` and configured environment values expand `~` and `$VAR`.
`argv` strings receive that expansion only when they start with `~`; bare
program names are resolved through PATH. Explicit URLs aren't expanded.
Generated argv runs without a shell; adopted installer commands run through
`/bin/sh`.

## Inspect and control

`fleet ls` lists registry entries without querying launchd or HTTP. Most target
arguments accept service names, groups or `all`, in registry order with
duplicates removed. **Omitting targets applies to every registered service** for
status and lifecycle commands. Prefer explicit names for changes.

After reviewing and authorizing the action:

```sh
fleet up example-greeting
fleet down example-greeting
fleet remove example-greeting
```

`up` creates a missing generated plist, enables the job, bootstraps it if needed
and requests a start. It preserves an existing plist. `down` unloads and disables
the job across logins; `up` re-enables it. `restart` uses `kickstart -k` and
requires a loaded job. `reinstall` stops the job, removes its plist and starts
again, regenerating a `run` plist or invoking the adopted job's installer. Fleet
refuses to reinstall an adopted job without an install command.

`remove` stops and disables the job before removing its registry entry. It also
deletes a generated plist unless `--keep-plist` is supplied. Adopted plists are
retained. Logs and application data aren't removed.

Lifecycle exit codes don't report every launchctl failure; exit 0 doesn't prove
a job stopped or unloaded. Check the printed outcome and service status before
relying on a control operation, especially removal or reinstall: registry/plist
changes can proceed after a failed unload.

`fleet status example-greeting` (or running Fleet without a command) queries
launchd and probes any configured HTTP port on `127.0.0.1`. The state column means:

- `absent`: no plist found and no loaded job reported.
- `off`: plist exists but launchd doesn't report a loaded job.
- `loaded`: launchd reports the job without a PID.
- `running`: launchd reports a PID.

The HTTP column reports a response code, including error and redirect codes,
using `probe` or `/` with a two-second request timeout. `-` means no configured
port or no response code. It doesn't prove application readiness or that the
listener belongs to the job. A PID alone doesn't prove health either. Control
commands also print this status, so they can make HTTP requests.

`fleet logs example-worker` tails the configured log; `-f` follows indefinitely.
`fleet url example-worker` prints a URL and `fleet open example-worker` opens it
in the system browser. URL selection uses the last regex match in a configured
log, then an explicit URL, then the configured loopback port.

## Trust and private data

Use only trusted registry files. Adopted installers execute through `/bin/sh`;
generated jobs run their configured argv and environment. Fleet provides no
approval system or sandbox. Generated plists carry PATH, HOME, USER and LOGNAME
from the invoking environment, plus configured overrides. Protect the registry,
plists and logs with appropriate filesystem permissions. Avoid putting secrets
in command arguments or plist environments.

Logs and extracted URLs can contain access tokens. Review output before sharing
it or taking screenshots. Fleet reads the whole configured log when extracting
a URL. `edit` validates after the editor exits; an invalid edit remains on disk
until corrected. Keep a private backup before changing a working registry.

## Source and tests

`bin/fleet` contains the CLI. `tests/test_fleet.py` covers configuration, plist
generation, target resolution and CLI behavior; `tests/fake_launchctl` emulates
launchctl for integration tests.

For a small offline subset, from the source root with a temporary HOME and
TMPDIR of your own:

```sh
PYTHONPATH=tests python3 -m unittest test_fleet.Validation test_fleet.Groups \
  test_fleet.UrlFromLog test_fleet.PlistGeneration.test_fields \
  test_fleet.PlistGeneration.test_defaults_keepalive_and_no_log \
  test_fleet.PlistGeneration.test_tilde_and_bare_program
```

The full suite (`python3 -m unittest discover -s tests`) uses subprocesses and
fake launchctl. It also invokes `plutil` when available, and some status tests
make real loopback HTTP requests. Run it only in a disposable, isolated test
environment with no sensitive local listeners. Native launchd behavior still
needs testing on macOS; passing mocked tests doesn't establish native behavior.

## License

Fleet's owned source, tests and documentation use the [MIT license](LICENSE).
The included AI-generated comic artwork is offered under MIT to the extent
applicable. See [RIGHTS.md](RIGHTS.md) for scope and outside terms.
