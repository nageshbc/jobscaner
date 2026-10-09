# India Job Finder — Daily Scanner

This is a local, best-effort career-page crawler for 100 company career portals. It creates a dated PDF with clickable job-title links and a CSV copy.

## Run once
1. Install Python 3.11+.
2. Extract this folder.
3. Open Command Prompt in this folder.
4. Run:
   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   py jobfinder.py
   ```
5. Open the `reports` folder. It creates `india_jobs_YYYY-MM-DD.pdf` and `.csv`.

## Run daily automatically on Windows
After testing a manual run, open **Task Scheduler**:
- Create Basic Task → name it `India Job Finder Daily`
- Trigger → Daily → choose your preferred time
- Action → Start a program
- Program/script → full path to `.venv\Scripts\python.exe`
- Add arguments → full path to `jobfinder.py`
- Start in → full path to this extracted project folder

Keep the computer awake and connected to the internet at the scheduled time.

## Important limitations
- A universal scraper cannot reliably read every career website. This crawler follows public HTML links to a limited depth and checks `robots.txt`; it does not bypass login, CAPTCHA, or access controls.
- JavaScript-heavy pages, search forms, paginated ATS results, and sites that disallow crawling may yield no results. A scan-status section is included in each PDF so a failed scan is not mistaken for no vacancies.
- It only includes a result when it can detect a target job title and an India location. Location extraction is heuristic; verify every posting before applying.
- The date is shown only when detectable. Some pages expose an update date rather than the original posting date.
- The report includes previously stored results as well as newly found results, so old vacancies may remain. Open each link to confirm it is still active. Remove stale records by deleting `jobs.sqlite3` if you want to start fresh.
- Do not use this to bypass site rules. The crawler uses a low request rate and checks robots.txt, but company terms and site-specific rules may impose stricter conditions.


## Run it with GitHub Actions (no local computer needed)

1. Create a GitHub repository, for example `india-job-finder`.
2. Upload/push the entire project folder, including `.github/workflows/daily-scan.yml`, to the repository's **default branch**.
3. Open the repository's **Actions** tab. If prompted, enable workflows.
4. Select **Daily India Job Scan** → **Run workflow** to test it immediately.
5. When it finishes, open the workflow run and download the `india-job-reports-N` artifact. It contains the dated PDF and CSV.
6. The workflow is scheduled for **7:30 AM India time daily**. The report artifact is retained for 30 days.

No GitHub token or other secret is required for this read-only scan. Scheduled GitHub Actions can be delayed by platform load, and scheduled workflows must exist on the default branch. GitHub documents scheduled workflows and manual `workflow_dispatch` runs:
https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows

Important: this automates the crawl and report generation, not a guarantee that every career portal can be scraped. The PDF's scan-status table reports which sites were visited; JavaScript-only or restricted sites may require a site-specific integration. The artifact is a downloadable report, not a permanent public PDF URL. If you want a stable URL, a separate publishing step (for example, GitHub Pages or a reports branch) must be configured.
