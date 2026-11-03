import collections
import datetime as dt
import functools
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.request

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash
from monitor.cloudflare import resolve_client

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'), static_url_path='/monitor/static')
app.config.update(SECRET_KEY=os.environ.get('MONITOR_SECRET'), SESSION_COOKIE_NAME='redis_monitor',
                  SESSION_COOKIE_PATH='/monitor', SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=os.environ.get('MONITOR_INSECURE_LOCAL') != '1',
                  SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12),
                  MAX_CONTENT_LENGTH=4096)
LOG = Path(os.environ.get('MONITOR_LOG', '/logs/logs.log'))
PROC = Path(os.environ.get('MONITOR_PROC', '/host/proc'))
API = os.environ.get('MONITOR_API', 'http://127.0.0.1:16379')
PATTERN = re.compile(r'^(\S+) .*?\[([^]]+)\] "(\S+) ([^ ]+) [^"]+" (\d{3}) (\S+) "[^"]*" "([^"]*)"(?: "([^"]*)")?(?: "([^"]*)" "([^"]*)" "([^"]*)")?')


@functools.lru_cache(maxsize=4096)
def log_time(value):
    return int(dt.datetime.strptime(value, '%d/%b/%Y:%H:%M:%S %z').timestamp())


def parse_line(line):
    match = PATTERN.match(line)
    if not match:
        return None
    peer, date, method, target, status, size, agent, forwarded, cf_ip, cf_ipv6, ray = match.groups()
    path = target.split('?', 1)[0][:200]
    if path.startswith('/monitor'):
        return None
    try:
        client, edge, source = resolve_client(peer, forwarded, cf_ip, cf_ipv6)
        return (log_time(date), client, method, path, int(status),
                int(size) if size.isdigit() else 0, agent[:160], edge, source,
                (ray or '')[:100] if source == 'cloudflare' else '')
    except (ValueError, OverflowError):
        return None


