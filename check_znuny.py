#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# check_znuny.py - Icinga2/Nagios plugin for the Znuny HealthStatus GenericInterface webservice.
# Copyright (C) 2026  Znuny GmbH, https://znuny.com/
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Fetches Webservice/HealthStatus/HealthStatusGet and evaluates exactly one
# --section of the response per invocation, so each aspect of Znuny's health
# (daemon, communication log, recurring tasks, ...) can be its own Icinga2
# service with its own thresholds and notification policy.

import argparse
import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request

STATE_OK = 0
STATE_WARNING = 1
STATE_CRITICAL = 2
STATE_UNKNOWN = 3
STATE_NAMES = ('OK', 'WARNING', 'CRITICAL', 'UNKNOWN')

__author__ = 'Znuny GmbH, https://znuny.com/'
__version__ = '1.0.0'

DEFAULT_TIMEOUT = 10

SECTIONS = ('daemon', 'commlog', 'cron', 'genericagent', 'future-tasks', 'unprocessed-emails', 'system')

HEALTH_STATE_MAP = {'ok': STATE_OK, 'warning': STATE_WARNING, 'critical': STATE_CRITICAL}

DURATION_UNIT_SECONDS = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400}
DURATION_RE = re.compile(r'^([\d.]+)\s*(Second|Minute|Hour|Day)\(s\)$', re.IGNORECASE)


class PluginError(Exception):
    def __init__(self, state, message):
        super().__init__(message)
        self.state = state
        self.message = message


class Result:
    """Collects the worst state seen so far plus human-readable messages and perfdata."""

    def __init__(self):
        self.state = STATE_OK
        self.messages = []
        self.perfdata = []

    def escalate(self, state, message):
        if message:
            self.messages.append((state, message))
        if state > self.state:
            self.state = state

    def add_perfdata(self, label, value, uom='', warn='', crit='', min_='', max_=''):
        self.perfdata.append(
            "'{0}'={1}{2};{3};{4};{5};{6}".format(label, value, uom, warn, crit, min_, max_)
        )


def health_state(value):
    return HEALTH_STATE_MAP.get((value or '').strip().lower(), STATE_UNKNOWN)


def is_fail_status(value):
    return (value or '').strip().lower() == 'fail'


def camel_to_snake(name):
    return ''.join(('_' + c.lower()) if c.isupper() else c for c in name).lstrip('_')


def parse_duration_seconds(text):
    """Parse a Znuny '_Seconds2String' formatted duration ('< 1 Second', '2.0 Minute(s)', ...).

    Returns None for unparseable/absent values (e.g. 'N/A' - task never ran yet).
    """
    text = (text or '').strip()
    if not text:
        return None
    if text.lower().startswith('<'):
        return 0.5
    match = DURATION_RE.match(text)
    if not match:
        return None
    return float(match.group(1)) * DURATION_UNIT_SECONDS[match.group(2).lower()]


def parse_range(spec):
    """Parse a Nagios/Icinga plugin threshold range spec, e.g. '10', '10:20', '~:10', '@10:20'.

    Returns (start, end, invert) or None if spec is empty.
    """
    if not spec:
        return None
    spec = str(spec).strip()
    invert = spec.startswith('@')
    if invert:
        spec = spec[1:]
    if ':' in spec:
        start_s, end_s = spec.split(':', 1)
    else:
        start_s, end_s = '0', spec
    start = float('-inf') if start_s in ('', '~') else float(start_s)
    end = float('inf') if end_s == '' else float(end_s)
    return (start, end, invert)


def range_breached(value, spec):
    """Return True if `value` violates the threshold range `spec` (Nagios semantics)."""
    parsed = parse_range(spec)
    if parsed is None:
        return False
    start, end, invert = parsed
    inside = start <= value <= end
    return inside if invert else not inside


