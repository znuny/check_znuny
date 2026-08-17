# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-14

### Added

- `check_znuny.py`: Icinga2/Nagios plugin for the Znuny `HealthStatus`
  GenericInterface webservice (`Webservice/HealthStatus/HealthStatusGet`).
  Python 3 standard library only, no third-party dependencies.
- `--section` argument to check exactly one aspect of Znuny's health per
  invocation: `daemon`, `commlog` (communication log + per-account status),
  `cron` (`RecurrentCronTasks`), `genericagent` (`RecurrentGenericAgentTasks`,
  with an optional last-run-duration threshold), `future-tasks`
  (`TasksToBeExecutedInFuture`), `unprocessed-emails`, and `system`
  (`MailQueue` + `Sessions`).
- `-w/--warning` and `-c/--critical` Nagios range thresholds, with section-
  appropriate defaults (e.g. warn on any failed recurring task).
- `-k/--api-key` and `--api-key-file` (to keep the API key out of the
  process list), plus `--insecure`, `--no-proxy`, `--timeout`, `--log-hours`.
- Support for both `CommunicationLog` response shapes seen across Znuny
  versions (counters nested under `Communications`, or directly on
  `CommunicationLog`).
- `icinga2/znuny_commands.conf` and `icinga2/znuny_services.conf`: a
  `CheckCommand` definition and an `apply Service` rule that generates all 7
  services automatically for any host with a URL and API key configured,
  with per-section threshold defaults overridable via host custom variables.
- `icinga2/director-basket-znuny-health.json`: an Icinga Director
  configuration basket for the `znuny_health` command and its fields.
- Licensed under the GNU General Public License v3.0 or later.
