"""Read Huawei Cloud LTS logs with the official SDK. Python 3.10+."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


CREDENTIAL_ENV = ('HUAWEICLOUD_SDK_AK', 'HUAWEICLOUD_SDK_SK',
                  'HUAWEICLOUD_SDK_SECURITY_TOKEN', 'HUAWEICLOUD_SDK_PASSWORD')
DEFAULT_REGION = 'cn-southwest-2'
RESOURCE_KEYS = ('region', 'project_id', 'log_group_id', 'log_stream_id')
CONFIG_KEYS = {*RESOURCE_KEYS, 'labels', 'timezone', 'auth'}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['resources', 'query', 'context'])
    parser.add_argument('--config', type=Path, help='Default: .huaweicloud-logs.json in cwd, then home directory')
    parser.add_argument('--auth', choices=['aksk', 'password'], help='Override config auth; default aksk')
    for key in RESOURCE_KEYS:
        parser.add_argument('--' + key.replace('_', '-'),
                            help=f'Default: {DEFAULT_REGION}, unless configured' if key == 'region' else None)
    parser.add_argument('--label', action='append', default=[], metavar='KEY=VALUE')
    period = parser.add_mutually_exclusive_group()
    period.add_argument('--since', help='Duration such as 30m, 1h, 24h, 7d; default 1h')
    period.add_argument('--start', help='ISO datetime, using config timezone when no offset is supplied')
    parser.add_argument('--end', help='ISO datetime; default now')
    parser.add_argument('--keywords', default='', help='Raw LTS search expression; cleared for context')
    parser.add_argument('--page-size', type=int, default=100, help='1..5000; default 100')
    parser.add_argument('--max-logs', type=int, default=5000, help='Query cap; default 5000')
    parser.add_argument('--max-pages', type=int, default=100, help='Page cap per direction; default 100')
    parser.add_argument('--line-num', help='Required for context: exact original line_num string')
    parser.add_argument('--anchor-time', help='Context anchor: original __time__, if present')
    parser.add_argument('--before', type=int, default=20, help='Context lines before anchor, 0..500; default 20')
    parser.add_argument('--after', type=int, default=20, help='Context lines after anchor, 0..500; default 20')
    parser.add_argument('--output', type=Path, help='Write data here; print only summary to stdout')
    parser.add_argument('--format', choices=['json', 'jsonl', 'text'], default='json',
                        help='Export format; stdout without --output is always JSON')
    return parser.parse_args(argv)


def parse_timezone(value):
    if value == 'UTC':
        return timezone.utc
    if not isinstance(value, str) or not re.fullmatch(r'[+-](?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('timezone must be UTC or an offset such as +08:00')
    offset = timedelta(hours=int(value[1:3]), minutes=int(value[4:6]))
    return timezone(offset if value[0] == '+' else -offset)


def resolve_options(args, now=None):
    options = vars(args).copy()
    config_path = args.config or Path.cwd() / '.huaweicloud-logs.json'
    if not args.config and not config_path.exists():
        config_path = Path.home() / '.huaweicloud-logs.json'
    config = {}
    if args.config or config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        if not isinstance(config, dict) or set(config) - CONFIG_KEYS:
            raise ValueError('Config accepts only resource IDs, region, labels, timezone and auth; credentials belong in environment variables')
    options['auth'] = args.auth or config.get('auth', 'aksk')
    if options['auth'] not in ('aksk', 'password'):
        raise ValueError('auth must be aksk or password')
    options['region'] = args.region or config.get('region', DEFAULT_REGION)
    required = RESOURCE_KEYS[:2] if args.mode == 'resources' else RESOURCE_KEYS
    for key in required:
        options[key] = options[key] or config.get(key)
        if not isinstance(options[key], str) or not options[key].strip():
            raise ValueError('Missing resource setting: ' + key)
    if args.mode == 'resources':
        if args.format != 'json':
            raise ValueError('resources supports only JSON output')
        return options
    labels = config.get('labels', {})
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        raise ValueError('labels must be a JSON object mapping strings to strings')
    labels = labels.copy()
    for label in args.label:
        key, separator, value = label.partition('=')
        if not separator or not key:
            raise ValueError('--label must be KEY=VALUE')
        labels[key] = value
    options['labels'] = labels
    options['timezone'] = config.get('timezone', '+08:00')
    tz = parse_timezone(options['timezone'])

    def instant(value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed

    end = instant(args.end) if args.end else (now or datetime.now(timezone.utc))
    if args.start:
        start = instant(args.start)
    else:
        match = re.fullmatch(r'([1-9]\d*)([mhd])', args.since or '1h')
        if not match:
            raise ValueError('--since must be a positive duration such as 30m, 24h or 7d')
        start = end - timedelta(seconds=int(match[1]) * {'m': 60, 'h': 3600, 'd': 86400}[match[2]])
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if not 0 < end_ms - start_ms <= 180 * 86400000:
        raise ValueError('Time range must be positive and at most 180 days')
    options.update(start_time=str(start_ms), end_time=str(end_ms),
                   start=start.astimezone(tz).isoformat(), end=end.astimezone(tz).isoformat())
    if not 1 <= args.page_size <= 5000 or args.max_logs < 1 or args.max_pages < 1:
        raise ValueError('page-size must be 1..5000; max-logs and max-pages must be positive')
    if args.mode == 'context':
        if not args.line_num or not 0 <= args.before <= 500 or not 0 <= args.after <= 500 or args.before + args.after == 0:
            raise ValueError('context needs --line-num and before/after in 0..500, with a nonzero total')
        options['keywords'] = ''
    return options


def credential_values(fields):
    values = [os.environ.get(field) for field in fields]
    # Use one complete source, so a saved identity/token cannot fill a process override.
    if any(field in os.environ for field in fields) or sys.platform != 'win32':
        return values
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            for index, field in enumerate(fields):
                try:
                    value, _ = winreg.QueryValueEx(key, field)
                    values[index] = value if isinstance(value, str) else None
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass
    return values


def build_password_client(options, secrets):
    from huaweicloudsdkcore.client import ClientBuilder
    from huaweicloudsdkcore.http.http_config import HttpConfig
    from huaweicloudsdkiam.v3 import IamClient, KeystoneCreateUserTokenByPasswordRequest
    from huaweicloudsdkiam.v3.iam_credentials import IamCredentials
    from huaweicloudsdkiam.v3.region.iam_region import IamRegion
    from huaweicloudsdklts.v2 import LtsClient
    from huaweicloudsdklts.v2.region.lts_region import LtsRegion

    fields = ('HUAWEICLOUD_SDK_USERNAME', 'HUAWEICLOUD_SDK_PASSWORD', 'HUAWEICLOUD_SDK_DOMAIN_NAME')
    values = credential_values(fields)
    missing = [key for key, value in zip(fields, values) if not value]
    if missing:
        raise ValueError('Password authentication requires: ' + ', '.join(missing))
    username, password, domain = values
    if secrets is not None:
        secrets.append(password)
    config = HttpConfig(timeout=(10, 30), retry_times=0)
    iam = (IamClient.new_builder().with_credentials(IamCredentials())
           .with_region(IamRegion.value_of(options['region'])).with_http_config(config).build())
    # Login is attempted once. All query pages reuse the resulting project token.
    response = iam.keystone_create_user_token_by_password(KeystoneCreateUserTokenByPasswordRequest(
        nocatalog='true', body={'auth': {
            'identity': {'methods': ['password'], 'password': {'user': {
                'name': username, 'password': password, 'domain': {'name': domain}}}},
            'scope': {'project': {'id': options['project_id']}},
        }}))
    token = response.x_subject_token
    if not isinstance(token, str) or not token:
        raise ValueError('IAM response is missing X-Subject-Token')
    if secrets is not None:
        secrets.append(token)

    # Reuse IAM SDK token authentication; LTS additionally needs project path substitution.
    class LtsTokenCredentials(IamCredentials):
        def get_update_path_params(self):
            return {'project_id': options['project_id']}

    return (ClientBuilder(LtsClient, 'LtsTokenCredentials')
            .with_credentials(LtsTokenCredentials(token))
            .with_region(LtsRegion.value_of(options['region'])).with_http_config(config).build())


def build_client(options, secrets=None):
    if options.get('auth', 'aksk') == 'password':
        return build_password_client(options, secrets)
    from huaweicloudsdkcore.auth.credentials import BasicCredentials
    from huaweicloudsdkcore.http.http_config import HttpConfig
    from huaweicloudsdklts.v2 import LtsClient
    from huaweicloudsdklts.v2.region.lts_region import LtsRegion

    ak, sk, security_token = credential_values(CREDENTIAL_ENV[:3])
    if secrets is not None:
        secrets.extend([ak, sk, security_token])
    if not ak or not sk:
        raise ValueError('Set HUAWEICLOUD_SDK_AK and HUAWEICLOUD_SDK_SK before querying')
    credentials = BasicCredentials(ak, sk, options['project_id'])
    if security_token:
        credentials.with_security_token(security_token)
    return (LtsClient.new_builder().with_credentials(credentials)
            .with_region(LtsRegion.value_of(options['region']))
            .with_http_config(HttpConfig(timeout=(10, 30), retry_times=0)).build())


def error_details(error):
    return {key: value for key, value in dict(
        message=getattr(error, 'error_msg', str(error)),
        status_code=getattr(error, 'status_code', None),
        error_code=getattr(error, 'error_code', None),
        request_id=getattr(error, 'request_id', None),
    ).items() if value is not None}


def request_page(client, request):
    from huaweicloudsdkcore.exceptions.exceptions import (
        ConnectionException, RequestTimeoutException, ServiceResponseException,
    )
    for attempt in range(3):
        try:
            # The SDK's typed LogContents omits __time__; retain the raw JSON.
            return client.list_logs(request).to_json_object()
        except (ConnectionException, RequestTimeoutException, ServiceResponseException) as error:
            status = getattr(error, 'status_code', None)
            if attempt == 2 or (status is not None and status != 429 and status < 500):
                raise
            time.sleep(2 ** attempt)


def collect(client, options, direction='forwards', anchor=None, anchor_time=None,
            target=None):
    from huaweicloudsdkcore.exceptions.exceptions import SdkException
    from huaweicloudsdklts.v2 import ListLogsRequest, QueryLtsLogParams

    logs, seen = [], set()
    cursor, cursor_time, scroll_id = anchor, anchor_time, None
    cap = target if target is not None else options['max_logs']
    pages, reason, complete, error = 0, 'max_pages', False, None
    if cap == 0:
        return dict(logs=[], complete=True, stop_reason='context_size', pages=0)
    for _ in range(options['max_pages']):
        body = QueryLtsLogParams(
            start_time=options['start_time'], end_time=options['end_time'],
            labels=options['labels'], keywords=options['keywords'],
            line_num=cursor, time__=cursor_time, scroll_id=scroll_id,
            # Advance in the chosen sort order; descending walks toward earlier logs.
            search_type='forwards' if cursor is not None else 'init',
            is_desc=direction == 'backwards', limit=min(options['page_size'], cap - len(logs)),
            highlight=False,
        )
        try:
            data = request_page(client, ListLogsRequest(
                log_group_id=options['log_group_id'], log_stream_id=options['log_stream_id'], body=body))
        except SdkException as exc:
            reason, error = 'api_error', error_details(exc)
            break
        pages += 1
        # Observed LTS terminal response after nonempty pages: HTTP 200 with {}.
        if data == {} and logs and cursor is not None:
            data = {'logs': []}
        if not isinstance(data, dict) or ('logs' not in data and data.get('count') != 0):
            raise ValueError('Unexpected LTS response: missing logs/count; cannot infer an empty result')
        page = data.get('logs')
        if page is None:
            page = []
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise ValueError('Unexpected LTS logs format')
        added = 0
        for row in page:
            line = row.get('line_num')
            if line is not None:
                row['line_num'] = str(line)
            if row.get('__time__') is not None:
                row['__time__'] = str(row['__time__'])
            key = (row.get('line_num'), row.get('__time__'))
            if (anchor is not None and row.get('line_num') == anchor) or key in seen:
                continue
            seen.add(key)
            logs.append(row)
            added += 1
            if len(logs) >= cap:
                break
        if data.get('isQueryComplete') is False:
            reason = 'service_incomplete'
            break
        if not page:
            reason, complete = 'exhausted', True
            break
        if len(logs) >= cap:
            reason = 'context_size' if target is not None else 'max_logs'
            complete = target is not None
            break
        last = page[-1]
        next_cursor = last.get('line_num')
        if not next_cursor:
            reason = 'missing_cursor'
            break
        next_time = last.get('__time__')
        if not added or (str(next_cursor), next_time) == (cursor, cursor_time):
            reason = 'no_progress'
            break
        cursor, cursor_time = str(next_cursor), next_time
        scroll_id = data.get('scrollId')
    result = dict(logs=logs, complete=complete, stop_reason=reason, pages=pages)
    if error:
        result['error'] = error
    return result


def execute(client, options):
    if options['mode'] == 'resources':
        from huaweicloudsdklts.v2 import ListLogGroupsRequest, ListLogStreamsRequest
        groups = client.list_log_groups(ListLogGroupsRequest()).to_json_object()
        streams = client.list_log_streams(ListLogStreamsRequest()).to_json_object()
        return dict(mode='resources', status='complete', complete=True,
                    resource={key: options[key] for key in RESOURCE_KEYS[:2]},
                    log_groups=groups.get('log_groups') or [],
                    log_streams=streams.get('log_streams') or [])
    options = options.copy()
    if options['mode'] == 'context':
        options['keywords'] = ''
        sides = {name: collect(client, options, direction, options['line_num'],
                               options.get('anchor_time'), options[name])
                 for name, direction in [('before', 'backwards'), ('after', 'forwards')]}
        logs = list(reversed(sides['before']['logs'])) + sides['after']['logs']
        complete = all(side['complete'] for side in sides.values())
        result = dict(logs=logs, complete=complete,
                      stop_reason='context_complete' if complete else 'context_partial',
                      anchor={'line_num': options['line_num'], '__time__': options.get('anchor_time')},
                      sides={name: {k: v for k, v in side.items() if k != 'logs'} for name, side in sides.items()})
    else:
        result = collect(client, options)
    result.update(mode=options['mode'], status='complete' if result['complete'] else 'partial',
                  count=len(result['logs']),
                  resource={key: options[key] for key in RESOURCE_KEYS},
                  window={key: options[key] for key in ['start', 'end', 'start_time', 'end_time', 'timezone']},
                  filters={'keywords': options['keywords'], 'labels': options['labels']})
    return result


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {key: '[REDACTED]' if re.sub(r'[^a-z]', '', key.lower()) in {
            'ak', 'sk', 'accesskey', 'secretkey', 'accesskeyid', 'secretaccesskey',
            'password', 'authorization', 'token', 'accesstoken', 'securitytoken',
        } else redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
    return value


def write_result(result, path, fmt):
    # Exclusive creation prevents accidental replacement of an earlier evidence file.
    with Path(path).open('x', encoding='utf-8', newline='\n') as output:
        metadata = {key: value for key, value in result.items() if key != 'logs'}
        if fmt == 'json':
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write('\n')
        elif fmt == 'jsonl':
            output.write(json.dumps(dict(metadata, type='query'), ensure_ascii=False) + '\n')
            for row in result['logs']:
                output.write(json.dumps(dict(row, type='log'), ensure_ascii=False) + '\n')
        else:
            output.write('# ' + json.dumps(metadata, ensure_ascii=False) + '\n')
            for row in result['logs']:
                header = {key: value for key, value in row.items() if key != 'content'}
                output.write('\n# ' + json.dumps(header, ensure_ascii=False) + '\n')
                output.write(str(row.get('content', '')) + '\n')


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    secrets = [os.environ.get(key) for key in CREDENTIAL_ENV]
    try:
        options = resolve_options(parse_args(argv))
        if options['output'] and options['output'].exists():
            raise ValueError('Output already exists; choose a new path')
        result = redact(execute(build_client(options, secrets), options), secrets)
        if options['output']:
            write_result(result, options['output'], options['format'])
            summary = {key: value for key, value in result.items() if key != 'logs'}
            summary['output'] = str(options['output'].resolve())
            print(json.dumps(summary, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['complete'] else 2
    except ImportError:
        error = {'message': 'Install dependencies with: python -m pip install -r <skill>/scripts/requirements.txt'}
    except Exception as exc:
        error = error_details(exc)
    print(json.dumps({'status': 'error', 'error': redact(error, secrets)}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
