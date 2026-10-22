import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import mail_report
import momentum_report
import report_delivery as d
from momentum_scoring import Config
from test_mail_report import COMPLETE_ROW


class MemoryStore:
    def __init__(self):
        self.data = {'schema': 1, 'start_date': '2026-10-18', 'reports': {}}
        self.files = {}
        self.persisted = copy.deepcopy(self.data)
        self.saves = 0

    def save(self, files=None):
        self.files.update(files or {})
        self.persisted = copy.deepcopy(self.data)
        self.saves += 1

    def read(self, path):
        return self.files[path]


def anchor(text):
    return datetime.fromisoformat(text)


def fixture(directory, day='2026-10-18'):
    metadata = dict(report_date=day, market_as_of='2026-10-16',
                    generated_at='2026-10-19T00:39:00+08:00',
                    data_observed_at='2026-10-19T00:39:02+08:00', mode='daily')
    (directory / 'generation.json').write_text(json.dumps(metadata), encoding='utf-8')
    for name in ('index.html', 'analysis_momentum.html'):
        (directory / name).write_text('<html><body>' + COMPLETE_ROW + '</body></html>', encoding='utf-8')


class DeliveryTests(unittest.TestCase):
    def test_due_boundary_and_cross_midnight(self):
        for timestamp, expected in [
            ('2026-10-18T20:17:59+08:00', '2026-10-17'),
            ('2026-10-18T20:18:00+08:00', '2026-10-18'),
            ('2026-10-18T16:39:00+00:00', '2026-10-18'),
            ('2026-10-19T20:17:59+08:00', '2026-10-18'),
            ('2026-10-19T20:18:00+08:00', '2026-10-19')]:
            self.assertEqual(d.latest_due_date(anchor(timestamp)).isoformat(), expected)

    def test_early_manual_publication_today_and_evening_dedup(self):
        store = MemoryStore()
        at = anchor('2026-10-22T17:31:00+08:00')
        self.assertEqual(d.latest_due_date(at).isoformat(), '2026-10-21')
        options = d.plan(store, at, 'b@example.com', '2026-10-22', early_today=True)
        self.assertEqual(options['report_date'], '2026-10-22')
        self.assertTrue(options['generate'])
        self.assertTrue(options['deploy'])
        self.assertTrue(options['send'])
        store.data['reports']['2026-10-22'] = {'deployed': True, 'deliveries': {}}
        sender = Mock(return_value='accepted-id')
        self.assertEqual(d.send_delivery(store, '2026-10-22', 'b@example.com', sender), 'accepted-id')
        # Before 20:18 the normal due date is still yesterday; don't resend it
        # automatically after today's early report was already emailed.
        before_due = d.plan(store, anchor('2026-10-22T18:18:00+08:00'), 'b@example.com')
        self.assertFalse(before_due['run'])
        self.assertIn('superseded', before_due['reason'])
        self.assertTrue(d.plan(store, anchor('2026-10-22T18:18:00+08:00'),
                               'different@example.com')['run'])
        self.assertFalse(d.plan(store, anchor('2026-10-22T20:30:00+08:00'),
                                'b@example.com')['run'])
        sender.assert_called_once()

    def test_early_manual_publication_requires_explicit_today_before_cutoff(self):
        store = MemoryStore()
        at = anchor('2026-10-22T17:31:00+08:00')
        for requested in ('', '2026-10-21', '2026-10-23'):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                d.plan(store, at, 'b@example.com', requested, early_today=True)
        with self.assertRaises(ValueError):
            d.plan(store, anchor('2026-10-22T20:18:00+08:00'), 'b@example.com',
                   '2026-10-22', early_today=True)
        with self.assertRaises(ValueError):
            d.plan(store, at, 'b@example.com', '2026-10-22')

    def test_rollout_does_not_backfill_old_mail(self):
        store = MemoryStore()
        self.assertFalse(d.plan(store, anchor('2026-10-18T19:00:00+08:00'), 'b@example.com')['run'])
        plan = d.plan(store, anchor('2026-10-25T22:00:00+08:00'), 'b@example.com')
        self.assertEqual(plan['report_date'], '2026-10-25')
        with self.assertRaises(ValueError):
            d.plan(store, anchor('2026-10-25T22:00:00+08:00'), 'b@example.com', '2026-10-18')

    def test_reissue_preserves_sent_r1_and_archives_r2_separately(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder)
            fixture(dest, day='2026-10-22')
            d.freeze(store, '2026-10-22', dest)
            record = store.data['reports']['2026-10-22']
            record['deployed'] = True
            d.send_delivery(store, '2026-10-22', 'b@example.com',
                            Mock(return_value='original-gmail-id'))
            original = copy.deepcopy(store.data['reports']['2026-10-22'])
            self.assertEqual(original['deliveries'][d.recipient_key('b@example.com')]['status'], 'sent')
            early = d.plan(store, anchor('2026-10-22T17:45:00+08:00'), 'b@example.com',
                           '2026-10-22', early_today=True, reissue=True)
            self.assertTrue(early['generate'])
            self.assertEqual(early['revision'], 2)
            fixture(dest, day='2026-10-22')
            d.freeze(store, '2026-10-22', dest, revision=2)
            self.assertEqual(store.data['reports']['2026-10-22'], original)
            self.assertIn('reports/2026-10-22/r1/analysis_momentum.html', store.files)
            self.assertIn('reports/2026-10-22/r2/analysis_momentum.html', store.files)
            self.assertEqual(set(store.data['reports']['2026-10-22-r2']['files']),
                             {'analysis_momentum.html', 'index.html', 'report_metadata.json'})
            self.assertNotIn('analysis.html', store.data['reports']['2026-10-22-r2']['files'])
            fixture(dest, day='2026-10-22')
            with self.assertRaisesRegex(ValueError, 'Cannot overwrite'):
                d.freeze(store, '2026-10-22', dest, revision=2)
            store.data['reports']['2026-10-22-r2']['deployed'] = True
            receipt = d.send_delivery(store, '2026-10-22', 'b@example.com',
                                      Mock(return_value='new-gmail-id'))
            self.assertEqual(receipt, 'new-gmail-id')
            self.assertEqual(store.data['reports']['2026-10-22'], original)
            self.assertEqual(d.active_revision(store, '2026-10-22'), 2)
            self.assertFalse(d.plan(store, anchor('2026-10-22T20:30:00+08:00'),
                                    'b@example.com')['run'])
            self.assertFalse(d.plan(store, anchor('2026-10-22T18:30:00+08:00'),
                                    'b@example.com')['run'])

    def test_reuse_frozen_r1_removes_navigation_without_finmind(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder)
            fixture(dest, day='2026-10-22')
            old_nav = ('<nav><a href="analysis.html">原版報告</a> · '
                       '<a href="analysis_param.html">原版參數頁</a> · '
                       '<a href="analysis_momentum.html" aria-current="page">評分v2</a></nav>')
            source = '<!doctype html><html><body>' + old_nav + COMPLETE_ROW + '</body></html>'
            (dest / 'analysis_momentum.html').write_text(source, encoding='utf-8')
            (dest / 'index.html').write_text(source, encoding='utf-8')
            d.freeze(store, '2026-10-22', dest)
            store.data['reports']['2026-10-22']['deployed'] = True
            original = copy.deepcopy(store.data['reports']['2026-10-22'])
            d.reissue_v2_from_r1(store, '2026-10-22', dest)
            content = (dest / 'analysis_momentum.html').read_text(encoding='utf-8')
            self.assertNotIn('原版報告', content)
            self.assertNotIn('analysis_param.html', content)
            self.assertIn('沿用今日 r1 的行情與評分資料', content)
            self.assertNotIn('正式日報：2026-10-22 · r1', content)
            generated = json.loads((dest / 'generation.json').read_text(encoding='utf-8'))
            self.assertEqual(generated['source_revision'], 1)
            d.freeze(store, '2026-10-22', dest, revision=2)
            r2 = json.loads((dest / 'report_metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(r2['revision'], 2)
            self.assertEqual(r2['source_revision'], 1)
            self.assertEqual(store.data['reports']['2026-10-22'], original)
            self.assertEqual(set(store.data['reports']['2026-10-22-r2']['files']),
                             {'index.html', 'analysis_momentum.html', 'report_metadata.json'})
            d.restore(store, '2026-10-22', dest)
            self.assertIn('正式日報：2026-10-22 · r2',
                          (dest / 'analysis_momentum.html').read_text(encoding='utf-8'))

    def test_reissue_needs_prior_r1_and_explicit_early_publication(self):
        store = MemoryStore()
        now = anchor('2026-10-22T17:45:00+08:00')
        with self.assertRaises(ValueError):
            d.plan(store, now, 'b@example.com', '2026-10-22',
                   early_today=True, reissue=True)
        store.data['reports']['2026-10-22'] = {'deployed': True, 'deliveries': {}}
        with self.assertRaises(ValueError):
            d.plan(store, now, 'b@example.com', '2026-10-22', reissue=True)

    def test_freeze_restore_and_hash_validation(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fixture(directory)
            d.freeze(store, '2026-10-18', directory)
            content = (directory / 'analysis_momentum.html').read_bytes()
            metadata = json.loads((directory / 'report_metadata.json').read_bytes())
            self.assertIn('正式日報：2026-10-18', content.decode())
            (directory / 'analysis_momentum.html').unlink()
            d.restore(store, '2026-10-18', directory)
            self.assertEqual((directory / 'analysis_momentum.html').read_bytes(), content)
            resumed = d.plan(store, anchor('2026-10-19T00:39:00+08:00'), 'b@example.com')
            self.assertFalse(resumed['generate'])
            self.assertTrue(resumed['deploy'])
            store.data['reports']['2026-10-18']['deployed'] = True
            self.assertFalse(d.plan(store, anchor('2026-10-19T00:39:00+08:00'), 'b@example.com')['deploy'])
            message = mail_report.build_message(directory / 'analysis_momentum.html', 'b@example.com', 'Report', metadata)
            body = message.get_body(preferencelist=('plain',)).get_content()
            self.assertIn('報告日期：2026-10-18', body)
            self.assertIn('行情資料日：2026-10-16', body)
            self.assertIn('2026-10-19 00:39:00', body)
            (directory / 'analysis_momentum.html').write_text(COMPLETE_ROW)
            with self.assertRaises(ValueError):
                mail_report.build_message(directory / 'analysis_momentum.html', 'b@example.com', 'Report', metadata)
            store.files['reports/2026-10-18/r1/index.html'] = b'corrupt'
            with self.assertRaises(ValueError):
                d.restore(store, '2026-10-18', directory)

    def test_preview_never_checkpoints_and_date_mismatch_fails(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fixture(directory)
            with self.assertRaises(ValueError):
                d.freeze(store, '2026-10-19', directory)
            d.freeze(store, '2026-10-18', directory, official=False)
            self.assertEqual(store.saves, 0)
            self.assertFalse(json.loads((directory / 'report_metadata.json').read_bytes())['official'])

    def make_record(self):
        store = MemoryStore()
        store.data['reports']['2026-10-18'] = {'deployed': True, 'deliveries': {}}
        return store

    def test_success_skips_every_subsequent_run(self):
        store = self.make_record()
        sender = Mock(return_value='gmail-id')
        self.assertEqual(d.send_delivery(store, '2026-10-18', 'B@example.com', sender), 'gmail-id')
        self.assertIsNone(d.send_delivery(store, '2026-10-18', 'b@example.com', sender))
        sender.assert_called_once()
        self.assertFalse(d.plan(store, anchor('2026-10-19T00:39:00+08:00'), 'b@example.com')['run'])

    def test_uncertain_send_retries_once_then_stops(self):
        store = self.make_record()
        def uncertain():
            # The reservation MUST already be durable when the Gmail call starts.
            saved = d.delivery(store.persisted['reports']['2026-10-18'], 'b@example.com')
            self.assertEqual(saved['status'], 'sending')
            raise TimeoutError('Gmail may have accepted the message')
        sender = Mock(side_effect=uncertain)
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                d.send_delivery(store, '2026-10-18', 'b@example.com', sender)
        self.assertIsNone(d.send_delivery(store, '2026-10-18', 'b@example.com', sender))
        self.assertEqual(sender.call_count, 2)
        self.assertFalse(d.plan(store, anchor('2026-10-18T23:00:00+08:00'), 'b@example.com')['run'])


    def test_preflight_failure_does_not_consume_email_attempt(self):
        store = self.make_record()
        with tempfile.TemporaryDirectory() as folder:
            public = Path(folder)
            (public / 'report_metadata.json').write_text(
                json.dumps({'report_date': '2026-10-18', 'official': True}), encoding='utf-8')
            with patch.object(d, 'PUBLIC', public), patch.dict(os.environ, {
                    'GMAIL_CLIENT_SECRET_JSON': '{"installed": {}}',
                    'GMAIL_TOKEN_JSON': '{"refresh_token": "expired"}',
            }), patch.object(mail_report, 'load_credentials',
                             side_effect=RuntimeError('Gmail OAuth refresh token expired')):
                with self.assertRaisesRegex(RuntimeError, 'expired'):
                    d.gmail_sender('2026-10-18', 'b@example.com')
        # Authentication happens before send_delivery, so the durable quota is untouched.
        receipt = d.delivery(store.data['reports']['2026-10-18'], 'b@example.com')
        self.assertEqual(receipt['attempts'], 0)
        self.assertEqual(store.saves, 0)

    def test_preflighted_credentials_are_reused_for_single_gmail_send(self):
        store = self.make_record()
        with tempfile.TemporaryDirectory() as folder:
            public = Path(folder)
            (public / 'report_metadata.json').write_text(
                json.dumps({'report_date': '2026-10-18', 'official': True}), encoding='utf-8')
            credentials = object()
            with patch.object(d, 'PUBLIC', public), patch.dict(os.environ, {
                    'GMAIL_CLIENT_SECRET_JSON': '{"installed": {}}',
                    'GMAIL_TOKEN_JSON': '{"refresh_token": "fresh"}',
            }), patch.object(mail_report, 'load_credentials',
                             return_value=credentials) as preflight, patch.object(
                             mail_report, 'send_report', return_value='gmail-id') as send:
                sender = d.gmail_sender('2026-10-18', 'b@example.com')
                preflight.assert_called_once()
                self.assertEqual(store.saves, 0)
                result = d.send_delivery(store, '2026-10-18', 'b@example.com', sender)
                self.assertEqual(result, 'gmail-id')
                self.assertIs(send.call_args.kwargs['credentials'], credentials)
                self.assertEqual(store.saves, 2)
                self.assertEqual(d.delivery(store.data['reports']['2026-10-18'],
                                            'b@example.com')['status'], 'sent')

    def test_exhausted_quota_explicitly_marks_unconfirmed_delivery(self):
        store = self.make_record()
        receipt = d.delivery(store.data['reports']['2026-10-18'], 'b@example.com')
        receipt.update(attempts=2, status='sending')
        result = d.plan(store, anchor('2026-10-19T00:39:00+08:00'), 'b@example.com')
        self.assertFalse(result['run'])
        self.assertTrue(result['delivery_blocked'])
        self.assertIn('NOT confirmed', result['reason'])

    def test_failure_to_persist_reservation_never_sends(self):
        store = self.make_record()
        store.save = Mock(side_effect=RuntimeError('GitHub unavailable'))
        sender = Mock()
        with self.assertRaises(RuntimeError):
            d.send_delivery(store, '2026-10-18', 'b@example.com', sender)
        sender.assert_not_called()

    def test_deployment_failure_cannot_send(self):
        store = self.make_record()
        store.data['reports']['2026-10-18']['deployed'] = False
        sender = Mock()
        with self.assertRaises(ValueError):
            d.send_delivery(store, '2026-10-18', 'b@example.com', sender)
        sender.assert_not_called()

    def test_failed_sent_checkpoint_allows_only_one_duplicate(self):
        store = self.make_record()
        original_save = store.save
        def save(files=None):
            if store.saves == 1:
                raise RuntimeError('Lost success checkpoint')
            original_save(files)
        store.save = save
        sender = Mock(return_value='accepted')
        with self.assertRaises(RuntimeError):
            d.send_delivery(store, '2026-10-18', 'b@example.com', sender)
        # A fresh run reads only the durable reservation, not in-memory success.
        store.data = copy.deepcopy(store.persisted)
        store.save = original_save
        d.send_delivery(store, '2026-10-18', 'b@example.com', sender)
        d.send_delivery(store, '2026-10-18', 'b@example.com', sender)
        self.assertEqual(sender.call_count, 2)

    def test_daily_midnight_catchup_does_not_become_historical(self):
        remote = Mock()
        remote._fetch_rows.return_value = [{'date': '2026-10-16'}]
        remote.fetch_stock_names.return_value = {'2330': '台積電'}
        cache = Mock()
        rows = [{'date': (datetime(2026, 10, 16) - timedelta(days=59-i)).date().isoformat(),
                 'close': 100+i, 'Trading_Volume': 1000+i} for i in range(60)]
        cache.fetch.side_effect = lambda dataset, *args: rows if dataset == 'TaiwanStockPrice' else []
        cache.fetch_stock_news.return_value = []
        cache.warnings = []
        tdcc = Mock()
        tdcc.fetch_recent_snapshots.return_value = {}
        now = anchor('2026-10-19T00:39:00+08:00')
        day = d.date(2026, 10, 18)
        metadata = {}
        report = momentum_report.generate(['2330'], day, remote, cache, tdcc, Config(), now,
                                          mode='daily', metadata=metadata)
        self.assertEqual(metadata['report_date'], '2026-10-18')
        self.assertEqual(metadata['market_as_of'], '2026-10-16')
        self.assertEqual(metadata['mode'], 'daily')
        self.assertIn('每日批次延遲執行', report)
        self.assertNotIn('歷史查詢無可驗證', report)
        historical = momentum_report.generate(['2330'], day, remote, cache, tdcc, Config(), now)
        self.assertIn('歷史查詢無可驗證', historical)


class StoreTests(unittest.TestCase):
    def test_new_branch_then_restore_uses_git_blobs_and_nonforced_updates(self):
        api = Mock()
        api.request.return_value = None
        store = d.Store(api)
        store.data['reports']['2026-10-18'] = {'deployed': False}
        ledger = d.dump(store.data)
        api.request.side_effect = [
            {'sha': 'html-blob'}, {'sha': 'ledger-blob'}, {'sha': 'new-tree'},
            {'sha': 'first-commit'}, None, {'ref': 'refs/heads/report-state'}]
        store.save({'reports/2026-10-18/r1/index.html': b'html'})
        self.assertEqual(store.head, 'first-commit')
        self.assertEqual(api.request.call_args.args,
                         ('POST', 'git/refs', {'ref': 'refs/heads/report-state', 'sha': 'first-commit'}))
        import base64
        api.request.side_effect = [
            {'object': {'sha': 'first-commit'}}, {'tree': {'sha': 'new-tree'}},
            {'tree': [{'path': 'delivery.json', 'type': 'blob', 'sha': 'ledger-blob'}]},
            {'encoding': 'base64', 'content': base64.b64encode(ledger).decode()}]
        restored = d.Store(api)
        self.assertEqual(restored.data, store.data)
        api.request.side_effect = [
            {'sha': 'ledger-2'}, {'sha': 'tree-2'}, {'sha': 'commit-2'},
            {'object': {'sha': 'first-commit'}}, {}]
        restored.save()
        self.assertEqual(api.request.call_args.args,
                         ('PATCH', 'git/refs/heads/report-state', {'sha': 'commit-2', 'force': False}))

    def test_existing_broken_branch_never_initializes_empty_ledger(self):
        api = Mock()
        api.request.side_effect = [
            {'object': {'sha': 'old'}}, {'tree': {'sha': 'tree'}},
            {'tree': [], 'truncated': False}]
        with self.assertRaises(KeyError):
            d.Store(api)
        self.assertTrue(all(call.args[0] == 'GET' for call in api.request.call_args_list))

    def test_lease_conflict_never_updates_branch(self):
        api = Mock()
        api.request.return_value = None
        store = d.Store(api)
        api.request.side_effect = [
            {'sha': 'blob'}, {'sha': 'tree'}, {'sha': 'new'}, {'object': {'sha': 'other'}}]
        with self.assertRaises(RuntimeError):
            store.save()
        self.assertFalse(any(call.args[0] == 'PATCH' for call in api.request.call_args_list))

    def test_http_failure_is_not_treated_as_missing_state(self):
        api = d.GitHub.__new__(d.GitHub)
        api.repo = 'owner/repo'
        api.session = Mock()
        api.session.request.return_value = Mock(status_code=403, ok=False)
        with self.assertRaises(RuntimeError):
            api.request('GET', 'git/ref/heads/report-state', missing=True)


if __name__ == '__main__':
    unittest.main()
