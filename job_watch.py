#!/usr/bin/env python3
"""Daily job watcher.

Fetches several job boards, keeps the postings that match your keywords and
location, and writes the ones it hasn't seen before to the top of jobs.csv.
The CSV itself is the memory: a job whose URL is already in the file is skipped.
"""
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Settings you will most likely want to edit
# ----------------------------------------------------------------------------

# A job is kept if any of these appears at the start of a word in its title or
# card text (case-insensitive). Add or remove terms freely.
KEYWORDS = (
    r"social[- ]media|community|content|communications?|kommunikation|"
    r"öffentlichkeitsarbeit|presse|events?|veranstaltung|workshop"
)

# A job is dropped if its title matches any of these.
EXCLUDE = r"software|engineer|developer|devops"

# For sources that aren't already filtered by city, the job must mention one of these.
LOCATION = r"berlin|remote"

# Pages to read. For HTML sources, "link_re" picks out links that point to a
# single job. "location_filtered" means the page already lists only Berlin/remote jobs.
# "optional" means a failure is only a warning and doesn't mark the run as failed.
HTML_SOURCES = [
    {
        "name": "Talents4Good",
        "urls": [
            "https://jobs.talents4good.org/jobs",
            "https://jobs.talents4good.org/jobs?page=2",
        ],
        "link_re": r"jobs\.talents4good\.org/jobs/\d{6,}",
        "skip_re": r"/apply",
        "location_filtered": False,
    },
    {
        "name": "baito",
        "urls": ["https://www.getbaito.com/de/jobs"],
        "link_re": r"getbaito\.com/de/job/",
        "skip_re": None,
        "location_filtered": False,
        "optional": True,
    },
    {
        "name": "EnglishJobsInGermany",
        "urls": [
            "https://www.englishjobsingermany.com/berlin/content-social-media",
            "https://www.englishjobsingermany.com/berlin/marketing",
            "https://www.englishjobsingermany.com/remote",
        ],
        "link_re": r"englishjobsingermany\.com/job/",
        "skip_re": None,
        "location_filtered": True,
    },
]

ARBEITNOW_API = "https://www.arbeitnow.com/api/job-board-api"
ARBEITNOW_PAGES = 5

# ----------------------------------------------------------------------------

OUT = Path(__file__).with_name("jobs.csv")
FIELDS = ["date_found", "source", "title", "company", "details", "url"]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
TIMEOUT = 30


@dataclass
class Job:
    source: str
    title: str
    company: str
    details: str
    url: str
    text: str  # what the keyword/location filters look at


def norm(url: str) -> str:
    """URL without query string or fragment, used as the job's identity."""
    return url.split("#")[0].split("?")[0].rstrip("/")


def get(url: str, **kwargs) -> requests.Response:
    last = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
            if r.status_code == 429:  # rate limited: wait longer, then try again
                retry_after = r.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else 0
                last = requests.HTTPError(f"429 Too Many Requests for url: {url}")
                time.sleep(min(max(wait, 15 * (attempt + 1)), 90))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- HTML sources
def parse_cards(html: str, base: str, cfg: dict) -> list:
    soup = BeautifulSoup(html, "html.parser")
    link_re, skip_re = cfg["link_re"], cfg.get("skip_re")

    def is_job_link(a) -> bool:
        href = urljoin(base, a["href"])
        if not re.search(link_re, href):
            return False
        return not (skip_re and re.search(skip_re, href))

    def job_urls(el) -> set:
        return {
            norm(urljoin(base, a["href"]))
            for a in el.find_all("a", href=True)
            if is_job_link(a)
        }

    found = {}
    for a in soup.find_all("a", href=True):
        if not is_job_link(a):
            continue
        url = norm(urljoin(base, a["href"]))
        if url in found:
            continue

        # Climb to the smallest container that holds only this one job.
        card = a
        for _ in range(5):
            parent = card.parent
            if parent is None or parent.name in ("body", "html"):
                break
            if not job_urls(parent) <= {url}:
                break
            card = parent

        heading = card.find(re.compile(r"^h[1-6]$"))
        title = (heading or a).get_text(" ", strip=True)
        if len(title) > 120:  # link wraps the whole card, so build a title from the URL slug
            title = url.rsplit("/", 1)[-1].replace("-", " ").strip().capitalize()

        company_link = card.find("a", href=re.compile(r"/compan|/orgas"))
        company = company_link.get_text(" ", strip=True) if company_link else ""
        details = card.get_text(" | ", strip=True)[:250]

        found[url] = Job(cfg["name"], title, company, details, url, f"{title} {details}")
    return list(found.values())


