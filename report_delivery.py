"""Daily publication ledger. Only this workflow may write report-state.

GitHub commits are durable checkpoints; branch updates are non-forced CAS writes.
No cache, artifact listing, or Gmail read permission is used for deduplication.
"""
import argparse
import base64
import hashlib
import html
import json
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo('Asia/Taipei')
STATE_BRANCH = 'report-state'
STATE_PATH = 'delivery.json'
START_DATE = date(2026, 10, 18)
MAX_ATTEMPTS = 2
PUBLIC = Path('public')


def digest(content):
    return hashlib.sha256(content).hexdigest()


def latest_due_date(anchor):
    local = anchor.astimezone(TAIPEI)
    return local.date() - timedelta(days=local.time() < time(20, 18))


def dump(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')


class GitHub:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })

    def request(self, method, path, data=None, missing=False):
        response = self.session.request(method, f'https://api.github.com/repos/{self.repo}/{path}',
                                        json=data, timeout=45)
        if missing and response.status_code == 404:
            return None
        # Do not print request bodies, authentication headers, or credentials.
        if not response.ok:
            raise RuntimeError(f'GitHub {method} {path.split("?")[0]}: HTTP {response.status_code}')
        return response.json()

    def anchor(self):
        run = self.request('GET', 'actions/runs/' + os.environ['GITHUB_RUN_ID'])
        return datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))


class Store:
    def __init__(self, api):
        self.api = api
        ref = api.request('GET', f'git/ref/heads/{STATE_BRANCH}', missing=True)
        self.head = ref['object']['sha'] if ref else None
        self.tree = None
        self.entries = {}
        if self.head:
            commit = api.request('GET', 'git/commits/' + self.head)
            self.tree = commit['tree']['sha']
            tree = api.request('GET', f'git/trees/{self.tree}?recursive=1')
            if tree.get('truncated'):
                raise ValueError('State tree listing truncated; cannot safely restore reports')
            self.entries = {item['path']: item['sha'] for item in tree['tree'] if item['type'] == 'blob'}
            # An existing state branch without a valid ledger is corruption, not a fresh start.
            self.data = json.loads(self.read(STATE_PATH))
            if self.data.get('schema') != 1 or not isinstance(self.data.get('reports'), dict):
                raise ValueError('Invalid publication ledger')
        else:
            self.data = {'schema': 1, 'start_date': START_DATE.isoformat(), 'reports': {}}

    def read(self, path):
        result = self.api.request('GET', 'git/blobs/' + self.entries[path])
        if result.get('encoding') != 'base64':
            raise ValueError('State file is not base64 encoded')
        return base64.b64decode(result['content'])

    def save(self, files=None):
        files = dict(files or {})
        files[STATE_PATH] = dump(self.data)
        entries = []
        for path, content in files.items():
            blob = self.api.request('POST', 'git/blobs', {
                'content': base64.b64encode(content).decode('ascii'), 'encoding': 'base64'})
            entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        payload = {'tree': entries}
        if self.tree:
            payload['base_tree'] = self.tree
        tree = self.api.request('POST', 'git/trees', payload)
        payload = {'message': 'Checkpoint daily stock report delivery', 'tree': tree['sha'],
                   'parents': [self.head] if self.head else []}
        commit = self.api.request('POST', 'git/commits', payload)
        # Check our lease before the non-forced ref update. A race also fails fast-forward.
        current = self.api.request('GET', f'git/ref/heads/{STATE_BRANCH}', missing=True)
        actual = current['object']['sha'] if current else None
        if actual != self.head:
            raise RuntimeError('Publication ledger changed; stop and re-plan in a new run')
        if self.head:
            self.api.request('PATCH', f'git/refs/heads/{STATE_BRANCH}', {'sha': commit['sha'], 'force': False})
        else:
            self.api.request('POST', 'git/refs', {'ref': 'refs/heads/' + STATE_BRANCH, 'sha': commit['sha']})
        self.head, self.tree = commit['sha'], tree['sha']
        self.entries.update({entry['path']: entry['sha'] for entry in entries})


def recipient_key(recipient):
    return digest(recipient.strip().lower().encode())


def delivery(record, recipient):
    return record.setdefault('deliveries', {}).setdefault(recipient_key(recipient),
        {'attempts': 0, 'status': 'pending'})


