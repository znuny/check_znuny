<div align="center">
  <a href="https://www.znuny.org">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://www.znuny.com/assets/znuny-logo.svg">
      <img alt="Znuny" src="https://www.znuny.com/assets/znuny-logo-black.svg" width="300">
    </picture>
  </a>
</div>

# check_znuny

Icinga2/Nagios plugin for the Znuny `HealthStatus` GenericInterface webservice
(`Webservice/HealthStatus/HealthStatusGet`). Python 3 standard library only —
no `pip install` required.

Each invocation evaluates exactly one `--section` of the response, so each
aspect of Znuny's health (daemon, communication log, recurring tasks, ...) can
be its own Icinga2 service with its own thresholds and notification policy.

## Requirements

- Python 3 on every host that will execute the check (no third-party packages)
- Network access from that host to the Znuny HealthStatus endpoint
- A Znuny HealthStatus API key: SysConfig `Znuny::HealthStatus::API::Key`
  (min. 20 characters) must be set on the monitored Znuny instance
- Icinga2 (or any Nagios-compatible core), if used as a monitoring check

## Installation

### 1. Install the plugin

`check_znuny.py` has no dependencies beyond Python 3, so the quickest way to
get it onto an Icinga2 master/satellite is to fetch the single file directly
— no clone needed. `PluginDir` is looked up from Icinga2 itself, so it lands
in the right place regardless of distro:

```
PLUGINDIR="$(icinga2 variable get PluginDir)"
curl -sSL -o "$PLUGINDIR/check_znuny.py" \
  https://raw.githubusercontent.com/znuny/check_znuny/main/check_znuny.py
chmod +x "$PLUGINDIR/check_znuny.py"
```

May need `sudo` depending on who owns `PluginDir`.

