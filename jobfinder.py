import csv
import json
import re
import sqlite3
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

ROOT = Path(__file__).resolve().parent
COMPANIES = json.loads((ROOT / "companies.json").read_text(encoding="utf-8"))
OUT = ROOT / "reports"
OUT.mkdir(exist_ok=True)
DB = ROOT / "jobs.sqlite3"
TIMEOUT = 18
MAX_PAGES_PER_COMPANY = 12
MAX_DEPTH = 2
REQUEST_DELAY_SECONDS = 0.35
USER_AGENT = "IndiaJobFinder/1.0 (personal job discovery; contact: local user)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})

CATEGORY_TERMS = {
    "Engineering": [
        "software engineer", "software developer", "sde", "developer", "backend engineer",
        "back-end engineer", "frontend engineer", "front-end engineer", "full stack",
        "full-stack", "qa engineer", "test engineer", "database engineer", "devops engineer",
        "cloud engineer", "data engineer", "systems engineer", "graduate engineer",
        "application engineer", "platform engineer", "site reliability engineer",
        "security engineer", "automation engineer", "engineering intern", "technology analyst"
    ],
    "Business Analysis": [
        "business analyst", "associate business analyst", "junior business analyst",
        "business intelligence analyst", "reporting analyst", "operations analyst",
        "data analyst", "product analyst", "business systems analyst", "functional analyst"
    ],
    "Project Management": [
        "project coordinator", "project analyst", "pmo analyst", "associate project manager",
        "junior project manager", "project coordinator", "program coordinator",
        "delivery coordinator", "project management analyst", "project management intern"
    ]
}
SENIOR_TERMS = [
    "senior ", "sr. ", "staff ", "principal ", "lead ", "tech lead", "director",
    "vice president", " vp ", "head of ", "chief ", "distinguished ", "engineering manager",
    "senior manager", "group manager", "architect "
]
JOB_LINK_TERMS = [
    "job", "jobs", "career", "careers", "opening", "openings", "position", "positions",
    "requisition", "vacancy", "vacancies", "employment", "opportunity", "opportunities",
    "greenhouse", "lever", "workday", "smartrecruiters", "icims", "ashby", "jobvite"
]
INDIA_TERMS = [
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai",
    "gurugram", "gurgaon", "noida", "new delhi", "delhi, india", "kolkata", "kochi",
    "ahmedabad", "jaipur", "indore", "thiruvananthapuram", "remote - india",
    "remote, india", "india - remote", "india (remote)"
]
REMOTE_TERMS = ["remote - india", "remote, india", "india - remote", "remote india", "india (remote)"]

