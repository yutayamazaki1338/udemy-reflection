import json
import io
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
from app import CurriculumFetchError, CurriculumParseError, parse_recorded_at

FIXTURE_PATH = Path(__file__).parent / 'tests' / 'fixtures' / 'udemy_curriculum.html'


class CurriculumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = FIXTURE_PATH.read_text(encoding='utf-8')

    def test_parse_saved_public_curriculum_fixture_returns_video_titles_only(self):
        curriculum = app.parse_course_curriculum(self.fixture)
        self.assertEqual(curriculum['expected_sections'], 2)
        self.assertEqual(curriculum['expected_lectures'], 3)
        self.assertIs(curriculum['complete'], True)
        self.assertEqual(curriculum['course_name'], 'Linux Basics')
        self.assertEqual(
            curriculum['lectures'],
            [
                {'title': 'Introduction', 'position': 1},
                {'title': 'Install the tools', 'position': 2},
                {'title': 'Read a file', 'position': 3},
            ],
        )
        self.assertNotIn('sections', curriculum)
        self.assertNotIn('duration_seconds', str(curriculum))

    def test_parse_hashed_class_names_and_curriculum_stats(self):
        markup = '''
        <html><head><title>Sample course | Udemy</title></head><body>
        <div data-purpose="course-curriculum">
          <div data-testid="curriculum-stats">10 sections • 100 lectures • 5h total length</div>
          <div class="curriculum-section-module__a1__panel">
            <span class="curriculum-section-module__a1__section-title">Section title</span>
            <span class="curriculum-section-module__a1__course-lecture-title">Video title</span>
          </div>
        </div></body></html>
        '''
        markup = markup.replace('10 sections • 100 lectures', '1 section • 1 lecture')
        curriculum = app.parse_course_curriculum(markup)
        self.assertEqual(curriculum['expected_sections'], 1)
        self.assertEqual(curriculum['expected_lectures'], 1)
        self.assertEqual(curriculum['course_name'], 'Sample course')
        self.assertEqual(curriculum['lectures'], [{'title': 'Video title', 'position': 1}])

    def test_stop_if_public_counts_do_not_match_parsed_curriculum(self):
        mismatch = self.fixture.replace('2 sections • 3 lectures', '2 sections • 4 lectures')
        with self.assertRaisesRegex(CurriculumParseError, '公開件数と取得件数が一致しない'):
            app.parse_course_curriculum(mismatch)

    def test_stop_if_curriculum_or_public_counts_are_missing(self):
        with self.assertRaisesRegex(CurriculumParseError, '公開カリキュラムが見つかりません'):
            app.parse_course_curriculum('<html><body>Course details</body></html>')
        no_counts = self.fixture.replace('2 sections • 3 lectures', 'Curriculum')
        with self.assertRaisesRegex(CurriculumParseError, '公開されているセクション数'):
            app.parse_course_curriculum(no_counts)

    def test_url_validation_trims_and_strips_query_and_fragment(self):
        self.assertEqual(
            app.normalize_udemy_course_url(' https://www.udemy.com/course/learn-basics-of-linux/?couponCode=OF260508JPB#reviews '),
            'https://www.udemy.com/course/learn-basics-of-linux/',
        )
        self.assertEqual(
            app.normalize_udemy_course_url('https://www.udemy.com:443/course/example/?a=b#top'),
            'https://www.udemy.com/course/example/',
        )
        self.assertEqual(
            app.normalize_udemy_course_url('https://udemy.com/course/example/?a=b'),
            'https://www.udemy.com/course/example/',
        )

    def test_url_validation_rejects_malicious_and_noncanonical_urls(self):
        for url in (
            'http://www.udemy.com/course/example/',
            'https://www.udemy.com.attacker.test/course/example/',
            'https://attacker.test/course/example/',
            'https://user@www.udemy.com/course/example/',
            'https://user:password@www.udemy.com/course/example/',
            'https://www.udemy.com:8443/course/example/',
            'https://www.udemy.com/course/%2f..%2fadmin/',
            'https://127.0.0.1/course/example/',
            'https://www.udemy.com/course/example/\n?x=1',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                app.normalize_udemy_course_url(url)

    def test_dns_rejects_private_addresses(self):
        private_answer = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443))]
        with patch.object(app.socket, 'getaddrinfo', return_value=private_answer):
            with self.assertRaisesRegex(CurriculumFetchError, '公開されていないIP'):
                app._resolve_udemy_addresses('www.udemy.com')

    def test_fetch_does_not_follow_http_errors_and_rejects_oversized_responses(self):
        address_info = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ('93.184.216.34', 443))

        class FakeResponse:
            def __init__(self, status, headers=None, body=b''):
                self.status = status
                self.headers = headers or {}
                self.body = body
                self.read_calls = 0

            def getheader(self, name):
                return self.headers.get(name)

            def read(self, limit):
                self.read_calls += 1
                return self.body[:limit]

        class FakeConnection:
            def __init__(self, response):
                self.response = response
                self.requests = []

            def request(self, *args, **kwargs):
                self.requests.append((args, kwargs))

            def getresponse(self):
                return self.response

            def close(self):
                pass

        redirect = FakeConnection(FakeResponse(302, {'Location': 'http://127.0.0.1/'}))
        with patch.object(app, '_resolve_udemy_addresses', return_value=address_info), patch.object(app, '_PinnedHTTPSConnection', return_value=redirect):
            with self.assertRaisesRegex(CurriculumFetchError, 'リダイレクトは追跡しません'):
                app.fetch_udemy_course_html('https://www.udemy.com/course/example/')
        self.assertEqual(len(redirect.requests), 1)

        oversized = FakeConnection(FakeResponse(200, {'Content-Length': str(app.CURRICULUM_MAX_BYTES + 1)}))
        with patch.object(app, '_resolve_udemy_addresses', return_value=address_info), patch.object(app, '_PinnedHTTPSConnection', return_value=oversized):
            with self.assertRaisesRegex(CurriculumFetchError, '応答サイズが上限'):
                app.fetch_udemy_course_html('https://www.udemy.com/course/example/')
        self.assertEqual(len(oversized.requests), 1)
        self.assertEqual(oversized.response.read_calls, 0)

        streamed_oversized = FakeConnection(FakeResponse(200, body=b'x' * (app.CURRICULUM_MAX_BYTES + 1)))
        with patch.object(app, '_resolve_udemy_addresses', return_value=address_info), patch.object(app, '_PinnedHTTPSConnection', return_value=streamed_oversized):
            with self.assertRaisesRegex(CurriculumFetchError, '応答サイズが上限'):
                app.fetch_udemy_course_html('https://www.udemy.com/course/example/')
        self.assertEqual(len(streamed_oversized.requests), 1)
        self.assertEqual(streamed_oversized.response.read_calls, 1)


class ParseRecordedAtTests(unittest.TestCase):
    def test_parses_utc_z_suffix_with_milliseconds(self):
        parsed = parse_recorded_at('2026-10-20T09:30:00.000Z')
        self.assertEqual(parsed, datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc))

    def test_parses_explicit_utc_offset(self):
        parsed = parse_recorded_at('2026-10-20T18:30:00+09:00')
        self.assertEqual(parsed.utcoffset(), timedelta(hours=9))

    def test_rejects_timestamp_without_timezone(self):
        with self.assertRaisesRegex(ValueError, '日時にはタイムゾーンが必要です'):
            parse_recorded_at('2026-10-20T18:30:00')

    def test_rejects_invalid_timestamp(self):
        with self.assertRaises(ValueError):
            parse_recorded_at('not-a-timestamp')


