import argparse
import base64
import json
import mimetypes
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
DEFAULT_RECIPIENT = "james732@gmail.com"
DEFAULT_REPORT = "analysis_momentum.html"
REPORT_URL = "https://james732.github.io/2026-BruceStock/analysis_momentum.html"
TOKEN_FILE = "gmail_token.json"


class ReportScoreParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scores: list[tuple[int, str, str]] = []
        self.partial_scores: list[tuple[int, str, str]] = []
        self._current: dict[str, str] | None = None
        self._text_target: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "tr" and "stock-row" in classes:
            self._current = {}
        elif self._current is not None and tag == "td" and "score-cell" in classes:
            self._current["score"] = attributes.get("data-sort-value") or ""
            self._text_target = "score_text"
        elif self._current is not None and tag == "td" and "stock-cell" in classes:
            label = (attributes.get("data-sort-value") or "").split(" ", 1)
            self._current["stock_id"] = label[0]
            self._current["stock_name"] = label[1] if len(label) > 1 else ""
        elif self._current is not None and tag == "span":
            if "stock-code" in classes:
                self._current["stock_id"] = ""
                self._text_target = "stock_id"
            elif "stock-name" in classes:
                self._current["stock_name"] = ""
                self._text_target = "stock_name"

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._text_target is not None:
            self._current[self._text_target] = (
                self._current.get(self._text_target, "") + data
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in ("span", "td"):
            self._text_target = None
        elif tag == "tr" and self._current is not None:
            try:
                score = int(self._current["score"])
                stock_id = self._current["stock_id"].strip()
                stock_name = self._current["stock_name"].strip()
            except (KeyError, ValueError):
                self._current = None
                return
            if stock_id:
                target = self.scores if self._current.get("score_text", "").strip() == str(score) else self.partial_scores
                target.append((score, stock_id, stock_name))
            self._current = None


def extract_report_scores(report_html: str) -> list[tuple[int, str, str]]:
    parser = ReportScoreParser()
    parser.feed(report_html)
    if not parser.scores:
        raise ValueError("The report contains no overview scores; regenerate the report first.")
    return sorted(parser.scores, key=lambda item: (-item[0], item[1]))


def build_email_body(
    scores: list[tuple[int, str, str]], modified_time: datetime,
    report_name: str = DEFAULT_REPORT,
    partial_scores: list[tuple[int, str, str]] | None = None,
) -> str:
    score_lines = [
        f"{rank}. {stock_id} {stock_name}：{score} 分"
        for rank, (score, stock_id, stock_name) in enumerate(scores, start=1)
    ]
    return "\n".join(
        [
            "台股分析報告 — 評分v2",
            "完整資料總分（由高至低）：",
            "",
            *(score_lines or ["目前沒有可完整評分的個股。"]),
            *(["", "資料不完整（不納入排名，僅列已知小計）："] + [
                f"{stock_id} {stock_name}：已知小計 {score} 分"
                for score, stock_id, stock_name in partial_scores
            ] if partial_scores else []),
            "",
            f"報告產生時間：{modified_time:%Y-%m-%d %H:%M:%S}",
            f"完整報告請見附件 {report_name}。",
            "附件請下載後以瀏覽器開啟。",
            f"線上報告：{REPORT_URL}",
        ]
    )


def find_client_secret(base_dir: Path) -> Path:
    matches = sorted(base_dir.glob("client_secret_*.json"))
    if not matches:
        raise FileNotFoundError(
            f"No OAuth client secret was found in {base_dir}. "
            "Expected a file named client_secret_*.json."
        )
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise RuntimeError(f"Multiple OAuth client secrets were found: {names}")
    return matches[0]


def load_credentials(client_secret: Path, token_path: Path, non_interactive: bool = False) -> Credentials:
    credentials = None

    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(token_path, SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        if non_interactive:
            raise RuntimeError(
                "Gmail authorization is required. Run uv run mail_report.py --authorize-only "
                "interactively, then run the scheduled task again."
            )
        flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
        credentials = flow.run_local_server(port=0)

    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def build_message(report_path: Path, recipient: str, subject: str) -> EmailMessage:
    modified_time = datetime.fromtimestamp(report_path.stat().st_mtime, ZoneInfo("Asia/Taipei"))
    report_bytes = report_path.read_bytes()
    parser = ReportScoreParser()
    parser.feed(report_bytes.decode("utf-8-sig"))
    if not parser.scores and not parser.partial_scores:
        raise ValueError("The report contains no overview scores; regenerate the report first.")
    scores = sorted(parser.scores, key=lambda item: (-item[0], item[1]))
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = "me"
    message["Subject"] = subject
    message.set_content(build_email_body(scores, modified_time, report_path.name, parser.partial_scores))

    for attachment_path, content in ((report_path, report_bytes),):
        mime_type, _ = mimetypes.guess_type(attachment_path.name)
        main_type, sub_type = (mime_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            content,
            maintype=main_type,
            subtype=sub_type,
            filename=attachment_path.name,
        )
    return message


def send_report(
    report_path: Path,
    recipient: str,
    subject: str,
    client_secret: Path,
    token_path: Path,
    non_interactive: bool = False,
) -> str:
    message = build_message(report_path, recipient, subject)
    credentials = load_credentials(client_secret, token_path, non_interactive)
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    with build("gmail", "v1", credentials=credentials) as service:
        result = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw_message})
            .execute()
        )

    message_id = result.get("id")
    if not message_id:
        raise RuntimeError(f"Gmail API returned no message ID: {json.dumps(result)}")
    return message_id


def parse_arguments() -> argparse.Namespace:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Send the v2 HTML report as a single attachment through the Gmail API."
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=base_dir / DEFAULT_REPORT,
        help="Path to the HTML report to attach (default: analysis_momentum.html).",
    )
    parser.add_argument(
        "--recipient",
        default=DEFAULT_RECIPIENT,
        help="Recipient email address.",
    )
    parser.add_argument(
        "--subject",
        default=f"Stock analysis report - 評分v2 - {datetime.now(ZoneInfo("Asia/Taipei")):%Y-%m-%d}",
        help="Email subject.",
    )
    parser.add_argument(
        "--client-secret",
        type=Path,
        default=None,
        help="Path to the Google OAuth client secret JSON file.",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=base_dir / TOKEN_FILE,
        help="Path used to store the OAuth access and refresh token.",
    )
    parser.add_argument("--non-interactive", action="store_true", help="Fail instead of opening an OAuth login browser.")
    parser.add_argument("--authorize-only", action="store_true", help="Authorize Gmail without sending a message.")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    base_dir = Path(__file__).resolve().parent
    report_path = args.report.resolve()
    token_path = args.token.resolve()
    client_secret = (
        args.client_secret.resolve()
        if args.client_secret
        else find_client_secret(base_dir)
    )

    if args.authorize_only:
        load_credentials(client_secret, token_path, args.non_interactive)
        print("Gmail authorization is ready.")
        return 0

    if not report_path.is_file():
        raise FileNotFoundError(f"Report file does not exist: {report_path}")

    try:
        message_id = send_report(
            report_path=report_path,
            recipient=args.recipient,
            subject=args.subject,
            client_secret=client_secret,
            token_path=token_path,
            non_interactive=args.non_interactive,
        )
    except HttpError as error:
        raise RuntimeError(f"Gmail API request failed: {error}") from error

    print(f"Email sent to {args.recipient}. Gmail message ID: {message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

