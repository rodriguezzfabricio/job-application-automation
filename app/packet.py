"""Generate a sample-only, evidence-bound PDF and MIME packet. No sending API."""
from email.message import EmailMessage
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path
import re
from xml.sax.saxutils import escape
import reportlab
from reportlab.lib import colors
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from pypdf import PdfReader
from policy import POLICY_VERSION, instant, validate_selection, merge_key, skill_labels,validate_skill_evidence
from policy import selection_covers_requirements


def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
def digest(data):return hashlib.sha256(data).hexdigest()
def job_fingerprint(job):return digest(canonical({k:v for k,v in job.items() if k not in {'source_observed_at','first_seen_at','last_seen_at','known_ids','bootstrap'}}))
def compact(text):return ' '.join(text.split())


def _selected(profile,ids):
    validate_skill_evidence(profile=profile)
    bank=profile.get('variants',[])
    if not skill_labels(profile.get('skills',[])):raise ValueError('invalid profile skill evidence schema')
    if not profile.get('approved') or not validate_selection(bank,ids):raise ValueError('unapproved profile or selection')
    by_id={v['id']:v for v in bank};chosen=[by_id[id] for id in ids]
    for v in chosen:
        if v.get('group') not in profile.get('groups',{}):raise ValueError('unsupported group')
        facts=v.get('facts',[])
        if not facts or any(f not in profile.get('facts',{}) or profile['facts'][f]['group']!=v['group'] for f in facts):raise ValueError('unsupported/cross-group fact')
        if len(v['text'])>200:raise ValueError('approved bullet exceeds character budget')
    return chosen


def _font_setup():
    root=Path(reportlab.__file__).parent/'fonts'
    if 'SampleVera' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('SampleVera',str(root/'Vera.ttf')))
        pdfmetrics.registerFont(TTFont('SampleVeraBold',str(root/'VeraBd.ttf')))
    return pdfmetrics.getFont('SampleVera').face.charWidths


def _pdf(profile,job,chosen):
    glyphs=_font_setup()
    strings=[profile['name'],profile['email'],profile['education'],'SAMPLE - SYNTHETIC CANDIDATE', 'SKILLS', 'PROJECTS']+[v['text'] for v in chosen]+[profile['groups'][v['group']] for v in chosen]
    selected_skills=sorted({s for v in chosen for s in v.get('skills',[]) if s in profile.get('skills',[])})
    strings+=selected_skills
    if any(len(s)>8192 for s in strings):raise ValueError('oversized mandatory content')
    if any(ord(c) not in glyphs for s in strings for c in s if c not in '\n\r\t'):raise ValueError('unsupported glyph; quarantine')
    body=ParagraphStyle('body',fontName='SampleVera',fontSize=10.5,leading=14,textColor=colors.HexColor('#20252a'),spaceAfter=7)
    heading=ParagraphStyle('heading',parent=body,fontName='SampleVeraBold',fontSize=11,spaceBefore=14,spaceAfter=8)
    name=ParagraphStyle('name',parent=heading,fontSize=19,leading=24,spaceBefore=0)
    label=ParagraphStyle('label',parent=body,fontSize=10,textColor=colors.HexColor('#6a3941'))
    story=[Paragraph(escape(profile['name']),name),Paragraph(escape(profile['email']),body),Paragraph('SAMPLE - SYNTHETIC CANDIDATE',label),Spacer(1,10),Paragraph('EDUCATION',heading),Paragraph(escape(profile['education']),body),Paragraph('SKILLS',heading),Paragraph(escape(', '.join(selected_skills) or 'See approved project evidence.'),body),Paragraph('PROJECTS',heading)]
    previous=None
    for v in chosen:
        if previous!=v['group']:story.append(Paragraph(escape(profile['groups'][v['group']]),heading));previous=v['group']
        story.append(Paragraph('- '+escape(v['text']),body))
    width=letter[0]-84;height=letter[1]-84
    # Fixed one-page geometry; quarantine instead of shrinking/clipping.
    total=0
    for flow in story:
        _,h=flow.wrap(width,height);total+=h+flow.getSpaceBefore()+flow.getSpaceAfter()
    if total>height:raise ValueError('one-page mandatory content overflow')
    out=io.BytesIO();binding=json.dumps({'job_id':job['id'],'profile_version':profile['version']},sort_keys=True)
    doc=SimpleDocTemplate(out,pagesize=letter,leftMargin=42,rightMargin=42,topMargin=42,bottomMargin=42,title='Sample tailored resume',author='Synthetic sample',subject=binding)
    def invariant_canvas(*args,**kwargs):
        kwargs['invariant']=1;return Canvas(*args,**kwargs)
    doc.build(story,canvasmaker=invariant_canvas);pdf=out.getvalue();reader=PdfReader(io.BytesIO(pdf))
    if len(reader.pages)!=1 or len(pdf)>1024*1024:raise ValueError('PDF page/size limit')
    extracted=compact(reader.pages[0].extract_text() or '')
    for expected in [profile['name'],profile['email'],profile['education']]+[v['text'] for v in chosen]:
        if compact(expected) not in extracted:raise ValueError('PDF text fidelity failed')
    return pdf


