"""Deterministic rubric arithmetic after evidence-backed language extraction.

Weights/bands transcribed from the connected shared rubric. New unlevelled data
roles use the existing generic-role band (25); absent requirements get the lower
requirements band (5) and an explicit uncertainty marker, never a fabricated match.
"""
import re

WEIGHTS={'role':40,'requirements':25,'freshness':20,'apply_path':10,'company':5}

def score(title,required_skill_matches,age_hours,apply_path,company_known=False,technologies_named=0):
    t=title.lower()
    if re.search(r'new[- ]grad|early career|university|2026|2027|college grad',t):role=40
    elif re.search(r'\bjunior\b|\bassociate\b|engineer (?:i|1)\b|^(?:software engineer|software developer)$',t):role=30
    elif re.search(r'\bai\b|machine learning|\bml\b|forward deployed|founding engineer',t):role=30+(5 if technologies_named>=3 else 0)
    else:role=25
    known=list(required_skill_matches);share=sum(bool(x) for x in known)/len(known) if known else 0
    requirements=25 if share>=0.8 else 15 if share>=0.5 else 5
    if any(not x for x in known):requirements=min(requirements,10)
    freshness=20 if age_hours is not None and 0<=age_hours<=2 else 15 if age_hours is not None and 0<=age_hours<=24 else 8 if age_hours is not None and 0<=age_hours<=72 else 0
    path={'employer':10,'aggregator':5,'one_click':3}[apply_path]
    parts={'role':role,'requirements':requirements,'freshness':freshness,'apply_path':path,'company':5 if company_known else 0}
    return {'score':sum(parts.values()),'parts':parts,'weights':WEIGHTS,'requirements_assessed':bool(known),'missing_required_skills':sum(not x for x in known)}
