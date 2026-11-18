# Compatibility

**Runtime compatibility is not catalog eligibility.** Read the [policy disclaimer](catalog-policy.md); a green validator or CI run does not waive the credential-policy blocker.

## Tested combination

The CI workflow (`.github/workflows/ci.yml`) covers exactly this setup:

| Component | Version |
|-----------|---------|
| Hermes Agent | commit `f7e2898aaefc8f967dbf7345e3999a65672fe60e` (reports `0.23.2`, a canary build after the `v0.23.2` tag) |
| Python | 3.14, from Hermes' `uv.lock` (`uv sync --locked --python 3.14 --no-dev`) |
| Native `gws` | 0.22.5 (release tarball, SHA-256 checked) |
| PyYAML | 6.0.3 (same pin as Hermes' `uv.lock`) |
| aiohttp | 3.14.5 (same pin as Hermes' `messaging` extra; required by the watch worker) |

The suite has also passed against runtime commit
`aaec1339aa325888919c522911c54a7e684c7f23`. This does not establish support for
every intervening release. Matching version numbers alone do not count. The
`requires_hermes` floor in `plugin.yaml` is a gate that stops installs on
Hermes versions known to be too old. It does not mean every newer version has
been tested.

The plugin depends on these host surfaces:
- the `pre_tool_call` `approve` directive and the approval gate in
  `model_tools.handle_function_call` (covered by `tests/test_api_policy.py::HostDispatchTests`)
- `ctx.register_tool`, `ctx.register_hook`, `ctx.inject_message`
- the private imports `gateway.session_context.bound_session_key` and
  `hermes_cli.webhook`, which `hermes plugins validate` flags because any
  release can break them

## What CI runs

1. Checks out Hermes at the pinned commit and installs its locked runtime
   environment, then installs PyYAML and aiohttp explicitly.
2. Installs native `gws` 0.22.5.
3. Runs every unittest with `HERMES_AGENT_SRC` set to that checkout, using an
   empty `HOME`/`HERMES_HOME` and no `GOOGLE_*` variables. The job fails if any
   test is skipped. That makes the real-host approval dispatch tests and the
   native `gws --dry-run` tests mandatory. No Google credentials are present,
   and no Google API calls are made.
4. Runs `hermes plugins validate <plugin> --json` and `hermes plugins doctor <plugin> --ci`.

## Verify locally

```bash
SCRATCH=${TMPDIR:-/tmp}/gws-ci
git clone https://github.com/NousResearch/hermes-agent "$SCRATCH/hermes-agent"
git -C "$SCRATCH/hermes-agent" checkout f7e2898aaefc8f967dbf7345e3999a65672fe60e
(cd "$SCRATCH/hermes-agent" && uv sync --locked --python 3.14 --no-dev &&
  uv pip install --python .venv/bin/python PyYAML==6.0.3 aiohttp==3.14.5)
H="$SCRATCH/hermes-agent"; PY="$H/.venv/bin/python"

# from the plugin checkout; gws 0.22.5 must be on PATH
env -i PATH="$PATH" HOME="$(mktemp -d)" HERMES_HOME="$(mktemp -d)" \
  HERMES_DISABLE_LAZY_INSTALLS=1 HERMES_AGENT_SRC="$H" PYTHONPATH="$H" \
  "$PY" -m unittest discover -s tests -v      # check that the output shows no "skipped"

(cd "$H" && HERMES_HOME="$(mktemp -d)" HERMES_DISABLE_LAZY_INSTALLS=1 \
  "$PY" -m hermes_cli.main plugins validate "$OLDPWD" --json &&
  HERMES_HOME="$(mktemp -d)" HERMES_DISABLE_LAZY_INSTALLS=1 \
  "$PY" -m hermes_cli.main plugins doctor "$OLDPWD" --ci)
```
