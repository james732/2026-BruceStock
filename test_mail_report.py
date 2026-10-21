import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch
from google.auth.exceptions import RefreshError

import mail_report

COMPLETE_ROW = '<tr class="stock-row"><td class="score-cell" data-sort-value="115">115</td><td><span class="stock-code">2330</span><span class="stock-name">台積電</span></td></tr>'


class MailReportTests(unittest.TestCase):
    def test_only_v2_attachment_survives_email_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'analysis_momentum.html'
            report_bytes = COMPLETE_ROW.encode('utf-8')
            report.write_bytes(report_bytes)
            report.with_name('analysis_param.html').write_text('DO NOT ATTACH', encoding='utf-8')
            message = mail_report.build_message(report, 'james732@gmail.com', 'Report')
            decoded = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
            attachments = list(decoded.iter_attachments())
            self.assertEqual([part.get_filename() for part in attachments], ['analysis_momentum.html'])
            self.assertEqual([part.get_payload(decode=True) for part in attachments], [report_bytes])
            self.assertEqual(attachments[0].get_content_type(), 'text/html')
            self.assertEqual(decoded['To'], 'james732@gmail.com')
            body = decoded.get_body(preferencelist=('plain',)).get_content()
            self.assertIn('2330 台積電：115 分', body)
            self.assertIn('analysis_momentum.html', body)
            self.assertNotIn('analysis_param.html', body)

    def test_missing_attachment_does_not_authorize_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'analysis_momentum.html'
            with patch.object(mail_report, 'load_credentials') as credentials, patch.object(mail_report, 'build') as gmail:
                with self.assertRaises(FileNotFoundError):
                    mail_report.send_report(report, 'test@example.com', 'Report', Path('unused'), Path('unused'))
                credentials.assert_not_called()
                gmail.assert_not_called()

    def test_v2_does_not_require_param_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'analysis_momentum.html'
            report.write_text(COMPLETE_ROW, encoding='utf-8')
            message = mail_report.build_message(report, 'james732@gmail.com', 'Report')
            self.assertEqual(len(list(message.iter_attachments())), 1)

    def test_all_incomplete_scores_are_labeled_and_can_send(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'analysis_momentum.html'
            report.write_text(COMPLETE_ROW.replace('115', '-15').replace('>-15</td>', '>-15（法人資料未齊）</td>'), encoding='utf-8')
            message = mail_report.build_message(report, 'james732@gmail.com', 'Report')
            body = message.get_body(preferencelist=('plain',)).get_content()
            self.assertIn('目前沒有可完整評分', body)
            self.assertIn('2330 台積電：已知小計 -15 分', body)
            self.assertNotIn('1. 2330', body)

    def test_no_price_row_uses_stock_label_for_incomplete_score(self):
        parser = mail_report.ReportScoreParser()
        parser.feed('<tr class="stock-row"><td class="score-cell" data-sort-value="0">0（行情資料未齊）</td><td class="stock-cell" data-sort-value="0050 元大台灣50"><span>▶</span>0050 元大台灣50</td></tr>')
        self.assertEqual(parser.partial_scores, [(0, '0050', '元大台灣50')])
        self.assertEqual(parser.scores, [])


    def test_interactive_authorization_recovers_revoked_refresh_token(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Path(folder) / 'client_secret.json'
            token = Path(folder) / 'gmail_token.json'
            client.write_text('{"installed": {}}', encoding='utf-8')
            token.write_text('{"refresh_token": "expired"}', encoding='utf-8')
            old = Mock(expired=True, refresh_token='expired', valid=False)
            old.refresh.side_effect = RefreshError('invalid_grant')
            renewed = Mock(valid=True)
            renewed.to_json.return_value = '{"refresh_token": "new-grant"}'
            flow = Mock()
            flow.run_local_server.return_value = renewed
            with patch.object(mail_report.Credentials, 'from_authorized_user_file',
                              return_value=old), patch.object(
                    mail_report.InstalledAppFlow, 'from_client_secrets_file',
                    return_value=flow):
                self.assertIs(mail_report.load_credentials(client, token), renewed)
            self.assertEqual(token.read_text(encoding='utf-8'), '{"refresh_token": "new-grant"}')
            flow.run_local_server.assert_called_once_with(port=0)

    def test_ci_auth_failure_explains_secret_replacement_without_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Path(folder) / 'client_secret.json'
            token = Path(folder) / 'gmail_token.json'
            token.write_text('{"refresh_token": "expired"}', encoding='utf-8')
            old = Mock(expired=True, refresh_token='expired', valid=False)
            old.refresh.side_effect = RefreshError('invalid_grant')
            with patch.object(mail_report.Credentials, 'from_authorized_user_file',
                              return_value=old), patch.object(
                    mail_report.InstalledAppFlow, 'from_client_secrets_file') as interactive:
                with self.assertRaisesRegex(RuntimeError, 'GMAIL_TOKEN_JSON'):
                    mail_report.load_credentials(client, token, non_interactive=True)
                interactive.assert_not_called()


if __name__ == '__main__':
    unittest.main()