class ApiFeatureTests(unittest.TestCase):
    AI_RESULT = {
        'yesterday_reflection': '昨日は記録がありません。',
        'past_observations': '2026-10-20の記録を確認しました。',
        'today_suggestions': '今日の計画を確認しましょう。',
    }

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, 'DB', Path(self.temp_dir.name) / 'reflection.sqlite3')
        self.db_patch.start()
        self.key_path_patch = patch.object(
            app, 'AI_KEY_CONFIG_PATH', Path(self.temp_dir.name) / 'user-config' / 'MetaWatch' / 'openai-key.json'
        )
        self.key_path_patch.start()
        self.environment_patch = patch.dict('os.environ', {'OPENAI_API_KEY': ''})
        self.environment_patch.start()
        app.init()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:%s' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.environment_patch.stop()
        self.key_path_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def request_json(self, path, data=None):
        headers = {'Content-Type': 'application/json'} if data is not None else {}
        request = Request(self.base_url + path, data=json.dumps(data).encode('utf-8') if data is not None else None, headers=headers)
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def restart_test_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:%s' % self.server.server_port

    def create_ai_daily_record(self, reflection_date=None, improvement='振り返りの記録'):
        day = reflection_date or app.tokyo_today().isoformat()
        return self.request_json('/api/daily-reflections', {
            'reflection_date': day,
            'plan_completed': True,
            'study_minutes': 35,
            'self_rating': 4,
            'improvement': improvement,
            'tomorrow_plan': '次の単元を確認する',
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        })

    def consent_to_ai(self):
        return self.request_json('/api/ai-reflection/consent', {'consent': True})

    def set_ai_key(self, key):
        return self.request_json('/api/ai-reflection/key', {'action': 'save', 'api_key': key})

    def delete_ai_key(self):
        return self.request_json('/api/ai-reflection/key', {'action': 'delete', 'confirm_delete': True})

    def test_ai_key_save_status_restart_and_secure_file_permissions(self):
        secret = 'test-key-never-return-this'
        status, response = self.set_ai_key(secret)
        self.assertEqual(status, 200)
        self.assertNotIn('api_key', response)
        self.assertTrue(response['api_key_configured'])
        self.assertEqual(response['api_key_source'], 'local_file')
        self.assertTrue(self.key_path_patch.new.parent.exists())
        self.assertEqual(self.key_path_patch.new.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.key_path_patch.new.stat().st_mode & 0o777, 0o600)
        self.assertEqual(app.load_ai_api_key(), (secret, 'local_file'))

        app.init()
        self.restart_test_server()
        status, response = self.request_json('/api/ai-reflection')
        self.assertEqual(status, 200)
        self.assertTrue(response['api_key_configured'])
        self.assertEqual(response['api_key_source'], 'local_file')
        self.assertNotIn(secret, json.dumps(response))
        for path in ('/api/state', '/api/export.json', '/api/export/planning.json', '/api/export/learning.json', '/api/export.csv'):
            with urlopen(self.base_url + path) as result:
                exported = result.read().decode('utf-8')
            self.assertNotIn(secret, exported)
        with app.connect() as connection:
            tables = [row['name'] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            database_values = [
                tuple(row) for table in tables
                for row in connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"')
            ]
        self.assertNotIn(secret, repr(database_values))

    def test_local_key_precedes_environment_and_delete_falls_back_explicitly(self):
        local_key = 'local-priority-key'
        environment_key = 'environment-fallback-key'
        self.assertEqual(self.set_ai_key(local_key)[0], 200)
        self.create_ai_daily_record()
        self.assertEqual(self.consent_to_ai()[0], 200)
        with patch.dict('os.environ', {'OPENAI_API_KEY': environment_key}):
            status, details = self.request_json('/api/ai-reflection')
            self.assertEqual(details['api_key_source'], 'local_file')
            self.assertTrue(details['environment_key_configured'])
            self.assertNotIn(local_key, json.dumps(details))
            self.assertNotIn(environment_key, json.dumps(details))

            response_body = json.dumps({
                'choices': [{'message': {'content': json.dumps(self.AI_RESULT, ensure_ascii=False)}}],
            }).encode('utf-8')
            response = Mock()
            response.status = 200
            response.read.return_value = response_body
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            with patch.object(app.urllib.request, 'urlopen', return_value=response) as urlopen_mock:
                status, result = self.request_json('/api/ai-reflection/auto', {})
            self.assertEqual(status, 200)
            self.assertEqual(result['status'], 'succeeded')
            headers = urlopen_mock.call_args_list[0].args[0].headers
            self.assertIn('Bearer ' + local_key, headers.values())

            status, details = self.delete_ai_key()
            self.assertEqual(status, 200)
            self.assertEqual(details['api_key_source'], 'environment')
            self.assertFalse(details['local_key_present'])
            self.assertTrue(details['api_key_configured'])
            self.assertFalse(self.key_path_patch.new.exists())
            self.assertEqual(app.load_ai_api_key(), (environment_key, 'environment'))
            with patch.object(app.urllib.request, 'urlopen', return_value=response) as urlopen_mock:
                status, result = self.request_json('/api/ai-reflection/manual', {})
            self.assertEqual(status, 200)
            self.assertEqual(result['status'], 'succeeded')
            self.assertIn('Bearer ' + environment_key, urlopen_mock.call_args.args[0].headers.values())

    def test_environment_key_fallback_is_reported_without_returning_value(self):
        secret = 'environment-only-secret'
        with patch.dict('os.environ', {'OPENAI_API_KEY': secret}):
            status, details = self.request_json('/api/ai-reflection')
            self.assertEqual(status, 200)
            self.assertTrue(details['api_key_configured'])
            self.assertEqual(details['api_key_source'], 'environment')
            self.assertTrue(details['environment_key_configured'])
            self.assertNotIn(secret, json.dumps(details))
            self.assertEqual(app.load_ai_api_key(), (secret, 'environment'))
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'invalid\nkey'}):
            status, details = self.request_json('/api/ai-reflection')
            self.assertEqual(status, 200)
            self.assertEqual(details['api_key_source'], 'error')
            self.assertFalse(details['api_key_configured'])
            self.assertIn('形式', details['key_settings_error'])

    def test_malformed_key_file_is_reported_and_can_be_deleted(self):
        self.key_path_patch.new.parent.mkdir(parents=True, mode=0o700)
        self.key_path_patch.new.write_text('{broken json', encoding='utf-8')
        self.key_path_patch.new.chmod(0o600)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'configured-env-fallback'}):
            status, details = self.request_json('/api/ai-reflection')
        self.assertEqual(status, 200)
        self.assertEqual(details['api_key_source'], 'error')
        self.assertTrue(details['local_key_present'])
        self.assertFalse(details['api_key_configured'])
        self.assertIn('JSON', details['key_settings_error'])
        self.assertNotIn('api_key', details)
        self.assertEqual(self.delete_ai_key()[0], 200)
        self.assertFalse(self.key_path_patch.new.exists())

    def test_key_file_access_and_write_errors_are_explicit(self):
        self.key_path_patch.new.parent.parent.mkdir(parents=True)
        self.key_path_patch.new.parent.write_text('not a directory', encoding='utf-8')
        status, details = self.request_json('/api/ai-reflection')
        self.assertEqual(status, 200)
        self.assertEqual(details['api_key_source'], 'error')
        self.assertIn('フォルダー', details['key_settings_error'])
        status, result = self.set_ai_key('write-error-test-key')
        self.assertEqual(status, 500)
        self.assertIn('アクセス', result['error'])
        self.assertNotIn('write-error-test-key', json.dumps(result))

    def test_key_file_read_errors_are_reported_without_environment_fallback(self):
        self.assertEqual(self.set_ai_key('saved-local-key')[0], 200)
        original_open = app.os.open

        def fail_config_read(path, *args, **kwargs):
            if Path(path) == self.key_path_patch.new:
                raise PermissionError('test read failure')
            return original_open(path, *args, **kwargs)

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'environment-fallback'}), patch.object(
            app.os, 'open', side_effect=fail_config_read
        ):
            status, details = self.request_json('/api/ai-reflection')
        self.assertEqual(status, 200)
        self.assertEqual(details['api_key_source'], 'error')
        self.assertFalse(details['api_key_configured'])
        self.assertIn('読み込めません', details['key_settings_error'])

    def test_key_settings_endpoint_rejects_cross_site_and_does_not_put_key_in_url(self):
        secret = 'url-and-log-exclusion-key'
        request = Request(
            self.base_url + '/api/ai-reflection/key',
            data=json.dumps({'action': 'save', 'api_key': secret}).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Origin': 'http://attacker.invalid'},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        self.assertNotIn(secret, request.full_url)
        self.assertFalse(self.key_path_patch.new.exists())
        log_output = io.StringIO()
        with redirect_stderr(log_output):
            status, response = self.set_ai_key(secret)
        self.assertEqual(status, 200)
        self.assertNotIn(secret, json.dumps(response))
        self.assertNotIn(secret, log_output.getvalue())

    def test_course_ratings_and_daily_plans_are_independent_histories(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '基礎講座'})[0], 200)
        self.assertEqual(self.request_json('/api/lectures', {'course_id': 1, 'title': '第1章'})[0], 200)
        rating = {
            'course_id': 1,
            'understanding': 4,
            'understanding_reason': '具体例が多く理解しやすい',
            'instructor_match': 5,
            'instructor_match_reason': '説明のテンポが合う',
            'recorded_at': '2026-10-20T09:00:00+09:00',
        }
        self.assertEqual(self.request_json('/api/course-ratings', rating)[0], 200)
        rating['understanding'] = 3
        rating['recorded_at'] = '2026-10-21T09:00:00+09:00'
        self.assertEqual(self.request_json('/api/course-ratings', rating)[0], 200)
        self.assertEqual(self.request_json('/api/plans', {'plan_date': '2026-10-21', 'lecture_id': 1})[0], 200)
        self.assertEqual(self.request_json('/api/plans', {
            'plan_date': '2026-10-21',
            'course_name': '新しい講座',
            'lecture_title': '手入力した単元',
        })[0], 200)
        status, state = self.request_json('/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(len(state['course_ratings']), 2)
        self.assertEqual(len(state['study_plans']), 2)
        self.assertEqual(len(state['courses']), 2)
        self.assertIn('新しい講座', [course['name'] for course in state['courses']])
        self.assertEqual(state['reflections'], [])
        self.assertEqual(state['daily_reflections'], [])
        planning_export = self.request_json('/api/export/planning.json')
        learning_export = self.request_json('/api/export/learning.json')
        self.assertEqual(set(planning_export[1]), {
            'courses', 'course_sections', 'lectures', 'study_plans', 'daily_reflections',
            'ai_chat_sessions', 'ai_chat_messages', 'ai_chat_settings', 'ai_chat_usage_settings',
            'ai_chat_daily_usage', 'ai_chat_requests', 'ai_chat_consent',
        })
        self.assertEqual(set(learning_export[1]), {'courses', 'course_sections', 'lectures', 'reflections', 'course_ratings'})
        self.assertEqual(len(planning_export[1]['study_plans']), 2)
        self.assertEqual(len(learning_export[1]['course_ratings']), 2)

    def test_bulk_plan_add_is_atomic_rejects_duplicates_and_preserves_watched_state(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '一括計画講座'})[0], 200)
        for title in ('第1単元', '第2単元', '第3単元'):
            self.assertEqual(self.request_json('/api/lectures', {'course_id': 1, 'title': title})[0], 200)
        self.assertEqual(self.request_json('/api/watched', {'id': 2, 'watched': True})[0], 200)
        day = '2026-10-21'

        status, result = self.request_json('/api/plans', {
            'plan_date': day,
            'lecture_ids': [1, 2],
        })
        self.assertEqual(status, 200)
        self.assertEqual(result, {'ok': True, 'added': 2})
        state = self.request_json('/api/state')[1]
        self.assertEqual(len([plan for plan in state['study_plans'] if plan['plan_date'] == day]), 2)
        self.assertEqual(next(lecture for lecture in state['lectures'] if lecture['id'] == 2)['watched'], 1)
        self.assertEqual(state['planning_progress'][day], {'planned_count': 2, 'reflected_count': 0, 'percentage': 0})

        for lecture_ids in ([2, 3], [3, 3], [3, 999], [True]):
            with self.subTest(lecture_ids=lecture_ids):
                self.assertEqual(self.request_json('/api/plans', {
                    'plan_date': day,
                    'lecture_ids': lecture_ids,
                })[0], 400)
                state = self.request_json('/api/state')[1]
                self.assertEqual(len([plan for plan in state['study_plans'] if plan['plan_date'] == day]), 2)

    def test_planning_page_exposes_bulk_selection_and_watched_status(self):
        with urlopen(self.base_url + '/planning') as response:
            page = response.read().decode('utf-8')
        with urlopen(self.base_url + '/app.js') as response:
            script = response.read().decode('utf-8')
        self.assertIn('id="plan-lecture-table"', page)
        self.assertIn('id="plan-course-select"', page)
        self.assertIn('id="plan-select-all"', page)
        self.assertIn('id="plan-add-selected"', page)
        self.assertIn('視聴済みの単元も復習用に選択できます', page)
        self.assertIn('講座を選択すると、その講座の単元が表示されます。', script)
        self.assertIn('courseLectures.forEach(lecture => {', script)
        self.assertIn('lecture_ids: lectureIds', script)
        self.assertIn("lecture.watched ? '視聴済み' : '未視聴'", script)

    def test_planning_progress_uses_distinct_reflections_on_tokyo_calendar_day(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '達成率講座'})[0], 200)
        for title in ('単元1', '単元2', '単元3'):
            self.assertEqual(self.request_json('/api/lectures', {'course_id': 1, 'title': title})[0], 200)
        day = '2026-10-21'
        self.assertEqual(self.request_json('/api/plans', {
            'plan_date': day,
            'lecture_ids': [1, 2, 3],
        })[0], 200)
        reflection = {
            'lecture_id': 1,
            'understood': '理解した',
            'unclear': '',
            'satisfaction': 4,
            'reason': '理由',
            'understanding': 4,
            'recorded_at': '2026-10-21T14:59:59Z',
            'notes': '',
        }
        self.assertEqual(self.request_json('/api/reflections', reflection)[0], 200)
        reflection['recorded_at'] = '2026-10-21T15:00:00Z'
        self.assertEqual(self.request_json('/api/reflections', reflection)[0], 200)
        reflection['lecture_id'] = 2
        reflection['recorded_at'] = '2026-10-21T23:00:00+09:00'
        self.assertEqual(self.request_json('/api/reflections', reflection)[0], 200)
        self.assertEqual(self.request_json('/api/daily-reflections', {
            'reflection_date': day,
            'plan_completed': True,
            'study_minutes': 30,
            'self_rating': 4,
            'improvement': '改善点',
            'tomorrow_plan': '明日の計画',
            'recorded_at': '2026-10-21T23:30:00+09:00',
        })[0], 200)
        self.assertEqual(self.request_json('/api/daily-reflections', {
            'reflection_date': '2026-10-22',
            'plan_completed': True,
            'study_minutes': 0,
            'self_rating': 3,
            'improvement': 'なし',
            'tomorrow_plan': '休む',
            'recorded_at': '2026-10-22T09:00:00+09:00',
        })[0], 200)

        state = self.request_json('/api/state')[1]
        self.assertEqual(state['planning_progress'][day], {'planned_count': 3, 'reflected_count': 2, 'percentage': 67})
        self.assertEqual(state['planning_progress']['2026-10-22'], {'planned_count': 0, 'reflected_count': 0, 'percentage': None})
        self.assertEqual(state['daily_reflections'][0]['plan_completed'], 1)
        self.assertEqual(self.request_json('/api/export/planning.json')[1]['daily_reflections'][0]['plan_completed'], 1)

    def test_daily_reflection_completion_is_retained_in_history(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '講座'})[0], 200)
        self.assertEqual(self.request_json('/api/lectures', {'course_id': 1, 'title': '単元'})[0], 200)
        self.assertEqual(self.request_json('/api/plans', {'plan_date': '2026-10-21', 'lecture_id': 1})[0], 200)
        reflection = {
            'reflection_date': '2026-10-21',
            'plan_completed': True,
            'study_minutes': 45,
            'self_rating': 4,
            'improvement': '復習時間を確保する',
            'tomorrow_plan': '次の単元を学ぶ',
            'recorded_at': '2026-10-21T09:00:00+09:00',
        }
        self.assertEqual(self.request_json('/api/daily-reflections', reflection)[0], 200)
        reflection['self_rating'] = 5
        self.assertEqual(self.request_json('/api/daily-reflections', reflection)[0], 200)
        state = self.request_json('/api/state')[1]
        self.assertEqual(len(state['daily_reflections']), 2)
        self.assertEqual(state['daily_reflections'][0]['reflection_date'], '2026-10-21')
        self.assertEqual(state['daily_reflections'][0]['plan_completed'], 1)

    def test_rating_and_daily_reflection_inputs_are_validated(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '講座'})[0], 200)
        invalid_rating = {
            'course_id': 1,
            'understanding': 6,
            'understanding_reason': '理由',
            'instructor_match': 3,
            'instructor_match_reason': '理由',
            'recorded_at': '2026-10-21T09:00:00+09:00',
        }
        self.assertEqual(self.request_json('/api/course-ratings', invalid_rating)[0], 400)
        invalid_reflection = {
            'reflection_date': '2026-10-21',
            'plan_completed': True,
            'study_minutes': -1,
            'self_rating': 3,
            'improvement': '改善',
            'tomorrow_plan': '計画',
            'recorded_at': '2026-10-21T09:00:00+09:00',
        }
        self.assertEqual(self.request_json('/api/daily-reflections', invalid_reflection)[0], 400)

    def test_init_adds_new_tables_without_changing_existing_records(self):
        legacy_db = Path(self.temp_dir.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy_db) as connection:
            connection.executescript('''
            CREATE TABLE courses(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE lectures(id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id), title TEXT NOT NULL, watched INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE reflections(id INTEGER PRIMARY KEY, lecture_id INTEGER NOT NULL REFERENCES lectures(id), understood TEXT NOT NULL, unclear TEXT NOT NULL, satisfaction INTEGER NOT NULL CHECK(satisfaction BETWEEN 1 AND 5), reason TEXT NOT NULL, understanding INTEGER NOT NULL CHECK(understanding BETWEEN 1 AND 5), recorded_at TEXT NOT NULL, notes TEXT NOT NULL);
            INSERT INTO courses(name) VALUES ('既存講座');
            INSERT INTO lectures(course_id,title) VALUES (1,'既存単元');
            ''')
        with patch.object(app, 'DB', legacy_db):
            app.init()
            state = app.snapshot()
        self.assertEqual(state['courses'][0]['name'], '既存講座')
        self.assertEqual(state['lectures'][0]['title'], '既存単元')
        self.assertEqual(state['course_ratings'], [])
        self.assertEqual(state['study_plans'], [])
        self.assertEqual(state['daily_reflections'], [])
        self.assertEqual(state['courses'][0]['course_url'], '')
        self.assertEqual(state['courses'][0]['name_key'], '既存講座')
        self.assertIsNone(state['lectures'][0]['section_id'])
        self.assertIsNone(state['lectures'][0]['duration_seconds'])

    def test_duplicate_course_names_are_rejected_after_unicode_and_whitespace_normalization(self):
        status, _ = self.request_json('/api/courses', {'name': '  Ｐｙｔｈｏｎ   基礎講座  '})
        self.assertEqual(status, 200)
        status, error = self.request_json('/api/courses', {'name': 'python 基礎講座'})
        self.assertEqual(status, 400)
        self.assertIn('同じ講座名がすでに登録されています', error['error'])
        state = self.request_json('/api/state')[1]
        self.assertEqual(len(state['courses']), 1)
        self.assertEqual(state['courses'][0]['name'], 'Python 基礎講座')
        with app.connect() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO courses(name,course_url) VALUES ('Another', '')")

    def test_legacy_duplicate_course_names_are_preserved_and_new_duplicates_blocked(self):
        legacy_db = Path(self.temp_dir.name) / 'legacy-duplicates.sqlite3'
        with sqlite3.connect(legacy_db) as connection:
            connection.executescript('''
            CREATE TABLE courses(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            INSERT INTO courses(name) VALUES ('Existing Course'), (' existing   course ');
            ''')
        with patch.object(app, 'DB', legacy_db):
            app.init()
            app.init()
            with app.connect() as connection:
                rows = connection.execute('SELECT id,name,name_key FROM courses ORDER BY id').fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual([row['name_key'] for row in rows], ['existing course', 'existing course'])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO courses(name,name_key,course_url) VALUES ('New','existing course','')")

    def test_pages_and_page_specific_downloads_are_served(self):
        for path, page_title in (
            ('/', 'メインページ'),
            ('/planning', '学習計画'),
            ('/learning', '講座の学習記録'),
        ):
            with urlopen(self.base_url + path) as response:
                self.assertEqual(response.status, 200)
                page = response.read().decode('utf-8')
                self.assertIn(page_title, page)
                self.assertIn('MetaWatch（メタウォッチ）', page)
                self.assertIn('「観た」を「学んだ」に変える。', page)
                self.assertIn('動画学習の理解と振り返りを記録するツール', page)
                self.assertIn('href="/favicon.svg"', page)
                self.assertIn('class="brand"', page)
                self.assertIn('src="/brand-mark.svg"', page)
                self.assertIn('src="/brand-wordmark.svg"', page)
                self.assertIn('href="/">メインページ</a>', page)
                self.assertIn('href="/planning">学習計画</a>', page)
                self.assertIn('href="/learning">講座の学習記録</a>', page)
                self.assertEqual(page.count('aria-current="page"'), 1)
                self.assertIn('class="site-header"', page)

    def test_home_calendar_is_compact_selectable_and_ai_reflection_is_unconnected(self):
        with urlopen(self.base_url + '/') as response:
            page = response.read().decode('utf-8')
        with urlopen(self.base_url + '/app.js') as response:
            script = response.read().decode('utf-8')
        with urlopen(self.base_url + '/style.css') as response:
            stylesheet = response.read().decode('utf-8')
        server_source = Path(app.__file__).read_text(encoding='utf-8')

        self.assertIn('id="heatmap"', page)
        self.assertIn('id="selected-day-reflections"', page)
        self.assertIn('id="ai-reflection"', page)
        self.assertIn('def ai_reflection_inputs(connection, target_date):', server_source)
        self.assertIn("api('/api/ai-reflection')", script)
        self.assertIn('function renderAiReflection(data', script)
        self.assertIn('function renderAiKeySettings(container, data)', script)
        self.assertIn("input.type = 'password';", script)
        self.assertIn("input.value = '';", script)
        self.assertIn("api('/api/ai-reflection/key'", script)
        self.assertIn('cell.dataset.date = date;', script)
        self.assertIn('cell.dataset.recorded = String(recorded);', script)
        self.assertIn("cell.setAttribute('aria-pressed'", script)
        self.assertIn('function renderSelectedDayReflections()', script)
        self.assertIn('height:36px', stylesheet)
        self.assertIn('@media(max-width:680px){#heatmap,.heatmap-weekdays{gap:3px}', stylesheet)
        self.assertIn('昨日の学習記録はありません。', script)
        self.assertIn('分析対象となる直近14日間の学習記録はありません。', script)
        self.assertIn('現在は未接続・未生成です。', script)

    def test_ai_reflection_skips_without_consent_key_or_records(self):
        status, details = self.request_json('/api/ai-reflection')
        self.assertEqual(status, 200)
        self.assertEqual(
            details['yesterday_date'],
            (app.tokyo_today() - timedelta(days=1)).isoformat(),
        )
        self.assertFalse(details['api_key_configured'])
        self.assertFalse(details['consented'])
        self.assertEqual(details['analysis_record_count'], 0)

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), patch.object(app, 'request_openai_reflection') as request:
            status, result = self.request_json('/api/ai-reflection/auto', {})
            self.assertEqual(status, 403)
            request.assert_not_called()
        self.assertEqual(self.consent_to_ai()[0], 200)
        with patch.dict('os.environ', {'OPENAI_API_KEY': ''}), patch.object(app, 'request_openai_reflection') as request:
            status, result = self.request_json('/api/ai-reflection/auto', {})
            self.assertEqual(status, 503)
            self.assertIn('分析は実行していません', result['error'])
            request.assert_not_called()

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), patch.object(app, 'request_openai_reflection') as request:
            status, result = self.request_json('/api/ai-reflection/auto', {})
            self.assertEqual(status, 200)
            self.assertEqual(result['status'], 'no_records')
            self.create_ai_daily_record()
            status, result = self.request_json('/api/ai-reflection/auto', {})
            self.assertEqual(status, 200)
            self.assertEqual(result['status'], 'already_checked')
            request.assert_not_called()
        with app.connect() as connection:
            daily = connection.execute(
                'SELECT auto_count,auto_checked FROM ai_reflection_daily_state WHERE target_date=?',
                (app.tokyo_today().isoformat(),),
            ).fetchone()
        self.assertEqual(tuple(daily), (0, 1))

    def test_ai_analysis_uses_only_records_in_the_tokyo_fourteen_day_window(self):
        today = app.tokyo_today()
        dates = [
            today - timedelta(days=14),
            today - timedelta(days=13),
            today - timedelta(days=1),
            today,
        ]
        for day in dates:
            self.create_ai_daily_record(day.isoformat(), '根拠 ' + day.isoformat())
        status, details = self.request_json('/api/ai-reflection')
        self.assertEqual(status, 200)
        self.assertEqual(details['period_start'], (today - timedelta(days=13)).isoformat())
        self.assertEqual(details['analysis_dates'], [
            (today - timedelta(days=13)).isoformat(),
            (today - timedelta(days=1)).isoformat(),
            today.isoformat(),
        ])
        self.assertTrue(details['yesterday_has_record'])
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), patch.object(
            app, 'request_openai_reflection', return_value=self.AI_RESULT
        ) as request:
            self.assertEqual(self.consent_to_ai()[0], 200)
            self.assertEqual(self.request_json('/api/ai-reflection/auto', {})[0], 200)
            payload = request.call_args.args[1]
            self.assertEqual(len(payload['daily_reflections']), 3)
            self.assertNotIn('id', payload['daily_reflections'][0])
        with app.connect() as connection:
            run = connection.execute(
                'SELECT target_date,record_ids_json,input_hash,model,prompt_version FROM ai_reflection_runs'
            ).fetchone()
        self.assertEqual(run['target_date'], today.isoformat())
        self.assertEqual(len(json.loads(run['record_ids_json'])['daily_reflection_ids']), 3)
        self.assertEqual(len(run['input_hash']), 64)
        self.assertEqual(run['model'], app.AI_REFLECTION_MODEL)
        self.assertEqual(run['prompt_version'], app.AI_REFLECTION_PROMPT_VERSION)

    def test_ai_auto_analysis_is_single_flight_across_concurrent_tabs(self):
        self.create_ai_daily_record()
        self.assertEqual(self.consent_to_ai()[0], 200)
        started = threading.Event()
        release = threading.Event()

        def fake_request(model, payload, api_key=None):
            started.set()
            release.wait(3)
            return self.AI_RESULT

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), patch.object(
            app, 'request_openai_reflection', side_effect=fake_request
        ) as request:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.request_json, '/api/ai-reflection/auto', {})
                self.assertTrue(started.wait(2))
                second = pool.submit(self.request_json, '/api/ai-reflection/auto', {})
                second_status, second_result = second.result(timeout=2)
                release.set()
                first_status, first_result = first.result(timeout=3)
            self.assertEqual(first_status, 200)
            self.assertEqual(first_result['status'], 'succeeded')
            self.assertEqual(second_status, 200)
            self.assertEqual(second_result['status'], 'already_checked')
            request.assert_called_once()
        status, details = self.request_json('/api/ai-reflection')
        self.assertEqual(status, 200)
        self.assertEqual(details['auto_count'], 1)
        self.assertIsNotNone(details['latest_result'])

    def test_ai_result_survives_restart_and_record_changes_require_manual_rerun(self):
        target_date = app.tokyo_today().isoformat()
        self.create_ai_daily_record(target_date, '最初の記録')
        self.assertEqual(self.consent_to_ai()[0], 200)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), patch.object(
            app, 'request_openai_reflection', return_value=self.AI_RESULT
        ) as request:
            self.assertEqual(self.request_json('/api/ai-reflection/auto', {})[0], 200)
            app.init()
            status, details = self.request_json('/api/ai-reflection')
            self.assertEqual(status, 200)
            self.assertEqual(details['auto_count'], 1)
            self.assertEqual(details['latest_result']['sections'], self.AI_RESULT)
            self.create_ai_daily_record(target_date, '追加された記録')
            status, details = self.request_json('/api/ai-reflection')
            self.assertTrue(details['stale'])
            status, result = self.request_json('/api/ai-reflection/auto', {})
            self.assertEqual(result['status'], 'already_checked')
            self.assertEqual(request.call_count, 1)
            status, result = self.request_json('/api/ai-reflection/manual', {})
            self.assertEqual(status, 200)
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(request.call_count, 2)
            status, details = self.request_json('/api/ai-reflection')
            self.assertEqual(details['auto_count'], 1)
            self.assertFalse(details['stale'])
            self.assertEqual(details['latest_result']['run_type'], 'manual')
            next_date = app.tokyo_today() + timedelta(days=1)
            with patch.object(app, 'tokyo_today', return_value=next_date):
                status, result = self.request_json('/api/ai-reflection/auto', {})
                self.assertEqual(status, 200)
                self.assertEqual(result['status'], 'succeeded')
                status, next_day = self.request_json('/api/ai-reflection')
                self.assertEqual(next_day['target_date'], next_date.isoformat())
                self.assertEqual(next_day['auto_count'], 1)
            self.assertEqual(request.call_count, 3)
        with app.connect() as connection:
            run_types = [row['run_type'] for row in connection.execute(
                'SELECT run_type FROM ai_reflection_runs WHERE target_date=? ORDER BY id',
                (target_date,),
            )]
        self.assertEqual(run_types, ['auto', 'manual'])

    def test_ai_api_failure_is_cached_and_does_not_auto_retry_or_erase_success(self):
        self.create_ai_daily_record()
        self.assertEqual(self.consent_to_ai()[0], 200)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), patch.object(
            app, 'request_openai_reflection', side_effect=[self.AI_RESULT, app.AIReflectionError('一時的なAPIエラー')]
        ) as request:
            self.assertEqual(self.request_json('/api/ai-reflection/auto', {})[0], 200)
            status, result = self.request_json('/api/ai-reflection/manual', {})
            self.assertEqual(status, 502)
            self.assertIn('一時的なAPIエラー', result['error'])
            status, details = self.request_json('/api/ai-reflection')
            self.assertEqual(details['latest_result']['sections'], self.AI_RESULT)
            self.assertIn('一時的なAPIエラー', details['last_error'])
            self.assertEqual(self.request_json('/api/ai-reflection/auto', {})[1]['status'], 'already_checked')
            self.assertEqual(request.call_count, 2)

    def test_openai_request_uses_server_key_and_validates_structured_response(self):
        response_body = json.dumps({
            'choices': [{'message': {'content': json.dumps(self.AI_RESULT, ensure_ascii=False)}}],
        }).encode('utf-8')
        response = Mock()
        response.status = 200
        response.read.return_value = response_body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        payload = {
            'target_date': '2026-10-21',
            'daily_reflections': [{'reflection_date': '2026-10-20', 'improvement': '記録'}],
            'today_study_plan': [{'course_name': '講座名', 'lecture_title': '単元名'}],
        }
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'server-only-test-key'}), patch.object(
            app.urllib.request, 'urlopen', return_value=response
        ) as urlopen_mock:
            self.assertEqual(app.request_openai_reflection('gpt-4o-mini', payload), self.AI_RESULT)
        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer server-only-test-key')
        request_body = json.loads(request.data)
        self.assertEqual(request_body['model'], 'gpt-4o-mini')
        self.assertEqual(request_body['response_format']['type'], 'json_schema')
        self.assertEqual(urlopen_mock.call_count, 1)

        response.read.return_value = json.dumps({'choices': [{'message': {'content': '{"invalid":true}'}}]}).encode()
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'server-only-test-key'}), patch.object(
            app.urllib.request, 'urlopen', return_value=response
        ):
            with self.assertRaisesRegex(app.AIReflectionError, '必要な項目'):
                app.request_openai_reflection('gpt-4o-mini', payload)

    def test_brand_assets_are_transparent_and_responsive_header_is_defined(self):
        for path, viewbox in (
            ('/brand-mark.svg', '0 0 44 44'),
            ('/brand-wordmark.svg', '0 0 190 40'),
            ('/favicon.svg', '0 0 32 32'),
        ):
            with urlopen(self.base_url + path) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), 'image/svg+xml')
                asset = response.read().decode('utf-8')
                self.assertIn(f'viewBox="{viewbox}"', asset)
                self.assertNotIn('<rect', asset)
        with urlopen(self.base_url + '/style.css') as response:
            stylesheet = response.read().decode('utf-8')
        self.assertIn('.brand-mark{display:block;width:42px;height:42px', stylesheet)
        self.assertIn('@media(max-width:680px){.brand{height:42px}.brand-mark{width:40px;height:40px}', stylesheet)
        self.assertIn('#courses .course-row{align-items:flex-start}', stylesheet)
        self.assertIn('#courses .course-row>button:first-child{display:block;flex:1 1 auto;width:auto;min-width:0;word-break:normal}', stylesheet)
        self.assertIn('#courses .course-row .delete-course{display:block;flex:0 0 auto;width:auto;min-width:0;margin-top:0}', stylesheet)

        for path, filename in (
            ('/api/export/planning.json', 'learning-plans.json'),
            ('/api/export/learning.json', 'course-learning.json'),
            ('/api/export.csv', 'reflection.csv'),
        ):
            with urlopen(self.base_url + path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(filename, response.headers['Content-Disposition'])

    def test_url_curriculum_preview_registers_one_course_and_preserves_existing_data(self):
        status, result = self.request_json('/api/courses', {
            'name': 'Linux講座',
            'course_url': 'https://www.udemy.com/course/learn-basics-of-linux/?couponCode=promo#overview',
        })
        self.assertEqual(status, 200)
        state = self.request_json('/api/state')[1]
        self.assertEqual(state['courses'][0]['course_url'], 'https://www.udemy.com/course/learn-basics-of-linux/')
        self.assertEqual(self.request_json('/api/lectures', {
            'course_id': result.get('id', 1),
            'title': '既存の手入力単元',
            'section_name': '手入力セクション',
            'duration_minutes': 17,
        })[0], 200)
        self.assertEqual(self.request_json('/api/watched', {'id': 1, 'watched': True})[0], 200)
        fixture = FIXTURE_PATH.read_text(encoding='utf-8')
        with patch.object(app, 'fetch_udemy_course_html', return_value=fixture) as fetch:
            status, preview = self.request_json('/api/course-curriculum/preview', {
                'url': ' https://www.udemy.com/course/learn-basics-of-linux/?couponCode=promo#overview ',
            })
        fetch.assert_called_once_with('https://www.udemy.com/course/learn-basics-of-linux/')
        self.assertEqual(status, 200)
        self.assertEqual(preview['expected_lectures'], 3)
        self.assertEqual(preview['course_name'], 'Linux Basics')
        self.assertEqual(preview['lectures'], [
            {'title': 'Introduction', 'position': 1},
            {'title': 'Install the tools', 'position': 2},
            {'title': 'Read a file', 'position': 3},
        ])
        self.assertEqual(len(self.request_json('/api/state')[1]['lectures']), 1)
        status, added = self.request_json('/api/course-curriculum/register', {'token': preview['token']})
        self.assertEqual(status, 200)
        self.assertFalse(added['course_created'])
        self.assertEqual(added['course_id'], 1)
        self.assertEqual(added['lectures_added'], 3)
        state = self.request_json('/api/state')[1]
        self.assertEqual(len(state['courses']), 1)
        self.assertEqual(len(state['lectures']), 4)
        self.assertEqual(len(state['course_sections']), 1)
        self.assertEqual(state['lectures'][0]['watched'], 1)
        self.assertEqual(state['lectures'][0]['duration_seconds'], 1020)
        imported = state['lectures'][1:]
        self.assertEqual([lecture['title'] for lecture in imported], ['Introduction', 'Install the tools', 'Read a file'])
        self.assertEqual([lecture['position'] for lecture in imported], [2, 3, 4])
        self.assertTrue(all(lecture['section_id'] is None and lecture['duration_seconds'] is None for lecture in imported))
        self.assertEqual(self.request_json('/api/course-curriculum/register', {'token': preview['token']})[0], 400)

    def test_invalid_and_incomplete_previews_never_create_lectures(self):
        mismatch = FIXTURE_PATH.read_text(encoding='utf-8').replace('2 sections • 3 lectures', '2 sections • 4 lectures')
        with patch.object(app, 'fetch_udemy_course_html', return_value=mismatch):
            status, error = self.request_json('/api/course-curriculum/preview', {'url': 'https://www.udemy.com/course/example/'})
        self.assertEqual(status, 422)
        self.assertIn('件数が一致しない', error['error'])
        self.assertEqual(self.request_json('/api/state')[1]['lectures'], [])
        self.assertEqual(self.request_json('/api/state')[1]['courses'], [])

        token = 'incomplete-preview'
        with app.connect() as connection:
            connection.execute(
                'INSERT INTO curriculum_url_previews(token,course_url,course_name,items_json,created_at) VALUES (?,?,?,?,?)',
                (token, 'https://www.udemy.com/course/example/', 'Example', json.dumps({'complete': False, 'lectures': []}), int(app.time.time())),
            )
        status, error = self.request_json('/api/course-curriculum/register', {'token': token})
        self.assertEqual(status, 400)
        self.assertIn('完全なプレビュー', error['error'])
        self.assertEqual(self.request_json('/api/state')[1]['lectures'], [])

    def test_course_url_is_validated_and_fetch_failures_offer_manual_entry(self):
        self.assertEqual(self.request_json('/api/courses', {
            'name': '不正URL講座',
            'course_url': 'https://example.com/course/not-udemy/',
        })[0], 400)
        with patch.object(app, 'fetch_udemy_course_html') as fetch:
            status, error = self.request_json('/api/course-curriculum/preview', {'url': 'https://example.com/course/not-udemy/'})
        self.assertEqual(status, 400)
        self.assertIn('Udemy講座URL', error['error'])
        fetch.assert_not_called()
        with patch.object(app, 'fetch_udemy_course_html', side_effect=CurriculumFetchError('Udemyへの接続に失敗しました')):
            status, error = self.request_json('/api/course-curriculum/preview', {'url': 'https://www.udemy.com/course/example/'})
        self.assertEqual(status, 502)
        self.assertIn('手動入力', error['error'])
        self.assertEqual(self.request_json('/api/courses', {'name': '通常講座'})[0], 200)
        self.assertEqual(self.request_json('/api/lectures', {'course_id': 1, 'title': '手入力単元'})[0], 200)
        state = self.request_json('/api/state')[1]
        self.assertEqual([course['name'] for course in state['courses']], ['通常講座'])
        self.assertEqual([lecture['title'] for lecture in state['lectures']], ['手入力単元'])

    def test_url_import_creates_course_only_after_confirmation_and_reuses_url(self):
        fixture = FIXTURE_PATH.read_text(encoding='utf-8')
        with patch.object(app, 'fetch_udemy_course_html', return_value=fixture):
            status, preview = self.request_json('/api/course-curriculum/preview', {'url': 'https://www.udemy.com/course/example/'})
        self.assertEqual(status, 200)
        self.assertEqual(self.request_json('/api/state')[1]['courses'], [])
        status, result = self.request_json('/api/course-curriculum/register', {'token': preview['token']})
        self.assertEqual(status, 200)
        self.assertTrue(result['course_created'])
        state = self.request_json('/api/state')[1]
        self.assertEqual(len(state['courses']), 1)
        self.assertEqual(state['courses'][0]['name'], 'Linux Basics')
        self.assertEqual(state['courses'][0]['course_url'], 'https://www.udemy.com/course/example/')
        self.assertEqual(len(state['lectures']), 3)

        with patch.object(app, 'fetch_udemy_course_html', return_value=fixture):
            _, repeat_preview = self.request_json('/api/course-curriculum/preview', {'url': 'https://udemy.com/course/example/?couponCode=second'})
        _, repeat_result = self.request_json('/api/course-curriculum/register', {'token': repeat_preview['token']})
        self.assertFalse(repeat_result['course_created'])
        self.assertEqual(repeat_result['course_id'], result['course_id'])
        self.assertEqual(repeat_result['lectures_added'], 0)
        self.assertEqual(len(self.request_json('/api/state')[1]['courses']), 1)
        self.assertEqual(len(self.request_json('/api/state')[1]['lectures']), 3)

    def test_url_import_rejects_course_name_conflict_before_registration(self):
        self.assertEqual(self.request_json('/api/courses', {'name': 'Linux Basics'})[0], 200)
        fixture = FIXTURE_PATH.read_text(encoding='utf-8')
        with patch.object(app, 'fetch_udemy_course_html', return_value=fixture):
            status, error = self.request_json('/api/course-curriculum/preview', {'url': 'https://www.udemy.com/course/example/'})
        self.assertEqual(status, 400)
        self.assertIn('同じ講座名がすでに登録されています', error['error'])
        state = self.request_json('/api/state')[1]
        self.assertEqual(len(state['courses']), 1)
        self.assertEqual(state['lectures'], [])

    def test_course_delete_preview_lists_related_targets_and_shared_daily_records(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '削除対象講座'})[0], 200)
        self.assertEqual(self.request_json('/api/lectures', {
            'course_id': 1,
            'title': '削除対象単元',
            'section_name': '第1章',
        })[0], 200)
        self.assertEqual(self.request_json('/api/plans', {
            'plan_date': '2026-10-21',
            'lecture_id': 1,
        })[0], 200)
        self.assertEqual(self.request_json('/api/daily-reflections', {
            'reflection_date': '2026-10-21',
            'plan_completed': True,
            'study_minutes': 30,
            'self_rating': 4,
            'improvement': '改善点',
            'tomorrow_plan': '明日の計画',
            'recorded_at': '2026-10-21T09:00:00+09:00',
        })[0], 200)
        status, preview = self.request_json('/api/course-delete-preview?course_id=1')
        self.assertEqual(status, 200)
        self.assertEqual(preview['course']['name'], '削除対象講座')
        self.assertEqual(preview['sections'][0]['title'], '第1章')
        self.assertEqual(preview['lectures'][0]['title'], '削除対象単元')
        self.assertEqual(preview['study_plans'][0]['plan_date'], '2026-10-21')
        self.assertEqual(len(preview['daily_reflections']), 1)

    def test_course_delete_removes_all_related_data_after_explicit_consent_without_archiving(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '講座削除'})[0], 200)
        self.assertEqual(self.request_json('/api/lectures', {
            'course_id': 1,
            'title': '単元',
            'section_name': '章',
        })[0], 200)
        self.assertEqual(self.request_json('/api/reflections', {
            'lecture_id': 1,
            'understood': '理解したこと',
            'unclear': '',
            'satisfaction': 4,
            'reason': '理由',
            'understanding': 4,
            'recorded_at': '2026-10-21T09:00:00+09:00',
            'notes': '',
        })[0], 200)
        self.assertEqual(self.request_json('/api/course-ratings', {
            'course_id': 1,
            'understanding': 4,
            'understanding_reason': '理解理由',
            'instructor_match': 3,
            'instructor_match_reason': '講師理由',
            'recorded_at': '2026-10-21T09:00:00+09:00',
        })[0], 200)
        self.assertEqual(self.request_json('/api/plans', {'plan_date': '2026-10-21', 'lecture_id': 1})[0], 200)
        self.assertEqual(self.request_json('/api/daily-reflections', {
            'reflection_date': '2026-10-21',
            'plan_completed': True,
            'study_minutes': 30,
            'self_rating': 4,
            'improvement': '改善点',
            'tomorrow_plan': '明日の計画',
            'recorded_at': '2026-10-21T09:00:00+09:00',
        })[0], 200)

        status, deletion_preview = self.request_json('/api/course-delete-preview?course_id=1')
        self.assertEqual(status, 200)
        status, deleted = self.request_json('/api/courses/delete', {
            'preview_token': deletion_preview['preview_token'],
            'delete_categories': [
                'course', 'sections_lectures', 'reflections',
                'course_ratings', 'study_plans', 'daily_reflections',
            ],
            'confirm_delete': True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(deleted['course_name'], '講座削除')
        self.assertEqual(deleted['daily_reflections_deleted'], 1)
        state = self.request_json('/api/state')[1]
        self.assertEqual(state['courses'], [])
        self.assertEqual(state['course_sections'], [])
        self.assertEqual(state['lectures'], [])
        self.assertEqual(state['reflections'], [])
        self.assertEqual(state['course_ratings'], [])
        self.assertEqual(state['study_plans'], [])
        self.assertEqual(state['daily_reflections'], [])
        self.assertEqual(state['course_archives'], [])
        full_export = self.request_json('/api/export.json')[1]
        self.assertEqual(full_export['course_archives'], [])

    def test_course_delete_rejects_missing_consent_or_categories_without_deleting(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '保護講座'})[0], 200)
        self.assertEqual(self.request_json('/api/lectures', {'course_id': 1, 'title': '保持する単元'})[0], 200)
        _, preview = self.request_json('/api/course-delete-preview?course_id=1')
        categories = [
            'course', 'sections_lectures', 'reflections',
            'course_ratings', 'study_plans', 'daily_reflections',
        ]
        status, error = self.request_json('/api/courses/delete', {
            'preview_token': preview['preview_token'],
            'delete_categories': categories,
            'confirm_delete': False,
        })
        self.assertEqual(status, 400)
        self.assertIn('明示的な同意', error['error'])
        status, error = self.request_json('/api/courses/delete', {
            'preview_token': preview['preview_token'],
            'delete_categories': ['course', 'sections_lectures'],
            'confirm_delete': True,
        })
        self.assertEqual(status, 400)
        self.assertIn('すべての削除に明示的な同意', error['error'])
        state = self.request_json('/api/state')[1]
        self.assertEqual(len(state['courses']), 1)
        self.assertEqual(len(state['lectures']), 1)
        self.assertEqual(state['course_archives'], [])

    def test_course_delete_requires_fresh_preview_of_exact_related_records(self):
        self.assertEqual(self.request_json('/api/courses', {'name': '変更確認講座'})[0], 200)
        _, preview = self.request_json('/api/course-delete-preview?course_id=1')
        self.assertEqual(self.request_json('/api/lectures', {'course_id': 1, 'title': '後から追加'})[0], 200)
        status, error = self.request_json('/api/courses/delete', {
            'preview_token': preview['preview_token'],
            'delete_categories': [
                'course', 'sections_lectures', 'reflections',
                'course_ratings', 'study_plans', 'daily_reflections',
            ],
            'confirm_delete': True,
        })
        self.assertEqual(status, 400)
        self.assertIn('プレビュー後に変更されました', error['error'])
        self.assertEqual(len(self.request_json('/api/state')[1]['courses']), 1)
        self.assertEqual(len(self.request_json('/api/state')[1]['lectures']), 1)

    def test_preview_uses_course_slug_when_page_title_is_missing(self):
        fixture = FIXTURE_PATH.read_text(encoding='utf-8').replace(
            '<head><meta property="og:title" content="Linux Basics | Udemy"></head>',
            '',
        )
        with patch.object(app, 'fetch_udemy_course_html', return_value=fixture):
            status, preview = self.request_json('/api/course-curriculum/preview', {'url': 'https://www.udemy.com/course/my-linux-course/'})
        self.assertEqual(status, 200)
        self.assertEqual(preview['course_name'], 'My Linux Course')

    def test_curriculum_ui_separates_url_preview_from_manual_course_creation(self):
        with urlopen(self.base_url + '/learning') as response:
            page = response.read().decode('utf-8')
        manual_form = page.split('<form id="course-form">', 1)[1].split('</form>', 1)[0]
        manual_panel = page.split('<section id="manual-course-panel"', 1)[1].split('</section>', 1)[0]
        udemy_panel = page.split('<section id="udemy-course-panel"', 1)[1].split('</section>', 1)[0]
        self.assertIn('新しい講座を登録', page)
        self.assertIn('role="tablist"', page)
        self.assertIn('role="tab" aria-selected="true"', page)
        self.assertIn('aria-controls="manual-course-panel"', page)
        self.assertIn('aria-controls="udemy-course-panel"', page)
        self.assertIn('id="course-form"', manual_panel)
        self.assertIn('id="curriculum-url"', udemy_panel)
        self.assertIn('hidden', udemy_panel)
        self.assertIn('name="name"', manual_form)
        self.assertNotIn('course_url', manual_form)
        self.assertIn('id="curriculum-url"', page)
        self.assertIn('id="fetch-curriculum"', page)
        with urlopen(self.base_url + '/app.js') as response:
            script = response.read().decode('utf-8')
        self.assertIn('\nfunction renderCurriculumImport() {', script)
        self.assertNotIn('\n  function renderCurriculumImport() {', script)
        self.assertIn('fetchButton.disabled = !input.value.trim() || curriculumLoading;', script)
        self.assertIn('手入力に切り替えて登録できます。', script)
        self.assertIn("selectCourseRegistrationMode('manual');", script)
        self.assertIn("event.key === 'ArrowRight'", script)

    def test_course_deletion_ui_requires_explicit_selection_of_every_category(self):
        with urlopen(self.base_url + '/app.js') as response:
            script = response.read().decode('utf-8')
        self.assertIn('1つでも未選択なら講座削除は中止され', script)
        self.assertIn('checkbox.onchange = () => { confirmDelete.disabled = checkboxes.some(item => !item.checked); };', script)
        self.assertIn("confirmDelete.disabled = true;", script)
        self.assertIn("confirm_delete: true", script)
        self.assertIn('アーカイブは作成されません', script)
        self.assertIn("$('#fetch-curriculum').onclick = fetchCurriculumPreview;", script)
        self.assertIn("api('/api/course-curriculum/preview', {url: input.value})", script)
        self.assertIn("api('/api/course-curriculum/register', {token: curriculumPreview.token})", script)


if __name__ == '__main__':
    unittest.main()
