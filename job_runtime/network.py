"""Public read transport with bounded rate-limit retries and Workday search POST allowlist."""
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request,urlopen

def public_read(url,headers=None,data=None,method=None,opener=urlopen,sleep=time.sleep,timeout=25):
    method=method or ('POST' if data is not None else 'GET')
    p=urlsplit(url)
    if p.scheme not in ('https','http'):raise ValueError('public HTTP(S) only')
    if method=='POST' and not (p.hostname and p.hostname.endswith('.myworkdayjobs.com') and p.path.startswith('/wday/cxs/') and p.path.endswith('/jobs')):raise ValueError('POST only permitted for public Workday CXS job search')
    if method not in ('GET','HEAD','POST'):raise ValueError('read-only transport')
    req=Request(url,data=data,method=method,headers=headers or {})
    for attempt in range(3):
        try:
            with opener(req,timeout=timeout) as response:return response.read().decode('utf-8','ignore'),response.geturl()
        except HTTPError as e:
            if e.code not in (403,429) or attempt==2:raise
            e.close()
            sleep(30)