def parse_args():
    parser = argparse.ArgumentParser(
        description='Checks one section of the Znuny HealthStatus GenericInterface webservice (HealthStatusGet).'
    )

    parser.add_argument(
        '-V', '--version',
        action='version',
        version='%(prog)s {0} by {1}'.format(__version__, __author__),
    )

    parser.add_argument(
        '-u', '--url',
        dest='URL',
        required=True,
        help='Full URL of the HealthStatusGet endpoint, '
             'e.g. https://HOST/znuny/nph-genericinterface.pl/Webservice/HealthStatus/HealthStatusGet',
    )

    key_group = parser.add_mutually_exclusive_group(required=True)
    key_group.add_argument(
        '-k', '--api-key',
        dest='API_KEY',
        help='API key configured in SysConfig "Znuny::HealthStatus::API::Key".',
    )
    key_group.add_argument(
        '--api-key-file',
        dest='API_KEY_FILE',
        help='Path to a file whose (stripped) content is the API key. '
             'Use this instead of --api-key to keep the key out of the process list.',
    )

    parser.add_argument(
        '-s', '--section',
        dest='SECTION',
        required=True,
        choices=SECTIONS,
        help='Which part of the health status response to evaluate: '
             'daemon, commlog (communication log + accounts), cron (RecurrentCronTasks), '
             'genericagent (RecurrentGenericAgentTasks), future-tasks (TasksToBeExecutedInFuture), '
             'unprocessed-emails, system (MailQueue + Sessions).',
    )

    parser.add_argument(
        '--log-hours',
        dest='LOG_HOURS',
        type=int,
        default=None,
        help='Number of hours of communication log history to evaluate (API "LogHours" parameter). '
             'Only relevant for --section commlog; defaults to whatever the webservice itself '
             'defaults to (currently 1 hour) if omitted.',
    )

    parser.add_argument(
        '--timeout',
        dest='TIMEOUT',
        type=int,
        default=DEFAULT_TIMEOUT,
        help='Network timeout in seconds. Default: %(default)s',
    )

    parser.add_argument(
        '--insecure',
        dest='INSECURE',
        action='store_true',
        default=False,
        help='Do not verify the server TLS certificate.',
    )

    parser.add_argument(
        '--no-proxy',
        dest='NO_PROXY',
        action='store_true',
        default=False,
        help='Ignore proxy environment variables (http_proxy/https_proxy).',
    )

    parser.add_argument(
        '-w', '--warning',
        dest='WARNING',
        default=None,
        help='Warning threshold (Nagios range). Meaning depends on --section: failed-entry count for '
             'cron/genericagent (default "0", i.e. warn on any failure), Failed count for commlog, '
             'queue length for future-tasks/system. Unused for daemon/unprocessed-emails.',
    )
    parser.add_argument(
        '-c', '--critical',
        dest='CRITICAL',
        default=None,
        help='Critical threshold (Nagios range). See --warning.',
    )

    parser.add_argument(
        '--warning-runtime',
        dest='WARNING_RUNTIME',
        default=None,
        help='(--section genericagent only) Warning threshold in seconds (Nagios range) for a single '
             "task's last run duration (LastWorkerRunningTime).",
    )
    parser.add_argument(
        '--critical-runtime',
        dest='CRITICAL_RUNTIME',
        default=None,
        help='(--section genericagent only) Critical threshold in seconds (Nagios range) for a single '
             "task's last run duration.",
    )

    return parser.parse_args()


def fetch_json(url, payload, timeout, insecure, no_proxy):
    """POST payload as JSON to url, return the parsed response body.

    Raises PluginError on any failure - HTTP transport failure, non-200 HTTP status,
    non-JSON body, or the webservice-level fault envelope ({"faultstring": ...}).
    """
    data = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=data,
        method='POST',
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        },
    )

    ctx = None
    if url.lower().startswith('https://') and insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if no_proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()

    try:
        with opener.open(request, timeout=timeout, context=ctx) if ctx else opener.open(request, timeout=timeout) as response:
            status = response.getcode()
            body = response.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode('utf-8', errors='replace')
    except (urllib.error.URLError, socket.timeout) as exc:
        raise PluginError(STATE_UNKNOWN, 'Could not reach {0}: {1}'.format(url, exc))

    try:
        parsed = json.loads(body)
    except ValueError:
        snippet = body.strip().replace('\n', ' ')[:200]
        raise PluginError(
            STATE_UNKNOWN,
            'HTTP {0}, non-JSON response from {1}: {2}'.format(status, url, snippet or '(empty body)'),
        )

    if isinstance(parsed, dict) and 'faultstring' in parsed:
        raise PluginError(STATE_UNKNOWN, 'API error: {0}'.format(parsed['faultstring']))

    if status != 200:
        raise PluginError(STATE_UNKNOWN, 'HTTP {0} from {1}'.format(status, url))

    if not isinstance(parsed, dict):
        raise PluginError(STATE_UNKNOWN, 'Unexpected response shape from {0}'.format(url))

    return parsed


def check_daemon(data, result):
    daemon = data.get('Daemon')
    if (daemon or '').strip().lower() == 'running':
        result.escalate(STATE_OK, 'Daemon: {0}'.format(daemon))
    else:
        result.escalate(STATE_CRITICAL, 'Daemon: {0}'.format(daemon or 'unknown'))


