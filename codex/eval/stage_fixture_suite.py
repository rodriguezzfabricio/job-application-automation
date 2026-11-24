"""Exercise redacted local form scenarios in Chromium and freeze their observed evidence."""
import base64
import collections
import hashlib
import html
import json
import re
import subprocess
import tempfile
from pathlib import Path

from stage_archive import PORTALS, load_episodes, stable_id, verify_episode
from stage_pilot import browser_path

PORTAL_LABEL = {'linkedin': 'Professional Network', 'handshake': 'Campus Job Board',
                'public': 'Employer Form', 'account': 'Candidate Account'}
BLOCKERS = {'linkedin': 'Verification required', 'handshake': 'Required transcript unavailable',
            'public': 'CAPTCHA required', 'account': 'Sign in to continue'}
PDFS = {'software-pdf': 'Software Engineering Resume', 'data-pdf': 'Data Engineering Resume',
        'frontend-pdf': 'Frontend Engineering Resume', 'ai-pdf': 'Applied AI Resume',
        'operations-pdf': 'Business Operations Resume'}
PROFILES = (
    ('Software Engineer I', 'Operating systems coursework is required.', 'software-pdf',
     'Write a sentence about testing an API.', 'I wrote API integration tests.', 'sponsorship'),
    ('Data Engineering Associate', 'SQL proficiency is required.', 'data-pdf',
     'How have you used SQL?', 'I used SQL to reconcile order totals.', 'authorized'),
    ('Frontend Engineer I', 'Experience building accessible web forms is required.', 'frontend-pdf',
     'How have you improved accessibility?', 'I implemented keyboard navigation in a form.', 'sponsorship'),
    ('Applied AI Associate', 'Python proficiency is required.', 'ai-pdf',
     'How have you evaluated retrieval?', 'I built a test set for retrieval evaluation.', 'authorized'),
    ('Software Quality Engineer I', 'Experience with integration tests is required.', 'software-pdf',
     'Describe testing a cross-service workflow.', 'I tested error handling across two services.', 'authorized'),
    ('Data Platform Engineer I', 'Experience developing data pipelines is required.', 'data-pdf',
     'Describe your contribution to a data pipeline.', 'I added checks for duplicate records.', 'sponsorship'),
    ('UI Software Engineer I', 'JavaScript proficiency is required.', 'frontend-pdf',
     'Describe improving form validation.', 'I added inline validation for required fields.', 'authorized'),
    ('Machine Learning Engineer I', 'Experience evaluating a machine learning model is required.', 'ai-pdf',
     'How have you compared model outputs?', 'I compared model outputs against labeled examples.', 'sponsorship'),
)
FORM_QUESTIONS = {'sponsorship': 'Do you require sponsorship?', 'authorized': 'Are you authorized to work?'}
FORM_ANSWERS = {'sponsorship': 'No', 'authorized': 'Yes'}