def build_packet(profile,job,selected_ids,recipient,now,*,authorized_recipient='demo@example.invalid'):
    validate_skill_evidence(profile,job)
    if not selection_covers_requirements(profile,job,selected_ids):raise ValueError('selected evidence omits mandatory requirements')
    if recipient!=authorized_recipient:raise ValueError('recipient differs from external authorization')
    instant(now);merge_key(job)
    if not isinstance(job.get('source_hash'),str) or not re.fullmatch(r'[0-9a-f]{64}',job['source_hash']):raise ValueError('source hash required')
    if profile.get('synthetic') is not True or job.get('synthetic') is not True:raise ValueError('prototype only accepts explicitly synthetic data')
    if not re.fullmatch(r'[A-Za-z0-9._+-]+@example\.invalid',recipient):raise ValueError('sample recipient required')
    chosen=_selected(profile,selected_ids);pdf=_pdf(profile,job,chosen)
    manifest={'synthetic':True,'job_id':job['id'],'job_url':job['url'],'job_hash':job_fingerprint(job),'source_hash':job.get('source_hash'),'profile_version':profile['version'],'profile_hash':digest(canonical(profile)),'policy_version':POLICY_VERSION,'selected_ids':selected_ids,'fact_ids':sorted({f for v in chosen for f in v['facts']}),'pdf_sha256':digest(pdf),'recipient':recipient,'created_at':instant(now).isoformat(),'delivery_mode':'local_capture'}
    msg=EmailMessage(policy=email_policy.SMTP);msg['From']='sample@example.invalid';msg['To']=recipient;msg['Subject']='SAMPLE: '+job['title'];msg['Message-ID']='<'+digest(canonical(manifest))[:32]+'@example.invalid>'
    msg.set_content('SAMPLE ONLY - archived/synthetic demonstration, not a current job alert.\n\n'+job['title']+'\n'+job['url']+'\n\nTailored using approved sample evidence IDs: '+', '.join(selected_ids)+'\nNo application was submitted. Attachment is a synthetic candidate resume.\n')
    filename=re.sub(r'[^A-Za-z0-9_-]','_',job['id'])[:100]+'-resume.pdf'
    msg.add_attachment(pdf,maintype='application',subtype='pdf',filename=filename)
    # E9-S-002: deterministic multipart boundary so identical packets are byte-identical.
    msg.set_boundary('sample-'+digest(canonical(manifest))[:32])
    packet={'manifest':manifest,'pdf':pdf,'email':msg.as_bytes()}
    if not check_packet(packet,profile,job,authorized_recipient=authorized_recipient):raise ValueError('packet binding failed')
    return packet



def _expected_text(profile,chosen):
    skills=sorted({s for v in chosen for s in v.get('skills',[]) if s in profile.get('skills',[])})
    parts=[profile['name'],profile['email'],'SAMPLE - SYNTHETIC CANDIDATE','EDUCATION',profile['education'],'SKILLS',', '.join(skills) or 'See approved project evidence.','PROJECTS']
    previous=None
    for v in chosen:
        if previous!=v['group']:parts.append(profile['groups'][v['group']]);previous=v['group']
        parts.append('- '+v['text'])
    return compact(' '.join(parts))


def check_packet(packet,profile,job,*,authorized_recipient='demo@example.invalid'):
    try:
        validate_skill_evidence(profile,job)
        merge_key(job)
        if not isinstance(job.get('source_hash'),str) or not re.fullmatch(r'[0-9a-f]{64}',job['source_hash']):return False
        if not selection_covers_requirements(profile,job,packet['manifest']['selected_ids']):return False
        m=packet['manifest'];pdf=packet['pdf'];chosen=_selected(profile,m['selected_ids'])
        if m.get('recipient')!=authorized_recipient:return False
        if m['synthetic'] is not True or profile.get('synthetic') is not True or job.get('synthetic') is not True:return False
        if m['job_id']!=job['id'] or m['job_url']!=job['url'] or m['job_hash']!=job_fingerprint(job) or m['profile_hash']!=digest(canonical(profile)) or m['profile_version']!=profile['version'] or m['policy_version']!=POLICY_VERSION or m['pdf_sha256']!=digest(pdf):return False
        if m['fact_ids']!=sorted({f for v in chosen for f in v['facts']}):return False
        if not re.fullmatch(r'[A-Za-z0-9._+-]+@example\.invalid',m['recipient']) or m.get('source_hash')!=job.get('source_hash'):return False
        if pdf!=_pdf(profile,job,chosen):return False
        reader=PdfReader(io.BytesIO(pdf))
        if len(reader.pages)!=1:return False
        if compact(reader.pages[0].extract_text() or '')!=_expected_text(profile,chosen):return False
        binding=json.loads(reader.metadata.subject)
        if binding!={'job_id':job['id'],'profile_version':profile['version']}:return False
        msg=BytesParser(policy=email_policy.default).parsebytes(packet['email']);attachments=list(msg.iter_attachments())
        allowed={'from','to','subject','message-id','mime-version','content-type'}
        if any(k.lower() not in allowed for k in msg.keys()) or any(len(msg.get_all(k,[]))!=1 for k in allowed):return False
        if msg['Message-ID']!='<'+digest(canonical(m))[:32]+'@example.invalid>':return False
        if msg['From']!='sample@example.invalid' or msg['Subject']!='SAMPLE: '+job['title']:return False
        expected='SAMPLE ONLY - archived/synthetic demonstration, not a current job alert.\n\n'+job['title']+'\n'+job['url']+'\n\nTailored using approved sample evidence IDs: '+', '.join(m['selected_ids'])+'\nNo application was submitted. Attachment is a synthetic candidate resume.\n'
        body=msg.get_body(preferencelist=('plain',))
        if body is None or body.get_content_type()!='text/plain' or body.get_content().replace('\r\n','\n')!=expected:return False
        if len(list(msg.iter_parts()))!=2:return False
        return msg['To']==m['recipient'] and len(attachments)==1 and attachments[0].get_content_type()=='application/pdf' and attachments[0].get_payload(decode=True)==pdf
    except (KeyError,TypeError,ValueError,AttributeError):return False