def check_commlog(data, args, result):
    comm_log = data.get('CommunicationLog') or {}
    # Some Znuny versions nest the counters under a "Communications" key, others
    # report them directly on CommunicationLog alongside "Accounts". Handle both.
    comms = comm_log.get('Communications')
    if not isinstance(comms, dict):
        comms = comm_log

    health = comms.get('Health', 'OK')
    successful = int(comms.get('Successful', 0) or 0)
    processing = int(comms.get('Processing', 0) or 0)
    failed = int(comms.get('Failed', 0) or 0)
    total = int(comms.get('All', successful + processing + failed) or 0)
    avg_time = comms.get('AverageProcessingTime', 0) or 0

    state = health_state(health)
    if range_breached(failed, args.CRITICAL):
        state = STATE_CRITICAL
    elif range_breached(failed, args.WARNING):
        state = max(state, STATE_WARNING)

    result.escalate(
        state,
        'CommunicationLog: {0} (Successful={1} Processing={2} Failed={3} All={4})'.format(
            health, successful, processing, failed, total
        ),
    )
    result.add_perfdata('comm_log_successful', successful)
    result.add_perfdata('comm_log_processing', processing)
    result.add_perfdata('comm_log_failed', failed, warn=args.WARNING or '', crit=args.CRITICAL or '')
    result.add_perfdata('comm_log_total', total)
    result.add_perfdata('comm_log_avg_processing_time', avg_time, uom='s')

    accounts = comm_log.get('Accounts')
    if isinstance(accounts, dict):
        total_accounts = 0
        for account_type, entries in accounts.items():
            if not isinstance(entries, list):
                continue
            for account in entries:
                total_accounts += 1
                status = account.get('Status', 'OK')
                account_state = health_state(status)
                if account_state != STATE_OK:
                    label = account.get('AccountID')
                    label = '{0}::{1}'.format(account_type, label) if label else account_type
                    result.escalate(account_state, 'Account {0}: {1}'.format(label, status))
        result.add_perfdata('comm_log_accounts_total', total_accounts)


def evaluate_recurrent_tasks(data, group_name, warn_spec, crit_spec, perf_prefix, result):
    group = data.get(group_name) or {}

    total = 0
    failed_names = []
    for entry_key, entry in group.items():
        if not isinstance(entry, dict):
            continue
        total += 1
        if is_fail_status(entry.get('LastWorkerStatus')):
            failed_names.append(entry.get('Name') or entry_key)
    failed_count = len(failed_names)

    result.add_perfdata(perf_prefix + '_total', total)
    result.add_perfdata(perf_prefix + '_failed', failed_count, warn=warn_spec or '', crit=crit_spec or '')

    if range_breached(failed_count, crit_spec):
        state = STATE_CRITICAL
    elif range_breached(failed_count, warn_spec):
        state = STATE_WARNING
    else:
        state = STATE_OK

    if failed_count:
        result.escalate(
            state, '{0}: {1}/{2} failed ({3})'.format(group_name, failed_count, total, ', '.join(failed_names))
        )
    else:
        result.escalate(STATE_OK, '{0}: {1}/{2} failed'.format(group_name, failed_count, total))

    return group


def check_cron(data, args, result):
    warn = args.WARNING if args.WARNING is not None else '0'
    evaluate_recurrent_tasks(data, 'RecurrentCronTasks', warn, args.CRITICAL, 'recurrent_cron_tasks', result)


def check_genericagent(data, args, result):
    warn = args.WARNING if args.WARNING is not None else '0'
    group = evaluate_recurrent_tasks(
        data, 'RecurrentGenericAgentTasks', warn, args.CRITICAL, 'recurrent_generic_agent_tasks', result
    )

    if not (args.WARNING_RUNTIME or args.CRITICAL_RUNTIME):
        return

    max_runtime = 0.0
    breaching = []
    for entry_key, entry in group.items():
        if not isinstance(entry, dict):
            continue
        seconds = parse_duration_seconds(entry.get('LastWorkerRunningTime'))
        if seconds is None:
            continue
        max_runtime = max(max_runtime, seconds)
        if range_breached(seconds, args.CRITICAL_RUNTIME) or range_breached(seconds, args.WARNING_RUNTIME):
            breaching.append((entry.get('Name') or entry_key, seconds))

    result.add_perfdata(
        'recurrent_generic_agent_tasks_max_runtime',
        round(max_runtime, 1),
        uom='s',
        warn=args.WARNING_RUNTIME or '',
        crit=args.CRITICAL_RUNTIME or '',
    )

    if breaching:
        state = STATE_CRITICAL if any(range_breached(s, args.CRITICAL_RUNTIME) for _, s in breaching) else STATE_WARNING
        details = ', '.join('{0}={1}s'.format(name, seconds) for name, seconds in breaching)
        result.escalate(state, 'Runtime threshold breached: {0}'.format(details))