def plan(store, anchor, recipient, requested='', early_today=False):
    due = latest_due_date(anchor)
    if early_today:
        local = anchor.astimezone(TAIPEI)
        if local.time() >= time(20, 18):
            raise ValueError('Early publication is only valid before 20:18 Taipei time')
        if not requested or date.fromisoformat(requested) != local.date():
            raise ValueError('Early publication requires an explicit end_date matching today')
        due = local.date()
    if requested and date.fromisoformat(requested) != due:
        raise ValueError('Official publication must use the latest due date; use preview for historical dates')
    day = due.isoformat()
    if due < date.fromisoformat(store.data['start_date']):
        return {'run': False, 'reason': 'Before rollout start date'}
    if not requested and due < anchor.astimezone(TAIPEI).date():
        # An early manual release of today's official report supersedes any
        # automatic retry of yesterday's already-outdated email.
        current_day = anchor.astimezone(TAIPEI).date().isoformat()
        newer = store.data['reports'].get(current_day)
        if newer and newer.get('deliveries', {}).get(recipient_key(recipient), {}).get('status') == 'sent':
            return {'run': False, 'reason': 'Current-day official report already emailed; superseded older retry'}
    record = store.data['reports'].get(day)
    if record:
        receipt = delivery(record, recipient)
        if receipt['status'] == 'sent':
            return {'run': False, 'reason': 'Gmail delivery confirmed; no duplicate'}
        if receipt['attempts'] >= MAX_ATTEMPTS:
            return {'run': False, 'report_date': day, 'delivery_blocked': True,
                    'reason': 'Gmail delivery NOT confirmed; two send attempts exhausted. Check the failed run logs and delivery ledger before manual recovery.'}
    return {'run': True, 'report_date': day, 'generate': record is None,
            'deploy': record is None or not record.get('deployed'), 'send': True,
            'mode': 'daily', 'official': True}


def restore(store, day, destination=PUBLIC):
    record = store.data['reports'][day]
    destination.mkdir(parents=True, exist_ok=True)
    for name, expected in record['files'].items():
        if Path(name).name != name:
            raise ValueError('Invalid stored report filename')
        content = store.read(f'reports/{day}/r1/{name}')
        if digest(content) != expected:
            raise ValueError('Frozen report hash mismatch')
        (destination / name).write_bytes(content)


def freeze(store, day, directory=PUBLIC, official=True):
    metadata = json.loads((directory / 'generation.json').read_text(encoding='utf-8'))
    if metadata['report_date'] != day:
        raise ValueError('Generated report date does not match publication date')
    metadata.update(revision=1, scheduled_for=f'{day}T20:18:00+08:00',
                    run_id=os.environ.get('GITHUB_RUN_ID', 'local'),
                    source_sha=os.environ.get('GITHUB_SHA', 'local'),
                    official=official)
    banner = (f'<aside class="notice"><strong>{"正式日報" if official else "預覽報告"}：'
              f'{html.escape(day)} · r1</strong> · 行情資料日：{html.escape(metadata["market_as_of"])}'
              f' · 產生時間：{html.escape(metadata["generated_at"])}'
              '<br>資料於實際執行時取得；同一正式版本重試沿用相同內容。</aside>')
    files = {}
    for name in ('analysis.html', 'analysis_param.html', 'analysis_momentum.html', 'index.html'):
        content = (directory / name).read_text(encoding='utf-8')
        if '<body>' not in content:
            raise ValueError('Report has no body')
        files[name] = content.replace('<body>', '<body>' + banner, 1).encode('utf-8')
    metadata['html_sha256'] = {name: digest(content) for name, content in files.items()}
    metadata['content_sha256'] = digest(dump(metadata['html_sha256']))
    files['report_metadata.json'] = dump(metadata)
    for name, content in files.items():
        (directory / name).write_bytes(content)
    (directory / 'generation.json').unlink()
    if official:
        if day in store.data['reports']:
            raise ValueError('Cannot overwrite a frozen official report')
        store.data['reports'][day] = {'files': {name: digest(content) for name, content in files.items()},
                                    'deployed': False, 'deliveries': {}}
        store.save({f'reports/{day}/r1/{name}': content for name, content in files.items()})


def send_delivery(store, day, recipient, sender):
    record = store.data['reports'][day]
    if not record['deployed']:
        raise ValueError('Website must be deployed before sending')
    receipt = delivery(record, recipient)
    if receipt['status'] == 'sent' or receipt['attempts'] >= MAX_ATTEMPTS:
        return None
    receipt.update(attempts=receipt['attempts'] + 1, status='sending',
                   attempted_at=datetime.now(TAIPEI).isoformat(), run_id=os.environ.get('GITHUB_RUN_ID', 'local'))
    # Reserve durably BEFORE any Gmail call; uncertain outcomes consume an attempt.
    store.save()
    message_id = sender()
    if not message_id:
        raise RuntimeError('Gmail returned no message ID')
    receipt.update(status='sent', message_id=message_id, sent_at=datetime.now(TAIPEI).isoformat())
    store.save()
    return message_id


def output(values):
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
        for key, value in values.items():
            value = str(value).lower() if isinstance(value, bool) else str(value)
            if '\n' in value or '\r' in value:
                raise ValueError('Multiline workflow output rejected')
            handle.write(f'{key}={value}\n')


