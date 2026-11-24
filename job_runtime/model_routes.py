"""Subscription model routing for bounded, validated job-application tasks."""
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import usage


@dataclass(frozen=True)
class Route:
    model: str
    effort: str
    reason: str


BASELINE = Route('gpt-6.1-sol', 'medium', 'incumbent')
CANDIDATES = {
    'classification': Route('gpt-6-luna', 'low', 'candidate'),
    'writing': Route('gpt-6.2-sol', 'low', 'candidate'),
    'visual': Route('gpt-6.1-terra', 'low', 'candidate'),
}
FALLBACK = Route('gpt-6.2-astra', 'low', 'validation miss')


def evidence_path(private_root):
    return Path(private_root) / 'codex-runtime' / 'stage-replay' / 'promotion.json'


def qualified(proof):
    if not isinstance(proof, dict) or proof.get('pilot_passed') is not True:
        return False
    batches = proof.get('batches')
    if not isinstance(batches, list) or len(batches) != 2 or any(not isinstance(x, dict) for x in batches):
        return False
    ids = [batch.get('batch_id') for batch in batches]
    if any(not isinstance(x, str) or not x for x in ids) or ids[0] == ids[1]:
        return False
    for batch in batches:
        base, candidate = batch.get('baseline_cost_per_correct'), batch.get('candidate_cost_per_correct')
        if any(type(x) not in (float, int) or not math.isfinite(x) or x <= 0 for x in (base, candidate)):
            return False
        parity, local = batch.get('execution_parity'), batch.get('local_execution_cases')
        if not (type(local) is int and 0 < local <= 20 and isinstance(parity, dict) and
                type(parity.get('candidate')) is int and parity['candidate'] == local):
            return False
        if not (batch.get('matched_cases') == 20 and batch.get('comparable') is True and batch.get('quality_passed') is True and candidate < base):
            return False
    return True


def select_route(private_root, stage, *, miss=False):
    if stage not in CANDIDATES:
        raise ValueError(f'Unknown model stage: {stage}')
    try:
        path = evidence_path(private_root)
        report = json.loads(path.read_text())
        for key, file in (('results_sha256', 'results.json'), ('suite_sha256', 'suite.json')):
            if report.get(key) != hashlib.sha256(path.with_name(file).read_bytes()).hexdigest():
                return BASELINE
        proof = report.get('stages', {}).get(stage)
    except (OSError, ValueError, AttributeError):
        return BASELINE
    if report.get('schema') != 1 or not qualified(proof):
        return BASELINE
    candidate = CANDIDATES[stage]
    if proof.get('model') != candidate.model or proof.get('effort') != candidate.effort:
        return BASELINE
    if miss:
        return FALLBACK if proof.get('fallback_on_miss') is True else BASELINE
    return Route(candidate.model, candidate.effort, 'qualified in both held-out batches')


class ModelRequestError(RuntimeError):
    pass


def subscription_env():
    env = dict(os.environ)
    for key in tuple(env):
        if key != 'CODEX_HOME' and key.startswith(('OPENAI_', 'AZURE_OPENAI_', 'CODEX_')):
            env.pop(key)
    return env


def require_subscription(executable='codex'):
    codex = shutil.which(executable)
    if codex is None:
        raise ModelRequestError('Codex CLI unavailable')
    auth = subprocess.run([codex, '-c', 'forced_login_method="chatgpt"', 'login', 'status'], capture_output=True,
                          text=True, env=subscription_env(), timeout=15)
    if auth.returncode != 0 or 'Logged in using ChatGPT' not in (auth.stdout + auth.stderr):
        raise ModelRequestError('The Codex CLI needs a ChatGPT subscription login')
    return codex


