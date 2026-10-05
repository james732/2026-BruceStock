import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import mail_report


class MailReportTests(unittest.TestCase):
    def test_both_attachments_survive_email_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "custom.html"
            report_bytes = (
                '<tr class="stock-row"><td class="score-cell" data-sort-value="85">85</td>'
                '<td><span class="stock-code">2330</span>'
                '<span class="stock-name">台積電</span></td></tr>'
            ).encode("utf-8")
            param_bytes = '<html lang="zh-Hant">評分參數<script>const score = 100;</script></html>'.encode("utf-8")
            report.write_bytes(report_bytes)
            report.with_name("analysis_param.html").write_bytes(param_bytes)
            message = mail_report.build_message(report, "test@example.com", "Report")
            decoded = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
            attachments = list(decoded.iter_attachments())
            self.assertEqual([part.get_filename() for part in attachments], ["custom.html", "analysis_param.html"])
            self.assertEqual([part.get_payload(decode=True) for part in attachments], [report_bytes, param_bytes])
            self.assertTrue(all(part.get_content_type() == "text/html" for part in attachments))
            body = decoded.get_body(preferencelist=("plain",)).get_content()
            self.assertIn("2330 台積電：85 分", body)
            self.assertIn("custom.html", body)
            self.assertIn("analysis_param.html", body)

    def test_missing_attachment_does_not_authorize_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "analysis.html"
            report.write_text("<html></html>", encoding="utf-8")
            with patch.object(mail_report, "load_credentials") as credentials, \
                 patch.object(mail_report, "build") as gmail:
                with self.assertRaises(FileNotFoundError):
                    mail_report.send_report(report, "test@example.com", "Report", Path("unused"), Path("unused"))
                credentials.assert_not_called()
                gmail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
