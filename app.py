#!/usr/bin/env python3
"""Local Udemy reflection journal. Python standard library only."""
import argparse
import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('REFLECTION_DB', str(ROOT / 'data' / 'reflection.sqlite3')))


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


def snapshot():
    with connect() as c:
        return {name: [dict(r) for r in c.execute('SELECT * FROM ' + name + ' ORDER BY id')] for name in ('courses', 'lectures', 'reflections')}


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
        if path == '/api/export.csv':
            data = snapshot()
            courses = {r['id']: r['name'] for r in data['courses']}
            refs = {}
            for r in data['reflections']:
                refs.setdefault(r['lecture_id'], []).append(r)
            out = io.StringIO(newline='')
            writer = csv.writer(out)
            writer.writerow(['course_id','course','lecture_id','lecture','watched','reflection_id','understood','unclear','satisfaction','reason','understanding','recorded_at','notes'])
            for lecture in data['lectures']:
                for r in refs.get(lecture['id'], [{}]):
                    row = [lecture['course_id'], courses[lecture['course_id']], lecture['id'], lecture['title'], lecture['watched']] + [r.get(k, '') for k in ('id','understood','unclear','satisfaction','reason','understanding','recorded_at','notes')]
                    # Avoid formula evaluation when opened in spreadsheet software.
                    writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v for v in row])
            return self.reply(200, ('\ufeff' + out.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8', 'reflection.csv')
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
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
                    c.execute('INSERT INTO courses(name) VALUES (?)', (required(d, 'name'),))
                elif self.path == '/api/lectures':
                    c.execute('INSERT INTO lectures(course_id,title) VALUES (?,?)', (int(d['course_id']), required(d, 'title')))
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
    print('学習リフレクション: http://127.0.0.1:%s (終了: Ctrl+C)' % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
