import csv, datetime as dt, html, json, os, re, subprocess, sys
# feeds.py runs this file with python -I, which drops the script's own folder from sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from title_filter import decide
from concurrent.futures import ThreadPoolExecutor as TP
# Reads public GitHub new-grad job lists and prints recent US software postings as JSON.
# Usage: poll_lists.py HOURS [KNOWN_FILE]   KNOWN_FILE lines look like: Company - Title
# Written with no backslashes or double quotes so it can sit inside a routine prompt.
DQ = chr(34)
LB, RB = re.escape(chr(91)), re.escape(chr(93))
CAP = 6  # most postings per company per run; the rest come back next hour
RAW = 'https://raw.githubusercontent.com/'
LISTS = [('simplify', RAW + 'SimplifyJobs/New-Grad-Positions/dev/README.md'),
         ('vanshb03', RAW + 'vanshb03/New-Grad-2027/dev/README.md'),
         ('speedyapply', RAW + 'speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md'),
         ('zapply', RAW + 'zapplyjobs/New-Grad-Software-Engineering-Jobs-2027/main/README.md'),
         ('harrycodingnow', RAW + 'harrycodingnow/new-grad-2027-tracker/main/README.md')]
KEEP = 'software engineer|software developer|swe|sde|software development engineer|backend|back-end|frontend|front-end|full stack|full-stack|fullstack|platform engineer|ai engineer|machine learning engineer|ml engineer|forward deployed|founding engineer|engineer i|engineer 1|new grad|early career|entry level|entry-level|junior|associate software|associate engineer|university grad|member of technical staff'
DROP = 'senior|sr|staff|principal|lead|manager|director|head of|intern|internship|co-op|coop|phd|fellow|architect|vp|summer 20[0-9]+|fall 20[0-9]+|spring 20[0-9]+'
NONUS = 'canada|india|uk|united kingdom|london|germany|berlin|amsterdam|netherlands|singapore|japan|tokyo|china|shanghai|beijing|ireland|dublin|france|poland|spain|israel|australia|mexico|brazil|bengaluru|bangalore|hyderabad|toronto|montreal|ottawa|korea|taiwan|romania|serbia|denmark|sweden'
USA = 'al|az|ca|co|ct|dc|fl|ga|id|il|in|ks|ky|la|ma|md|me|mi|mn|mo|nc|ne|nj|nv|ny|oh|or|pa|ri|sc|tn|tx|ut|va|wa|wi|usa|us|united states|remote|california|new york|nyc|seattle|san francisco|boston|chicago|austin|texas|washington|virginia|oregon|massachusetts|bay area|locations'
def has(words, text):
    return re.search('(^|[^a-z0-9])(' + words + ')($|[^a-z0-9])', text.lower()) is not None
def get(url):
    return subprocess.run(['curl', '-sL', '-m', '30', '-A', 'Mozilla/5.0', url], capture_output=True, text=True, errors='ignore').stdout
def clean(s):
    s = re.sub('<[^>]+>', ' ', s)
    s = re.sub(LB + '([^' + RB + ']*)' + RB + '[(][^)]*[)]', lambda m: m.group(1), s)
    s = html.unescape(s).replace('*', ' ').replace(chr(96), ' ')
    return ' '.join(s.split())
NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
def age(c):
    c = c.strip()
    m = re.fullmatch('([0-9]+) ?(m|min|h|d|w|mo)', c)
    if m:
        n, u = int(m.group(1)), m.group(2)
        if u in ('d', 'w', 'mo'):
            return n * {'d': 24, 'w': 168, 'mo': 720}[u] + 12
        return n * {'m': 1 / 60, 'min': 1 / 60, 'h': 1}[u]
    d = None
    for f, t in (('%b %d %Y', c + ' ' + str(NOW.year)), ('%Y-%m-%d', c)):
        try:
            d = dt.datetime.strptime(t, f)
            break
        except ValueError:
            pass
    if d is None:
        return None
    if d > NOW + dt.timedelta(days=2):
        d = d.replace(year=d.year - 1)
    return max(0, (NOW - d).total_seconds() / 3600)
def rows(t):
    out = []
    for block in t.split('<tr>')[1:]:
        cells = re.findall('<td[^>]*>(.*?)</td>', block.split('</tr>')[0], re.S)
        if len(cells) >= 4:
            out.append(cells)
    for line in t.splitlines():
        if line.startswith('|') and 'http' in line:
            cells = [c.strip() for c in line.strip().strip('|').split(' | ')]
            if len(cells) >= 4:
                out.append(cells)
    return out
HREF = 'href=' + DQ + '([^' + DQ + ']+)' + DQ + '|' + RB + '[(](http[^) ]+)[)]'
def links(c):
    return [a or b for a, b in re.findall(HREF, c) if not re.search('simplify.jobs|imgur|[.]png|[.]svg|github.com', a or b)]
def parse(src, t, hours):
    res, last = [], ''
    for cells in rows(t):
        txt = [clean(c) for c in cells]
        a = next((x for x in (age(c) for c in reversed(txt)) if x is not None), None)
        if a is None or a > hours:
            continue
        link = ''
        for c in cells:
            if re.search('alt=.Apply|' + LB + 'Apply|apply.png|apply.svg|Apply</a>', c) and links(c):
                link = links(c)[0]
                break
        if not link:
            every = [u for c in cells for u in links(c)]
            link = every[-1] if every else ''
        i = 1 if txt[0] else 0
        if txt[0].lower() == 'apply':
            i = 2
        comp = txt[i - 1].replace(chr(0x1F525), '').strip()
        if comp in ('', chr(0x21B3)):
            comp = last
        last = comp
        title, loc = txt[i], txt[i + 1] if i + 1 < len(txt) else ''
        mts = 'member of technical staff' in title.lower()
        if link and decide(title, loc)['keep']:
            res.append(dict(company=comp, title=title, location=loc, link=link, source=src, age_hours=round(a, 1)))
    return res
def key(company, title):
    f = lambda s, n: re.sub('[^a-z0-9]', '', s.lower())[:n]
    return f(company, 14) + '|' + f(title, 32)
def resolve(r):
    p = subprocess.run(['curl', '-sIL', '-m', '20', '-A', 'Mozilla/5.0', '-o', '/dev/null', '-w', '%{http_code} %{url_effective}', r['link']], capture_output=True, text=True).stdout.split(' ', 1)
    code, url = (p[0], p[1]) if len(p) == 2 else ('000', r['link'])
    host = url.split('/')[2] if url.count('/') >= 2 else ''
    if code in ('404', '410') or host.endswith(('zapply.jobs', 'vansh.nyc')):
        return None
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from job_runtime.policy import canonical_url
    r['link'] = canonical_url(url)
    return r
def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24
    known = set()
    if len(sys.argv) > 2:
        for line in open(sys.argv[2], errors='ignore'):
            if ' - ' in line:
                known.add(key(*line.strip().split(' - ', 1)))
    found, seen, failed, per = [], set(), [], {}
    for src, url in LISTS:
        t = get(url)
        if len(t) < 1000:
            failed.append(src)
            continue
        for r in parse(src, t, hours):
            k = r['link']
            c = key(r['company'], '').split('|')[0]
            if k not in seen and per.get(c, 0) < CAP:
                seen.add(k)
                per[c] = per.get(c, 0) + 1
                found.append(r)
    with TP(8) as ex:
        done = [r for r in ex.map(resolve, found) if r]
    uniq = {r['link']: r for r in done}
    print(json.dumps(dict(failed_lists=failed, count=len(uniq), postings=list(uniq.values())), indent=0))
if __name__ == "__main__":
    main()
