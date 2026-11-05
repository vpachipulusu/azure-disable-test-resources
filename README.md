# weekend-saver

A [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) workflow that
disables Azure resources on Friday evening and enables them again on Monday morning, with a
local dashboard to choose the resources, watch the workflow run, approve plans and set the
schedule. Nothing outside the resources you choose is ever stopped or started.

> Status: covered by unit, workflow and API tests with a faked Azure, and the dashboard was
> exercised in a browser in demo mode. It has **not** been run against a live subscription
> yet. Start with the demo, then a dry run on a throwaway resource.

## Quick start

```bash
uv sync
```

Try it with made-up resources first (no Azure involved):

```bash
uv run weekend-saver dashboard --demo
```

Then for real: sign in and open the dashboard.

```bash
az login
```

```bash
uv run weekend-saver dashboard
```

The terminal prints a link such as `http://127.0.0.1:8765/?token=…` and opens it. The link
works once; restart the dashboard to get a new one. In the page:

1. **Manage resources** lists what your account can see; tick the ones to manage and save.
2. Tick **Preview only** and press **Disable** to see what would happen.
3. Press **Disable** or **Enable** for real; you get the plan and an Approve / Reject choice.
4. Turn the **Schedule** on to have it run by itself (see the limits below).

## Signing in

There is no role definition or subscription ID to configure. weekend-saver acts as **you**:

- If you are signed in with the Azure CLI (`az login`), that session is used.
- Otherwise use the dashboard's **Sign in** button or `uv run weekend-saver login`, which opens
  a browser sign-in and caches it on this PC (encrypted by the OS). Some organisations block
  this generic sign-in; `az login` is the reliable path.

You still need rights to start and stop the resources (for example Contributor on their
resource groups). Because your account can usually do far more than that, the "only what you
ticked" limit is enforced by this tool, not by Azure.

### Where your settings are kept

The selection (`resources.yaml`), state, schedule, run history and sign-in record live in a
folder only your account can normally reach:

- Windows: `%LOCALAPPDATA%\weekend-saver`
- Linux/macOS: `~/.local/state/weekend-saver`

They are not kept in the project folder on purpose: the schedule and the selection drive runs
that nobody approves, and a shared project folder is often writable by other accounts. Use
`--data-dir` to choose another folder, and keep it private.

## The dashboard

- **Workflow** shows the six steps and lights each one up as it runs:

  ```
  Load targets -> Resolve state -> Approval -> Apply -> Verify -> Report
                       ^               |
                       +-- approved ---+   (planned again before applying)
  ```

- **Managed resources** shows live power state, order, and which ones weekend-saver switched off.
- **Schedule** fires the weekly disable and enable. Times are this PC's local time.
- **Recent runs** keeps what happened, including unattended runs; click one for the detail.

### What the built-in schedule can and cannot do

The schedule lives inside the dashboard process, so:

- The dashboard must be **running**, the PC **awake**, and your sign-in still valid at both times.
- If the dashboard was closed at enable time, the enable runs as soon as it is opened again.
  A missed disable is not run late; it is only reported.
- A scheduled enable that fails is tried again every 5 minutes, up to 6 times (the usual
  cause is no network right after the PC wakes). If it still fails, it shows as failed in
  Recent runs and in the webhook, if set. The resources stay off until you act.
- Scheduled actions that never ran (missed, or the selection could not be read) also appear
  in Recent runs as "not run".
- A plan left waiting for approval is rejected after 30 minutes, or as soon as a scheduled
  run needs to start, so a forgotten dialog cannot block the schedule.

