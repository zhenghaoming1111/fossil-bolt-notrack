"""Reproducible Fossil HTTP / SQLite / BOLT experiment (run under Linux).

The operation mixes are designed experiments, never claimed as production traces.
No third-party server is load-tested: HTTP replay binds to 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import hashlib
import collections
import concurrent.futures
import gzip
import html.parser
import http.client
import itertools
import json
import math
import os
from pathlib import Path
import random
import re
import resource
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlencode

DEFAULT_LAB = Path('/home/zhm/bolt-lab/fossil')
BOLT_ROOT = Path('/home/zhm/bolt-lab/notrack-fix-18.1.3')
TARGETS = ('sqlite3VdbeExec', 'vdbeRecordCompareInt')
VARIANTS = {'A-original': 'fossil.orig', 'B-before-fix': 'fossil.before',
            'C-after-fix': 'fossil.after'}
MIXES = {
    'browse': {'timeline': 30, 'commit': 25, 'directory': 15, 'file': 15, 'history': 10, 'diff': 5},
    'review': {'timeline': 35, 'commit': 10, 'directory': 5, 'file': 5, 'history': 35, 'diff': 10},
}
WEB_MIXES = dict(MIXES)
DASHBOARD_MIXES = {
    'dashboard': {'report_file': 40, 'report_user': 30, 'report_month': 15, 'report_year': 15},
}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run(lab, label, command, *, cwd=None, env=None, timeout=600, allow_failure=False):
    log = lab / 'logs'
    log.mkdir(exist_ok=True, parents=True)
    command = [str(item) for item in command]
    save(log / (label + '.command.json'), command)
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    proc = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    (log / (label + '.stdout.txt')).write_bytes(proc.stdout)
    (log / (label + '.stderr.txt')).write_bytes(proc.stderr)
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    save(log / (label + '.status.json'), {'returncode': proc.returncode,
        'elapsed_seconds': time.monotonic() - started,
        'user_seconds': cpu_after.ru_utime - cpu_before.ru_utime,
        'system_seconds': cpu_after.ru_stime - cpu_before.ru_stime})
    if proc.returncode and not allow_failure:
        raise RuntimeError(f'{label} failed ({proc.returncode}); see {log}/{label}.stderr.txt')
    return proc.stdout.decode('utf-8', errors='replace')


def prepare(args):
    lab = args.lab
    for directory in ('downloads', 'src', 'bin', 'data', 'profiles', 'logs', 'scripts', 'results'):
        (lab / directory).mkdir(parents=True, exist_ok=True)
    archive = lab / 'downloads/fossil-src-2.28.tar.gz'
    url = 'https://fossil-scm.org/home/tarball/version-2.28/fossil-src-2.28.tar.gz'
    try:
        with tarfile.open(archive) as bundle:
            bundle.getmembers()
    except (OSError, tarfile.TarError, EOFError):
        run(lab, 'source-download', ['curl', '-4', '-fsSL', '--retry', '2',
            '--connect-timeout', '15', '--max-time', '600', '-o', archive, url], timeout=1250)
    source = lab / 'src/fossil-2.28'
    if not source.exists():
        source.mkdir()
        run(lab, 'extract-source', ['tar', '-xzf', archive, '--strip-components=1', '-C', source])
    deps = lab / 'deps'
    devs = list((lab / 'downloads').glob('libssl-dev_*.deb'))
    if not Path('/usr/include/openssl/ssl.h').exists():
        if not devs:
            run(lab, 'openssl-download', ['apt-get', '-o', 'Acquire::ForceIPv4=true',
                'download', 'libssl-dev'], cwd=lab / 'downloads')
            devs = list((lab / 'downloads').glob('libssl-dev_*.deb'))
        if len(devs) != 1:
            raise RuntimeError('Need exactly one recorded OpenSSL development package.')
        run(lab, 'openssl-extract', ['dpkg-deb', '-x', devs[0], deps])
        links = deps / 'linklib'
        links.mkdir(exist_ok=True)
        for name in ('ssl', 'crypto'):
            target = Path(f'/usr/lib/x86_64-linux-gnu/lib{name}.so.3')
            if not target.exists():
                raise RuntimeError(f'Missing system runtime {target}')
            (links / f'lib{name}.so').unlink(missing_ok=True)
            (links / f'lib{name}.so').symlink_to(target)
    cflags = '-O2 -g -fno-omit-frame-pointer -fno-reorder-blocks-and-partition -fno-pie -no-pie'
    ldflags = '-no-pie -Wl,--emit-relocs'
    if not Path('/usr/include/openssl/ssl.h').exists():
        cflags += f' -I{deps}/usr/include -I{deps}/usr/include/x86_64-linux-gnu'
        ldflags += f' -L{deps}/linklib'
        prefix = deps / 'ssl-prefix'
        prefix.mkdir(exist_ok=True)
        for name, destination in (('include', deps / 'usr/include'), ('lib', deps / 'linklib')):
            link = prefix / name
            if not link.exists():
                link.symlink_to(destination, target_is_directory=True)
    env = dict(os.environ, CFLAGS=cflags, LDFLAGS=ldflags)
    original = lab / 'bin/fossil.orig'
    if not original.exists():
        print('Configure and build Fossil 2.28 with its own bundled SQLite; normal -O2.', flush=True)
        configure = ['./configure', '--disable-fusefs']
        if not Path('/usr/include/openssl/ssl.h').exists():
            configure.append('--with-openssl=' + str(deps / 'ssl-prefix'))
        run(lab, 'configure', configure, cwd=source, env=env)
        run(lab, 'make', ['make', '-j4'], cwd=source, env=env, timeout=1800)
        shutil.copy2(source / 'fossil', original)
    info = {
        'fossil_source_url': url, 'source_archive_sha256': digest(archive),
        'source': str(source), 'original_sha256': digest(original),
        'cflags': cflags, 'ldflags': ldflags, 'cpu': args.cpu,
        'bolt_before': str(args.before), 'bolt_after': str(args.after),
        'bolt_before_sha256': digest(args.before), 'bolt_after_sha256': digest(args.after),
        'fossil_version': run(lab, 'version', [original, 'version', '-v']),
        'gcc_version': run(lab, 'gcc-version', ['gcc', '--version']),
        'uname': run(lab, 'uname', ['uname', '-a']),
        'cpu_info': run(lab, 'cpu-info', ['lscpu']),
        'openssl_headers_debs': {str(p): digest(p) for p in devs},
    }
    run(lab, 'original-sections', ['readelf', '-SW', original])
    run(lab, 'original-symbols', ['nm', '-S', original])
    for symbol in TARGETS:
        run(lab, 'original-' + symbol, ['objdump', '-d', '--disassemble=' + symbol, original])
    for label, bolt in (('before', args.before), ('after', args.after)):
        run(lab, label + '-cfg', [bolt, original, '-o', lab / 'bin' / ('probe.' + label),
            '--lite=0', '--no-threads', '--sequential-disassembly', '--print-cfg',
            '-v=2', '--print-only=' + ','.join(
                name + suffix for name in TARGETS for suffix in ('', '/1', '/2'))], timeout=180)
    save(lab / 'environment.json', info)
    print(info['fossil_version'], flush=True)
    print('Environment and recognition probes saved: ' + str(lab), flush=True)


def data(args):
    lab = args.lab
    original = lab / 'bin/fossil.orig'
    upstream = lab / 'data/upstream.fossil'
    if not upstream.exists():
        print('Clone the real public Fossil repository once; no load test against upstream.', flush=True)
        run(lab, 'clone', [original, 'clone', '--no-open', '--once',
            'https://fossil-scm.org/home', upstream], timeout=1200)
    if Path(str(upstream) + '-journal').exists():
        raise RuntimeError('Clone has not finished (journal exists).')
    run(lab, 'repository-check', [original, 'test-integrity', '--db-only', '-R', upstream])
    frozen = lab / 'data/snapshot.fossil'
    if not frozen.exists():
        shutil.copyfile(upstream, frozen)
        with sqlite3.connect(frozen) as db:
            # Change only private mirror settings, never the live upstream.
            for name, value in (('backoffice-disable', '1'), ('redirect-to-https', '0')):
                db.execute('INSERT OR REPLACE INTO config(name,value,mtime) VALUES(?,?,?)',
                           (name, value, int(time.time())))
            for user in ('nobody', 'anonymous'):
                cap = db.execute('SELECT cap FROM user WHERE login=?', (user,)).fetchone()[0]
                db.execute('UPDATE user SET cap=? WHERE login=?',
                           (''.join(sorted(set(cap + 'hjor'))), user))
    with sqlite3.connect(f'file:{frozen}?mode=ro', uri=True) as db:
        commits = db.execute(
            "SELECT b.uuid FROM event e JOIN blob b ON b.rid=e.objid "
            "WHERE e.type='ci' ORDER BY e.mtime DESC LIMIT 1200").fetchall()
        tip = commits[0][0]
        files = db.execute(
            "SELECT f.name,COUNT(*) FROM filename f JOIN mlink m ON m.fnid=f.fnid "
            "WHERE f.name GLOB 'src/*.c' AND m.fid>0 GROUP BY f.name "
            "HAVING COUNT(*)>=10 ORDER BY f.name").fetchall()
        diffs = db.execute(
            "SELECT old.uuid,new.uuid,f.name FROM mlink m JOIN filename f ON f.fnid=m.fnid "
            "JOIN blob old ON old.rid=m.pid JOIN blob new ON new.rid=m.fid "
            "WHERE f.name GLOB 'src/*.c' AND m.pid>0 AND m.fid>0 "
            "ORDER BY m.mid DESC,m.fnid LIMIT 1500").fetchall()
        users = db.execute("SELECT coalesce(euser,user),COUNT(*) FROM event WHERE type='ci' "
            "GROUP BY 1 HAVING COUNT(*)>=100 ORDER BY COUNT(*) DESC LIMIT 15").fetchall()
        info = {'upstream_url': 'https://fossil-scm.org/home',
                'snapshot_sha256': digest(frozen), 'upstream_sha256': digest(upstream),
                'snapshot_bytes': frozen.stat().st_size, 'tip': commits[0][0],
                'checkins': db.execute("SELECT COUNT(*) FROM event WHERE type='ci'").fetchone()[0],
                'artifacts': db.execute('SELECT COUNT(*) FROM blob').fetchone()[0],
                'training_seed': 42, 'evaluation_seed': 43,
                'mixes_are_designed_not_observed_traffic': True, 'mixes': MIXES}
    # filename lists historical names too, including files since deleted.
    # Validate names against the exact check-in used by /file requests.
    listing = run(lab, 'current-file-list', [original, 'ls', '-R', frozen, '-r', tip])
    current_files = set(listing.splitlines())
    files = [item for item in files if item[0] in current_files]
    info['current_source_files_used'] = len(files)
    info['valid_file_checkin'] = tip
    if min(len(commits), len(files), len(diffs)) < 10:
        raise RuntimeError('Insufficient repository history for the workload.')
    traces = lab / 'data/traces'
    traces.mkdir(exist_ok=True)
    for seed in (42, 43, 44):
        for case, mix in MIXES.items():
            rng = random.Random(seed)
            requests = []
            for kind, count in mix.items():
                for _ in range(count):
                    commit = rng.choice(commits)[0]
                    filename = rng.choice(files)[0]
                    if kind == 'timeline':
                        query = {'y': 'ci', 'n': 50 if case == 'browse' else 100, 'c': commit}
                        endpoint = 'timeline'
                    elif kind == 'commit':
                        endpoint, query = 'ci/' + commit, {}
                    elif kind == 'directory':
                        endpoint, query = 'dir', {'ci': commit, 'name': 'src'}
                    elif kind == 'file':
                        endpoint, query = 'file', {'ci': info['tip'], 'name': filename}
                    elif kind == 'history':
                        endpoint, query = 'finfo', {'name': filename, 'n': 50 if case == 'browse' else 100}
                    elif kind.startswith('report_'):
                        views = {'report_file': 'byfile', 'report_user': 'byuser',
                                 'report_month': 'bymonth', 'report_year': 'byyear'}
                        endpoint, query = 'reports', {'view': views[kind]}
                        if kind in ('report_month', 'report_year'):
                            query['type'] = 'ci'
                        # byfile does not implement from/to filtering, so use
                        # its documented user filter rather than fictitious dates.
                        if kind != 'report_user' and rng.random() < 0.8:
                            query['user'] = rng.choice(users)[0]
                    else:
                        old, new, _ = rng.choice(diffs)
                        endpoint, query = 'fdiff', {'v1': old, 'v2': new, 'sbs': 0}
                    path = '/' + endpoint + ('?' + urlencode(query) if query else '')
                    requests.append({'kind': kind, 'path': path})
            rng.shuffle(requests)
            save(traces / f'{case}-seed{seed}.json', requests)
    save(lab / 'dataset.json', info)
    print(f'Real repository: {info["checkins"]} check-ins, {info["artifacts"]} artifacts, '
          f'{info["snapshot_bytes"] / 1024**2:.1f} MiB. Frozen traces saved.', flush=True)


class VisibleText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)


def semantic_digest(body):
    # Ignore script nonces, CSRF attributes and timing footer, preserving
    # the actual visible repository content.
    text = body.decode('utf-8', errors='strict')
    text = text.split('<div class="footer"', 1)[0].split('<footer>', 1)[0]
    parser = VisibleText()
    parser.feed(text)
    visible = ' '.join(' '.join(parser.parts).split())
    return hashlib.sha256(visible.encode()).hexdigest(), visible


class Server:
    def __init__(self, args, binary, label, *, instrumented=False):
        self.args, self.binary, self.label = args, Path(binary), label
        self.instrumented = instrumented
        self.cookie = ''

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='http-', dir=self.args.lab / 'results')
        self.repo = Path(self.temp.name) / 'repository.fossil'
        shutil.copyfile(self.args.lab / 'data/snapshot.fossil', self.repo)
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            self.port = reserve.getsockname()[1]
        self.stdout = (self.args.lab / 'logs' / (self.label + '.server.stdout.txt')).open('wb')
        self.stderr = (self.args.lab / 'logs' / (self.label + '.server.stderr.txt')).open('wb')
        command = ['taskset', '-c', str(self.args.cpu), str(self.binary), 'server',
                   '--localhost', '--port', '127.0.0.1:' + str(self.port),
                   '--baseurl', 'http://localhost:8080', '--nossl', str(self.repo)]
        save(self.args.lab / 'logs' / (self.label + '.server.command.json'), command)
        self.process = subprocess.Popen(command, stdout=self.stdout, stderr=self.stderr, start_new_session=True)
        try:
            for _ in range(300):
                if self.process.poll() is not None:
                    raise RuntimeError(f'{self.label}: server exited; inspect server logs.')
                try:
                    self.request({'path': '/robots.txt', 'kind': 'health'})
                    break
                except (ConnectionError, OSError):
                    time.sleep(0.02)
            else:
                raise RuntimeError('Server did not become ready.')
            # Complete Fossil's own anonymous-browser verification outside timing.
            self.request({'path': '/finfo?name=src%2Fdb.c&n=1', 'kind': 'auth'})
            time.sleep(0.15)
            # Periodic BOLT profile dumper is the only persistent child.
            self.persistent_children = set(self.children()) if self.instrumented else set()
            self.drain()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def request(self, request, verify=False):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        started = time.perf_counter()
        try:
            connection.request('GET', request['path'], headers={
                'Host': 'localhost:8080', 'User-Agent': 'Mozilla/5.0 Fossil-BOLT-Lab/1.0',
                'Accept': 'text/html', 'Accept-Encoding': 'gzip', 'Connection': 'close',
                'Cookie': self.cookie})
            response = connection.getresponse()
            body = response.read()
            elapsed = time.perf_counter() - started
            wire_bytes = len(body)
            if response.getheader('Content-Encoding') == 'gzip':
                body = gzip.decompress(body)
            cookie = response.getheader('Set-Cookie')
            if cookie and 'fossil-client-ok=' in cookie:
                self.cookie = cookie.split(';', 1)[0]
            if b'<title>Fossil: Browser Verification</title>' in body:
                if 'proof=' in request['path']:
                    raise RuntimeError('Browser verification did not accept the computed proof.')
                factors = re.search(rb'return \(a\*(\d+)\+b\)\*(\d+)\+c;', body)
                inputs = re.search(rb'bbb\(ccc\((\d+),(\d+),(\d+)\),(\d+)\)', body)
                if not factors or not inputs:
                    raise RuntimeError('Unexpected Fossil browser challenge; inspect robot.c.')
                a, b = map(int, factors.groups())
                x, y, z, delay = map(int, inputs.groups())
                proof = (x * a + y) * b + z + delay * (delay + 1) // 2
                separator = '&' if '?' in request['path'] else '?'
                return self.request({**request, 'path': request['path'] + separator + 'proof=' + str(proof)}, verify)
            if response.status != 200 or (request['kind'] != 'health' and len(body) < 200):
                raise RuntimeError(f'HTTP {response.status}: {request["path"]}; {body[:250]!r}')
            result = {'kind': request['kind'], 'latency_seconds': elapsed, 'wire_bytes': wire_bytes}
            if verify:
                result['semantic_sha256'], visible = semantic_digest(body)
                title = re.search(rb'<title>(.*?)</title>', body, re.S)
                if title and any(word in title[1] for word in (
                        b': Login', b'Access Denied', b'Browser Verification',
                        b'No such file', b'No such artifact', b'Not Found', b': Error')):
                    (self.args.lab / 'logs/rejected-response.html').write_bytes(body)
                    raise RuntimeError(f'Request returned {title[1]!r}: {request["path"]}')
                result['title'] = title[1].decode('utf-8') if title else ''
                result['visible_chars'] = len(visible)
            return result
        finally:
            connection.close()

    def children(self):
        path = Path(f'/proc/{self.process.pid}/task/{self.process.pid}/children')
        return [int(item) for item in path.read_text().split()] if path.exists() else []

    def drain(self):
        for _ in range(300):
            if not (set(self.children()) - getattr(self, 'persistent_children', set())):
                return
            time.sleep(0.01)
        raise RuntimeError('Request children did not finish.')

    def cpu(self):
        fields = Path(f'/proc/{self.process.pid}/stat').read_text().rsplit(')', 1)[1].split()
        hz = os.sysconf('SC_CLK_TCK')
        return {'user': (int(fields[11]) + int(fields[13])) / hz,
                'system': (int(fields[12]) + int(fields[14])) / hz}

    def __exit__(self, *_):
        if hasattr(self, 'process') and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        # Shared cumulative counters are dumped once more after parent exit.
        if self.instrumented:
            for child in getattr(self, 'persistent_children', set()):
                for _ in range(300):
                    path = Path(f'/proc/{child}/stat')
                    if not path.exists() or path.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                        break
                    time.sleep(0.01)
                else:
                    os.kill(child, signal.SIGTERM)
                    raise RuntimeError('BOLT profile watcher did not exit normally.')
        self.stdout.close()
        self.stderr.close()
        self.temp.cleanup()


def pin_client(args):
    if args.client_cpu == args.cpu:
        raise RuntimeError('Use different client/server CPU IDs.')
    os.sched_setaffinity(0, {args.client_cpu})


def trace(args, case, seed):
    return json.loads((args.lab / f'data/traces/{case}-seed{seed}.json').read_text())


def replay(server, requests, repeats=1, concurrency=1, verify=False):
    jobs = requests * repeats
    server.drain()
    before = server.cpu()
    started = time.perf_counter()
    if concurrency == 1:
        records = [server.request(request, verify) for request in jobs]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            records = list(pool.map(lambda item: server.request(item, verify), jobs))
    elapsed = time.perf_counter() - started
    server.drain()
    after = server.cpu()
    result = {'elapsed_seconds': elapsed, 'requests': len(jobs),
              'requests_per_second': len(jobs) / elapsed, 'errors': 0,
              'user_seconds': after['user'] - before['user'],
              'system_seconds': after['system'] - before['system'],
              'cpu_seconds': sum(after.values()) - sum(before.values()),
              'wire_bytes': sum(item['wire_bytes'] for item in records), 'by_operation': {}}
    for kind in sorted({item['kind'] for item in records}):
        latencies = sorted(item['latency_seconds'] for item in records if item['kind'] == kind)
        result['by_operation'][kind] = {'count': len(latencies),
            'latency_p50_seconds': statistics.median(latencies),
            'latency_p95_seconds': latencies[math.ceil(len(latencies) * 0.95) - 1]}
    if verify:
        result['semantic_sha256'] = [record['semantic_sha256'] for record in records]
        result['visible_chars'] = [record['visible_chars'] for record in records]
    return result


def train(args):
    lab = args.lab
    pin_client(args)
    profile = lab / 'profiles/fossil.fdata'
    instrumented = lab / 'bin/fossil.inst'
    if profile.exists():
        raise RuntimeError('Profile already exists. Preserve it or use a new lab before retraining.')
    print('Instrument with patched BOLT; counters shared across HTTP worker forks.', flush=True)
    run(lab, 'instrument', [args.after, lab / 'bin/fossil.orig', '-instrument',
        '-runtime-instrumentation-lib=libbolt_rt_instr.a', '-instrumentation-file=' + str(profile),
        '-instrumentation-sleep-time=1', '-instrumentation-no-counters-clear', '-o', instrumented])
    training = {}
    with Server(args, lab / 'bin/fossil.orig', 'training-reference') as server:
        for case in MIXES:
            training[case] = replay(server, trace(args, case, 42), verify=True)
    with Server(args, instrumented, 'training-instrumented', instrumented=True) as server:
        for case in MIXES:
            print('Train and verify actual HTTP routes: ' + case, flush=True)
            observed = replay(server, trace(args, case, 42), repeats=2, verify=True)
            expected = training[case]['semantic_sha256'] * 2
            if observed['semantic_sha256'] != expected:
                save(lab / 'training-mismatch.json', {'case': case, 'observed': observed,
                                                       'expected': training[case]})
                raise RuntimeError('Instrumented HTTP response content differs from original.')
    if not profile.exists() or profile.stat().st_size == 0:
        raise RuntimeError('Forked service did not produce a usable training profile.')
    targets = {name: {'nonzero_records': 0, 'counter_sum_not_function_calls': 0} for name in TARGETS}
    for line in profile.read_text().splitlines():
        fields = line.split()
        if not fields or not fields[-1].isdigit():
            continue
        for name in TARGETS:
            if any(item.split('/')[0] == name for item in fields) and int(fields[-1]) > 0:
                targets[name]['nonzero_records'] += 1
                targets[name]['counter_sum_not_function_calls'] += int(fields[-1])
    save(lab / 'training.json', {'profile_sha256': digest(profile), 'profile_bytes': profile.stat().st_size,
        'patched_bolt_sha256': digest(args.after), 'original_sha256': digest(lab / 'bin/fossil.orig'),
        'training_seed': 42, 'training_requests': len(MIXES) * 200, 'targets': targets,
        'counter_mode': 'MAP_SHARED across forks; periodic cumulative dump; final dump on parent exit',
        'correctness': 'visible HTTP text matches original for both training traces'})
    if any(not item['nonzero_records'] for item in targets.values()):
        raise RuntimeError('A patch-target function has no nonzero profile; inspect training.json.')
    print('Fresh profile has both target functions: ' + json.dumps(targets), flush=True)


def cfg_states(text):
    states = {}
    for match in re.finditer(r'Binary Function "([^"]+)" after building cfg \{(.*?)(?=\nBinary Function |\Z)',
                             text, re.S):
        name, body = match.groups()
        short = name.split('/')[0]
        if short in TARGETS:
            simple = re.search(r'IsSimple\s*:\s*(\d+)', body)
            states[short] = {'name': name, 'IsSimple': int(simple[1]) if simple else None,
                             'jump_table_markers': body.count('JUMPTABLE')}
    return states


def rewrite(args):
    lab = args.lab
    training = json.loads((lab / 'training.json').read_text())
    profile = lab / 'profiles/fossil.fdata'
    if digest(profile) != training['profile_sha256'] or digest(lab / 'bin/fossil.orig') != training['original_sha256']:
        raise RuntimeError('Binary/profile differs from recorded training inputs.')
    outputs = {}
    for label, bolt, filename in (('before', args.before, 'fossil.before'),
                                   ('after', args.after, 'fossil.after')):
        print('Rewrite with identical fresh profile and ext-tsp+hfsort: ' + label, flush=True)
        binary = lab / 'bin' / filename
        text = run(lab, label + '-rewrite', [bolt, lab / 'bin/fossil.orig', '-data=' + str(profile),
            '-o', binary, '-reorder-blocks=ext-tsp', '-reorder-functions=hfsort',
            '-split-functions=false', '-jump-tables=move', '-no-threads',
            '-sequential-disassembly', '-print-cfg', '-v=2', '-time-build', '-time-rewrite', '-time-opts',
            '-print-only=' + ','.join(name + suffix for name in TARGETS for suffix in ('', '/1', '/2'))])
        states = cfg_states(text)
        for target in TARGETS:
            if states.get(target, {}).get('IsSimple') != int(label == 'after'):
                raise RuntimeError(f'Unexpected CFG state: {label}, {target}; inspect rewrite log.')
        prefixes = {}
        for target in TARGETS:
            assembly = run(lab, f'{label}-{target}', ['objdump', '-d', '--disassemble=' + target, binary])
            prefixes[target] = assembly.count('notrack jmp')
        if label == 'after' and any(not value for value in prefixes.values()):
            raise RuntimeError('NOTRACK prefix not found in rewritten symbol.')
        outputs[label] = {'binary_sha256': digest(binary), 'cfg': states, 'notrack_jumps': prefixes}
    save(lab / 'rewrites.json', {'profile_sha256': digest(profile), 'variants': outputs,
                               'flags': ['ext-tsp', 'hfsort', 'split-functions=false', 'jump-tables=move']})
    print('Both rewrites complete; CFG and preserved prefixes recorded.', flush=True)


def check(args):
    pin_client(args)
    result = {'seed': args.seed, 'variants': {}, 'status': 'RUNNING'}
    expected = {}
    for variant, filename in VARIANTS.items():
        print('Verify real HTTP response text: ' + variant, flush=True)
        with Server(args, args.lab / 'bin' / filename, 'check-' + variant) as server:
            result['variants'][variant] = {}
            for case in MIXES:
                observed = replay(server, trace(args, case, args.seed), verify=True)
                result['variants'][variant][case] = observed
                if variant == 'A-original':
                    expected[case] = observed['semantic_sha256']
                elif observed['semantic_sha256'] != expected[case]:
                    save(args.lab / 'check.json', result)
                    raise RuntimeError(f'HTTP content mismatch: {variant}, {case}')
    result['status'] = 'PASS'
    result['binary_sha256'] = {name: digest(args.lab / 'bin' / filename)
                               for name, filename in VARIANTS.items()}
    result['snapshot_sha256'] = digest(args.lab / 'data/snapshot.fossil')
    result['trace_sha256'] = {case: digest(args.lab / f'data/traces/{case}-seed{args.seed}.json') for case in MIXES}
    save(args.lab / 'check.json', result)
    print('PASS: A/B/C HTTP 200 and identical visible repository content.', flush=True)


def require_checked(args):
    checked = json.loads((args.lab / 'check.json').read_text())
    if checked['status'] != 'PASS' or checked['seed'] != args.seed:
        raise RuntimeError('Run check for the same evaluation seed first.')
    for name, filename in VARIANTS.items():
        if checked['binary_sha256'][name] != digest(args.lab / 'bin' / filename):
            raise RuntimeError('Binary changed since correctness check.')
    if 'snapshot_sha256' in checked and checked['snapshot_sha256'] != digest(args.lab / 'data/snapshot.fossil'):
        raise RuntimeError('Database snapshot changed since correctness check.')
    if 'trace_sha256' in checked:
        for case, expected in checked['trace_sha256'].items():
            if expected != digest(args.lab / f'data/traces/{case}-seed{args.seed}.json'):
                raise RuntimeError('HTTP request list changed since correctness check.')


def reuse(args):
    source_lab = args.base_lab.resolve()
    if args.lab == source_lab:
        raise RuntimeError('Reuse requires a separate destination lab.')
    for name in ('bin', 'data', 'data/traces', 'logs', 'profiles', 'results', 'scripts'):
        (args.lab / name).mkdir(parents=True, exist_ok=True)
    for source, target in ((source_lab / 'bin/fossil.orig', args.lab / 'bin/fossil.orig'),
                           (source_lab / 'data/upstream.fossil', args.lab / 'data/upstream.fossil'),
                           (source_lab / 'data/snapshot.fossil', args.lab / 'data/snapshot.fossil')):
        if target.exists() and digest(target) != digest(source):
            raise RuntimeError('Destination contains unrelated existing data: ' + str(target))
        if not target.exists():
            shutil.copy2(source, target)
    environment = json.loads((source_lab / 'environment.json').read_text())
    environment['reused_original_from'] = str(source_lab)
    save(args.lab / 'environment.json', environment)
    print('Reuse the identical original Fossil and repository snapshot; fresh profile/output lab: ' + str(args.lab))


def paired_summary(rows):
    variants = {}
    for variant in VARIANTS:
        selected = [row for row in rows if row['variant'] == variant]
        elapsed = [row['elapsed_seconds'] for row in selected]
        variants[variant] = {'elapsed_median': statistics.median(elapsed),
            'server_cpu_median': statistics.median(row['cpu_seconds'] for row in selected),
            'rps_median': statistics.median(row['requests_per_second'] for row in selected),
            'elapsed_cv_percent': statistics.stdev(elapsed) / statistics.mean(elapsed) * 100}
    comparisons = {}
    by_round = collections.defaultdict(dict)
    for row in rows:
        by_round[row['round']][row['variant']] = row
    for before, after in (('A-original', 'B-before-fix'), ('A-original', 'C-after-fix'),
                           ('B-before-fix', 'C-after-fix')):
        reductions, rps_increases, cpu_reductions = [], [], []
        wins = ties = losses = 0
        for group in by_round.values():
            a, b = group[before], group[after]
            delta = 100 * (1 - b['elapsed_seconds'] / a['elapsed_seconds'])
            reductions.append(delta)
            rps_increases.append(100 * (b['requests_per_second'] / a['requests_per_second'] - 1))
            cpu_reductions.append(100 * (1 - b['cpu_seconds'] / a['cpu_seconds']))
            wins += delta > 0
            ties += delta == 0
            losses += delta < 0
        rng = random.Random(20261118)
        bootstrap = sorted(statistics.median(rng.choices(reductions, k=len(reductions)))
                           for _ in range(10000))
        comparisons[before + ' -> ' + after] = {
            'elapsed_reduction_paired_median_percent': statistics.median(reductions),
            'rps_increase_paired_median_percent': statistics.median(rps_increases),
            'server_cpu_reduction_paired_median_percent': statistics.median(cpu_reductions),
            'wins_ties_losses': [wins, ties, losses], 'per_round_reductions': reductions,
            'paired_bootstrap_median_interval_95': [bootstrap[250], bootstrap[9749]]}
    return {'variants': variants, 'comparisons': comparisons}


def benchmark(args):
    if args.rounds < 6 or args.rounds % 6:
        raise RuntimeError('Use a multiple of six rounds for balanced A/B/C orders (24 recommended).')
    require_checked(args)
    pin_client(args)
    output = Path(tempfile.mkdtemp(prefix='measurements-' + time.strftime('%Y%m%d-%H%M%S') + '-',
                                   dir=args.lab / 'results'))
    report = {'status': 'RUNNING', 'server_cpu': args.cpu, 'client_cpu': args.client_cpu,
        'rounds': args.rounds, 'concurrency': args.concurrency, 'evaluation_seed': args.seed,
        'order_seed': args.order_seed, 'target_seconds': args.target_seconds,
        'two_warmup_trace_passes_per_variant_per_round': True,
        'dataset_sha256': digest(args.lab / 'data/snapshot.fossil'),
        'profile_sha256': digest(args.lab / 'profiles/fossil.fdata'),
        'script_sha256': digest(__file__),
        'trace_sha256': {case: digest(args.lab / f'data/traces/{case}-seed{args.seed}.json') for case in MIXES},
        'binary_sha256': {name: digest(args.lab / 'bin' / filename) for name, filename in VARIANTS.items()},
        'cases': {}}
    print('Results directory: ' + str(output), flush=True)
    save(output / 'config.json', report)
    for case in (list(MIXES) if args.case == 'all' else [args.case]):
        requests = trace(args, case, args.seed)
        with Server(args, args.lab / 'bin/fossil.orig', 'calibrate-' + case) as server:
            replay(server, requests, concurrency=args.concurrency)
            pilot = replay(server, requests, concurrency=args.concurrency)
        repeats = max(1, min(100, math.ceil(args.target_seconds / pilot['elapsed_seconds'])))
        orders = list(itertools.permutations(VARIANTS)) * (args.rounds // 6)
        random.Random(args.order_seed).shuffle(orders)
        report['cases'][case] = {'pilot': pilot, 'repeats': repeats,
                                'requests_per_measured_run': repeats * len(requests), 'orders': orders, 'rows': []}
        print(f'{case}: {len(requests) * repeats} requests/run; {args.rounds} balanced rounds.', flush=True)
        for round_id, order in enumerate(orders, 1):
            print(f'{case} round {round_id}/{args.rounds}: ' + ' / '.join(order), flush=True)
            for variant in order:
                label = f'{case}-{round_id:02d}-{variant}'
                with Server(args, args.lab / 'bin' / VARIANTS[variant], label) as server:
                    for _ in range(2):
                        replay(server, requests, concurrency=args.concurrency)
                    observed = replay(server, requests, repeats, args.concurrency)
                observed.update(round=round_id, variant=variant)
                report['cases'][case]['rows'].append(observed)
                with (output / 'raw.jsonl').open('a') as raw:
                    raw.write(json.dumps({'case': case, **observed}) + '\n')
                print(f'  {variant}: {observed["elapsed_seconds"]:.4f}s, '
                      f'{observed["requests_per_second"]:.2f} req/s, '
                      f'server CPU={observed["cpu_seconds"]:.3f}s', flush=True)
            save(output / 'progress.json', report)
        report['cases'][case]['summary'] = paired_summary(report['cases'][case]['rows'])
    for filename, expected in ((args.lab / 'data/snapshot.fossil', report['dataset_sha256']),
                               (args.lab / 'profiles/fossil.fdata', report['profile_sha256'])):
        if digest(filename) != expected:
            raise RuntimeError('Input changed during benchmark: ' + str(filename))
    report['status'] = 'COMPLETE'
    save(output / 'summary.json', report)
    save(args.lab / 'latest-benchmark.json', {'directory': str(output)})
    for case, item in report['cases'].items():
        for name, comparison in item['summary']['comparisons'].items():
            print(f'{case}: {name}: elapsed reduction '
                  f'{comparison["elapsed_reduction_paired_median_percent"]:+.3f}%; '
                  f'wins/ties/losses={comparison["wins_ties_losses"]}', flush=True)
    print('SUCCESS: all requested cases completed. ' + str(output), flush=True)


def profile(args):
    require_checked(args)
    pin_client(args)
    lab = args.lab
    perf = Path('/usr/lib/linux-tools-6.8.0-147/perf')
    if not perf.exists():
        perf = Path(shutil.which('perf') or '/usr/bin/perf')
    output = Path(tempfile.mkdtemp(prefix='perf-' + time.strftime('%Y%m%d-%H%M%S') + '-', dir=lab / 'results'))
    report = {'status': 'RUNNING', 'event': 'cpu-clock:u', 'frequency': 499, 'variants': {}}
    for case in (list(MIXES) if args.case == 'all' else [args.case]):
        for variant, filename in VARIANTS.items():
            print('Sample HTTP worker CPU (inherited forks): ' + case + ' ' + variant, flush=True)
            label = case + '-' + variant
            cache = output / (label + '.buildid-cache')
            cache.mkdir()
            capture = output / (label + '.perf.data')
            with Server(args, lab / 'bin' / filename, 'perf-' + label) as server:
                requests = trace(args, case, args.seed)
                replay(server, requests, concurrency=args.concurrency)
                command = [str(perf), '--buildid-dir', str(cache), 'record', '-e', 'cpu-clock:u',
                    '-F', '499', '--call-graph', 'fp', '-p', str(server.process.pid), '-o', str(capture)]
                save(output / (label + '.record.command.json'), command)
                with (output / (label + '.record.stdout.txt')).open('wb') as stdout, \
                     (output / (label + '.record.stderr.txt')).open('wb') as stderr:
                    recorder = subprocess.Popen(command, stdout=stdout, stderr=stderr)
                    try:
                        time.sleep(0.3)
                        if recorder.poll() is not None:
                            raise RuntimeError('perf attach failed; inspect record.stderr.txt.')
                        observed = replay(server, requests, repeats=10, concurrency=args.concurrency)
                    finally:
                        if recorder.poll() is None:
                            recorder.send_signal(signal.SIGINT)
                        recorder.wait(timeout=15)
            command = [str(perf), '--buildid-dir', str(cache), 'report', '--stdio',
                '--stdio-color', 'never', '--no-children', '--call-graph', 'none', '--no-inline',
                '--percent-limit', '0', '--sort', 'overhead,dso,symbol',
                '--fields', 'overhead,sample,period,dso,symbol', '--field-separator', '\t', '-i', str(capture)]
            text = run(lab, 'perf-report-' + label, command, timeout=120)
            (output / (label + '.report.txt')).write_text(text)
            rows = []
            for line in text.splitlines():
                fields = [item.strip() for item in line.strip().split('\t')]
                if len(fields) >= 5 and fields[0].endswith('%'):
                    rows.append({'samples': int(fields[1]), 'period': int(fields[2]),
                        'dso': fields[3], 'symbol': re.sub(r'^\[[^]]+\]\s*', '', fields[4])})
            total = sum(item['period'] for item in rows)
            if not total:
                raise RuntimeError('No self samples parsed from perf report.')
            targets = {target: sum(item['period'] for item in rows
                        if item['symbol'].split('/')[0] == target) / total * 100 for target in TARGETS}
            rows.sort(key=lambda item: item['period'], reverse=True)
            report['variants'][label] = {'workload': observed, 'total_samples': sum(r['samples'] for r in rows),
                'target_self_percent': targets, 'top20': [{**r, 'self_percent': r['period'] / total * 100}
                                                        for r in rows[:20]],
                'buildid_cache': str(cache), 'capture': str(capture)}
            save(output / 'summary.json', report)
    report['status'] = 'COMPLETE'
    save(output / 'summary.json', report)
    save(lab / 'latest-perf.json', {'directory': str(output)})
    print('CPU hotspot reports saved: ' + str(output), flush=True)


def report(args):
    lab = args.lab
    environment = json.loads((lab / 'environment.json').read_text())
    dataset = json.loads((lab / 'dataset.json').read_text())
    training = json.loads((lab / 'training.json').read_text())
    rewrites = json.loads((lab / 'rewrites.json').read_text())
    timing_directory = Path(json.loads((lab / 'latest-benchmark.json').read_text())['directory'])
    measured = json.loads((timing_directory / 'summary.json').read_text())
    perf_directory = Path(json.loads((lab / 'latest-perf.json').read_text())['directory'])
    sampled = json.loads((perf_directory / 'summary.json').read_text())
    if measured['status'] != 'COMPLETE' or sampled['status'] != 'COMPLETE':
        raise RuntimeError('Report requires completed measurements and CPU captures.')
    lines = [
        '# Fossil 真实应用：SQLite NOTRACK 补丁对照实验',
        '',
        '本实验运行完整 Fossil HTTP 服务，数据来自 Fossil 官方真实开发仓库。'
        '请求比例由实验设计，**不是线上用户行为统计或生产访问日志回放**。',
        '',
        '## 版本与条件',
        '',
        '~~~text', environment['fossil_version'].strip(), '~~~', '',
        f'- 本地 WSL/Linux；服务 CPU {measured["server_cpu"]}，压测客户端 CPU {measured["client_cpu"]}。',
        f'- 同时最多 {measured["concurrency"]} 个请求，服务固定单个 CPU。',
        f'- 仓库 {dataset["checkins"]:,} 次提交、{dataset["artifacts"]:,} 个内容对象，'
        f'{dataset["snapshot_bytes"] / 1024**2:.1f} MiB。',
        '- 所有请求只发送到 127.0.0.1；保留 gzip 压缩、真实页面生成和数据库查询。',
        '- 每轮每个版本使用同一仓库快照的新副本；2 次完整请求列表预热后计时。',
        f'- 编译参数：{environment["cflags"]}；链接参数：{environment["ldflags"]}。',
        '- 保持正常 -O2；非 PIE、符号及重定位用于 BOLT。Fossil 自带 SQLite，没有换成旧 SQLite。',
        '',
        '## 访问场景',
        '',
        '| 操作 | 日常浏览 browse | 历史审查 review |',
        '|---|---:|---:|',
    ]
    names = {'timeline': '提交时间线', 'commit': '提交详情', 'directory': '源码目录',
             'file': '源码文件', 'history': '文件修改历史', 'diff': '版本差异'}
    if args.preset == 'dashboard':
        lines = lines[:-2] + ['| 操作 | 比例 |', '|---|---:|']
        dashboard_names = {'report_file': '按文件统计修改次数', 'report_user': '按用户统计提交',
                           'report_month': '按月统计提交', 'report_year': '按年统计提交'}
        for kind, count in MIXES['dashboard'].items():
            lines.append(f'| {dashboard_names[kind]} | {count}% |')
    else:
        for kind in MIXES['browse']:
            lines.append(f'| {names[kind]} | {MIXES["browse"][kind]}% | {MIXES["review"][kind]}% |')
    lines += [
        '',
        ('每份列表 100 个请求。统计看板使用 Fossil 原有 /reports 功能，按真实开发者筛选，'
         '不添加 SQL、不修改 Fossil/SQLite 源代码。它是聚合排序集中的场景，并非普通访问分布。'
         if args.preset == 'dashboard' else
         '每份列表 100 个请求。日常浏览时间线/历史每页 50 条；历史审查每页 100 条。')
        +
        '请求参数取自真实仓库。训练种子 42，评估种子 43；'
        '种子只控制请求的选取与顺序，不生成仓库内容。所有正式场景的参数均在该场景计时前固定。',
        '',
        '## 补丁是否作用到本应用',
        '',
        '| 函数 | 修复前 IsSimple | 修复后 IsSimple | 修复后跳转表 | 保留 NOTRACK |',
        '|---|---:|---:|---:|---|',
    ]
    for target in TARGETS:
        before = rewrites['variants']['before']['cfg'][target]
        after = rewrites['variants']['after']['cfg'][target]
        prefixes = rewrites['variants']['after']['notrack_jumps'][target]
        lines.append(f'| {target} | {before["IsSimple"]} | {after["IsSimple"]} | '
                     f'{after["jump_table_markers"]} | {prefixes} 处 |')
    lines += [
        '',
        '修复版 BOLT 重新插桩，计数器在 HTTP 子进程间共享，定期保存累计值，父进程退出后最终保存。'
        '这避免只采到监听进程、遗漏真正运行 SQL 的工作进程。插桩版页面可见内容与原版一致。',
        '',
        f'新 profile：{training["profile_bytes"]:,} 字节；SHA-256：{training["profile_sha256"]}。',
    ]
    for target, item in training['targets'].items():
        lines.append(f'- {target}：{item["nonzero_records"]} 条非零记录；**不是函数调用次数**。')
    lines += [
        '',
        'A=原始 Fossil；B=未修改 BOLT 重写；C=修改 BOLT 重写。B/C 使用同一原始文件、'
        '同一新 profile、ext-tsp+hfsort、关闭函数拆分，强制移动跳转表。BOLT 源码基线相同、只差补丁。',
        '',
        f'评估种子 {measured["evaluation_seed"]}：三组共 {len(MIXES) * 300} 个页面请求的 HTTP 200 和可见内容哈希校验通过。'
        '哈希去掉随机脚本、nonce 和生成耗时页脚，保留实际可见仓库内容；不等于浏览器 JavaScript 功能全面验证。',
        '',
        '## 正式性能结果',
        '',
        '耗时下降为逐轮配对百分比的中位数；吞吐提升另按逐轮吞吐比计算。'
        '置信区间是对轮次配对值作 bootstrap 的描述性区间，不能替代独立机器/请求种子验证。',
    ]
    for case, case_data in measured['cases'].items():
        summary = case_data['summary']
        lines += [
            '', f'### {case}', '',
            f'{measured["rounds"]} 轮；每个计时运行 {case_data["requests_per_measured_run"]} 个请求；'
            '三组在每个位置出现次数相同。',
            '',
            '| 版本 | 耗时中位数/s | 吞吐中位数/请求每秒 | 服务 CPU 中位数/s | 耗时 CV |',
            '|---|---:|---:|---:|---:|',
        ]
        for variant, item in summary['variants'].items():
            lines.append(f'| {variant} | {item["elapsed_median"]:.4f} | {item["rps_median"]:.2f} | '
                         f'{item["server_cpu_median"]:.3f} | {item["elapsed_cv_percent"]:.2f}% |')
        lines += ['', '| 比较 | 耗时下降 | 吞吐提升 | 耗时下降区间 | 胜/平/负 |',
                  '|---|---:|---:|---|---|']
        for comparison, item in summary['comparisons'].items():
            low, high = item['paired_bootstrap_median_interval_95']
            wtl = '/'.join(map(str, item['wins_ties_losses']))
            lines.append(f'| {comparison} | {item["elapsed_reduction_paired_median_percent"]:+.3f}% | '
                         f'{item["rps_increase_paired_median_percent"]:+.3f}% | '
                         f'[{low:+.3f}%, {high:+.3f}%] | {wtl} |')
        lines += ['', '各类请求的 P50/P95 延迟、原始逐轮数据保存在 summary.json 和 raw.jsonl。'
                  '服务 CPU 从 /proc 统计服务进程以及已回收工作子进程的用户态和内核态时间；'
                  '客户端 Python、仓库复制和预热不计入服务 CPU。']
    lines += [
        '',
        '## CPU 热点',
        '',
        '独立采样，不把插桩或 perf 运行纳入上述正式计时。事件 cpu-clock:u、499 Hz，'
        '附加到服务进程并继承到后续 HTTP 工作子进程。每个变体使用独立 build-id 缓存。',
        '',
        '| 场景/版本 | 样本数 | sqlite3VdbeExec self | vdbeRecordCompareInt self |',
        '|---|---:|---:|---:|',
    ]
    for label, item in sampled['variants'].items():
        lines.append(f'| {label} | {item["total_samples"]} | '
                     f'{item["target_self_percent"]["sqlite3VdbeExec"]:.2f}% | '
                     f'{item["target_self_percent"]["vdbeRecordCompareInt"]:.2f}% |')
    lines += [
        '',
        '采样百分比表示本次用户态 CPU 时间的分布，不能直接当作加速百分比；'
        'B/C 的相对样本占比也不能单独证明某函数的绝对时间下降。'
        '动态 libc/zlib 部分内部符号缺失，报告保留模块和地址，不编造函数名。'
        '本实验没有硬件 cache-miss/branch-miss 数据，不能据此认定具体缓存瓶颈。',
        '',
        '## 局限与下一步',
        '',
        '- 当前是匿名用户的只读仓库浏览，未覆盖 push/clone 同步、写工单、多人写入及锁竞争。',
        '- 使用真实应用和真实仓库，操作比例仍是设计参数；不声称代表全部生产部署。',
        '- 只有评估种子 43 的正式计时；42 仅训练，44 已生成，后续独立确认时使用。',
        '- 单个 WSL 实例、回环 HTTP、单服务 CPU；没有 TLS、跨机器网络、浏览器静态资源请求。',
        '- gzip、内容解压、差异算法和 fork 等正常开销会稀释 SQLite 布局优化收益。',
        '- 非 PIE 构建触发本次指针跳转表形式；其他发行版/PIE 构建须重新检查，不能直接外推。',
        '- 已检查 NOTRACK 前缀保留，未在强制启用 CET/IBT 的环境执行。',
        '- 控制流识别和当前请求内容校验通过，不等于所有 Fossil 路径均已证明正确。',
        '- B/C 差异是整个补丁重写后程序的效果，不能全部归到某一个函数或某一条 SQL。',
        '',
        '## 产物位置',
        '',
        f'- 正式测量：{timing_directory}',
        f'- CPU 采样：{perf_directory}',
        f'- 构建/补丁识别/重写计时：{lab / "logs"}',
        f'- 脚本：{lab / "scripts/fossil_lab.py"}',
        '- 文件校验、环境、数据快照来源、profile、正确性分别记录在 experiment 根目录的 JSON 中。',
        '',
    ]
    (lab / 'RESULTS.md').write_text('\n'.join(lines), encoding='utf-8')
    print('Report generated: ' + str(lab / 'RESULTS.md'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    global MIXES
    parser.add_argument('action', choices=['prepare', 'reuse', 'data', 'train', 'rewrite', 'check', 'benchmark', 'profile', 'report'])
    parser.add_argument('--lab', type=Path, default=DEFAULT_LAB)
    parser.add_argument('--cpu', type=int, default=0)
    parser.add_argument('--client-cpu', type=int, default=1)
    parser.add_argument('--preset', choices=['web', 'dashboard'], default='web')
    parser.add_argument('--base-lab', type=Path, default=DEFAULT_LAB)
    parser.add_argument('--seed', type=int, choices=[43, 44], default=43)
    parser.add_argument('--rounds', type=int, default=24)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--order-seed', type=int, default=20261118)
    parser.add_argument('--target-seconds', type=float, default=3.0)
    parser.add_argument('--case', choices=['all', *WEB_MIXES, *DASHBOARD_MIXES], default='all')
    parser.add_argument('--before', type=Path, default=BOLT_ROOT / 'saved-tools/before-fix/bin/llvm-bolt')
    parser.add_argument('--after', type=Path, default=BOLT_ROOT / 'saved-tools/after-fix/bin/llvm-bolt')
    args = parser.parse_args()
    if args.preset == 'dashboard':
        MIXES = DASHBOARD_MIXES
        if args.lab == DEFAULT_LAB:
            args.lab = DEFAULT_LAB.parent / 'fossil-dashboard'
    if args.case != 'all' and args.case not in MIXES:
        parser.error('Case does not belong to the selected preset.')
    if os.name != 'posix':
        parser.error('Run this script in WSL/Linux.')
    if args.cpu not in os.sched_getaffinity(0):
        parser.error('CPU is outside the current affinity mask.')
    if args.client_cpu not in os.sched_getaffinity(0) or args.client_cpu == args.cpu:
        parser.error('Choose a separate available client CPU.')
    if not 1 <= args.concurrency <= 32 or not 0.5 <= args.target_seconds <= 30:
        parser.error('Use concurrency 1..32 and target seconds 0.5..30.')
    args.lab = args.lab.resolve()
    if args.action in ('prepare', 'reuse', 'data', 'train', 'rewrite', 'check'):
        (args.lab / 'scripts').mkdir(parents=True, exist_ok=True)
        destination = args.lab / 'scripts/fossil_lab.py'
        if Path(__file__).resolve() != destination:
            shutil.copy2(__file__, destination)
    globals()[args.action](args)


if __name__ == '__main__':
    main()