def input_digest(prompt, images=()):
    payload = [prompt, [hashlib.sha256(Path(image).read_bytes()).hexdigest() for image in images]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def private_session_metadata(session_id):
    from .pace import session_file
    filename = session_file(session_id)
    if not filename:
        return {}
    confirmed = {}
    try:
        lines = open(filename, encoding='utf-8', errors='replace')
    except OSError:
        return confirmed
    with lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get('type') == 'turn_context':
                payload = record.get('payload') or {}
                confirmed['served_model'] = payload.get('model')
                confirmed['served_effort'] = payload.get('effort') or (payload.get('config') or {}).get('effort')
            elif record.get('type') == 'session_meta':
                confirmed['account_id'] = (record.get('payload') or {}).get('creator_account_id')
    return confirmed


def request_subscription(codex, route, prompt, images=(), *, timeout=180):
    """Send only the supplied prompt/attachments. Never run with project tools or writable workspace access."""
    with tempfile.TemporaryDirectory(prefix='jr-stage-') as folder:
        output = str(Path(folder) / 'reply.json')
        command = [codex, 'exec', '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check',
                   '--sandbox', 'read-only', '--cd', folder, '--json', '--color', 'never',
                   '-c', 'forced_login_method="chatgpt"', '-c', 'model_provider="openai"',
                   '-c', 'model_reasoning_effort=' + json.dumps(route.effort),
                   '-c', 'web_search="disabled"', '--disable', 'shell_tool',
                   '--disable', 'apps', '--disable', 'plugins', '--disable', 'hooks',
                   '--disable', 'unified_exec', '--disable', 'code_mode_host', '--disable', 'image_generation',
                   '--disable', 'multi_agent_v2', '--disable', 'computer_use', '--disable', 'browser_use',
                   '--disable', 'sleep_tool', '--disable', 'view_image', '--disable', 'skill_search',
                   '--enable', 'skip_host_skill_discovery', '-c', 'approval_policy="never"',
                   '--model', route.model, '--output-last-message', output]
        for image in images:
            command.extend(['--image', str(Path(image).resolve())])
        try:
            process = subprocess.run([*command, '-'], input=prompt, capture_output=True, text=True,
                                     env=subscription_env(), timeout=timeout)
            events, returncode = process.stdout, process.returncode
        except subprocess.TimeoutExpired as error:
            events, returncode = error.stdout or b'', 124
            if isinstance(events, bytes):
                events = events.decode(errors='replace')
        observed = []
        for line in events.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                observed.append(event)
        session_id = next((e.get('thread_id') for e in observed if e.get('type') == 'thread.started'), None)
        external_tool = any((e.get('item') or {}).get('type') in
                            ('mcp_tool_call', 'command_execution', 'web_search', 'file_change', 'dynamic_tool_call')
                            for e in observed)
        try:
            response = json.loads(Path(output).read_text()) if returncode == 0 and not external_tool else None
        except (OSError, ValueError):
            response = None
        metadata = private_session_metadata(session_id) if session_id else {}
        return {'response': response, 'usage': usage.read_codex_usage(events.splitlines()).get('usage'), 'session_id': session_id,
                'returncode': returncode, 'tools_used': external_tool, **metadata}


def run_stage(private_root, stage, prompt, *, validate, approved_request, images=()):
    """One bounded call with optional fallback after a validated miss. Caller owns quota approval."""
    first = BASELINE if stage == 'visual' and not images else select_route(private_root, stage)
    runs = [approved_request(first, prompt, images)]
    response = validated_response(runs[-1], first, validate)
    if response is None and first != BASELINE:
        fallback = select_route(private_root, stage, miss=True)
        runs.append(approved_request(fallback, prompt, images))
        response = validated_response(runs[-1], fallback, validate)
    return {'response': response, 'attempts': runs}


def validated_response(attempt, route, validate):
    if (attempt.get('served_model') == route.model and attempt.get('served_effort') == route.effort
            and attempt.get('returncode') == 0 and not attempt.get('tools_used')):
        response = attempt.get('response')
        if response is not None and validate(response):
            return response
    return None