If you need runs that do not depend on your PC, use the CLI from a scheduler instead
([Unattended runs without the dashboard](#unattended-runs-without-the-dashboard)).

### Safety of the local page

The page can stop and start resources with your rights, so it only listens on `127.0.0.1`,
needs the one-time link printed at start-up, and refuses changes that do not come from the
page itself. Close the dashboard when you are done on a shared PC.

### Inspecting the workflow in DevUI (optional)

Microsoft's DevUI can show the same workflow for debugging. It is a development sample and a
pre-release package, and its input form can request pre-approved runs, so weekend-saver only
ever starts it with made-up resources, never with your Azure sign-in:

```bash
uv sync --extra devui
```

```bash
uv run weekend-saver devui
```

The terminal prints the token DevUI's page asks for.

## How a run decides what to do

- `disable` only acts on resources that are running, and records them in the state file
  (`state.json` in the settings folder) *before* acting. A resource whose state cannot be determined
  is reported as failed, not silently skipped.
- `enable` only starts resources recorded in that file, so anything that was already off on
  Friday stays off.
- `enable` fails instead of quietly doing nothing when the state file is missing, or when its
  last disable run is more than 6 days old. The dashboard's **Advanced** option (CLI:
  `--ignore-prior-state`) starts every managed resource that is stopped.
- If a resource was disabled and then removed from the selection, `enable` reports it as
  failed until it is running again or selected again. It is never started for you.
- **Order**: lower numbers are disabled first and enabled last, so give a database a higher
  number than the apps that use it. Equal numbers run in parallel, five at a time.
- Tag a resource `weekend-saver=skip` to keep `disable` away from it. The tag does not stop
  `enable` from restoring something weekend-saver itself switched off.
- After you approve, the run is planned again from the current state of Azure, so what is
  applied can differ from the plan you were shown. The report shows what was actually done.
- Failure lines name the error type only; the full Azure message is in the terminal log.

## Supported resource types

| Type | Disable | Enable | What you still pay for while disabled |
|---|---|---|---|
| `Microsoft.Compute/virtualMachines` | deallocate | start | disks, static public IPs |
| `Microsoft.Compute/virtualMachineScaleSets` | deallocate | start | disks |
| `Microsoft.ContainerService/managedClusters` | stop | start | disks, load balancer IPs |
| `Microsoft.Web/sites` | stop | start | **the whole App Service plan** (stopping an app alone saves nothing) |
| `Microsoft.DBforPostgreSQL/flexibleServers` | stop | start | storage, backups |
| `Microsoft.DBforMySQL/flexibleServers` | stop | start | storage, backups |
| `Microsoft.Network/applicationGateways` | stop | start | nothing while stopped |

A VM that is stopped from inside the OS but not deallocated is still billed. It counts as
"already stopped" and is left alone. Azure restarts a stopped flexible server by itself after
a few days; a weekend is well inside that.

## Command line

Everything the dashboard does is also available from the CLI. It shares the dashboard's state
file by default; point `--resources` at the dashboard's `resources.yaml` (in the settings
folder above) or at a file of your own. Besides `az login` and the browser sign-in, the CLI
also accepts a managed identity, workload identity or service-principal environment
variables, so it can run unattended.

```bash
uv run weekend-saver disable --resources resources.yaml --dry-run
```

```bash
uv run weekend-saver disable --resources resources.yaml
```

```bash
uv run weekend-saver enable --resources resources.yaml
```

`resources.yaml` can also be written by hand (see [resources.example.yaml](resources.example.yaml)):

```yaml
resources:
  - /subscriptions/<guid>/resourceGroups/<group>/providers/Microsoft.Web/sites/<name>
  - id: /subscriptions/<guid>/resourceGroups/<group>/providers/Microsoft.DBforPostgreSQL/flexibleServers/<name>
    order: 10
```

| Exit code | Meaning |
|---|---|
| 0 | completed, dry run, or plan rejected by you |
| 1 | at least one resource failed, or an unexpected error |
| 2 | invalid input: bad resource list, not signed in, missing or stale state file, approval needed but no terminal |
| 3 | paused awaiting approval (only with `--checkpoint-dir`) |

Set `WEEKEND_SAVER_WEBHOOK_URL` (https only) to have every report posted as `{"text": "..."}`,
the shape Slack and Teams incoming webhooks accept. Treat the URL as a secret.

### Approving later from the CLI

With `--checkpoint-dir DIR` and no `--yes`, an unattended CLI run prints its plan, exits 3,
and leaves the approval pending in a workflow checkpoint:

```bash
uv run weekend-saver resume --checkpoint-dir DIR --approve
```

Use `--reject` to discard it. An approval pending for more than 12 hours is refused. Pass the
same `--state-file` as the original run. Checkpoints are pickled: anyone who can write to that
directory can run code as you, so keep it as private as a credential.

## Unattended runs without the dashboard

For runs that must not depend on your PC, schedule the CLI with `--yes` under a separate
identity that has only start/stop rights:

- [deploy/weekend-saver-role.json](deploy/weekend-saver-role.json) is a least-privilege role
  for that identity. Assign it on the test resource groups only, never a whole subscription.
- [deploy/github-actions-example.yml](deploy/github-actions-example.yml) is an untested cron
  example; read the checklist at the top before using it.

Neither file is needed for the dashboard, and neither has been exercised against live Azure.

## Development

```bash
uv run pytest --cov
```

```bash
uv run ruff check .
```

Tests use fake handlers and never call Azure. To add a resource type, add one
`AzureOperations` entry in [src/weekend_saver/azure_handlers.py](src/weekend_saver/azure_handlers.py);
the dashboard's picker picks it up automatically. Add its actions to the role file by hand.
