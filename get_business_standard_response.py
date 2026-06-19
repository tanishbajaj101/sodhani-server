from __future__ import annotations

import argparse
import json
import os
import warnings
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup


URL = "https://www.business-standard.com/markets/research-report"
DATE_FORMAT = "%d-%b-%Y"


@dataclass(frozen=True)
class ResearchReport:
    company: str
    action: str
    target_price: str
    broker: str
    date: str
    report_url: str


def build_headers() -> dict[str, str]:
    headers = {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "accept-language": "en-US,en;q=0.9",
        "priority": "u=0, i",
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
        ),
    }

    cookie = os.environ.get("BS_COOKIE")
    if cookie:
        headers["cookie"] = cookie

    return headers


def fetch_html(url: str, timeout: int):
    warnings.filterwarnings("ignore", module=r"requests(\.|$)")
    import requests

    with requests.Session() as session:
        return session.get(
            url,
            headers=build_headers(),
            timeout=timeout,
            allow_redirects=True,
        )


def parse_report_date(value: str) -> date:
    value = value.replace("-Sept-", "-Sep-")
    return datetime.strptime(value, DATE_FORMAT).date()


def clean_text(value: str) -> str:
    return " ".join(value.split())


def find_research_table(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        headers = [clean_text(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        if (
            len(headers) >= 6
            and headers[0] == "Company"
            and headers[1] == "Action"
            and headers[2].startswith("Target Price")
            and headers[3] == "Broker"
            and headers[4] == "Date"
            and headers[5] == "Reports"
        ):
            return table

    raise ValueError("Could not find the Brokerage/Research Reports table in the HTML.")


def parse_reports(html: bytes | str) -> list[ResearchReport]:
    soup = BeautifulSoup(html, "html.parser")
    table = find_research_table(soup)
    reports: list[ResearchReport] = []

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        report_link = cells[5].find("a", href=True)
        reports.append(
            ResearchReport(
                company=clean_text(cells[0].get_text(" ", strip=True)),
                action=clean_text(cells[1].get_text(" ", strip=True)),
                target_price=clean_text(cells[2].get_text(" ", strip=True)),
                broker=clean_text(cells[3].get_text(" ", strip=True)),
                date=clean_text(cells[4].get_text(" ", strip=True)),
                report_url=report_link["href"] if report_link else "",
            )
        )

    return reports


def filter_recent_reports(
    reports: list[ResearchReport], run_date: date, days: int
) -> list[ResearchReport]:
    start_date = run_date - timedelta(days=days)
    filtered = []

    for report in reports:
        try:
            report_date = parse_report_date(report.date)
        except ValueError:
            continue

        if start_date <= report_date <= run_date:
            filtered.append(report)

    return filtered


def build_reports_payload(
    days: int = 15,
    url: str = URL,
    timeout: int = 30,
    run_date: date | None = None,
) -> dict:
    run_date = run_date or date.today()
    response = fetch_html(url, timeout)
    response.raise_for_status()

    reports = parse_reports(response.content)
    recent_reports = filter_recent_reports(reports, run_date, days)
    start_date = run_date - timedelta(days=days)

    return {
        "source": url,
        "status_code": response.status_code,
        "run_date": run_date.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": run_date.isoformat(),
        "days": days,
        "total_reports": len(reports),
        "reports_count": len(recent_reports),
        "reports": [asdict(report) for report in recent_reports],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Business Standard brokerage research reports and print recent "
            "reports as a JSON array."
        )
    )
    parser.add_argument("--url", default=URL, help="URL to fetch")
    parser.add_argument(
        "--input",
        help="Optional: parse an existing HTML file instead of fetching the page",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Request timeout in seconds",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=15,
        help="Keep reports from today minus this many days through today",
    )
    parser.add_argument(
        "--run-date",
        help="Override today's date for testing, in YYYY-MM-DD format",
    )
    args = parser.parse_args()

    run_date = (
        datetime.strptime(args.run_date, "%Y-%m-%d").date()
        if args.run_date
        else date.today()
    )
    if args.input:
        html = Path(args.input).read_bytes()
    else:
        response = fetch_html(args.url, args.timeout)
        response.raise_for_status()
        html = response.content

    reports = parse_reports(html)
    recent_reports = filter_recent_reports(reports, run_date, args.days)
    payload = [asdict(report) for report in recent_reports]

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
