"""Job watcher: SOC L1 / Pentest jobs -> Discord webhook.

Usage:
  $env:DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
  python job_watcher.py            # one poll; sends only NEW jobs
  python job_watcher.py --dry-run  # print, don't send, don't save state
  python job_watcher.py --loop 15  # poll every 15 minutes forever

Optional: JOOBLE_KEY (free key from jooble.org/api/about) enables Thailand search.
Only uses public APIs / RSS feeds (no scraping of sites that forbid it).

Each job shows: company, date posted, country, salary, type, description,
benefits, contact email / phone. Email/phone/benefits/salary are shown only
when the posting itself contains them; otherwise "ไม่ระบุ".
"""
import html, json, os, re, sys, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

STATE = Path(__file__).with_name("seen_jobs.json")
UA = {"User-Agent": "job-watcher/1.0"}
NA = "ไม่ระบุ"

# Only JUNIOR SOC / L1 roles. A title must (1) be a SOC / security-monitoring role
# AND (2) carry a junior / L1 marker. Plain "SOC Analyst" (level unstated) is skipped.
INCLUDE = re.compile(
    r"(\bsoc\b|security operations|security analyst|cyber ?security analyst|"
    r"security monitoring|threat (detection|analyst)|incident (response|responder)|blue team|cyber defen[cs]e)",
    re.I,
)
JUNIOR = re.compile(
    r"(\bjunior\b|\bjr\.?\b|\bl\s?-?1\b|\blevel\s?-?1\b|\btier\s?-?1\b|\bt1\b|\bentry[- ]level\b|"
    r"\bassociate\b|\btrainee\b|\bgraduate\b|\bintern(ship)?\b|\banalyst\s+(i|1)\b|\bi\s*\(?l1\)?\b)",
    re.I,
)
EXCLUDE = re.compile(
    r"\b(senior|sr\.?|lead|principal|head of|director|manager|staff|architect|"
    r"l\s?-?[23]|level\s?-?[23]|tier\s?-?[23]|analyst\s+(ii|iii|2|3)|ii|iii)\b",
    re.I,
)


def is_target(title):
    return bool(INCLUDE.search(title) and JUNIOR.search(title) and not EXCLUDE.search(title))

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w/])(\+?\d[\d\s().-]{8,16}\d)(?![\w/])")
BENEFIT_HEAD = re.compile(r"^\W*(benefits?|perks|what we offer|we offer|compensation (&|and) benefits|why join)\b", re.I)