def scrape_html(cfg: dict) -> list:
    jobs = {}
    for i, url in enumerate(cfg["urls"]):
        try:
            html = get(url).text
        except Exception as e:
            if i == 0:
                raise
            print(f"  [{cfg['name']}] skipped {url}: {e}")
            continue
        for job in parse_cards(html, url, cfg):
            jobs.setdefault(job.url, job)
        time.sleep(1)
    return list(jobs.values())


# ------------------------------------------------------------------- Arbeitnow
def scrape_arbeitnow() -> list:
    jobs = []
    for page in range(1, ARBEITNOW_PAGES + 1):
        data = get(ARBEITNOW_API, params={"page": page}).json().get("data", [])
        if not data:
            break
        for j in data:
            location = j.get("location") or ""
            remote = bool(j.get("remote"))
            tags = " ".join((j.get("tags") or []) + (j.get("job_types") or []))
            title = j.get("title") or ""
            where = location + (" (remote)" if remote else "")
            jobs.append(
                Job(
                    "Arbeitnow",
                    title,
                    j.get("company_name") or "",
                    where,
                    j.get("url") or "",
                    f"{title} {where} {tags}",
                )
            )
        time.sleep(1)
    return jobs


# ----------------------------------------------------------------- filter + io
def wanted(job: Job, location_filtered: bool) -> bool:
    if not job.url:
        return False
    if re.search(EXCLUDE, job.title, re.I):
        return False
    if not re.search(rf"\b({KEYWORDS})", job.text, re.I):
        return False
    if not location_filtered and not re.search(LOCATION, f"{job.text} {job.url}", re.I):
        return False
    return True


def load_existing() -> list:
    if not OUT.exists():
        return []
    with OUT.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def save(rows: list) -> None:
    with OUT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    existing = load_existing()
    known = {norm(r["url"]) for r in existing}
    today = date.today().isoformat()
    new_rows, problems = [], []

    sources = [
        (c["name"], c["location_filtered"], c.get("optional", False), lambda c=c: scrape_html(c))
        for c in HTML_SOURCES
    ]
    sources.append(("Arbeitnow", False, False, scrape_arbeitnow))

    for name, loc_filtered, optional, fetch in sources:
        try:
            jobs = fetch()
        except Exception as e:
            if optional:
                print(f"[{name}] WARNING (optional source, not counted as a failure): {e}")
            else:
                print(f"[{name}] FAILED: {e}")
                problems.append(name)
            continue
        hits = [j for j in jobs if wanted(j, loc_filtered)]
        fresh = []
        for j in hits:
            if norm(j.url) not in known:
                known.add(norm(j.url))
                fresh.append(j)
        print(f"[{name}] read {len(jobs)}, matched {len(hits)}, new {len(fresh)}")
        if not jobs:
            print(f"[{name}] WARNING: nothing parsed, the page layout may have changed")
            if not optional:
                problems.append(name)
        for j in fresh:
            new_rows.append(
                {
                    "date_found": today,
                    "source": j.source,
                    "title": j.title,
                    "company": j.company,
                    "details": j.details,
                    "url": j.url,
                }
            )

    if new_rows or not OUT.exists():
        save(new_rows + existing)
    print(f"Added {len(new_rows)} new jobs, {len(new_rows) + len(existing)} in total.")

    if problems:
        print("Sources with problems: " + ", ".join(problems))
        return 1  # makes the GitHub run show as failed, which triggers GitHub's alert email
    return 0


if __name__ == "__main__":
    sys.exit(main())