class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.buckets = {}
        self.recent = collections.deque(maxlen=500)
        self.resources = collections.deque(maxlen=720)
        self.started = int(time.time())
        self.coverage = None
        self.log_error = None
        self.system_error = None
        self.inventory_error = None
        self.inventory = None
        self.inventory_at = None
        self.system = {}
        self.last_sample = None
        self.thread = None
        self.position = None
        self.inode = None
        self.previous_cpu = None
        self.previous_net = None
        self.previous_at = None
        self.pending = b''

    def ingest(self, event):
        stamp, ip, method, path, status, size, agent, edge, source, ray = event
        if stamp < time.time() - 3600 or stamp > time.time() + 60:
            return
        minute = stamp // 60
        bucket = self.buckets.setdefault(minute, {'count': 0, 'errors': 0, 'bytes': 0,
            'ips': collections.Counter(), 'routes': collections.Counter(), 'unresolved_ips': 0})
        bucket['count'] += 1
        bucket['errors'] += status >= 400
        bucket['bytes'] += size
        if source == 'cloudflare_missing':
            bucket['unresolved_ips'] += 1
        elif ip in bucket['ips'] or len(bucket['ips']) < 2000:
            bucket['ips'][ip] += 1
        bucket['routes'][path if path in bucket['routes'] or len(bucket['routes']) < 200 else '其他'] += 1
        self.recent.append(event)
        self.coverage = min(self.coverage or stamp, stamp)

    def read_log(self):
        stat = LOG.stat()
        rotated = self.inode is not None and (stat.st_ino != self.inode or stat.st_size < (self.position or 0))
        if self.position is None or rotated:
            self.position = max(0, stat.st_size - 8 * 1024 * 1024) if self.position is None else 0
            self.pending = b''
            self.inode = stat.st_ino
            skip = self.position > 0
        else:
            skip = False
        with LOG.open('rb') as handle:
            handle.seek(self.position)
            if skip:
                handle.readline()
            data = handle.read(8 * 1024 * 1024)
            self.position = handle.tell()
        lines = (self.pending + data).split(b'\n')
        self.pending = lines.pop()[-65536:]
        for line in lines:
            event = parse_line(line.decode('utf-8', errors='replace'))
            if event:
                self.ingest(event)
        cutoff = int(time.time()) // 60 - 59
        for minute in list(self.buckets):
            if minute < cutoff:
                del self.buckets[minute]

    def read_system(self):
        now = time.monotonic()
        lines = (PROC / 'stat').read_text().splitlines()
        fields = list(map(int, lines[0].split()[1:9]))
        total, idle = sum(fields), fields[3] + fields[4]
        cpu = None
        if self.previous_cpu:
            diff = total - self.previous_cpu[0]
            cpu = max(0, min(100, 100 * (1 - (idle - self.previous_cpu[1]) / diff))) if diff > 0 else 0
        self.previous_cpu = total, idle
        mem = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in (PROC / 'meminfo').read_text().splitlines()}
        net = [0, 0]
        for line in (PROC / 'net/dev').read_text().splitlines()[2:]:
            interface, counters = line.split(':')
            if interface.strip() == 'lo' or interface.strip().startswith(('veth', 'br-', 'docker')):
                continue
            values = list(map(int, counters.split()))
            net[0] += values[0]
            net[1] += values[8]
        rates = [0, 0]
        if self.previous_net and self.previous_at:
            rates = [max(0, (net[i] - self.previous_net[i]) / (now - self.previous_at)) for i in range(2)]
        self.previous_net, self.previous_at = net, now
        disk = shutil.disk_usage(LOG.parent if LOG.parent.exists() else '/')
        load = [float(n) for n in (PROC / 'loadavg').read_text().split()[:3]]
        self.system = {'cpu': cpu, 'cores': sum(line.startswith('cpu') for line in lines) - 1,
            'memory_total': mem['MemTotal'], 'memory_used': mem['MemTotal'] - mem.get('MemAvailable', mem['MemFree']),
            'swap_total': mem['SwapTotal'], 'swap_used': mem['SwapTotal'] - mem['SwapFree'],
            'disk_total': disk.total, 'disk_used': disk.used, 'load': load,
            'uptime': float((PROC / 'uptime').read_text().split()[0]), 'net_in': rates[0], 'net_out': rates[1]}
        self.resources.append({'time': int(time.time()), 'cpu': cpu,
            'memory': round(100 * self.system['memory_used'] / mem['MemTotal'], 1)})

    def run(self):
        while True:
            with self.lock:
                try:
                    self.read_log()
                    self.log_error = None
                except (OSError, ValueError) as exc:
                    self.log_error = type(exc).__name__
                try:
                    self.read_system()
                    self.system_error = None
                except (OSError, ValueError, ZeroDivisionError, KeyError) as exc:
                    self.system_error = type(exc).__name__
                self.last_sample = int(time.time())
            try:
                with urllib.request.urlopen(API + '/api/counts', timeout=3) as response:
                    data = json.load(response)
                if data.get('code') != 1:
                    raise ValueError('Invalid inventory response')
                with self.lock:
                    self.inventory = {name: int(data[name]) for name in ('slides', 'yiduns')}
                    self.inventory_at = int(time.time())
                    self.inventory_error = None
            except Exception as exc:
                with self.lock:
                    self.inventory_error = type(exc).__name__
            time.sleep(5)

    def start(self):
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, daemon=True, name='monitor-collector')
                self.thread.start()

    def snapshot(self, minutes):
        now = int(time.time())
        first = now // 60 - minutes + 1
        with self.lock:
            buckets = [self.buckets.get(m, {}) for m in range(first, now // 60 + 1)]
            total = sum(b.get('count', 0) for b in buckets)
            errors = sum(b.get('errors', 0) for b in buckets)
            ips, routes = collections.Counter(), collections.Counter()
            for bucket in buckets:
                ips.update(bucket.get('ips', {}))
                routes.update(bucket.get('routes', {}))
            series = [{'time': m * 60, 'count': self.buckets.get(m, {}).get('count', 0),
                       'errors': self.buckets.get(m, {}).get('errors', 0)} for m in range(first, now // 60 + 1)]
            recent = [dict(zip(('time', 'ip', 'method', 'path', 'status', 'bytes', 'agent', 'edge_ip', 'ip_source', 'ray_id'), event))
                      for event in reversed(self.recent) if event[0] >= first * 60][:100]
            observed = max(1, now - max(first * 60, self.coverage or now) + 1)
            return {'time': now, 'started': self.started, 'coverage': self.coverage,
                'last_sample': self.last_sample, 'window': minutes,
                'summary': {'total': total, 'errors': errors, 'unique_ips': len(ips),
                    'unresolved_ips': sum(b.get('unresolved_ips', 0) for b in buckets),
                    'rpm': round(total * 60 / observed, 1), 'success': round(100 * (total - errors) / total, 2) if total else None},
                'series': series, 'ips': [{'ip': ip, 'count': count} for ip, count in ips.most_common(10)],
                'routes': [{'path': path, 'count': count} for path, count in routes.most_common(8)],
                'recent': recent, 'system': self.system.copy(),
                'resources': list(self.resources)[-minutes * 12:],
                'inventory': self.inventory, 'inventory_at': self.inventory_at,
                'errors': {'log': self.log_error, 'system': self.system_error, 'inventory': self.inventory_error}}


collector = Collector()
attempts = collections.deque(maxlen=30)
auth_lock = threading.Lock()


@app.before_request
def guard():
    if request.path.startswith('/monitor/static/') or request.path == '/monitor/login':
        return None
    if not session.get('authenticated'):
        if request.path.startswith('/monitor/api/'):
            return jsonify({'error': '请先登录'}), 401
        return redirect('/monitor/login')
    return None


@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


@app.route('/monitor/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        with auth_lock:
            now = time.monotonic()
            while attempts and attempts[0] < now - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return render_template('login.html', error='尝试次数过多，请一分钟后重试。'), 429
            attempts.append(now)
        expected = os.environ.get('MONITOR_PASSWORD_HASH', '')
        if expected and app.secret_key and check_password_hash(expected, request.form.get('password', '')[:256]):
            session.clear()
            session['authenticated'] = True
            session.permanent = True
            return redirect('/monitor/')
        error = '访问密码不正确。'
    return render_template('login.html', error=error), 401 if error else 200


@app.post('/monitor/logout')
def logout():
    session.clear()
    return redirect('/monitor/login')


@app.get('/monitor/')
def dashboard():
    return render_template('index.html')


@app.get('/monitor/api/snapshot')
def snapshot():
    minutes = request.args.get('minutes', '15')
    if minutes not in ('5', '15', '60'):
        return jsonify({'error': '时间范围只能是 5、15、60 分钟'}), 400
    collector.start()
    return jsonify(collector.snapshot(int(minutes)))


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=16380, threaded=True)