def gmail_sender(day, recipient):
    """Preauthorize Gmail before send_delivery reserves its bounded send attempt.

    A failed refresh grant cannot have sent a message, so it must not use up a
    Gmail-send attempt. Reuse the validated credentials for the actual call.
    """
    from mail_report import load_credentials, send_report
    metadata = json.loads((PUBLIC / 'report_metadata.json').read_text(encoding='utf-8'))
    if metadata['report_date'] != day or not metadata['official']:
        raise ValueError('Email metadata does not match official publication')
    with tempfile.TemporaryDirectory(prefix='gmail-auth-', dir=os.environ.get('RUNNER_TEMP')) as directory:
        paths = []
        for variable, filename in (('GMAIL_CLIENT_SECRET_JSON', 'client_secret.json'),
                                   ('GMAIL_TOKEN_JSON', 'gmail_token.json')):
            data = json.loads(os.environ[variable])
            if not isinstance(data, dict) or (variable == 'GMAIL_TOKEN_JSON' and not data.get('refresh_token')):
                raise ValueError('Invalid Gmail credentials; reauthorize and update GMAIL_TOKEN_JSON')
            path = Path(directory) / filename
            path.touch(mode=0o600)
            path.write_bytes(dump(data))
            paths.append(path)
        credentials = load_credentials(paths[0], paths[1], non_interactive=True)
        # Temporary files are no longer needed once credentials are in memory.
    return lambda: send_report(
        PUBLIC / 'analysis_momentum.html', recipient,
        f'Stock analysis report - 評分v2 - {day} - r1', paths[0], paths[1],
        non_interactive=True, metadata=metadata, credentials=credentials)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'freeze', 'deployed', 'send'))
    args = parser.parse_args()
    recipient = os.environ.get('RECIPIENT', 'bruce.sy.chen@gmail.com').strip()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', recipient):
        raise ValueError('Invalid recipient')
    official = os.environ.get('OFFICIAL', 'false') == 'true'
    if args.command == 'prepare':
        api = GitHub()
        anchor = api.anchor()
        official = os.environ.get('GITHUB_EVENT_NAME') == 'schedule' or os.environ.get('SEND_EMAIL') == 'true'
        requested = os.environ.get('END_DATE', '').strip()
        early_today = os.environ.get('EARLY_PUBLISH', 'false') == 'true'
        if early_today and (os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                             or os.environ.get('SEND_EMAIL') != 'true'):
            raise ValueError('Early publication requires manual dispatch with send_email enabled')
        if official:
            if os.environ.get('GITHUB_REF') != 'refs/heads/main':
                raise ValueError('Official publication must run from main')
            store = Store(api)
            current = datetime.now(TAIPEI)
            if early_today:
                stale = (anchor.astimezone(TAIPEI).date() != current.date())
            else:
                stale = latest_due_date(anchor) != latest_due_date(current)
            if stale:
                values = {'run': False, 'reason': 'Expired run; a fresh schedule will handle latest due date'}
            else:
                values = plan(store, anchor, recipient, requested, early_today=early_today)
            if values['run'] and not values['generate']:
                restore(store, values['report_date'])
        else:
            day = date.fromisoformat(requested) if requested else anchor.astimezone(TAIPEI).date()
            if day > anchor.astimezone(TAIPEI).date():
                raise ValueError('Future preview date rejected')
            values = dict(run=True, generate=True, deploy=False, send=False, mode='auto',
                          report_date=day.isoformat(), official=False)
        output(values)
        print(json.dumps(values, ensure_ascii=False))
        return
    day = date.fromisoformat(os.environ['REPORT_DATE']).isoformat()
    if not official:
        if args.command != 'freeze':
            raise ValueError('Preview cannot deploy or send')
        freeze(None, day, official=False)
        return
    store = Store(GitHub())
    # Do not publish an expired batch after the next daily publication is due.
    current = datetime.now(TAIPEI)
    early_today = os.environ.get('EARLY_PUBLISH', 'false') == 'true'
    eligible = day == latest_due_date(current).isoformat()
    if early_today and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
        eligible = eligible or day == current.date().isoformat()
    if not eligible:
        raise ValueError('Batch expired while running; next run will process latest due date')
    if args.command == 'freeze':
        freeze(store, day)
    elif args.command == 'deployed':
        store.data['reports'][day]['deployed'] = True
        store.save()
    else:
        # Verify the frozen files again immediately before reserving a send attempt.
        restore(store, day)
        sender = gmail_sender(day, recipient)
        message_id = send_delivery(store, day, recipient, sender)
        print('Gmail delivery checkpoint: ' + (message_id or 'skipped'))


if __name__ == '__main__':
    main()