def check_future_tasks(data, args, result):
    group = data.get('TasksToBeExecutedInFuture') or {}
    count = sum(1 for entry in group.values() if isinstance(entry, dict))

    if range_breached(count, args.CRITICAL):
        state = STATE_CRITICAL
    elif range_breached(count, args.WARNING):
        state = STATE_WARNING
    else:
        state = STATE_OK

    result.add_perfdata('tasks_to_be_executed_in_future', count, warn=args.WARNING or '', crit=args.CRITICAL or '')
    result.escalate(state, 'TasksToBeExecutedInFuture: {0} waiting'.format(count))


def check_unprocessed_emails(data, result):
    entry = data.get('UnprocessedEmails') or {}
    health = entry.get('Health', 'OK')
    count = int(entry.get('Count', 0) or 0)

    result.escalate(health_state(health), 'UnprocessedEmails: {0} (Count={1})'.format(health, count))
    result.add_perfdata('unprocessed_emails', count)


def check_system(data, args, result):
    entry = data.get('MailQueue') or {}
    count = int(entry.get('Count', 0) or 0)

    if range_breached(count, args.CRITICAL):
        state = STATE_CRITICAL
    elif range_breached(count, args.WARNING):
        state = STATE_WARNING
    else:
        state = STATE_OK

    result.add_perfdata('mailqueue', count, warn=args.WARNING or '', crit=args.CRITICAL or '')
    result.escalate(state, 'MailQueue: {0}'.format(count))

    sessions = data.get('Sessions') or {}
    for key, value in sessions.items():
        try:
            result.add_perfdata(camel_to_snake(key), int(value))
        except (TypeError, ValueError):
            pass


SECTION_HANDLERS = {
    'daemon': lambda data, args, result: check_daemon(data, result),
    'commlog': check_commlog,
    'cron': check_cron,
    'genericagent': check_genericagent,
    'future-tasks': check_future_tasks,
    'unprocessed-emails': lambda data, args, result: check_unprocessed_emails(data, result),
    'system': check_system,
}


def main():
    try:
        args = parse_args()
    except SystemExit:
        sys.exit(STATE_UNKNOWN)

    api_key = args.API_KEY
    if args.API_KEY_FILE:
        try:
            with open(args.API_KEY_FILE, 'r') as key_file:
                api_key = key_file.read().strip()
        except OSError as exc:
            oao('Could not read --api-key-file {0}: {1}'.format(args.API_KEY_FILE, exc), STATE_UNKNOWN)

    payload = {'APIKey': api_key}
    if args.LOG_HOURS is not None:
        payload['LogHours'] = args.LOG_HOURS

    try:
        data = fetch_json(args.URL, payload, args.TIMEOUT, args.INSECURE, args.NO_PROXY)
    except PluginError as exc:
        oao(exc.message, exc.state)

    result = Result()
    SECTION_HANDLERS[args.SECTION](data, args, result)

    problems = [msg for state, msg in result.messages if state != STATE_OK]
    if problems:
        summary = '{0} - {1}'.format(STATE_NAMES[result.state], '; '.join(problems))
        # OK-state messages (e.g. the overall CommunicationLog health line when only one
        # account is the actual problem) are kept as context in the long output.
        detail_lines = [msg for state, msg in result.messages if state == STATE_OK]
    else:
        summary = 'OK - {0}'.format('; '.join(msg for _, msg in result.messages) or 'no data')
        detail_lines = []

    output = summary
    if detail_lines:
        output += '\n' + '\n'.join(detail_lines)

    oao(output, result.state, '\n'.join(result.perfdata))


def oao(msg, state=STATE_OK, perfdata=''):
    """Over and Out (OaO): print the plugin output and exit with `state`."""
    if perfdata:
        print(msg.strip() + '|' + perfdata.strip())
    else:
        print(msg.strip())
    sys.exit(state)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # pylint: disable=broad-except
        oao('UNKNOWN - Unhandled exception: {0}'.format(exc), STATE_UNKNOWN)
