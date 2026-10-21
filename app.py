#!/usr/bin/env python3
"""Local Udemy reflection journal. Python standard library only."""
import argparse
import csv
import html
import http.client
import ipaddress
import io
import json
import os
import queue
import re
import secrets
import sqlite3
import socket
import ssl
import threading
import time
from html.parser import HTMLParser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('REFLECTION_DB', str(ROOT / 'data' / 'reflection.sqlite3')))
CURRICULUM_MAX_BYTES = 5 * 1024 * 1024
CURRICULUM_TIMEOUT_SECONDS = 12
UDEMY_HOSTS = frozenset(('udemy.com', 'www.udemy.com'))


class CurriculumFetchError(Exception):
    pass


class CurriculumParseError(Exception):
    pass


def connect():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS courses(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lectures(id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id), title TEXT NOT NULL, watched INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reflections(id INTEGER PRIMARY KEY, lecture_id INTEGER NOT NULL REFERENCES lectures(id), understood TEXT NOT NULL, unclear TEXT NOT NULL, satisfaction INTEGER NOT NULL CHECK(satisfaction BETWEEN 1 AND 5), reason TEXT NOT NULL, understanding INTEGER NOT NULL CHECK(understanding BETWEEN 1 AND 5), recorded_at TEXT NOT NULL, notes TEXT NOT NULL);
        ''')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS course_sections(id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id), title TEXT NOT NULL, position INTEGER NOT NULL, UNIQUE(course_id, position));
        CREATE TABLE IF NOT EXISTS course_ratings(id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id), understanding INTEGER NOT NULL CHECK(understanding BETWEEN 1 AND 5), understanding_reason TEXT NOT NULL, instructor_match INTEGER NOT NULL CHECK(instructor_match BETWEEN 1 AND 5), instructor_match_reason TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS study_plans(id INTEGER PRIMARY KEY, plan_date TEXT NOT NULL, lecture_id INTEGER NOT NULL REFERENCES lectures(id), UNIQUE(plan_date, lecture_id));
        CREATE TABLE IF NOT EXISTS daily_reflections(id INTEGER PRIMARY KEY, reflection_date TEXT NOT NULL, plan_completed INTEGER NOT NULL CHECK(plan_completed IN (0, 1)), study_minutes INTEGER NOT NULL CHECK(study_minutes >= 0), self_rating INTEGER NOT NULL CHECK(self_rating BETWEEN 1 AND 5), improvement TEXT NOT NULL, tomorrow_plan TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS curriculum_previews(token TEXT PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id), items_json TEXT NOT NULL, created_at INTEGER NOT NULL);
        ''')
        columns = {row['name'] for row in c.execute('PRAGMA table_info(courses)')}
        if 'course_url' not in columns:
            c.execute("ALTER TABLE courses ADD COLUMN course_url TEXT NOT NULL DEFAULT ''")
        lecture_columns = {row['name'] for row in c.execute('PRAGMA table_info(lectures)')}
        for name, sql_type in (
            ('section_id', 'INTEGER REFERENCES course_sections(id)'),
            ('position', 'INTEGER'),
            ('duration_seconds', 'INTEGER'),
        ):
            if name not in lecture_columns:
                c.execute('ALTER TABLE lectures ADD COLUMN %s %s' % (name, sql_type))


def snapshot():
    with connect() as c:
        return {name: [dict(r) for r in c.execute('SELECT * FROM ' + name + ' ORDER BY id')] for name in ('courses', 'course_sections', 'lectures', 'reflections', 'course_ratings', 'study_plans', 'daily_reflections')}


