#!/usr/bin/env python3
"""Decides from a job title and location whether a posting is worth opening, with the owner's role list
(2026-11-04: software, backend, frontend, full stack, platform, AI/ML, forward deployed, data engineer,
DevOps/SRE/cloud, QA/test automation, and entry-level product manager).

Usage: title_filter.py "<title>" ["<location>"]   -> prints JSON {"keep": true|false, "reason": "...", "role": "swe|ai|data|devops|qa|pm"}
Importable: decide(title, location) -> dict. Exit code 0 keep, 1 drop.
Only the title and location are judged here; the description, citizenship, clearance and years rules are the
rubric's job after the page is read.
"""
import json
import re
import sys

ROLES = [
    ("pm", r"product manager|associate product manager|\bapm\b|product management"),
    ("ai", r"\bai engineer|machine learning engineer|\bml engineer|\bmle\b|applied ai|genai|generative ai|llm engineer|ai software|ai/ml"),
    ("data", r"data engineer|analytics engineer|data platform engineer|data analyst|data scientist|data science|business intelligence|\bbi (analyst|developer|engineer)|analytics analyst|reporting analyst|data analytics|etl developer|data developer"),  # owner 2026-11-12: data analyst, junior data scientist and the rest of that realm
    ("devops", r"devops|site reliability|\bsre\b|cloud engineer|infrastructure engineer|platform engineer|systems engineer"),
    ("qa", r"\bqa\b|quality engineer|test engineer|\bsdet\b|test automation|automation engineer|software.*test|quality assurance"),
    ("swe", r"software engineer|software developer|\bswe\b|\bsde\b|software development engineer|backend|back-end|frontend|front-end|front end|full stack|full-stack|fullstack|web developer|application developer|mobile engineer|ios engineer|android engineer|forward deployed|founding engineer|solutions engineer|member of technical staff|engineer i\b|engineer 1\b|developer i\b|programmer|software"),
]
LEVEL_HINT = r"new grad|new-grad|early career|entry level|entry-level|junior|associate|university grad|graduate|rotational|rotation program|2026|2027|engineer i\b|engineer 1\b|\bi\b$|level 1|l1\b|campus|college grad"
DROP = r"\bsenior\b|\bsr\.?\b|\bstaff\b|principal|\blead\b|\bdirector\b|head of|\bintern\b|internship|co-op|\bcoop\b|\bphd\b|fellow|architect|\bvp\b|vice president|chief|\bmanager\b|\bmgr\b|\bstf\b|\bprin\b|intermediate|mid level|mid-level|\bmid\b|expert|distinguished|summer 20\d\d|spring 20\d\d|fall 20\d\d|\b(iii|iv|3|4)\b(?! locations)|\bexperienced\b|\bmid-level\b|clearance|ts/sci|\bsecret\b|\bplc\b|controls engineer|electrical engineer|mechanical engineer|civil engineer|manufacturing engineer|hvac|structural engineer|process engineer|chemical engineer|field service"
# 2026-11-13: hardware, manufacturing and field-engineering titles that slipped through the role patterns
# ("Laser Test Engineer", "Optical Hardware Test Engineer", "Product Quality Engineer", "RF Engineer I"). A title
# that also names software/data/AI/cloud work keeps passing.
NONSOFT = r"\blaser\b|\boptical\b|\brf engineer|\bmechanical\b|\bstructures?\b|propulsion|avionics|\bmanufacturing\b|\bhardware\b|\belectrical\b|\bpcb\b|\bvehicle\b|\brocket\b|\bchemical\b|\bcivil\b|\bthermal\b|\bfluids?\b|\bmachining\b|\bwelding\b|\binspector\b|\btechnician\b|\bquality engineer|\bprocess engineer|\bcontrols? engineer|\bfield engineer|\bphotonics\b|\bpower systems?\b"
SOFT_OK = r"software|developer|\bsde\b|\bswe\b|programmer|\bdata\b|machine learning|\bai\b|\bml\b|devops|\bsre\b|cloud|\bapi\b|full.?stack|front.?end|back.?end|\bqa\b|automation|\bweb\b"
DROP_PM_OK = r"\bmanager\b"  # "product manager" is the one manager title that may pass
NONUS = r"\btwn\b|\bchn\b|\bind\b|\bgbr\b|\bcan\b|\bdeu\b|\bfra\b|\bsgp\b|\bjpn\b|\bkor\b|\bmex\b|\bbra\b|\baus\b|\bisr\b|\birl\b|\bnld\b|\bpol\b|\besp\b|\bita\b|\bche\b|\bswe\b|\bphl\b|\bvnm\b|\bidn\b|\bmys\b|\bthai?\b|\bcol\b|\barg\b|\bchl\b|\bper\b|\bzaf\b|\bare\b|\bsau\b|\bhkg\b|\bnzl\b|\bcze\b|\bhun\b|\brou\b|\bprt\b|\bfin\b|\bnor\b|\bdnk\b|\bbel\b|\baut\b|\bgrc\b|\btur\b|\begy\b|\bnga\b|\bken\b|canada|india|\buk\b|united kingdom|london|germany|berlin|amsterdam|netherlands|singapore|japan|tokyo|china|shanghai|beijing|ireland|dublin|france|paris|poland|spain|israel|australia|mexico|brazil|bengaluru|bangalore|hyderabad|toronto|montreal|ottawa|vancouver|korea|taiwan|romania|serbia|denmark|sweden|colombia|argentina|philippines|vietnam|indonesia|pakistan|nigeria|egypt|dubai|\buae\b|switzerland|zurich|italy|portugal|lisbon|czech|prague|hungary|finland|norway|belgium|austria|new zealand|south africa|kenya|chile|peru|turkey|munich|hamburg|barcelona|madrid|milan|stockholm|copenhagen|oslo|helsinki|warsaw|bucharest|tallinn|riga|vilnius|athens|ankara|istanbul|hong kong|macau|sydney|melbourne|brisbane|auckland|manila|jakarta|kuala lumpur|bangkok|ho chi minh|hanoi|seoul|taipei|osaka|kyoto|mumbai|pune|chennai|delhi|noida|gurgaon|gurugram|kolkata|ahmedabad|karachi|lahore|dhaka|colombo|kathmandu|cairo|lagos|nairobi|johannesburg|cape town|casablanca|tunis|tel aviv|haifa|beirut|amman|riyadh|jeddah|doha|kuwait|bahrain|muscat|mexico city|guadalajara|monterrey|bogota|medellin|lima|santiago|buenos aires|sao paulo|rio de janeiro|brasilia|montevideo|quito|caracas|san jose, costa rica|costa rica|panama|guatemala|havana|kingston|san juan, pr"