Verify it runs standalone before wiring up Icinga2 (see [Usage](#usage)
below):

```
"$PLUGINDIR/check_znuny.py" -u <health-status-url> -k <api-key> -s daemon
```

### 2. Install the Icinga2 config

Fetch the two files under `icinga2/` into your config tree (e.g.
`/etc/icinga2/zones.d/<zone>/` or `conf.d/`):

```
curl -sSL -o /etc/icinga2/zones.d/<zone>/znuny_commands.conf \
  https://raw.githubusercontent.com/znuny/check_znuny/main/icinga2/znuny_commands.conf
curl -sSL -o /etc/icinga2/zones.d/<zone>/znuny_services.conf \
  https://raw.githubusercontent.com/znuny/check_znuny/main/icinga2/znuny_services.conf
```

(or, from a full checkout: `cp icinga2/znuny_commands.conf
icinga2/znuny_services.conf /etc/icinga2/zones.d/<zone>/`), then edit the
copy of `znuny_services.conf` to point `vars.znuny_healthstatus_url` /
`vars.znuny_healthstatus_api_key` (or `vars.znuny_healthstatus_api_key_file`)
at your own instance.

Validate and reload:

```
icinga2 daemon -C
systemctl reload icinga2
```

`znuny_commands.conf` defines the `znuny_health` `CheckCommand` once;
`znuny_services.conf` auto-generates all 7 services for every host that sets
`vars.znuny_healthstatus_url` plus an API key — see
[Icinga2 configuration files](#icinga2-configuration-files) below for how to
add more hosts and override thresholds.

### 3. (Optional) Icinga2 Director

See [Icinga2 Director](#icinga2-director) below.

## Usage

```
check_znuny.py -u <health-status-url> (-k <api-key> | --api-key-file <path>) -s <section> [options]
```

```
./check_znuny.py -u https://FQDN/znuny/nph-genericinterface.pl/Webservice/HealthStatus/HealthStatusGet \
                  -k API-KEY -s commlog --log-hours 12
```

Exit codes follow the standard Nagios/Icinga2 plugin convention: `0` OK, `1`
WARNING, `2` CRITICAL, `3` UNKNOWN (also used for connection/API errors, e.g.
an invalid API key).

Full option reference (`check_znuny.py --help`):

```
-u, --url URL           Full URL of the HealthStatusGet endpoint.
-k, --api-key API_KEY   API key configured in SysConfig "Znuny::HealthStatus::API::Key".
--api-key-file PATH     Path to a file whose (stripped) content is the API key
                        (mutually exclusive with -k; keeps the key out of the process list).
-s, --section SECTION   daemon | commlog | cron | genericagent | future-tasks
                        | unprocessed-emails | system (see table below).
--log-hours N           Communication log history window in hours. Only used by -s commlog;
                        defaults to whatever the webservice itself defaults to (currently 1h).
--timeout N             Network timeout in seconds. Default: 10.
--insecure              Do not verify the server TLS certificate.
--no-proxy              Ignore proxy environment variables (http_proxy/https_proxy).
-w, --warning SPEC      Warning threshold (Nagios range, e.g. "10", "10:20", "~:10", "@10:20").
                        Meaning depends on --section (see table below).
-c, --critical SPEC     Critical threshold (Nagios range). See --warning.
--warning-runtime N     (-s genericagent only) Warning threshold in seconds for a task's
                        last run duration (LastWorkerRunningTime).
--critical-runtime N    (-s genericagent only) Critical threshold in seconds, same field.
```

### Sections and thresholds

| `--section`          | Covers                                                | Alerts on |
|-----------------------|--------------------------------------------------------|-----------|
| `daemon`              | `Daemon`                                                | daemon not running |
| `commlog`             | `CommunicationLog` (counters + per-account status)      | `Health` != OK, `--warning`/`--critical` on Failed count, any account `Status` != OK |
| `cron`                | `RecurrentCronTasks`                                    | `LastWorkerStatus: Fail`; `--warning`/`--critical` on failed-task count (default warning: any failure) |
| `genericagent`        | `RecurrentGenericAgentTasks`                            | same as `cron`, plus optional `--warning-runtime`/`--critical-runtime` (seconds) on a task's last run duration |
| `future-tasks`        | `TasksToBeExecutedInFuture`                             | informational unless `--warning`/`--critical` set (queue size) |
| `unprocessed-emails`  | `UnprocessedEmails`                                     | `Health` != OK |
| `system`              | `MailQueue` + `Sessions`                                | informational unless `--warning`/`--critical` set (mail queue length); Sessions is perfdata only |

Any other/unexpected key in the API response (e.g. add-ons like
`Znuny-PrometheusExporter`, currently-running worker tasks) isn't covered by
any of the 7 sections above — this is a deliberate scope decision, not an
oversight; ask if you want a section added for one of those.

`--warning-runtime`/`--critical-runtime` (genericagent only) compare against
each task's `LastWorkerRunningTime` — the duration of its most recent
completed run — not a "currently stuck" signal (the API doesn't expose live
in-progress duration per recurring task).

Both `CommunicationLog` response shapes seen across Znuny versions are
supported: counters nested under `CommunicationLog.Communications`, or
directly on `CommunicationLog` alongside `Accounts`.

## Icinga2 configuration files

`icinga2/znuny_commands.conf` — single `CheckCommand "znuny_health"` definition
covering all 7 sections via `$znuny_health_section$`.

`icinga2/znuny_services.conf` — example `Host` (the staging endpoint) plus a single
`apply Service "znuny-health-" for (section => display_label in {...})` rule
(iterating a dictionary so Icinga2 auto-appends each key to the name prefix —
the loop variable can't be referenced directly in the name expression) that
generates all 7 services automatically for every host defining
`vars.znuny_healthstatus_url` and either `vars.znuny_healthstatus_api_key` or
`vars.znuny_healthstatus_api_key_file` — add more hosts by just setting those vars,
no per-host Service objects needed. Object names stay machine-friendly
(`znuny-health-cron`); the dictionary value becomes `display_name` (e.g.
"Znuny Health CronTask") for a readable label in Icinga Web 2:

| `--section`          | `display_name`             |
|-----------------------|-----------------------------|
| `daemon`              | Znuny Health Daemon         |
| `commlog`             | Znuny Health CommunicationLog |
| `cron`                | Znuny Health CronTask       |
| `genericagent`        | Znuny Health GenericAgents  |
| `future-tasks`        | Znuny Health FutureTask     |
| `unprocessed-emails`  | Znuny Health Unprocessed Emails |
| `system`              | Znuny Health System         |

Threshold defaults live in that apply rule, and any host can override them
via section-scoped custom vars (the plugin never sees these directly — the
apply rule translates them into the generic `$znuny_health_warning$` etc.
per service):

| Host var                                          | Default        |
|----------------------------------------------------|----------------|
| `znuny_health_commlog_log_hours`                    | `12`           |
| `znuny_health_commlog_warning` / `_critical`         | unset (opt-in) |
| `znuny_health_cron_warning` / `_critical`            | `"0"` / unset  |
| `znuny_health_genericagent_warning` / `_critical`    | `"0"` / unset  |
| `znuny_health_genericagent_warning_runtime` / `_critical_runtime` | unset (opt-in) |
| `znuny_health_future_tasks_warning` / `_critical`    | unset (opt-in) |
| `znuny_health_system_warning` / `_critical`          | unset (opt-in) |

Only `cron`/`genericagent` get a non-empty default (warn on any failed
task) — mail queue depth, comm-log failure volume and future-task backlog
size vary too much by installation to guess a safe universal number, so
those stay informational until a host opts in.

## Icinga2 Director

`icinga2/director-basket-znuny-health.json` is a
[Director configuration basket](https://icinga.com/docs/icinga-director/latest/doc/30-Configuration-Baskets/)
containing the `znuny_health` Command and its data fields (one per CLI
argument, named `znuny_health_*` to match `znuny_commands.conf`).

Import it under **Icinga Director → Configuration → Import/Export → Basket
Snapshots → Import basket** (or `icingacli director basket restore <
director-basket-znuny-health.json` on the Director host), or pull it
straight from GitHub without a checkout:

```
curl -sSL https://raw.githubusercontent.com/znuny/check_znuny/main/icinga2/director-basket-znuny-health.json \
  | icingacli director basket restore
```

Then:

1. Open the imported **znuny_health** command and fix up the `command` field to
   the actual plugin path on your masters/satellites (e.g.
   `/usr/lib/nagios/plugins/check_znuny.py`) — the basket ships it as a bare
   filename, same as most other community command baskets.
2. All fields import as plain string inputs. Optionally change the datatype of
   `znuny_health_insecure`/`znuny_health_no_proxy` to Boolean, `znuny_health_section`
   to a fixed-list/enum, and the threshold/numeric fields to Number, via
   **Fields** on the command if you want nicer form widgets — functionally the
   plugin only needs strings, so this is cosmetic.
3. Baskets intentionally don't include Service objects/templates (Director
   baskets cover templates and commands, not concrete host/service instances).
   Create 7 **Service Templates** (or apply rules) in Director, one per
   section, each fixing `znuny_health_section` to one of `daemon`, `commlog`,
   `cron`, `genericagent`, `future-tasks`, `unprocessed-emails`, `system`, and
   filling in `znuny_healthstatus_url` plus `znuny_healthstatus_api_key` (or
   `znuny_healthstatus_api_key_file`) — matching `icinga2/znuny_services.conf`. Set each
   template's **Display name** to match the table above (e.g. "Znuny Health
   CronTask") for the same readable labels in Icinga Web 2.

## License

Copyright (C) 2026 Znuny GmbH, https://znuny.com/

Licensed under the GNU General Public License v3.0 or later — see
[LICENSE](LICENSE) for the full text.