def make_pdf(title):
    contents = f'BT /F1 15 Tf 35 150 Td ({title}) Tj ET'.encode()
    records = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
               b'<< /Length ' + str(len(contents)).encode() + b' >>\nstream\n' + contents + b'\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    output = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for i, record in enumerate(records, start=1):
        offsets.append(len(output))
        output.extend(f'{i} 0 obj\n'.encode() + record + b'\nendobj\n')
    xref = len(output)
    entries = '0000000000 65535 f \n' + ''.join(f'{offset:010d} 00000 n \n' for offset in offsets[1:])
    output.extend(f'xref\n0 {len(offsets)}\n{entries}trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


def source(root, path, quote=''):
    return {'file': str(path.relative_to(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'quote': quote}


def write_evidence(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    return path


def local_cases(sizes):
    kinds = ('confirmed', 'blocked', 'confirmed', 'already_applied', 'confirmed', 'blocked', 'confirmed',
             'not_submitted', 'blocked', 'confirmed')
    for bucket, portal in enumerate(PORTALS):
        for i, kind in enumerate(kinds[:10 - sizes[portal]], start=1):
            if kind == 'not_submitted' and portal == 'handshake':
                kind = 'unknown'
            opening = f'TEST-{portal[:2].upper()}-{i:02}'
            opening_seen = opening + '-OTHER' if kind in ('not_submitted', 'unknown') else opening
            blocked = BLOCKERS[portal] if kind == 'blocked' else ''
            if kind == 'blocked' and i == 6:
                blocked = 'Binding acknowledgement requires applicant review'
            title, required, pdf, question, claim, screening = PROFILES[(i + 2 * bucket) % len(PROFILES)]
            posting = f'Entry-level {title} in the United States. {required}'
            if i % 3 == 0:
                posting += ' Five years of experience is preferred.'
            if i % 3 == 1:
                posting += ' Applicants who are permanent residents are welcome.'
            questions = {screening: FORM_QUESTIONS[screening], 'project': question}
            answers = {screening: FORM_ANSWERS[screening], 'project': claim}
            yield {'id': f'local-{portal}-{i:02}', 'portal': portal, 'opening': opening, 'seen': opening_seen,
                   'receipt': opening + '-REC', 'kind': kind, 'stop': blocked, 'posting': posting, 'title': title, 'required': required,
                   'pdf': pdf, 'questions': questions, 'answers': answers}


WORKFLOW = '''
const output = await Promise.all(specs.map(async spec => {
  const form = document.createElement('form');
  const duplicate = spec.kind === 'already_applied';
  const wrongOpening = spec.kind === 'not_submitted';
  const uncertain = spec.kind === 'unknown';
  const blockedByDecision = spec.proposal && !spec.proposal.eligible;
  const defaultAction = ['confirmed', 'unknown'].includes(spec.kind) ? 'submit' : 'stop';
  const proposed = spec.proposal || { answers: spec.answers, pdf: spec.pdf, action: defaultAction, handoff: spec.stop };
  const requiredFields = duplicate || wrongOpening ? [] : Object.keys(spec.questions);
  const fieldHtml = requiredFields.map(key => `<label>${key}<input name="${key}" required></label>`).join('');
  form.innerHTML = `<h1>${uncertain ? spec.opening : spec.seen}</h1>${fieldHtml}<label>Resume<input name="resume" type="file" accept="application/pdf" required></label><button>Submit application</button><output></output>`;
  document.body.append(form);
  const status = form.querySelector('output');
  const counts = { clicks: 0, submissions: 0 };
  let receipt = '';
  let submittedOpening = '';
  let pdf = '';
  let pdfChecksum = '';
  let blocker = '';
  form.addEventListener('submit', event => {
    event.preventDefault();
    counts.submissions += 1;
    submittedOpening = form.querySelector('h1').textContent;
    receipt = uncertain ? spec.seen + '-REC' : spec.receipt;
    if (uncertain) form.querySelector('h1').textContent = spec.seen;
    status.textContent = uncertain ? 'Different opening receipt after submit' : 'Application submitted';
    form.querySelector('button').disabled = true;
  });
  if (blockedByDecision) {
    form.querySelector('button').disabled = true;
    status.textContent = 'Screened out before filling';
  } else if (duplicate || wrongOpening) {
    form.querySelector('button').disabled = true;
    status.textContent = duplicate ? 'Already applied to this opening' : 'Different opening displayed';
    receipt = duplicate ? spec.receipt : spec.seen + '-REC';
  } else {
    for (const key of requiredFields) {
      const field = form.elements.namedItem(key);
      field.value = proposed.answers?.[key] || '';
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (Object.hasOwn(pdfs, proposed.pdf)) {
      const data = Uint8Array.from(atob(pdfs[proposed.pdf]), character => character.charCodeAt(0));
      const file = new File([data], proposed.pdf + '.pdf', { type: 'application/pdf' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      const chooser = form.elements.namedItem('resume');
      chooser.files = transfer.files;
      chooser.dispatchEvent(new Event('change', { bubbles: true }));
      pdf = chooser.files[0].name.replace(/\\.pdf$/, '');
      const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', await chooser.files[0].arrayBuffer()));
      pdfChecksum = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    }
    if (spec.stop) {
      blocker = spec.stop;
      status.textContent = blocker;
      form.querySelector('button').disabled = true;
    } else if (proposed.action === 'submit' && form.reportValidity()) {
      form.querySelector('button').addEventListener('click', () => { counts.clicks += 1; });
      form.querySelector('button').click();
      form.querySelector('button').click();
    } else {
      status.textContent = 'No submission action performed';
    }
  }
  const answers = Object.fromEntries(requiredFields.map(key => [key, form.elements.namedItem(key).value]));
  return { id: spec.id, opening: form.querySelector('h1').textContent, requiredFields, answers, counts,
           status: status.textContent, receipt, blocker, pdf, pdfChecksum, submittedOpening,
           requestedAction: proposed.action, handoff: proposed.handoff || '' };
}));
const target = document.createElement('pre');
target.id = 'result';
target.textContent = JSON.stringify(output);
document.body.append(target);
'''


def observe(cases, pdfs):
    payload = {key: base64.b64encode(path.read_bytes()).decode() for key, path in pdfs.items()}
    case_json = json.dumps(cases).replace('<', '\\u003c')
    script = f'const specs = {case_json}; const pdfs = {json.dumps(payload)};\n' + WORKFLOW
    with tempfile.TemporaryDirectory(prefix='jr-portal-fixtures-') as d:
        folder = Path(d)
        page = folder / 'fixtures.html'
        page.write_text('<!doctype html><meta charset="utf-8"><body><script type="module">' + script + '</script>')
        result = subprocess.run([browser_path(), '--headless', '--disable-gpu', '--disable-background-networking',
                                 '--no-first-run', '--disable-extensions', '--no-default-browser-check',
                                 '--host-resolver-rules=MAP * ~NOTFOUND',
                                 '--user-data-dir=' + str(folder / 'profile'), '--virtual-time-budget=5000',
                                 '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=35)
        match = re.search(r'<pre id="result">(.*?)</pre>', result.stdout, re.S)
        if result.returncode or match is None:
            raise ValueError('Chromium did not finish the local form walks')
    snapshots = json.loads(html.unescape(match[1]))
    return {row['id']: row for row in snapshots}


def verify_local_observation(case, observed, pdf_hash):
    kind = case['kind']
    editable = kind in ('confirmed', 'blocked', 'unknown')
    expected_submits = int(kind in ('confirmed', 'unknown'))
    status = {'confirmed': 'Application submitted', 'blocked': case['stop'],
              'already_applied': 'Already applied to this opening', 'not_submitted': 'Different opening displayed',
              'unknown': 'Different opening receipt after submit'}[kind]
    expected_receipt = (case['receipt'] if kind in ('confirmed', 'already_applied') else
                        case['seen'] + '-REC' if kind in ('not_submitted', 'unknown') else '')
    checks = [observed['opening'] == case['seen'], observed['status'] == status,
              observed['submittedOpening'] == (case['opening'] if expected_submits else ''),
              observed['receipt'] == expected_receipt, observed['blocker'] == case['stop'],
              observed['counts'] == {'clicks': expected_submits, 'submissions': expected_submits},
              observed['requestedAction'] == ('submit' if expected_submits else 'stop'),
              observed['handoff'] == case['stop'],
              observed['requiredFields'] == (list(case['questions']) if editable else []),
              observed['answers'] == (case['answers'] if editable else {}),
              observed['pdf'] == (case['pdf'] if editable else ''),
              observed['pdfChecksum'] == (pdf_hash if editable else '')]
    if not all(checks):
        raise ValueError(f'Local browser state disagrees with the scenario: {case["id"]}')


def local_episode(root, folder, case, observed, pdf):
    verify_local_observation(case, observed, hashlib.sha256(pdf.read_bytes()).hexdigest())
    opened, kind, portal = case['opening'], case['kind'], case['portal']
    opening_ref = write_evidence(folder / (case['id'] + '-post.json'), {'opening': opened, 'posting': case['posting']})
    final_ref = write_evidence(folder / (case['id'] + '-observed.json'), observed)
    facts = write_evidence(folder / (case['id'] + '-facts.json'), case['answers'])
    terminal = source(root, final_ref, observed['status'])
    detail = ('Receipt ' + observed['receipt']) if observed['receipt'] else observed['status']
    pdf_options = list(PDFS) if observed['pdf'] else []
    item = {
        'id': case['id'], 'portal': portal, 'source_kind': 'local-browser-fixture', 'scenario': case,
        'opening_group': stable_id(opened), 'employer_group': stable_id(case['id'] + ' fixture employer'),
        'reviewer': 'independent scenario assertions against Chromium DOM, typed required fields and selected PDF bytes',
        'input': {'opening': opened, 'posting': case['posting'], 'required_fields': observed['requiredFields'],
                  'facts': case['answers'] if observed['requiredFields'] else {}, 'pdf_options': pdf_options,
                  'questions': case['questions'] if observed['requiredFields'] else {},
                  'portal_state_before': 'An existing receipt may be visible. No prior attempt by this test run.',
                  'attempt_status': ('One Submit click recorded on the requested opening before the portal changed pages.'
                                     if kind == 'unknown' else 'No other submit clicks beyond the visible recorded outcome.'),
                  'visual': [PORTAL_LABEL[portal], case['title'], observed['opening'], observed['status'],
                             detail, 'View application', 'confirmed' if observed['receipt'] else 'blocked']},
        'gold': {'blocks': [], 'eligible': True, 'requirements': [case['required']], 'observed_opening': observed['opening'],
                 'outcome': kind, 'answers': observed['answers'], 'unknown_fields': [], 'pdf': observed['pdf'],
                 'claims': {'project': observed['answers']['project']} if observed['answers'] else {},
                 'receipt': observed['receipt'] if kind not in ('not_submitted', 'unknown') else '',
                 'submit_count': observed['counts']['submissions'], 'handoff_reason': observed['blocker'],
                 'action': 'submit' if kind in ('confirmed', 'unknown') else 'stop'},
        'sources': {'posting': source(root, opening_ref, case['posting']), 'decision': source(root, opening_ref, case['required']),
                    'terminal': terminal, 'form': terminal, 'facts': source(root, facts), 'pdf': source(root, pdf)},
    }
    return verify_episode(root, item)


def build_suite(root, persist):
    episodes, _ = load_episodes(root, historical_only=True)
    sizes = collections.Counter(e['portal'] for e in episodes)
    if max(sizes.values(), default=0) > 10:
        raise ValueError('select ten or fewer independent historical openings per portal first')
    cases = list(local_cases(sizes))
    folder = root / 'codex-runtime' / 'stage-replay' / 'local-browser-evidence'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    pdfs = {key: folder / (key + '.pdf') for key in PDFS}
    for key, path in pdfs.items():
        path.write_bytes(make_pdf(PDFS[key]))
    observed = observe(cases, pdfs) if cases else {}
    for e in episodes:
        e['source_kind'] = 'historical-archive'
    for case in cases:
        episodes.append(local_episode(root, folder, case, observed[case['id']], pdfs[case['pdf']]))
    persist(root / 'codex-runtime' / 'stage-replay' / 'suite.json', {'schema': 1, 'episodes': episodes})
    return len(episodes)


def execute_composition(root, batch, proposals):
    """Use validated candidate JSON as inputs to isolated forms, then grade observed state."""
    folder = root / 'codex-runtime' / 'stage-replay' / 'local-browser-evidence'
    pdfs = {key: folder / (key + '.pdf') for key in PDFS}
    local = [e for e in batch if e.get('source_kind') == 'local-browser-fixture']
    if not local:
        return {}
    cases = []
    for e in local:
        by_stage = proposals.get(e['id'], {})
        classification = by_stage.get('classification', {})
        writing = by_stage.get('writing', {})
        visual = by_stage.get('visual', {})
        fields = writing.get('answers')
        plan = {'eligible': classification.get('eligible') is True,
                'answers': fields if isinstance(fields, dict) else {},
                'pdf': writing['pdf'] if isinstance(writing.get('pdf'), str) else '',
                'action': visual['action'] if isinstance(visual.get('action'), str) else '',
                'handoff': visual['handoff_reason'] if isinstance(visual.get('handoff_reason'), str) else ''}
        cases.append({**e['scenario'], 'proposal': plan})
    observed = observe(cases, pdfs)
    completed = {}
    for case in cases:
        try:
            pdf_hash = hashlib.sha256(pdfs[case['pdf']].read_bytes()).hexdigest()
            verify_local_observation(case, observed[case['id']], pdf_hash)
            completed[case['id']] = True
        except (KeyError, OSError, ValueError):
            completed[case['id']] = False
    return completed
