import importlib.util
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from huaweicloudsdklts.v2 import ListLogsResponse

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'lts.py'
spec = importlib.util.spec_from_file_location('lts', SCRIPT)
lts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lts)


def log(number, **extra):
    return dict(content='事件 ' + str(number), line_num=str(number), **extra)


class FakeLts:
    def __init__(self, *pages):
        self.pages = iter(pages)
        self.requests = []

    def list_logs(self, request):
        self.requests.append(request)
        response = ListLogsResponse()
        response.raw_content = json.dumps(next(self.pages)).encode('utf-8')
        return response


class LogQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for method in ('home', 'cwd'):
            mock_path = patch.object(Path, method, return_value=Path(temporary.name))
            mock_path.start()
            self.addCleanup(mock_path.stop)
        registry = MagicMock()
        registry.QueryValueEx.side_effect = FileNotFoundError
        mock_registry = patch.dict(sys.modules, {'winreg': registry})
        mock_registry.start()
        self.addCleanup(mock_registry.stop)

    def options(self, *args):
        return lts.resolve_options(lts.parse_args([
            'query', '--region', 'cn-north-4', '--project-id', 'project',
            '--log-group-id', 'group', '--log-stream-id', 'stream',
            '--start', '2026-10-27T00:00:00', '--end', '2026-10-28T00:00:00',
            *args,
        ]))

    def test_fixed_window_and_exact_cursor_through_overlapping_pages(self):
        first, second, third = '1730000000000000001', '1730000000000000002', '1730000000000000003'
        client = FakeLts(
            dict(logs=[log(first), log(second, __time__='1730000000123')],
                 count=99, isQueryComplete=True, scrollId='scroll-1'),
            dict(logs=[log(second, __time__='1730000000123'), log(third)], scrollId='scroll-2'),
            dict(logs=None, count=0),
        )
        result = lts.execute(client, self.options('--page-size', '2'))
        self.assertTrue(result['complete'])
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['logs'][1]['__time__'], '1730000000123')
        self.assertEqual(client.requests[1].body.line_num, second)
        self.assertEqual(client.requests[1].body.time__, '1730000000123')
        self.assertEqual(client.requests[1].body.scroll_id, 'scroll-1')
        self.assertEqual(client.requests[2].body.scroll_id, 'scroll-2')
        self.assertEqual({r.body.start_time for r in client.requests}, {'1793030400000'})
        self.assertEqual({r.body.end_time for r in client.requests}, {'1793116800000'})

    def test_service_incomplete_is_not_success(self):
        client = FakeLts(dict(logs=[log(1)], isQueryComplete=False))
        result = lts.execute(client, self.options())
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'], 'service_incomplete')
        self.assertEqual(result['count'], 1)

    def test_live_api_empty_object_after_data_is_terminal_page(self):
        client = FakeLts(
            dict(logs=[log(1), log(2)], isQueryComplete=True, scrollId='page-1'),
            dict(logs=[log(3)], isQueryComplete=True, scrollId='page-2'),
            {},
        )
        result = lts.execute(client, self.options('--page-size', '2'))
        self.assertTrue(result['complete'])
        self.assertEqual(result['stop_reason'], 'exhausted')
        self.assertEqual(result['count'], 3)
        self.assertEqual(len(client.requests), 3)

    def test_repeated_page_and_limits_are_partial(self):
        for pages, args, reason in [
            ([dict(logs=[log(1)]), dict(logs=[log(1)])], [], 'no_progress'),
            ([dict(logs=[log(1), log(2)], isQueryComplete=True)], ['--max-logs', '2'], 'max_logs'),
            ([dict(logs=[log(1)])], ['--max-pages', '1'], 'max_pages'),
        ]:
            with self.subTest(reason=reason):
                result = lts.execute(FakeLts(*pages), self.options(*args))
                self.assertFalse(result['complete'])
                self.assertEqual(result['stop_reason'], reason)

    def test_counted_empty_results_for_query_paging_and_context(self):
        for mode, payloads, expected_count, complete in [
            ('query', [{'count': 0}], 0, True),
            ('query', [{'logs': [log(1)], 'count': 1}, {'count': 0}], 1, True),
            ('context', [{'count': 0}, {'count': 0}], 0, True),
            ('query', [{'count': 0, 'isQueryComplete': False}], 0, False),
        ]:
            pages, captured = iter(payloads), []

            def send(request, **kwargs):
                body = json.loads(request.body)
                captured.append(body)
                payload = dict(next(pages))
                # Reproduce LTS omitting count when statistics were not requested.
                if body.get('is_count') is not True:
                    payload.pop('count', None)
                response = requests.Response()
                response.request = request
                response.status_code = 200
                response.headers['Content-Type'] = 'application/json'
                response._content = json.dumps(payload).encode()
                return response

            with self.subTest(mode=mode, payloads=payloads), \
                    patch.dict(os.environ, {'HUAWEICLOUD_SDK_AK': 'test-ak',
                                           'HUAWEICLOUD_SDK_SK': 'test-sk'}, clear=True), \
                    patch('requests.sessions.Session.send', side_effect=send):
                options = self.options('--page-size', '1')
                if mode == 'context':
                    options.update(mode='context', line_num='2', before=1, after=1)
                result = lts.execute(lts.build_client(options), options)
                self.assertEqual(result['complete'], complete)
                self.assertEqual(result['count'], expected_count)
                self.assertEqual(len(captured), len(payloads))
                self.assertTrue(all(body['is_count'] is True for body in captured))

    def test_malformed_empty_response_is_not_no_matches(self):
        for payload in [{}, {'logs': {}}, {'logs': ''}, {'logs': False}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                lts.execute(FakeLts(payload), self.options())

    def test_real_sdk_signing_serialization_and_raw_response_without_network(self):
        captured = []

        def send(request, **kwargs):
            captured.append(request)
            response = requests.Response()
            response.request = request
            response.status_code = 200
            response.headers['Content-Type'] = 'application/json'
            response._content = json.dumps(dict(logs=[log('1730000000000000001',
                __time__='1730000000123')], isQueryComplete=True)).encode()
            self.assertTrue(kwargs['verify'])
            return response

        with patch.dict(os.environ, {'HUAWEICLOUD_SDK_AK': 'synthetic-ak',
                                     'HUAWEICLOUD_SDK_SK': 'synthetic-sk',
                                     'HUAWEICLOUD_SDK_SECURITY_TOKEN': 'synthetic-sts'}, clear=True), \
                patch('requests.sessions.Session.send', side_effect=send):
            options = self.options('--max-logs', '1', '--keywords', 'ERROR')
            result = lts.execute(lts.build_client(options), options)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url,
            'https://lts.cn-north-4.myhuaweicloud.com/v2/project/groups/group/streams/stream/content/query')
        self.assertIn('SDK-HMAC-SHA256', captured[0].headers['Authorization'])
        self.assertEqual(captured[0].headers['X-Security-Token'], 'synthetic-sts')
        self.assertEqual(captured[0].headers['X-Project-Id'], 'project')
        body = json.loads(captured[0].body)
        self.assertFalse(body['highlight'])
        self.assertEqual(body['start_time'], options['start_time'])
        self.assertEqual(result['logs'][0]['__time__'], '1730000000123')
        self.assertEqual(result['window']['start'], '2026-10-27T00:00:00+08:00')

    def test_transient_retries_are_bounded_and_auth_is_not_retried(self):
        for status, expected_calls in [(503, 3), (403, 1)]:
            def send(request, **kwargs):
                response = requests.Response()
                response.request = request
                response.status_code = status
                response.headers['Content-Type'] = 'application/json'
                response.headers['X-Request-Id'] = 'request-1'
                response._content = b'{"error_code":"LTS.test","error_msg":"test failure","request_id":"request-1"}'
                return response
            with self.subTest(status=status), \
                    patch.dict(os.environ, {'HUAWEICLOUD_SDK_AK': 'synthetic-ak',
                                           'HUAWEICLOUD_SDK_SK': 'synthetic-sk'}, clear=True), \
                    patch('requests.sessions.Session.send', side_effect=send) as transport, \
                    patch.object(lts.time, 'sleep'):
                options = self.options()
                result = lts.execute(lts.build_client(options), options)
            self.assertEqual(transport.call_count, expected_calls)
            self.assertFalse(result['complete'])
            self.assertEqual(result['error']['status_code'], status)
            self.assertEqual(result['error']['request_id'], 'request-1')

    def test_context_keeps_labels_and_drops_keywords(self):
        options = self.options('--label', 'pod=worker-1', '--keywords', 'ERROR')
        options.update(mode='context', line_num='1730000000000000002',
                       anchor_time='1730000000123', before=1, after=1)
        client = FakeLts(dict(logs=[log('1730000000000000001')]),
                         dict(logs=[log('1730000000000000003')]))
        result = lts.execute(client, options)
        self.assertTrue(result['complete'])
        # Live LTS advances relative to is_desc; backwards+descending returns later rows.
        self.assertEqual([r.body.search_type for r in client.requests], ['forwards', 'forwards'])
        self.assertEqual([r.body.is_desc for r in client.requests], [True, False])
        for request in client.requests:
            self.assertEqual(request.body.keywords, '')
            self.assertEqual(request.body.labels, {'pod': 'worker-1'})
            self.assertEqual(request.body.line_num, '1730000000000000002')
            self.assertEqual(request.body.time__, '1730000000123')
        self.assertEqual(result['filters']['keywords'], '')

    def test_config_overrides_and_relative_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            config.write_text(json.dumps(dict(region='cn-north-4', project_id='p',
                log_group_id='g', log_stream_id='s', labels={'pod': 'old', 'app': 'api'},
                timezone='+08:00', auth='password')), encoding='utf-8')
            args = lts.parse_args(['query', '--config', str(config), '--since', '24h',
                                   '--label', 'pod=new', '--auth', 'aksk'])
            options = lts.resolve_options(args, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
        self.assertEqual(options['labels'], {'pod': 'new', 'app': 'api'})
        self.assertEqual(options['auth'], 'aksk')
        self.assertEqual(int(options['end_time']) - int(options['start_time']), 86400000)

    def test_default_region_and_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            base = dict(project_id='p', log_group_id='g', log_stream_id='s')
            for configured, cli, expected in [
                ({}, [], 'cn-southwest-2'),
                ({'region': 'cn-north-4'}, [], 'cn-north-4'),
                ({'region': 'cn-north-4'}, ['--region', 'ap-southeast-1'], 'ap-southeast-1'),
            ]:
                with self.subTest(configured=configured, cli=cli):
                    config.write_text(json.dumps(dict(base, **configured)), encoding='utf-8')
                    options = lts.resolve_options(lts.parse_args(['query', '--config', str(config), *cli]))
                    self.assertEqual(options['region'], expected)

            with patch.object(Path, 'cwd', return_value=Path(tmp)):
                options = lts.resolve_options(lts.parse_args(['context', '--project-id', 'p',
                    '--log-group-id', 'g', '--log-stream-id', 's', '--line-num', '123']))
            self.assertEqual(options['region'], 'cn-southwest-2')

    def test_user_config_fallback_and_config_precedence(self):
        saved = dict(project_id='saved-project', log_group_id='saved-group',
                     log_stream_id='saved-stream', auth='password', labels={'app': 'saved'})
        (Path.home() / '.huaweicloud-logs.json').write_text(json.dumps(saved), encoding='utf-8')
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'cwd', return_value=Path(tmp)):
            options = lts.resolve_options(lts.parse_args(['query']))
            self.assertEqual(options['log_stream_id'], 'saved-stream')
            self.assertEqual(options['region'], 'cn-southwest-2')
            self.assertEqual(options['auth'], 'password')
            options = lts.resolve_options(lts.parse_args(['query', '--log-stream-id', 'cli-stream']))
            self.assertEqual(options['log_stream_id'], 'cli-stream')
            local = dict(project_id='local-project', log_group_id='local-group', log_stream_id='local-stream')
            config = Path(tmp) / '.huaweicloud-logs.json'
            config.write_text(json.dumps(local), encoding='utf-8')
            options = lts.resolve_options(lts.parse_args(['query']))
            self.assertEqual(options['log_stream_id'], 'local-stream')
            self.assertEqual(options['labels'], {})
            self.assertEqual(options['auth'], 'aksk')
            explicit = Path(tmp) / 'selected.json'
            explicit.write_text(json.dumps(dict(local, log_stream_id='explicit-stream')), encoding='utf-8')
            options = lts.resolve_options(lts.parse_args(['query', '--config', str(explicit)]))
            self.assertEqual(options['log_stream_id'], 'explicit-stream')
            config.write_text('{"project_id": "local-project"}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Missing resource setting: log_group_id'):
                lts.resolve_options(lts.parse_args(['query']))
            with self.assertRaises(FileNotFoundError):
                lts.resolve_options(lts.parse_args(['query', '--config', str(Path(tmp) / 'missing.json')]))

    def test_project_config_from_project_runtime_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            work = project / '.huaweicloud-logs'
            work.mkdir()
            config = project / '.huaweicloud-logs.json'
            config.write_text('{"project_id": "project-config"}', encoding='utf-8')
            (Path.home() / '.huaweicloud-logs.json').write_text(
                '{"project_id": "user-config"}', encoding='utf-8')
            explicit = project / 'selected.json'
            explicit.write_text('{"project_id": "explicit-config"}', encoding='utf-8')
            with patch.object(Path, 'cwd', return_value=work):
                options = lts.resolve_options(lts.parse_args(['resources']))
                self.assertEqual(options['project_id'], 'project-config')
                options = lts.resolve_options(lts.parse_args(['resources', '--config', str(explicit)]))
                self.assertEqual(options['project_id'], 'explicit-config')
                config.unlink()
                options = lts.resolve_options(lts.parse_args(['resources']))
                self.assertEqual(options['project_id'], 'user-config')

    def test_invalid_window_and_credentials_in_config(self):
        with self.assertRaises(ValueError):
            self.options('--end', '2026-10-26T00:00:00')
        with self.assertRaises(ValueError):
            self.options('--start', '2025-01-01T00:00:00')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{"ak":"do-not-store-credentials"}', encoding='utf-8')
            with self.assertRaises(ValueError):
                self.options('--config', str(path))

    def test_resources_without_fixed_group_or_stream(self):
        config = Path.home() / '.huaweicloud-logs.json'
        config.write_text('{"project_id": "project"}', encoding='utf-8')
        captured = []
        groups = [{'log_group_id': 'group-low', 'log_group_name': 'lts-group-low-priority'}]
        streams = [{'log_group_id': 'group-low', 'log_stream_id': 'stream-low',
                    'log_stream_name': 'original-name', 'log_stream_name_alias': 'zqxy-low-log'}]

        def send(request, **kwargs):
            captured.append(request)
            response = requests.Response()
            response.request = request
            response.status_code = 200
            response.headers['Content-Type'] = 'application/json'
            payload = {'log_groups': groups} if len(captured) == 1 else {'log_streams': streams}
            response._content = json.dumps(payload).encode()
            return response

        stdout = io.StringIO()
        with patch.dict(os.environ, {'HUAWEICLOUD_SDK_AK': 'test-ak', 'HUAWEICLOUD_SDK_SK': 'test-sk'}, clear=True), \
                patch('requests.sessions.Session.send', side_effect=send), contextlib.redirect_stdout(stdout):
            code = lts.main(['resources'])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['log_groups'], groups)
        self.assertEqual(result['log_streams'], streams)
        self.assertNotIn('logs', result)
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(r.method == 'GET' and '/project/' in r.url for r in captured))
        self.assertEqual(json.loads(config.read_text()), {'project_id': 'project'})
        with self.assertRaisesRegex(ValueError, 'Missing resource setting: log_group_id'):
            lts.resolve_options(lts.parse_args(['query']))
        with self.assertRaisesRegex(ValueError, 'resources supports only JSON'):
            lts.resolve_options(lts.parse_args(['resources', '--format', 'jsonl']))

    def test_redaction_preserves_non_secret_context(self):
        value = dict(content='failed with secret-value-123', line_num='1730000000000000001',
                     labels={'access_token': 'sensitive', 'pod': 'worker-1'})
        safe = lts.redact(value, ['secret-value-123'])
        self.assertNotIn('secret-value-123', json.dumps(safe))
        self.assertEqual(safe['labels']['access_token'], '[REDACTED]')
        self.assertEqual(safe['line_num'], value['line_num'])
        self.assertEqual(safe['labels']['pod'], 'worker-1')

    def test_jsonl_export_retains_metadata_and_unicode(self):
        result = lts.execute(FakeLts(dict(logs=[log(1)]), dict(logs=[])), self.options())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'logs.jsonl'
            lts.write_result(result, path, 'jsonl')
            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[0]['type'], 'query')
        self.assertTrue(rows[0]['complete'])
        self.assertEqual(rows[1]['type'], 'log')
        self.assertEqual(rows[1]['content'], '事件 1')

    def test_password_auth_uses_official_sdk_and_reuses_project_token(self):
        captured = []
        env = {'HUAWEICLOUD_SDK_USERNAME': 'test-user',
               'HUAWEICLOUD_SDK_PASSWORD': 'test-password',
               'HUAWEICLOUD_SDK_DOMAIN_NAME': 'test-account',
               'HUAWEICLOUD_SDK_AK': 'ignored-ak', 'HUAWEICLOUD_SDK_SK': 'ignored-sk'}

        def send(request, **kwargs):
            captured.append(request)
            response = requests.Response()
            response.request = request
            response.status_code = 200
            response.headers['Content-Type'] = 'application/json'
            self.assertTrue(kwargs['verify'])
            if '/v3/auth/tokens' in request.url:
                response.status_code = 201
                response.headers['X-Subject-Token'] = 'test-iam-token'
                payload = {'token': {'project': {'id': 'project'}}}
            elif len(captured) == 2:
                payload = {'logs': [dict(log(1), content='test-password test-iam-token')]}
            else:
                payload = {'logs': []}
            response._content = json.dumps(payload).encode()
            return response

        stdout = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch('requests.sessions.Session.send', side_effect=send), \
                contextlib.redirect_stdout(stdout):
            code = lts.main(['query', '--auth', 'password', '--region', 'cn-north-4',
                '--project-id', 'project', '--log-group-id', 'group', '--log-stream-id', 'stream'])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['logs'][0]['content'], '[REDACTED] [REDACTED]')
        self.assertEqual(len(captured), 3)
        self.assertEqual(captured[0].url, 'https://iam.cn-north-4.myhuaweicloud.com/v3/auth/tokens?nocatalog=true')
        auth = json.loads(captured[0].body)['auth']
        self.assertEqual(auth['identity']['methods'], ['password'])
        self.assertEqual(auth['identity']['password']['user'],
            {'name': 'test-user', 'password': 'test-password', 'domain': {'name': 'test-account'}})
        self.assertEqual(auth['scope'], {'project': {'id': 'project'}})
        self.assertNotIn('X-Auth-Token', captured[0].headers)
        for request in captured:
            self.assertNotIn('Authorization', request.headers)
            self.assertNotIn('X-Security-Token', request.headers)
        for request in captured[1:]:
            self.assertEqual(request.headers['X-Auth-Token'], 'test-iam-token')
            self.assertIn('/v2/project/groups/group/streams/stream/', request.url)
            self.assertNotIn('test-password', request.body.decode() if isinstance(request.body, bytes) else request.body)

    def test_password_login_failure_stops_without_retry_or_lts_request(self):
        env = {'HUAWEICLOUD_SDK_USERNAME': 'test-user',
               'HUAWEICLOUD_SDK_PASSWORD': 'test-password',
               'HUAWEICLOUD_SDK_DOMAIN_NAME': 'test-account'}
        for status in [403, 201]:
            def send(request, **kwargs):
                response = requests.Response()
                response.request = request
                response.status_code = status
                response.headers['Content-Type'] = 'application/json'
                response.headers['X-Request-Id'] = 'iam-request-1'
                response._content = b'{"error":{"code":403,"message":"console access only"}}' if status == 403 else b'{}'
                return response
            with self.subTest(status=status), patch.dict(os.environ, env, clear=True), \
                    patch('requests.sessions.Session.send', side_effect=send) as transport, \
                    self.assertRaises(Exception) as caught:
                lts.build_client(self.options('--auth', 'password'))
            self.assertEqual(transport.call_count, 1)
            if status == 403:
                self.assertEqual(caught.exception.status_code, 403)
            else:
                self.assertIn('X-Subject-Token', str(caught.exception))

    def test_credential_sources_are_not_mixed(self):
        fields = lts.CREDENTIAL_ENV[:3]
        saved = dict(zip(fields, ['saved-ak', 'saved-sk', 'saved-token']))
        registry = MagicMock()
        registry.QueryValueEx.side_effect = lambda key, name: (saved[name], 1)
        with patch('sys.platform', 'win32'), patch.dict(sys.modules, {'winreg': registry}):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(lts.credential_values(fields), list(saved.values()))
            registry.reset_mock()
            for env, expected in [
                ({fields[0]: 'process-ak', fields[1]: 'process-sk'}, ['process-ak', 'process-sk', None]),
                ({fields[0]: 'process-ak'}, ['process-ak', None, None]),
                ({fields[0]: ''}, ['', None, None]),
            ]:
                with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                    self.assertEqual(lts.credential_values(fields), expected)
            registry.OpenKey.assert_not_called()
            registry.QueryValueEx.side_effect = FileNotFoundError
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(lts.credential_values(fields), [None, None, None])
        with patch('sys.platform', 'linux'), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(lts.credential_values(fields), [None, None, None])

    def test_saved_password_is_used_and_redacted_on_login_failure(self):
        saved = {'HUAWEICLOUD_SDK_USERNAME': 'saved-user',
                 'HUAWEICLOUD_SDK_PASSWORD': 'saved-password-%literal%',
                 'HUAWEICLOUD_SDK_DOMAIN_NAME': 'saved-account'}
        def registry_value(key, name):
            if name not in saved:
                raise FileNotFoundError(name)
            return saved[name], 1

        registry = MagicMock()
        registry.QueryValueEx.side_effect = registry_value
        captured = []

        def send(request, **kwargs):
            captured.append(request)
            response = requests.Response()
            response.request = request
            response.status_code = 403
            response.headers['Content-Type'] = 'application/json'
            response._content = json.dumps({'error': {'code': 403,
                'message': saved['HUAWEICLOUD_SDK_PASSWORD']}}).encode()
            return response

        stderr = io.StringIO()
        with patch('sys.platform', 'win32'), patch.dict(sys.modules, {'winreg': registry}), \
                patch.dict(os.environ, {}, clear=True), \
                patch('requests.sessions.Session.send', side_effect=send), contextlib.redirect_stderr(stderr):
            code = lts.main(['resources', '--auth', 'password', '--project-id', 'project'])
        self.assertEqual(code, 1)
        self.assertEqual(len(captured), 1, stderr.getvalue())
        user = json.loads(captured[0].body)['auth']['identity']['password']['user']
        self.assertEqual(user['password'], saved['HUAWEICLOUD_SDK_PASSWORD'])
        self.assertNotIn(saved['HUAWEICLOUD_SDK_PASSWORD'], stderr.getvalue())
        self.assertIn('[REDACTED]', stderr.getvalue())

    def test_password_auth_requires_all_login_fields(self):
        env = {'HUAWEICLOUD_SDK_USERNAME': 'test-user', 'HUAWEICLOUD_SDK_PASSWORD': 'test-password'}
        with patch.dict(os.environ, env, clear=True), \
                patch('requests.sessions.Session.send') as transport, \
                self.assertRaisesRegex(ValueError, 'HUAWEICLOUD_SDK_DOMAIN_NAME'):
            lts.build_client(self.options('--auth', 'password'))
        transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
