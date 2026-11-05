# weekend-saver

A [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) workflow that
disables Azure resources on Friday evening and enables them again on Monday morning.
You list the resources; nothing outside that list is ever stopped or started.

> Status: covered by unit and workflow tests with a faked Azure. It has **not** been run
> against a live subscription yet. Try `--dry-run` and a throwaway resource first.

## Quick start

```bash
uv sync
```

```bash
cp resources.example.yaml resources.yaml
```

Edit `resources.yaml`, sign in (`az login`), then preview and run:

```bash
uv run weekend-saver disable --resources resources.yaml --dry-run
```

```bash
uv run weekend-saver disable --resources resources.yaml
```

```bash
uv run weekend-saver enable --resources resources.yaml
```

Without `--yes` the plan is printed and you are asked to approve it.

## Resource list

```yaml
resources:
  - /subscriptions/<guid>/resourceGroups/<group>/providers/Microsoft.Web/sites/<name>
  - id: /subscriptions/<guid>/resourceGroups/<group>/providers/Microsoft.DBforPostgreSQL/flexibleServers/<name>
    order: 10
```

`order` (default 0): lower numbers are disabled first and enabled last, so give a database
a higher number than the apps that use it. Resources with the same order run in parallel.

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

Notes:

- A VM that is stopped from inside the OS but not deallocated is still billed. It counts
  as "already stopped" and is left alone, so it is not started on Monday either.
- Azure restarts a stopped flexible server by itself after a few days; a weekend is well inside that.

## How it works

```
LoadTargets -> ResolveState -> Approval -> Apply -> Verify -> Report
                    ^              |
                    +-- approved --+   (planned again before applying)
```

- **LoadTargets** validates every ARM ID and rejects unsupported types before any Azure call.
- **ResolveState** reads each resource's current state and decides what to do.
- **Approval** pauses for a yes/no (`ctx.request_info`), unless `--yes` or `--dry-run`.
  After a yes the run is planned again from current state, so what is applied reflects
  Azure at that moment rather than when the plan was shown.
- **Apply** runs the stop/start calls in `order` groups, five at a time. One failure does not stop the rest.
- **Verify** re-reads each resource until it reaches the target state.
- **Report** prints the outcome and posts it to a webhook if one is configured.

### Safety rules

- `disable` only acts on resources that are running, and records them in the state file
  (default `.weekend-saver/state.json`) *before* acting. A resource whose state cannot be
  determined is reported as failed, not silently skipped.
- `enable` only starts resources recorded in that file, so anything that was already off
  on Friday stays off.
- `enable` exits 2 instead of quietly doing nothing when the state file is missing, or
  when its last disable run is more than 6 days old (a left-over from an earlier week).
  Point `--state-file` at the right file, or pass `--ignore-prior-state` to start every
  listed resource that is stopped.
- If a resource was disabled and then removed from your list, `enable` reports it as
  failed until it is running again or back on the list. It is never started for you.
- Tag a resource `weekend-saver=skip` to keep `disable` away from it. The tag does not stop
  `enable` from restoring something weekend-saver itself switched off.
- Failure lines name the error type only. The full Azure message is in the log (stderr),
  which keeps tenant and principal IDs out of chat webhooks.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | completed, dry run, or plan rejected by you |
| 1 | at least one resource failed, or an unexpected error |
| 2 | invalid input: bad resource list, missing or stale state file, approval needed but no terminal |
| 3 | paused awaiting approval (only with `--checkpoint-dir`) |

## Scheduling

The workflow does not schedule itself; run the CLI from any scheduler with `--yes`.

- **Windows Task Scheduler:** two tasks, Friday evening and Monday morning, each running
  `uv run --locked weekend-saver <disable|enable> --resources resources.yaml --yes` from this
  folder. The state file persists on disk between them.
- **GitHub Actions:** see [deploy/github-actions-example.yml](deploy/github-actions-example.yml).
  It is an untested example; read the checklist at the top (pin actions to commit SHAs,
  scope the identity) before using it.

Set `WEEKEND_SAVER_WEBHOOK_URL` (https only) to have the report posted as `{"text": "..."}`,
the shape Slack and Teams incoming webhooks accept. Treat the URL as a secret.

### Approving later

With `--checkpoint-dir DIR` and no `--yes`, an unattended run prints its plan, exits 3,
and leaves the approval pending in a workflow checkpoint:

```bash
uv run weekend-saver resume --checkpoint-dir DIR --approve
```

Use `--reject` to discard it. Things to know:

- Approving re-plans from the current state of Azure and applies that, so the result can
  differ from the plan printed earlier. The final report shows what was actually done.
- An approval left pending for more than 12 hours is refused; start a fresh run.
- Pass the same `--state-file` to `resume` as to the original run.
- Checkpoints are pickled. Anyone who can write to that directory can run code as the
  identity weekend-saver uses, so keep it as private as a credential.

## Authentication and permissions

`DefaultAzureCredential` is used, so `az login`, a managed identity, or workload identity
federation all work. On a shared scheduler host, set `AZURE_TOKEN_CREDENTIALS` (see the
azure-identity docs) so it cannot fall back to someone's personal `az login`.

The resource list is enforced by this tool, not by Azure: the identity can start and stop
anything of these types wherever the role is assigned. Keep that scope small.

1. Replace `<SUBSCRIPTION_ID>` in [deploy/weekend-saver-role.json](deploy/weekend-saver-role.json)
   and create the role:

```bash
az role definition create --role-definition deploy/weekend-saver-role.json
```

2. Assign it on each **test resource group**, never on the subscription or anything production:

```bash
az role assignment create --assignee <principal-id> --role "Weekend Saver Operator" --scope /subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<test-rg>
```

The role file has not been exercised against live Azure. If a start or stop is denied
(Application Gateway is the likeliest, since starting it joins subnets and public IPs),
the resource shows as failed and the missing action is in the log.

## Development

```bash
uv run pytest --cov
```

```bash
uv run ruff check .
```

Tests use fake handlers and never call Azure. To add a resource type, add one
`AzureOperations` entry in [src/weekend_saver/azure_handlers.py](src/weekend_saver/azure_handlers.py)
and its actions to the role file.