def init_db():
    with sqlite3.connect(DB) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs(
            url TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, category TEXT,
            posted_date TEXT, experience TEXT, source_page TEXT, found_at TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS scans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, scan_date TEXT, company TEXT,
            status TEXT, pages_visited INTEGER, jobs_found INTEGER, detail TEXT)""")

def robots_allowed(url):
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        rp = RobotFileParser()
        rp.set_url(robots_url)
        # Fetch robots explicitly with timeout rather than RobotFileParser's unbounded URL read.
        r = SESSION.get(robots_url, timeout=TIMEOUT)
        if r.status_code >= 400:
            return True  # missing robots.txt is not a crawl prohibition
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True  # robots status unavailable; keep conservative request limits

def normalize_url(url):
    url, _ = urldefrag(url)
    return url.rstrip("/")

def same_site_or_job_host(seed, target):
    a, b = urlparse(seed), urlparse(target)
    if not b.scheme.startswith("http"):
        return False
    # Permit links on company subdomains and common ATS providers linked from company career sites.
    host_a = (a.hostname or "").lower().removeprefix("www.")
    host_b = (b.hostname or "").lower().removeprefix("www.")
    if host_a == host_b or host_a.endswith("." + host_b) or host_b.endswith("." + host_a):
        return True
    ats = ["greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
           "smartrecruiters.com", "ashbyhq.com", "icims.com", "jobvite.com",
           "successfactors.com", "oraclecloud.com", "taleo.net", "workable.com"]
    return any(host_b == x or host_b.endswith("." + x) for x in ats)

def is_jobish_link(text, url):
    hay = ((text or "") + " " + (url or "")).lower()
    return any(term in hay for term in JOB_LINK_TERMS)

def classify(title):
    t = re.sub(r"\s+", " ", (title or "").strip()).lower()
    if not t or len(t) > 220:
        return None
    if any(term in f" {t} " for term in SENIOR_TERMS):
        return None
    for cat, terms in CATEGORY_TERMS.items():
        if any(term in t for term in terms):
            return cat
    return None

def is_india(location, title="", url=""):
    loc = (location or "").lower()
    # Require evidence of India in location text; do not infer from company HQ.
    return any(term in loc for term in INDIA_TERMS)

def clean_text(s):
    return re.sub(r"\s+", " ", s or "").strip()

def extract_date(soup, text):
    # Try common structured metadata; this is usually a posting date only when explicitly provided.
    for key in ["datePosted", "datePublished", "dateCreated", "dateModified"]:
        el = soup.find(attrs={"itemprop": key})
        if el:
            val = el.get("content") or el.get("datetime") or el.get_text(" ", strip=True)
            m = re.search(r"\d{4}-\d{2}-\d{2}", val or "")
            if m: return m.group(0)
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in ["article:published_time", "date", "datepublished", "dateposted"]:
            val = meta.get("content", "")
            m = re.search(r"\d{4}-\d{2}-\d{2}", val)
            if m: return m.group(0)
    m = re.search(r"(?:posted|published|date posted)\s*:?\s*((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})", text, re.I)
    return m.group(1) if m else "Not stated"

def extract_experience(text):
    t = (text or "").lower()
    if any(x in t for x in ["fresher", "new grad", "new graduate", "graduate program", "graduate programme", "entry level", "entry-level", "0-1 year", "0–1 year", "internship", "intern "]):
        return "Entry-level signal"
    m = re.search(r"(\d+)\s*(?:-|to|–)\s*(\d+)\s*(?:years?|yrs?)", t)
    if m: return f"{m.group(1)}-{m.group(2)} years"
    m = re.search(r"(\d+)\s*\+?\s*(?:years?|yrs?)\s+(?:of )?experience", t)
    if m: return f"{m.group(1)}+ years"
    return "Not stated"

def candidate_title(soup, anchor_text, page_url):
    # Prefer structured title, then H1, then anchor label. Avoid treating navigation pages as job titles.
    for sel in ["h1", "[itemprop='title']", "[data-automation='jobTitle']"]:
        el = soup.select_one(sel)
        if el:
            val = clean_text(el.get_text(" ", strip=True))
            if 3 <= len(val) <= 180:
                return val
    path = urlparse(page_url).path.strip("/").split("/")
    if path:
        slug = re.sub(r"[-_]+", " ", path[-1])
        slug = re.sub(r"\b\d{4,}\b", "", slug).strip()
        if 3 <= len(slug) <= 140 and classify(slug):
            return slug.title()
    return clean_text(anchor_text)

def get_location(soup, text):
    for sel in ["[itemprop='jobLocation']", "[data-automation='locations']", "[data-testid*='location']", ".job-location", ".location"]:
        el = soup.select_one(sel)
        if el:
            val = clean_text(el.get_text(" ", strip=True))
            if val and len(val) < 200:
                return val
    # Only infer location from page text when a common Indian location appears nearby.
    for term in INDIA_TERMS:
        if term in text.lower():
            return term.title()
    return "Not stated"

def parse_page(company, url, source_page, anchor_text=""):
    if not robots_allowed(url):
        return None, []
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", "").lower():
            return None, []
        soup = BeautifulSoup(r.text, "html.parser")
        for el in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
            el.decompose()
        text = clean_text(soup.get_text(" ", strip=True))
        title = candidate_title(soup, anchor_text, r.url)
        category = classify(title)
        location = get_location(soup, text)
        job_url = normalize_url(r.url)
        record = None
        if category and is_india(location):
            record = {
                "url": job_url, "company": company, "title": title, "location": location,
                "category": category, "posted_date": extract_date(soup, text),
                "experience": extract_experience(text), "source_page": source_page,
                "found_at": datetime.now().isoformat(timespec="seconds")
            }
        links = []
        for a in soup.find_all("a", href=True):
            href = urljoin(r.url, a["href"])
            label = clean_text(a.get_text(" ", strip=True))[:180]
            if not same_site_or_job_host(source_page, href):
                continue
            if not href.startswith(("http://", "https://")):
                continue
            if any(x in urlparse(href).path.lower() for x in ["/privacy", "/cookie", "/terms", "/login", "/sign-in", "/signin"]):
                continue
            if is_jobish_link(label, href):
                links.append((normalize_url(href), label))
        return record, links
    except Exception:
        return None, []

def crawl_company(company):
    seed = company["career_url"]
    q = deque([(seed, 0, "Career homepage")])
    visited = set()
    found = {}
    while q and len(visited) < MAX_PAGES_PER_COMPANY:
        url, depth, anchor = q.popleft()
        url = normalize_url(url)
        if url in visited:
            continue
        visited.add(url)
        record, links = parse_page(company["name"], url, seed, anchor)
        if record:
            found[record["url"]] = record
        if depth < MAX_DEPTH:
            for link, label in links:
                if link not in visited and len(visited) + len(q) < MAX_PAGES_PER_COMPANY * 2:
                    q.append((link, depth + 1, label or "Job link"))
        time.sleep(REQUEST_DELAY_SECONDS)
    return list(found.values()), len(visited)

def save_results(records):
    with sqlite3.connect(DB) as con:
        for j in records:
            con.execute("""INSERT INTO jobs(url,company,title,location,category,posted_date,experience,source_page,found_at)
              VALUES(?,?,?,?,?,?,?,?,?)
              ON CONFLICT(url) DO UPDATE SET company=excluded.company,title=excluded.title,
              location=excluded.location,category=excluded.category,posted_date=excluded.posted_date,
              experience=excluded.experience,source_page=excluded.source_page,found_at=excluded.found_at""",
              (j["url"],j["company"],j["title"],j["location"],j["category"],j["posted_date"],j["experience"],j["source_page"],j["found_at"]))

def generate_outputs(scan_date):
    with sqlite3.connect(DB) as con:
        rows = con.execute("""SELECT company,title,location,category,posted_date,experience,url,found_at
            FROM jobs ORDER BY company,category,title""").fetchall()
        scans = con.execute("""SELECT company,status,pages_visited,jobs_found,detail FROM scans
            WHERE scan_date=? ORDER BY company""", (scan_date,)).fetchall()
    csv_path = OUT / f"india_jobs_{scan_date}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Company","Job title","Location","Category","Posting date (if available)","Experience signal","Clickable application URL","Last found"])
        w.writerows(rows)

    pdf_path = OUT / f"india_jobs_{scan_date}.pdf"
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="DocTitle2", parent=styles["Title"], fontSize=17, leading=21, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="Small2", parent=styles["BodyText"], fontSize=7.5, leading=9))
    styles.add(ParagraphStyle(name="URL2", parent=styles["BodyText"], fontSize=7, leading=8, wordWrap="CJK"))
    story = [
        Paragraph(f"India Job Finder — {scan_date}", styles["DocTitle2"]),
        Spacer(1, 4*mm),
        Paragraph(f"Matched jobs currently stored: {len(rows)}. Scope: India-only locations; Engineering, Business Analysis, Project Management; explicit senior titles excluded.", styles["BodyText"]),
        Paragraph("Posting dates are included only when exposed by a page; otherwise shown as Not stated. Every job title link opens the original posting. Results may include false positives or miss JavaScript-only listings; verify details on the employer website.", styles["Small2"]),
        Spacer(1, 4*mm)
    ]
    if rows:
        data = [[Paragraph("<b>Company</b>", styles["Small2"]), Paragraph("<b>Job title (click to open)</b>", styles["Small2"]),
                 Paragraph("<b>Location</b>", styles["Small2"]), Paragraph("<b>Category</b>", styles["Small2"]),
                 Paragraph("<b>Posted</b>", styles["Small2"]), Paragraph("<b>Experience</b>", styles["Small2"])]]
        for company,title,location,category,posted,experience,url,found_at in rows:
            u = url.replace("&","&amp;")
            data.append([
                Paragraph(company, styles["Small2"]),
                Paragraph(f'<link href="{u}" color="blue">{title}</link>', styles["Small2"]),
                Paragraph(location, styles["Small2"]), Paragraph(category, styles["Small2"]),
                Paragraph(posted or "Not stated", styles["Small2"]),
                Paragraph(experience or "Not stated", styles["Small2"])
            ])
        table = Table(data, colWidths=[37*mm, 70*mm, 36*mm, 36*mm, 25*mm, 32*mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E7EDF5")),
            ("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#C8CDD3")),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),
            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("No matching jobs were extracted during this scan. This can mean no matches were found, or the career pages could not expose their listings to the generic crawler. Check the scan status report and use the manual career links.", styles["BodyText"]))
    story.extend([Spacer(1, 5*mm), Paragraph("<b>Scan status</b>", styles["Heading2"])])
    status_data = [[Paragraph("<b>Company</b>", styles["Small2"]), Paragraph("<b>Status</b>", styles["Small2"]),
                    Paragraph("<b>Pages</b>", styles["Small2"]), Paragraph("<b>Matches</b>", styles["Small2"]),
                    Paragraph("<b>Notes</b>", styles["Small2"])]]
    for company,status,pages,jobs,detail in scans:
        status_data.append([Paragraph(company,styles["Small2"]), Paragraph(status,styles["Small2"]),
                            str(pages), str(jobs), Paragraph((detail or "")[:180],styles["Small2"])])
    st = Table(status_data, colWidths=[48*mm, 25*mm, 18*mm, 18*mm, 127*mm], repeatRows=1)
    st.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E7EDF5")),
        ("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#C8CDD3")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),
        ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
    ]))
    story.append(st)
    doc = SimpleDocTemplate(str(pdf_path), pagesize=landscape(A4), rightMargin=8*mm, leftMargin=8*mm, topMargin=9*mm, bottomMargin=9*mm)
    doc.build(story)
    return pdf_path, csv_path, len(rows)

def run_scan():
    init_db()
    scan_date = datetime.now().date().isoformat()
    total = 0
    for c in COMPANIES:
        name = c["name"]
        try:
            records, pages = crawl_company(c)
            save_results(records)
            total += len(records)
            status = "OK" if pages else "FAILED"
            detail = "Generic HTML crawl; JS-only pages and deep ATS workflows may not be visible."
            with sqlite3.connect(DB) as con:
                con.execute("INSERT INTO scans(scan_date,company,status,pages_visited,jobs_found,detail) VALUES(?,?,?,?,?,?)",
                            (scan_date,name,status,pages,len(records),detail))
            print(f"[{status}] {name}: visited {pages}, matched {len(records)}")
        except Exception as exc:
            with sqlite3.connect(DB) as con:
                con.execute("INSERT INTO scans(scan_date,company,status,pages_visited,jobs_found,detail) VALUES(?,?,?,?,?,?)",
                            (scan_date,name,"ERROR",0,0,str(exc)[:300]))
            print(f"[ERROR] {name}: {exc}")
    pdf, csv_path, count = generate_outputs(scan_date)
    print(f"\nToday's report: {pdf}")
    print(f"CSV: {csv_path}")
    print(f"Matching jobs stored (including previously found, still in database): {count}")
    print(f"Newly extracted matches in this scan: {total}")

if __name__ == "__main__":
    run_scan()