def get(url, data=None, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def to_text(raw):
    t = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div|/ul)\s*/?>", "\n", raw or "")
    t = re.sub(r"(?i)<li[^>]*>", "• ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def fmt_date(v):
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            d = datetime.fromtimestamp(int(v), timezone.utc)
        elif isinstance(v, str) and v:
            try:
                d = parsedate_to_datetime(v)
            except Exception:
                d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        else:
            return NA
        return d.strftime("%d/%m/%Y")
    except Exception:
        return NA


def fmt_salary(lo=None, hi=None, cur=None, period=None, text=None):
    if text:
        return text
    if lo or hi:
        rng = f"{lo:,.0f}–{hi:,.0f}" if lo and hi else f"{(lo or hi):,.0f}"
        return f"{rng} {cur or ''} {('/' + period) if period else ''}".strip()
    return NA


def job(src, title, company, country, url, date=None, salary=None, jtype=None, desc_html="", company_url=None):
    text = to_text(desc_html)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    # benefits: lines after a "Benefits/Perks" style heading
    ben = []
    for i, l in enumerate(lines):
        if BENEFIT_HEAD.match(l) and len(l) < 60:
            ben = [x for x in lines[i + 1:i + 9]]
            break
    emails = list(dict.fromkeys(EMAIL_RE.findall(text)))
    phones = [p.strip() for p in PHONE_RE.findall(text) if len(re.sub(r"\D", "", p)) >= 9]
    return {
        "src": src, "title": (title or "").strip(), "company": (company or "").strip(),
        "country": (country or "").strip() or NA, "url": url, "date": fmt_date(date),
        "salary": salary or NA, "type": jtype or NA, "desc": text,
        "benefits": "\n".join(ben) or NA,
        "email": ", ".join(emails[:2]) or NA, "phone": ", ".join(dict.fromkeys(phones[:2])) or NA,
        "company_url": company_url,
    }


def remotive():
    out = []
    for q in ("junior soc analyst", "soc analyst l1", "tier 1 soc analyst", "soc analyst"):
        d = json.loads(get("https://remotive.com/api/remote-jobs?search=" + urllib.parse.quote(q)))
        for j in d.get("jobs", []):
            out.append(job("Remotive", j["title"], j["company_name"], j.get("candidate_required_location"), j["url"],
                           j.get("publication_date"), fmt_salary(text=j.get("salary") or None), j.get("job_type"),
                           j.get("description")))
    return out


def remoteok():
    d = json.loads(get("https://remoteok.com/api"))
    return [job("RemoteOK", j.get("position"), j.get("company"), j.get("location") or "Remote", j.get("url"),
                j.get("epoch"), fmt_salary(j.get("salary_min") or None, j.get("salary_max") or None, "USD", "year"),
                None, j.get("description"))
            for j in d if isinstance(j, dict) and j.get("position")]


def jobicy():
    d = json.loads(get("https://jobicy.com/api/v2/remote-jobs?count=50&tag=security"))
    return [job("Jobicy", j["jobTitle"], j["companyName"], j.get("jobGeo"), j["url"], j.get("pubDate"),
                None, ", ".join(j["jobType"]) if isinstance(j.get("jobType"), list) else j.get("jobType"),
                j.get("jobDescription")) for j in d.get("jobs", [])]


def himalayas():
    out = []
    for q in ("junior soc analyst", "soc analyst l1", "tier 1 soc analyst", "soc analyst"):
        d = json.loads(get("https://himalayas.app/jobs/api/search?q=" + urllib.parse.quote(q)))
        for j in d.get("jobs", []):
            out.append(job("Himalayas", j.get("title"), j.get("companyName"),
                           ", ".join(j.get("locationRestrictions") or []) or "Remote (ทั่วโลก)",
                           j.get("applicationLink") or j.get("guid"), j.get("pubDate"),
                           fmt_salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency"), j.get("salaryPeriod")),
                           j.get("employmentType"), j.get("description"),
                           f"https://himalayas.app/companies/{j.get('companySlug')}"))
    return out


def wwr():
    out = []
    for feed in ("remote-devops-sysadmin-jobs", "remote-back-end-programming-jobs"):
        root = ET.fromstring(get(f"https://weworkremotely.com/categories/{feed}.rss"))
        for it in root.iter("item"):
            company, _, title = (it.findtext("title") or "").partition(": ")
            country = it.findtext("country") or it.findtext("region") or "Remote"
            out.append(job("WeWorkRemotely", title, company, country, it.findtext("link"),
                           it.findtext("pubDate"), None, it.findtext("type"), it.findtext("description")))
    return out


def jooble():
    key = os.environ.get("JOOBLE_KEY")
    if not key:
        return []
    out = []
    for q in ("junior SOC analyst", "SOC analyst L1", "tier 1 SOC analyst"):
        body = json.dumps({"keywords": q, "location": "Thailand"}).encode()
        d = json.loads(get(f"https://jooble.org/api/{key}", body, {"Content-Type": "application/json"}))
        for j in d.get("jobs", []):
            out.append(job("Jooble TH", j.get("title"), j.get("company"), "Thailand 🇹🇭 " + (j.get("location") or ""),
                           j.get("link"), j.get("updated"), fmt_salary(text=j.get("salary") or None), j.get("type"),
                           j.get("snippet")))
    return out


SOURCES = [remotive, remoteok, jobicy, himalayas, wwr, jooble]


def collect():
    jobs = []
    for fn in SOURCES:
        try:
            jobs += fn()
        except Exception as e:
            print(f"[warn] {fn.__name__}: {e}", file=sys.stderr)
    seen, res = set(), []
    for j in jobs:
        if not j["url"] or j["url"] in seen:
            continue
        seen.add(j["url"])
        if is_target(j["title"]):
            res.append(j)
    return res


def cut(s, n):
    s = s or NA
    return s if len(s) <= n else s[: n - 1] + "…"


def embed(j):
    fields = [
        ("🏢 บริษัท", j["company"] or NA, True),
        ("📅 วันที่ประกาศ", j["date"], True),
        ("🌍 ประเทศที่เปิดรับ", j["country"], True),
        ("💰 เงินเดือน", j["salary"], True),
        ("🕒 รูปแบบงาน", j["type"], True),
        ("📧 อีเมลบริษัท", j["email"], True),
        ("📞 เบอร์โทรบริษัท", j["phone"], True),
        ("🎁 สวัสดิการ", cut(j["benefits"], 350), False),
        ("📝 รายละเอียดงาน", cut(j["desc"], 500), False),
    ]
    e = {"title": cut(j["title"], 250), "url": j["url"], "color": 0x2ECC71,
         "fields": [{"name": n, "value": cut(v, 1000), "inline": i} for n, v, i in fields],
         "footer": {"text": f"แหล่งข้อมูล: {j['src']}"}}
    return e


def send(webhook, batch):
    # Discord caps one message at 6000 chars across all embeds -> 3 jobs per message
    for i in range(0, len(batch), 3):
        payload = json.dumps({"content": "🚨 งานใหม่ SOC / Pentest" if i == 0 else None,
                              "embeds": [embed(j) for j in batch[i:i + 3]]}).encode()
        get(webhook, payload, {"Content-Type": "application/json"})
        time.sleep(1.5)


def run(dry):
    state = set(json.loads(STATE.read_text())) if STATE.exists() else None
    jobs = collect()
    first_run = state is None
    state = state or set()
    new = [j for j in jobs if j["url"] not in state]
    print(f"{len(jobs)} matching, {len(new)} new")
    if dry:
        for j in new:
            print(f"- [{j['src']}] {j['title']} | {j['company']} | {j['country']} | {j['date']} | {j['salary']} | "
                  f"mail={j['email']} tel={j['phone']} | benefits={'yes' if j['benefits'] != NA else 'no'}")
        return
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        sys.exit("Set DISCORD_WEBHOOK_URL first")
    if new:
        send(webhook, new[:15] if first_run else new)
    state |= {j["url"] for j in jobs}
    STATE.write_text(json.dumps(sorted(state)))


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if "--loop" in sys.argv:
        mins = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            try:
                run(dry)
            except Exception as e:
                print("[error]", e, file=sys.stderr)
            time.sleep(mins * 60)
    else:
        run(dry)
