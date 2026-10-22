import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app


class AIChatTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / 'reflection.sqlite3'
        self.db_patch = patch.object(app, 'DB', self.database)
        self.db_patch.start()
        self.key_path = Path(self.temp_dir.name) / 'config' / 'MetaWatch' / 'openai-key.json'
        self.key_path_patch = patch.object(app, 'AI_KEY_CONFIG_PATH', self.key_path)
        self.key_path_patch.start()
        self.environment_patch = patch.dict('os.environ', {'OPENAI_API_KEY': '', 'OPENAI_MODEL': ''})
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
        request = Request(
            self.base_url + path,
            data=json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None,
            headers={
                'Content-Type': 'application/json',
                'Origin': self.base_url,
            } if data is not None else {},
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def create_session(self, session_date=None):
        day = session_date or app.tokyo_today().isoformat()
        status, response = self.request_json('/api/ai-chat/session', {'session_date': day})
        self.assertEqual(status, 201)
        return response['session_id']

    def consent(self):
        return self.request_json('/api/ai-chat/consent', {'consent': True})

    def set_key(self):
        return self.request_json('/api/ai-reflection/key', {'action': 'save', 'api_key': 'chat-test-secret'})

    def send(self, session_id, content):
        return self.request_json('/api/ai-chat/send', {'session_id': session_id, 'content': content})

    def test_chat_page_and_navigation_are_independent_and_get_does_not_call_openai(self):
        with patch.object(app.urllib.request, 'urlopen') as openai:
            for path in ('/ai-chat', '/api/ai-chat'):
                with urlopen(self.base_url + path) as response:
                    body = response.read().decode('utf-8')
                    self.assertEqual(response.status, 200)
                    if path == '/ai-chat':
                        self.assertIn('MetaWatch Assistant', body)
                        self.assertIn('/assistant-portrait.png', body)
                        self.assertIn('maxlength="4000"', body)
                        self.assertIn('placeholder="今の学習の様子や、気づいた変化を話してみてください。"', body)
                        self.assertIn('<details class="chat-management">', body)
            openai.assert_not_called()
        with urlopen(self.base_url + '/assistant-portrait.png') as response:
            self.assertEqual(response.headers.get_content_type(), 'image/png')
            self.assertTrue(response.read().startswith(b'\x89PNG'))
        for path in ('/', '/planning', '/learning', '/ai-chat'):
            with urlopen(self.base_url + path) as response:
                page = response.read().decode('utf-8')
            self.assertIn('href="/ai-chat">AIチャット</a>', page)
            self.assertEqual(page.count('aria-current="page"'), 1)
        with urlopen(self.base_url + '/app.js') as response:
            script = response.read().decode('utf-8')
        self.assertIn("body.append(el('p', message.content, 'chat-message-content'));", script)
        self.assertNotIn('innerHTML', script)
        with urlopen(self.base_url + '/style.css') as response:
            stylesheet = response.read().decode('utf-8')
        self.assertIn('.chat-workspace{min-width:0;min-height:calc(100vh - 170px);display:flex;flex-direction:column}', stylesheet)
        self.assertIn('.chat-messages{flex:1;height:auto;min-height:420px;overflow-y:auto', stylesheet)
        self.assertIn('.chat-composer textarea{height:64px;min-height:64px;max-height:64px;resize:none}', stylesheet)
        self.assertIn('.chat-messages{height:48vh;min-height:240px;max-height:60vh;padding:12px}', stylesheet)

    def test_system_prompt_supports_natural_state_reflection_without_forcing_it(self):
        prompt = app.ai_chat_system_prompt('natural')
        self.assertIn('学習中の些細な状態変化を自然な対話で言語化', prompt)
        self.assertIn('眠気、集中、理解、感情、行動', prompt)
        self.assertIn('いつ頃起きたか', prompt)
        self.assertIn('その直前に何をしていたか', prompt)
        self.assertIn('前後で何が変わったか', prompt)
        self.assertIn('一回答につき質問は原則ひとつ', prompt)
        self.assertIn('原因を根拠なく断定せず', prompt)
        self.assertIn('無理に学習状態の聞き取りへ結び付けない', prompt)
        self.assertIn('事実・仮説・推測を区別', prompt)

    def test_multiple_sessions_on_same_day_date_retrieval_and_speech_style_prompt(self):
        day = app.tokyo_today().isoformat()
        first = self.create_session(day)
        second = self.create_session(day)
        self.assertNotEqual(first, second)
        status, saved = self.request_json('/api/ai-chat/settings', {'speech_style': 'friendly', 'daily_limit': 20})
        self.assertEqual(status, 200)
        self.assertEqual(saved['speech_style'], 'friendly')
        self.assertEqual(self.consent()[0], 200)
        self.assertEqual(self.set_key()[0], 200)
        with patch.object(app, 'request_openai_chat', return_value=('回答です。', 12, 6)) as request:
            status, result = self.send(first, '学んだ内容を説明してください')
        self.assertEqual(status, 200)
        self.assertEqual(result['model'], app.AI_CHAT_DEFAULT_MODEL)
        self.assertEqual(request.call_count, 1)
        model, messages, _key = request.call_args.args
        self.assertEqual(model, 'gpt-6-luna')
        self.assertIn('親しみやすく柔らかな日本語', messages[0]['content'])
        self.assertEqual(messages[-1], {'role': 'user', 'content': '学んだ内容を説明してください'})

        status, data = self.request_json('/api/ai-chat?date=' + day + '&session_id=' + str(first))
        self.assertEqual(status, 200)
        self.assertEqual([session['id'] for session in data['sessions']], [first, second])
        self.assertEqual([message['role'] for message in data['messages']], ['user', 'assistant'])
        self.assertEqual(data['messages'][0]['speech_style'], 'friendly')
        self.assertEqual(data['messages'][1]['speech_style'], 'friendly')
        self.assertEqual(data['messages'][1]['model'], 'gpt-6-luna')
        self.assertEqual(data['messages'][1]['input_tokens'], 12)
        self.assertEqual(data['messages'][1]['output_tokens'], 6)
        self.assertEqual(data['daily_input_tokens'], 12)
        self.assertEqual(data['daily_output_tokens'], 6)
        self.assertEqual(data['speech_style'], 'friendly')
        with app.connect() as connection:
            rows = connection.execute(
                'SELECT sequence,role FROM ai_chat_messages WHERE session_id=? ORDER BY sequence', (first,)
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, 'user'), (2, 'assistant')])

    def test_no_consent_or_api_key_never_calls_openai_or_saves_message(self):
        session_id = self.create_session()
        with patch.object(app, 'request_openai_chat') as request:
            status, result = self.send(session_id, '送信しない')
            self.assertEqual(status, 403)
            self.assertIn('同意', result['error'])
            request.assert_not_called()
            self.assertEqual(self.consent()[0], 200)
            status, result = self.send(session_id, 'キーがない')
            self.assertEqual(status, 503)
            request.assert_not_called()
        status, data = self.request_json('/api/ai-chat?date=' + app.tokyo_today().isoformat() + '&session_id=' + str(session_id))
        self.assertEqual(status, 200)
        self.assertEqual(data['messages'], [])
        self.assertEqual(data['daily_usage'], 0)

    def test_chat_write_endpoints_reject_cross_origin_and_missing_origin(self):
        session_id = self.create_session()
        payload = json.dumps({'session_id': session_id, 'content': 'cross-origin'}).encode('utf-8')
        for headers in (
            {'Content-Type': 'application/json', 'Origin': 'http://attacker.invalid'},
            {'Content-Type': 'application/json'},
        ):
            request = Request(self.base_url + '/api/ai-chat/send', data=payload, headers=headers)
            with self.assertRaises(HTTPError) as response:
                urlopen(request)
            self.assertEqual(response.exception.code, 403)

    def test_api_failure_preserves_user_message_and_retry_consumes_a_new_slot(self):
        session_id = self.create_session()
        self.consent()
        self.set_key()
        with patch.object(app, 'request_openai_chat', side_effect=app.AIReflectionError('API応答に失敗しました')):
            status, result = self.send(session_id, 'この内容は保存する')
        self.assertEqual(status, 502)
        self.assertTrue(result['quota_consumed'])
        self.assertIn('再送信すると利用枠をもう1回使います', result['error'])
        status, data = self.request_json('/api/ai-chat?date=' + app.tokyo_today().isoformat() + '&session_id=' + str(session_id))
        self.assertEqual(status, 200)
        self.assertEqual(len(data['messages']), 1)
        self.assertEqual(data['messages'][0]['content'], 'この内容は保存する')
        self.assertEqual(data['messages'][0]['request_status'], 'failed')
        self.assertEqual(data['daily_usage'], 1)
        with patch.object(app, 'request_openai_chat', return_value=('再送信への回答', 7, 3)):
            status, _ = self.send(session_id, 'この内容は保存する')
        self.assertEqual(status, 200)
        status, data = self.request_json('/api/ai-chat?date=' + app.tokyo_today().isoformat() + '&session_id=' + str(session_id))
        self.assertEqual([message['role'] for message in data['messages']], ['user', 'user', 'assistant'])
        self.assertEqual(data['daily_usage'], 2)

    def test_same_session_in_flight_and_daily_quota_are_atomic(self):
        first = self.create_session()
        second = self.create_session()
        self.assertEqual(self.request_json('/api/ai-chat/settings', {'daily_limit': 1})[0], 200)
        self.consent()
        self.set_key()
        started = threading.Event()
        release = threading.Event()

        def slow_response(model, messages, api_key):
            started.set()
            release.wait(3)
            return '応答', 5, 2

        with patch.object(app, 'request_openai_chat', side_effect=slow_response) as request:
            with ThreadPoolExecutor(max_workers=2) as pool:
                running = pool.submit(self.send, first, '一つ目')
                self.assertTrue(started.wait(2))
                status, result = self.send(first, '重複')
                self.assertEqual(status, 409)
                self.assertIn('処理中', result['error'])
                release.set()
                status, _ = running.result(timeout=4)
            self.assertEqual(status, 200)
            self.assertEqual(request.call_count, 1)
            status, result = self.send(second, '上限超過')
            self.assertEqual(status, 429)
            self.assertEqual(result['daily_usage'], 1)
            self.assertEqual(request.call_count, 1)
        status, data = self.request_json('/api/ai-chat?date=' + app.tokyo_today().isoformat() + '&session_id=' + str(second))
        self.assertEqual(data['messages'], [])
        self.assertEqual(data['daily_usage'], 1)

    def test_default_twenty_request_quota_blocks_the_twenty_first_call(self):
        session_id = self.create_session()
        self.consent()
        self.set_key()
        with patch.object(app, 'request_openai_chat', return_value=('応答', 1, 1)) as request:
            for index in range(app.AI_CHAT_DEFAULT_DAILY_LIMIT):
                status, _ = self.send(session_id, '質問 %s' % index)
                self.assertEqual(status, 200)
            status, result = self.send(session_id, '上限超過')
        self.assertEqual(status, 429)
        self.assertEqual(result['daily_usage'], 20)
        self.assertEqual(request.call_count, 20)
        status, data = self.request_json('/api/ai-chat?date=' + app.tokyo_today().isoformat() + '&session_id=' + str(session_id))
        self.assertEqual(data['daily_usage'], 20)
        self.assertEqual(len(data['messages']), 40)

    def test_context_keeps_recent_messages_with_count_and_character_limits(self):
        session_id = self.create_session()
        with app.connect() as connection:
            now = app.datetime.now(app.timezone.utc).isoformat()
            connection.executemany(
                '''INSERT INTO ai_chat_messages(session_id,role,content,created_at,sequence)
                   VALUES (?,?,?,?,?)''',
                (
                    (session_id, 'user' if sequence % 2 else 'assistant', 'x' * 1300, now, sequence)
                    for sequence in range(1, 16)
                ),
            )
            context = app._ai_chat_context(connection, session_id)
        self.assertLessEqual(len(context), app.AI_CHAT_MAX_CONTEXT_MESSAGES)
        self.assertLessEqual(sum(len(message['content']) for message in context), app.AI_CHAT_MAX_CONTEXT_CHARS)
        self.assertEqual(context[-1]['content'], 'x' * 1300)
        self.assertEqual(context[-1]['role'], 'user')

    def test_malicious_html_message_is_stored_as_text_and_never_rendered_as_markup(self):
        session_id = self.create_session()
        self.consent()
        self.set_key()
        malicious = '<img src=x onerror=alert(1)>'
        with patch.object(app, 'request_openai_chat', return_value=('通常の回答', 2, 1)):
            self.assertEqual(self.send(session_id, malicious)[0], 200)
        status, data = self.request_json('/api/ai-chat?date=' + app.tokyo_today().isoformat() + '&session_id=' + str(session_id))
        self.assertEqual(status, 200)
        self.assertEqual(data['messages'][0]['content'], malicious)
        with urlopen(self.base_url + '/app.js') as response:
            script = response.read().decode('utf-8')
        self.assertIn("body.append(el('p', message.content, 'chat-message-content'));", script)
        self.assertIn('e.textContent = text;', script)

    def test_input_validation_and_old_database_migration_preserve_learning_records(self):
        session_id = self.create_session()
        for content in ('   ', 'x' * (app.AI_CHAT_MAX_MESSAGE_CHARS + 1), 'null\x00byte'):
            status, result = self.send(session_id, content)
            self.assertEqual(status, 400)
            self.assertIn('error', result)
        status, _ = self.request_json('/api/ai-chat/settings', {'speech_style': 'diagnostic'})
        self.assertEqual(status, 400)
        status, _ = self.request_json('/api/ai-chat/settings', {'daily_limit': 501})
        self.assertEqual(status, 400)

        legacy = Path(self.temp_dir.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as connection:
            connection.executescript('''
                CREATE TABLE courses(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE lectures(id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id), title TEXT NOT NULL, watched INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE reflections(id INTEGER PRIMARY KEY, lecture_id INTEGER NOT NULL REFERENCES lectures(id), understood TEXT NOT NULL, unclear TEXT NOT NULL, satisfaction INTEGER NOT NULL, reason TEXT NOT NULL, understanding INTEGER NOT NULL, recorded_at TEXT NOT NULL, notes TEXT NOT NULL);
                INSERT INTO courses(name) VALUES ('維持する講座');
                INSERT INTO lectures(course_id,title,watched) VALUES (1,'維持する単元',1);
            ''')
        with patch.object(app, 'DB', legacy):
            app.init()
            app.init()
            snapshot = app.snapshot()
            with app.connect() as connection:
                style = connection.execute('SELECT speech_style FROM ai_chat_settings WHERE id=1').fetchone()[0]
                daily_limit = connection.execute('SELECT daily_limit FROM ai_chat_usage_settings WHERE id=1').fetchone()[0]
        self.assertEqual(snapshot['courses'][0]['name'], '維持する講座')
        self.assertEqual(snapshot['lectures'][0]['watched'], 1)
        self.assertEqual(style, 'natural')
        self.assertEqual(daily_limit, 20)

    def test_exports_include_chat_data_csv_is_safe_and_never_contains_api_key(self):
        session_id = self.create_session()
        self.consent()
        self.set_key()
        malicious = '=HYPERLINK("https://example.invalid")'
        with patch.object(app, 'request_openai_chat', return_value=('返答', 3, 2)):
            self.assertEqual(self.send(session_id, malicious)[0], 200)
        with urlopen(self.base_url + '/api/export.json') as response:
            all_data = json.loads(response.read())
        with urlopen(self.base_url + '/api/export/planning.json') as response:
            planning_data = json.loads(response.read())
        for exported in (all_data, planning_data):
            self.assertIn('ai_chat_sessions', exported)
            self.assertEqual(exported['ai_chat_messages'][0]['role'], 'user')
            self.assertEqual(exported['ai_chat_messages'][0]['content'], malicious)
            self.assertEqual(exported['ai_chat_messages'][1]['input_tokens'], 3)
            self.assertIn('ai_chat_settings', exported)
            self.assertEqual(exported['ai_chat_usage_settings']['daily_limit'], 20)
            self.assertNotIn('chat-test-secret', json.dumps(exported))
        with urlopen(self.base_url + '/api/export/ai-chat.csv') as response:
            csv_bytes = response.read()
            self.assertIn('ai-chat.csv', response.headers['Content-Disposition'])
        self.assertTrue(csv_bytes.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig'))))
        self.assertEqual(
            list(rows[0]),
            ['session_date', 'session_id', 'message_id', 'role', 'content', 'created_at',
             'sequence', 'speech_style', 'model', 'input_tokens', 'output_tokens'],
        )
        self.assertEqual(rows[0]['content'], "'" + malicious)
        self.assertEqual(rows[1]['speech_style'], 'natural')
        self.assertEqual(rows[1]['model'], 'gpt-6-luna')
        self.assertNotIn('chat-test-secret', csv_bytes.decode('utf-8'))

    def test_openai_transport_is_mocked_bounded_and_uses_openai_model_override(self):
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps({
            'choices': [{'message': {'content': '  AIの回答  '}}],
            'usage': {
                'prompt_tokens': 11,
                'completion_tokens': 28,
                'completion_tokens_details': {'reasoning_tokens': 20},
            },
        }).encode('utf-8')
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        messages = [{'role': 'system', 'content': 'system prompt'}, {'role': 'user', 'content': 'input'}]
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'server-key', 'OPENAI_MODEL': 'custom-model'}), patch.object(
            app.urllib.request, 'urlopen', return_value=response
        ) as request:
            result = app.request_openai_chat(app.ai_chat_model(), messages)
        self.assertEqual(result, ('AIの回答', 11, 28))
        call = request.call_args
        self.assertEqual(call.kwargs['timeout'], app.AI_CHAT_TIMEOUT_SECONDS)
        self.assertEqual(call.args[0].full_url, 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(call.args[0].get_header('Authorization'), 'Bearer server-key')
        request_body = json.loads(call.args[0].data)
        self.assertEqual(request_body['model'], 'custom-model')
        self.assertEqual(request_body['messages'], messages)
        self.assertEqual(request_body['max_completion_tokens'], 1200)
        self.assertNotIn('max_tokens', request_body)
        self.assertNotIn('temperature', request_body)
        self.assertEqual(response.read.call_args.args[0], app.AI_CHAT_MAX_RESPONSE_BYTES + 1)

        response.read.return_value = b'x' * (app.AI_CHAT_MAX_RESPONSE_BYTES + 1)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'server-key'}), patch.object(
            app.urllib.request, 'urlopen', return_value=response
        ):
            with self.assertRaisesRegex(app.AIReflectionError, 'サイズ上限'):
                app.request_openai_chat('gpt-4o-mini', messages)


if __name__ == '__main__':
    unittest.main()
