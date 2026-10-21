import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

    def test_parse_saved_public_curriculum_fixture_and_durations(self):
        curriculum = app.parse_course_curriculum(self.fixture)
        self.assertEqual(curriculum['expected_sections'], 2)
        self.assertEqual(curriculum['expected_lectures'], 3)
        self.assertIs(curriculum['complete'], True)
        self.assertEqual([section['title'] for section in curriculum['sections']], ['Getting started', 'Working with files'])
        lectures = [lecture for section in curriculum['sections'] for lecture in section['lectures']]
        self.assertEqual([lecture['title'] for lecture in lectures], ['Introduction', 'Install the tools', 'Read a file'])
        self.assertEqual([lecture['position'] for lecture in lectures], [1, 2, 3])
        self.assertEqual([lecture['duration_seconds'] for lecture in lectures], [300, 720, 3720])

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

    def test_url_validation_allows_only_canonical_udemy_course_urls(self):
        self.assertEqual(
            app.normalize_udemy_course_url('https://www.udemy.com/course/learn-basics-of-linux'),
            'https://www.udemy.com/course/learn-basics-of-linux/',
        )
        for url in (
            'http://www.udemy.com/course/example/',
            'https://www.udemy.com.attacker.test/course/example/',
            'https://attacker.test/course/example/',
            'https://user@www.udemy.com/course/example/',
            'https://www.udemy.com:8443/course/example/',
            'https://www.udemy.com/course/example/?redirect=localhost',
            'https://www.udemy.com/course/example/#fragment',
            'https://www.udemy.com/course/%2f..%2fadmin/',
            'https://127.0.0.1/course/example/',
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
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, 'DB', Path(self.temp_dir.name) / 'reflection.sqlite3')
        self.db_patch.start()
        app.init()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:%s' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
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
        self.assertEqual(set(planning_export[1]), {'courses', 'course_sections', 'lectures', 'study_plans', 'daily_reflections'})
        self.assertEqual(set(learning_export[1]), {'courses', 'course_sections', 'lectures', 'reflections', 'course_ratings'})
        self.assertEqual(len(planning_export[1]['study_plans']), 2)
        self.assertEqual(len(learning_export[1]['course_ratings']), 2)

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
        self.assertIsNone(state['lectures'][0]['section_id'])
        self.assertIsNone(state['lectures'][0]['duration_seconds'])

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

        for path, filename in (
            ('/api/export/planning.json', 'learning-plans.json'),
            ('/api/export/learning.json', 'course-learning.json'),
            ('/api/export.csv', 'reflection.csv'),
        ):
            with urlopen(self.base_url + path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(filename, response.headers['Content-Disposition'])

    def test_curriculum_preview_requires_explicit_import_and_preserves_existing_progress(self):
        status, result = self.request_json('/api/courses', {
            'name': 'Linux講座',
            'course_url': 'https://www.udemy.com/course/learn-basics-of-linux/',
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
        with patch.object(app, 'fetch_udemy_course_html', return_value=fixture):
            status, preview = self.request_json('/api/course-curriculum/preview', {'course_id': 1})
        self.assertEqual(status, 200)
        self.assertEqual(preview['expected_lectures'], 3)
        self.assertEqual(len(self.request_json('/api/state')[1]['lectures']), 1)
        status, added = self.request_json('/api/course-curriculum/register', {'token': preview['token']})
        self.assertEqual(status, 200)
        self.assertEqual(added['sections_added'], 2)
        self.assertEqual(added['lectures_added'], 3)
        state = self.request_json('/api/state')[1]
        self.assertEqual(len(state['lectures']), 4)
        self.assertEqual(len(state['course_sections']), 3)
        self.assertEqual([section['position'] for section in state['course_sections']], [1, 2, 3])
        self.assertEqual(state['lectures'][0]['watched'], 1)
        self.assertEqual(state['lectures'][0]['duration_seconds'], 1020)
        imported = state['lectures'][1:]
        self.assertEqual([lecture['position'] for lecture in imported], [2, 3, 4])
        self.assertEqual([lecture['duration_seconds'] for lecture in imported], [300, 720, 3720])
        section_titles = {section['id']: section['title'] for section in state['course_sections']}
        self.assertEqual(
            [section_titles[lecture['section_id']] for lecture in imported],
            ['Getting started', 'Getting started', 'Working with files'],
        )
        self.assertEqual(self.request_json('/api/course-curriculum/register', {'token': preview['token']})[0], 400)

    def test_invalid_and_incomplete_previews_never_create_lectures(self):
        self.assertEqual(self.request_json('/api/courses', {
            'name': 'Udemy講座',
            'course_url': 'https://www.udemy.com/course/example/',
        })[0], 200)
        mismatch = FIXTURE_PATH.read_text(encoding='utf-8').replace('2 sections • 3 lectures', '2 sections • 4 lectures')
        with patch.object(app, 'fetch_udemy_course_html', return_value=mismatch):
            status, error = self.request_json('/api/course-curriculum/preview', {'course_id': 1})
        self.assertEqual(status, 422)
        self.assertIn('件数が一致しない', error['error'])
        self.assertEqual(self.request_json('/api/state')[1]['lectures'], [])

        token = 'incomplete-preview'
        with app.connect() as connection:
            connection.execute(
                'INSERT INTO curriculum_previews(token,course_id,items_json,created_at) VALUES (?,?,?,?)',
                (token, 1, json.dumps({'complete': False, 'sections': []}), int(app.time.time())),
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
        self.assertEqual(self.request_json('/api/courses', {'name': '通常講座'})[0], 200)
        status, result = self.request_json('/api/courses', {
            'name': 'Udemy講座',
            'course_url': 'https://www.udemy.com/course/example/',
        })
        self.assertEqual(status, 200)
        with patch.object(app, 'fetch_udemy_course_html', side_effect=CurriculumFetchError('Udemyへの接続に失敗しました')):
            status, error = self.request_json('/api/course-curriculum/preview', {'course_id': result.get('id', 2)})
        self.assertEqual(status, 502)
        self.assertIn('手動入力', error['error'])


if __name__ == '__main__':
    unittest.main()