def decide(title, location=""):
    t = (title or "").lower()
    loc = (location or "").lower()
    role = next((r for r, pat in ROLES if re.search(pat, t)), "")
    if not role:
        return {"keep": False, "reason": "title is not a target role", "role": ""}
    if re.search(NONSOFT, t) and not re.search(SOFT_OK, t):
        return {"keep": False, "reason": "not a software role (" + re.search(NONSOFT, t).group(0).strip() + ")", "role": role}
    drop = re.search(DROP, t)
    if drop and drop.group(0) == "staff" and "member of technical staff" in t:
        drop = re.search(DROP, t.replace("member of technical staff", "mts"))
    if drop and not (role == "pm" and drop.group(0) == "manager" and not re.search(r"senior|sr\.?|staff|principal|lead|director|group|head", t)):
        return {"keep": False, "reason": f"title says {drop.group(0).strip()}", "role": role}
    if role == "pm" and not re.search(LEVEL_HINT, t):
        return {"keep": False, "reason": "product manager without an entry-level marker", "role": role}
    if loc and re.search(NONUS, loc) and not re.search(r"united states|\busa?\b|remote \(us\)|us remote|remote - us", loc):
        return {"keep": False, "reason": f"location outside the US ({location[:40]})", "role": role}
    if re.search(NONUS.replace(r"|\bswe\b", ""), t) and not re.search(r"\bus\b|usa|united states", t):
        return {"keep": False, "reason": "title names a non-US location", "role": role}
    return {"keep": True, "reason": "target role" + (" with entry-level marker" if re.search(LEVEL_HINT, t) else ""), "role": role}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    d = decide(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    print(json.dumps(d))
    sys.exit(0 if d["keep"] else 1)