def required(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(key + ' を入力してください')
    return value.strip()


def parse_recorded_at(value):
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('日時にはタイムゾーンが必要です')
    return parsed


def parse_date(value):
    if not isinstance(value, str):
        raise ValueError('日付が不正です')
    try:
        return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise ValueError('日付はYYYY-MM-DD形式で入力してください')


def normalize_udemy_course_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Udemy講座URLを入力してください')
    if len(value.strip()) > 500:
        raise ValueError('Udemy講座URLが長すぎます')
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        raise ValueError('URLの形式が正しくありません')
    if parsed.scheme != 'https' or parsed.hostname not in UDEMY_HOSTS:
        raise ValueError('HTTPSのUdemy講座URL（udemy.com/course/...）を指定してください')
    if parsed.username or parsed.password or port not in (None, 443) or parsed.query or parsed.fragment:
        raise ValueError('認証情報・別ポート・クエリ・フラグメントを含むURLは使用できません')
    if not re.fullmatch(r'/course/[A-Za-z0-9_-]+/?', parsed.path):
        raise ValueError('Udemyの講座ページURL（/course/講座名/）を指定してください')
    return 'https://%s%s' % (parsed.hostname, parsed.path.rstrip('/') + '/')


class CurriculumNode:
    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []
        self.content = []

    def text(self):
        return ''.join(item if isinstance(item, str) else item.text() for item in self.content)

    def descendants(self):
        pending = list(reversed(self.children))
        while pending:
            node = pending.pop()
            yield node
            pending.extend(reversed(node.children))


class CurriculumHTMLParser(HTMLParser):
    VOID_ELEMENTS = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = CurriculumNode('root', {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = CurriculumNode(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        self.stack[-1].content.append(node)
        if tag not in self.VOID_ELEMENTS:
            if len(self.stack) >= 512:
                raise CurriculumParseError('カリキュラムHTMLの階層が深すぎます。')
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = CurriculumNode(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        self.stack[-1].content.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].content.append(data)


def _has_class(node, class_name):
    return class_name in node.attrs.get('class', '').split()


def _public_count(text, kind):
    names = {
        'sections': r'(?:num_sections|section_count|sections_count|numSections|sectionCount)',
        'lectures': r'(?:num_lectures|lecture_count|lectures_count|numLectures|lectureCount)',
    }[kind]
    values = {int(match) for match in re.findall(r'\b(\d+)\s+' + kind[:-1] + r's?\b', text, re.IGNORECASE)}
    values.update(int(match) for match in re.findall(r'["\']' + names + r'["\']\s*:\s*["\']?(\d+)', text))
    return values


def _duration_seconds(text):
    value = ' '.join(text.split()).strip()
    clock = re.search(r'(?<!\d)(\d{1,2}):([0-5]\d)(?::([0-5]\d))?(?!\d)', value)
    if clock:
        if clock.group(3) is not None:
            return int(clock.group(1)) * 3600 + int(clock.group(2)) * 60 + int(clock.group(3))
        return int(clock.group(1)) * 60 + int(clock.group(2))
    hours = re.search(r'(\d+)\s*(?:hours?|hrs?|h)\b', value, re.IGNORECASE)
    minutes = re.search(r'(\d+)\s*(?:minutes?|mins?|m)\b', value, re.IGNORECASE)
    localized = re.search(r'(?:(\d+)\s*時間)?\s*(?:(\d+)\s*分)?', value)
    seconds = 0
    if hours:
        seconds += int(hours.group(1)) * 3600
    if minutes:
        seconds += int(minutes.group(1)) * 60
    if not seconds and localized and (localized.group(1) or localized.group(2)):
        seconds = int(localized.group(1) or 0) * 3600 + int(localized.group(2) or 0) * 60
    return seconds or None


def parse_course_curriculum(markup):
    parser = CurriculumHTMLParser()
    try:
        parser.feed(markup)
        parser.close()
        nodes = list(parser.root.descendants())
        page_text = html.unescape(parser.root.text())
    except (ValueError, RecursionError, CurriculumParseError) as error:
        raise CurriculumParseError('公開カリキュラムHTMLを解析できませんでした: %s' % error)
    containers = [node for node in nodes if node.attrs.get('data-purpose') == 'course-curriculum']
    if not containers:
        raise CurriculumParseError('公開カリキュラムが見つかりませんでした。手動入力で登録してください。')
    curriculum = containers[0]
    elements = list(curriculum.descendants())
    section_nodes = [node for node in elements if _has_class(node, '__section-title')]
    lecture_nodes = [node for node in elements if _has_class(node, '__course-lecture-title')]
    section_counts = _public_count(page_text, 'sections')
    lecture_counts = _public_count(page_text, 'lectures')
    if len(section_counts) != 1 or len(lecture_counts) != 1:
        raise CurriculumParseError('公開されているセクション数・レクチャー数を特定できないため、登録を停止しました。')
    expected_sections = next(iter(section_counts))
    expected_lectures = next(iter(lecture_counts))
    if not section_nodes or not lecture_nodes:
        raise CurriculumParseError('公開カリキュラムのセクションまたはレクチャーが見つかりませんでした。')
    if len(section_nodes) != expected_sections or len(lecture_nodes) != expected_lectures:
        raise CurriculumParseError(
            '公開件数と取得件数が一致しないため登録を停止しました（セクション %s/%s、レクチャー %s/%s）。' %
            (len(section_nodes), expected_sections, len(lecture_nodes), expected_lectures)
        )

    sections = []
    current = None
    for node in elements:
        if _has_class(node, '__section-title'):
            title = ' '.join(node.text().split())
            if not title:
                raise CurriculumParseError('セクション名が空のため登録を停止しました。')
            current = {'title': title, 'position': len(sections) + 1, 'lectures': []}
            sections.append(current)
        elif _has_class(node, '__course-lecture-title'):
            title = ' '.join(node.text().split())
            if not title or current is None:
                raise CurriculumParseError('レクチャー名または所属セクションを特定できないため登録を停止しました。')
            summary = None
            ancestor = node.parent
            while ancestor is not None and ancestor is not curriculum:
                related_titles = [desc for desc in ancestor.descendants() if _has_class(desc, '__course-lecture-title')]
                summaries = [desc for desc in ancestor.descendants() if _has_class(desc, '__item-content-summary')]
                if len(related_titles) == 1 and summaries:
                    summary = summaries[0]
                    break
                ancestor = ancestor.parent
            current['lectures'].append({
                'title': title,
                'position': sum(len(section['lectures']) for section in sections) + 1,
                'duration_seconds': _duration_seconds(summary.text()) if summary else None,
            })
    if sum(len(section['lectures']) for section in sections) != expected_lectures:
        raise CurriculumParseError('レクチャー順序または所属セクションを検証できませんでした。')
    return {
        'complete': True,
        'expected_sections': expected_sections,
        'expected_lectures': expected_lectures,
        'sections': sections,
    }


def _resolve_udemy_addresses(host):
    result_queue = queue.Queue(maxsize=1)

    def resolve():
        try:
            result_queue.put(('ok', socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)))
        except OSError as error:
            result_queue.put(('error', error))

    worker = threading.Thread(target=resolve, daemon=True)
    worker.start()
    try:
        status, result = result_queue.get(timeout=CURRICULUM_TIMEOUT_SECONDS)
    except queue.Empty:
        raise CurriculumFetchError('UdemyのDNS解決がタイムアウトしました。')
    if status == 'error':
        raise CurriculumFetchError('Udemyのホスト名を解決できませんでした。')
    addresses = []
    for family, socktype, proto, _, sockaddr in result:
        try:
            address = ipaddress.ip_address(sockaddr[0].split('%', 1)[0])
        except ValueError:
            raise CurriculumFetchError('UdemyのDNS応答が不正です。')
        if not address.is_global:
            raise CurriculumFetchError('UdemyのDNS応答に公開されていないIPアドレスが含まれるため取得を停止しました。')
        if not addresses:
            addresses.append((family, socktype, proto, sockaddr))
    if not addresses:
        raise CurriculumFetchError('Udemyの公開IPアドレスを確認できませんでした。')
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address_info, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address_info = address_info

    def connect(self):
        family, socktype, proto, sockaddr = self.address_info
        raw_socket = socket.socket(family, socktype, proto)
        raw_socket.settimeout(self.timeout)
        try:
            raw_socket.connect(sockaddr)
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def fetch_udemy_course_html(url):
    canonical_url = normalize_udemy_course_url(url)
    parsed = urlsplit(canonical_url)
    address_info = _resolve_udemy_addresses(parsed.hostname)
    connection = _PinnedHTTPSConnection(parsed.hostname, address_info, CURRICULUM_TIMEOUT_SECONDS)
    try:
        path = parsed.path or '/'
        connection.request('GET', path, headers={
            'Host': parsed.hostname,
            'User-Agent': 'UdemyReflection/1.0 (public curriculum preview)',
            'Accept': 'text/html',
            'Accept-Encoding': 'identity',
            'Connection': 'close',
        })
        response = connection.getresponse()
        if response.status != 200:
            raise CurriculumFetchError('UdemyからHTTP %sが返されました。リダイレクトは追跡しません。' % response.status)
        content_length = response.getheader('Content-Length')
        if content_length:
            try:
                if int(content_length) > CURRICULUM_MAX_BYTES:
                    raise CurriculumFetchError('Udemyの応答サイズが上限を超えています。')
            except ValueError:
                raise CurriculumFetchError('Udemyの応答サイズを確認できませんでした。')
        body = response.read(CURRICULUM_MAX_BYTES + 1)
        if len(body) > CURRICULUM_MAX_BYTES:
            raise CurriculumFetchError('Udemyの応答サイズが上限を超えています。')
        try:
            return body.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise CurriculumFetchError('UdemyのHTMLをUTF-8として読み取れませんでした。')
    except CurriculumFetchError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError) as error:
        raise CurriculumFetchError('Udemyへの接続に失敗しました: %s' % error)
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, kind='application/json; charset=utf-8', filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/state':
            return self.reply(200, snapshot())
        if path == '/api/export.json':
            return self.reply(200, snapshot(), filename='reflection.json')
        if path == '/api/export/planning.json':
            data = snapshot()
            planning = {key: data[key] for key in ('courses', 'course_sections', 'lectures', 'study_plans', 'daily_reflections')}
            return self.reply(200, planning, filename='learning-plans.json')
        if path == '/api/export/learning.json':
            data = snapshot()
            learning = {key: data[key] for key in ('courses', 'course_sections', 'lectures', 'reflections', 'course_ratings')}
            return self.reply(200, learning, filename='course-learning.json')
        if path == '/api/export.csv':
            data = snapshot()
            courses = {r['id']: r for r in data['courses']}
            refs = {}
            for r in data['reflections']:
                refs.setdefault(r['lecture_id'], []).append(r)
            out = io.StringIO(newline='')
            writer = csv.writer(out)
            sections = {r['id']: r['title'] for r in data['course_sections']}
            writer.writerow(['course_id','course','course_url','section','lecture_id','lecture','lecture_order','duration_seconds','watched','reflection_id','understood','unclear','satisfaction','reason','understanding','recorded_at','notes'])
            for lecture in data['lectures']:
                for r in refs.get(lecture['id'], [{}]):
                    course = courses[lecture['course_id']]
                    row = [
                        lecture['course_id'], course['name'], course['course_url'],
                        sections.get(lecture.get('section_id'), ''), lecture['id'],
                        lecture['title'], lecture.get('position') or '', lecture.get('duration_seconds') or '',
                        lecture['watched'],
                    ] + [r.get(k, '') for k in ('id','understood','unclear','satisfaction','reason','understanding','recorded_at','notes')]
                    # Avoid formula evaluation when opened in spreadsheet software.
                    writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v for v in row])
            return self.reply(200, ('\ufeff' + out.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8', 'reflection.csv')
        if path == '/planning':
            path = '/planning.html'
        elif path == '/learning':
            path = '/learning.html'
        files = {
            '/': ('index.html', 'text/html; charset=utf-8'),
            '/planning.html': ('planning.html', 'text/html; charset=utf-8'),
            '/learning.html': ('learning.html', 'text/html; charset=utf-8'),
            '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
            '/style.css': ('style.css', 'text/css; charset=utf-8'),
        }
        if path in files:
            name, kind = files[path]
            return self.reply(200, (ROOT / 'static' / name).read_bytes(), kind)
        self.reply(404, {'error': '見つかりません'})

    def do_POST(self):
        # JSON + same-origin checks prevent other web pages from writing local data.
        expected = 'http://' + self.headers.get('Host', '')
        if self.headers.get('Origin') not in (None, expected) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.reply(403, {'error': 'アクセスできません'})
        if self.headers.get_content_type() != 'application/json':
            return self.reply(415, {'error': 'JSONが必要です'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1000000:
                raise ValueError('入力サイズが不正です')
            d = json.loads(self.rfile.read(size))
            if not isinstance(d, dict):
                raise ValueError('入力が不正です')
            with connect() as c:
                if self.path == '/api/courses':
                    course_url = d.get('course_url', '')
                    if not isinstance(course_url, str):
                        raise ValueError('Udemy講座URLの形式が正しくありません')
                    course_url = course_url.strip()
                    if course_url:
                        course_url = normalize_udemy_course_url(course_url)
                    c.execute('INSERT INTO courses(name,course_url) VALUES (?,?)', (required(d, 'name'), course_url))
                elif self.path == '/api/lectures':
                    course_id = int(d['course_id'])
                    section_id = None
                    section_name = d.get('section_name', '').strip()
                    if section_name:
                        existing = c.execute(
                            'SELECT id FROM course_sections WHERE course_id=? AND title=? ORDER BY position LIMIT 1',
                            (course_id, section_name),
                        ).fetchone()
                        if existing:
                            section_id = existing['id']
                        else:
                            section_position = c.execute(
                                'SELECT COALESCE(MAX(position), 0) + 1 FROM course_sections WHERE course_id=?',
                                (course_id,),
                            ).fetchone()[0]
                            section_id = c.execute(
                                'INSERT INTO course_sections(course_id,title,position) VALUES (?,?,?)',
                                (course_id, section_name, section_position),
                            ).lastrowid
                    position = c.execute(
                        'SELECT COALESCE(MAX(position), 0) + 1 FROM lectures WHERE course_id=?',
                        (course_id,),
                    ).fetchone()[0]
                    duration_minutes = d.get('duration_minutes')
                    if duration_minutes not in (None, ''):
                        duration_minutes = int(duration_minutes)
                        if duration_minutes < 0:
                            raise ValueError('時間は0分以上で入力してください')
                        duration_seconds = duration_minutes * 60
                    else:
                        duration_seconds = None
                    c.execute(
                        'INSERT INTO lectures(course_id,title,section_id,position,duration_seconds) VALUES (?,?,?,?,?)',
                        (course_id, required(d, 'title'), section_id, position, duration_seconds),
                    )
                elif self.path == '/api/watched':
                    if type(d['watched']) is not bool:
                        raise ValueError('視聴状態が不正です')
                    if c.execute('UPDATE lectures SET watched=? WHERE id=?', (int(d['watched']), int(d['id']))).rowcount != 1:
                        raise ValueError('単元が見つかりません')
                elif self.path == '/api/reflections':
                    scores = [d.get(k) for k in ('satisfaction', 'understanding')]
                    if any(type(v) is not int or not 1 <= v <= 5 for v in scores):
                        raise ValueError('評価は1〜5です')
                    stamp = required(d, 'recorded_at')
                    parse_recorded_at(stamp)
                    values = [int(d['lecture_id'])] + [str(d.get(k, '')) for k in ('understood','unclear')] + [scores[0], required(d, 'reason'), scores[1], stamp, str(d.get('notes', ''))]
                    c.execute('INSERT INTO reflections(lecture_id,understood,unclear,satisfaction,reason,understanding,recorded_at,notes) VALUES (?,?,?,?,?,?,?,?)', values)
                elif self.path == '/api/course-ratings':
                    scores = [d.get(k) for k in ('understanding', 'instructor_match')]
                    if any(type(v) is not int or not 1 <= v <= 5 for v in scores):
                        raise ValueError('評価は1〜5です')
                    stamp = required(d, 'recorded_at')
                    parse_recorded_at(stamp)
                    c.execute(
                        'INSERT INTO course_ratings(course_id,understanding,understanding_reason,instructor_match,instructor_match_reason,recorded_at) VALUES (?,?,?,?,?,?)',
                        (int(d['course_id']), scores[0], required(d, 'understanding_reason'), scores[1], required(d, 'instructor_match_reason'), stamp),
                    )
                elif self.path == '/api/course-curriculum/preview':
                    course_id = int(d['course_id'])
                    course = c.execute('SELECT course_url FROM courses WHERE id=?', (course_id,)).fetchone()
                    if not course:
                        raise ValueError('講座が見つかりません')
                    if not course['course_url']:
                        raise ValueError('講座にUdemy URLが登録されていません。講座情報を確認してください。')
                    canonical_url = normalize_udemy_course_url(course['course_url'])
                    try:
                        curriculum = parse_course_curriculum(fetch_udemy_course_html(canonical_url))
                    except CurriculumFetchError as error:
                        return self.reply(502, {'error': str(error) + ' 講座の学習記録ページから手動入力してください。'})
                    except CurriculumParseError as error:
                        return self.reply(422, {'error': str(error) + ' 講座の学習記録ページから手動入力してください。'})
                    token = secrets.token_urlsafe(32)
                    c.execute('DELETE FROM curriculum_previews WHERE created_at < ?', (int(time.time()) - 3600,))
                    c.execute(
                        'INSERT INTO curriculum_previews(token,course_id,items_json,created_at) VALUES (?,?,?,?)',
                        (token, course_id, json.dumps(curriculum, ensure_ascii=False), int(time.time())),
                    )
                    return self.reply(200, {'token': token, 'course_id': course_id, 'course_url': canonical_url, **curriculum})
                elif self.path == '/api/course-curriculum/register':
                    token = required(d, 'token')
                    c.execute('BEGIN IMMEDIATE')
                    preview = c.execute(
                        'SELECT course_id,items_json,created_at FROM curriculum_previews WHERE token=?',
                        (token,),
                    ).fetchone()
                    if not preview or preview['created_at'] < int(time.time()) - 3600:
                        raise ValueError('プレビューが見つからないか、有効期限が切れています。もう一度取得してください。')
                    curriculum = json.loads(preview['items_json'])
                    if curriculum.get('complete') is not True:
                        raise ValueError('件数照合済みの完全なプレビューではないため、登録できません。')
                    course_id = preview['course_id']
                    section_offset = c.execute(
                        'SELECT COALESCE(MAX(position), 0) FROM course_sections WHERE course_id=?',
                        (course_id,),
                    ).fetchone()[0]
                    lecture_offset = c.execute(
                        'SELECT COALESCE(MAX(position), 0) FROM lectures WHERE course_id=?',
                        (course_id,),
                    ).fetchone()[0]
                    inserted_sections = 0
                    inserted_lectures = 0
                    for section in curriculum['sections']:
                        section_id = c.execute(
                            'INSERT INTO course_sections(course_id,title,position) VALUES (?,?,?)',
                            (course_id, section['title'], section_offset + section['position']),
                        ).lastrowid
                        inserted_sections += 1
                        for lecture in section['lectures']:
                            c.execute(
                                'INSERT INTO lectures(course_id,title,watched,section_id,position,duration_seconds) VALUES (?,?,0,?,?,?)',
                                (
                                    course_id, lecture['title'], section_id,
                                    lecture_offset + lecture['position'], lecture['duration_seconds'],
                                ),
                            )
                            inserted_lectures += 1
                    c.execute('DELETE FROM curriculum_previews WHERE token=?', (token,))
                    return self.reply(200, {'ok': True, 'sections_added': inserted_sections, 'lectures_added': inserted_lectures})
                elif self.path == '/api/plans':
                    plan_date = parse_date(required(d, 'plan_date'))
                    if d.get('lecture_id') not in (None, ''):
                        lecture_id = int(d['lecture_id'])
                    else:
                        course_name = required(d, 'course_name')
                        lecture_title = required(d, 'lecture_title')
                        course = c.execute('SELECT id FROM courses WHERE name=? ORDER BY id LIMIT 1', (course_name,)).fetchone()
                        if course:
                            course_id = course['id']
                        else:
                            course_id = c.execute('INSERT INTO courses(name) VALUES (?)', (course_name,)).lastrowid
                        lecture = c.execute('SELECT id FROM lectures WHERE course_id=? AND title=? ORDER BY id LIMIT 1', (course_id, lecture_title)).fetchone()
                        if lecture:
                            lecture_id = lecture['id']
                        else:
                            lecture_id = c.execute('INSERT INTO lectures(course_id,title) VALUES (?,?)', (course_id, lecture_title)).lastrowid
                    c.execute('INSERT INTO study_plans(plan_date,lecture_id) VALUES (?,?)', (plan_date, lecture_id))
                elif self.path == '/api/daily-reflections':
                    reflection_date = parse_date(required(d, 'reflection_date'))
                    if type(d.get('plan_completed')) is not bool:
                        raise ValueError('計画の実施状況が不正です')
                    minutes = d.get('study_minutes')
                    rating = d.get('self_rating')
                    if type(minutes) is not int or minutes < 0:
                        raise ValueError('学習時間は0分以上で入力してください')
                    if type(rating) is not int or not 1 <= rating <= 5:
                        raise ValueError('自己評価は1〜5です')
                    stamp = required(d, 'recorded_at')
                    parse_recorded_at(stamp)
                    c.execute(
                        'INSERT INTO daily_reflections(reflection_date,plan_completed,study_minutes,self_rating,improvement,tomorrow_plan,recorded_at) VALUES (?,?,?,?,?,?,?)',
                        (reflection_date, int(d['plan_completed']), minutes, rating, required(d, 'improvement'), required(d, 'tomorrow_plan'), stamp),
                    )
                else:
                    return self.reply(404, {'error': '見つかりません'})
            self.reply(200, {'ok': True})
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as e:
            self.reply(400, {'error': str(e)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    init()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('MetaWatch（メタウォッチ）: http://127.0.0.1:%s (終了: Ctrl+C)' % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
