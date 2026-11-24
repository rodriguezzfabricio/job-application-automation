"""Read reported usage only; no price inference or provider transcript-based run audit."""
import hashlib
import json

def read_codex_usage(lines):
    found=None
    for line in lines:
        try:event=json.loads(line)
        except (ValueError,TypeError):continue
        if event.get('type')=='turn.completed' and isinstance(event.get('usage'),dict):
            found={'source':'codex exec turn.completed','usage':event['usage']}
        payload=event.get('payload',{})
        if event.get('type')=='event_msg' and payload.get('type')=='token_count':
            info=payload.get('info') or {}
            if info.get('total_token_usage'):found={'source':'Codex session total_token_usage','usage':info['total_token_usage']}
    return found or {'source':'meter unavailable','usage':None}


def _host_rpc(command, timeout):
    """Use only the read-only app-server methods. No model request or login is started."""
    import os
    import selectors
    import subprocess
    import time

    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as server, selectors.DefaultSelector() as ready:
        ready.register(server.stdout, selectors.EVENT_READ)
        buffer = bytearray()

        def send(method, params=None, request_id=None):
            payload = {'method': method}
            if params is not None:
                payload['params'] = params
            if request_id is not None:
                payload['id'] = request_id
            server.stdin.write(json.dumps(payload).encode() + b'\n')
            server.stdin.flush()
            if request_id is None:
                return None
            while time.monotonic() < deadline:
                if b'\n' not in buffer:
                    if not ready.select(max(0, deadline - time.monotonic())):
                        break
                    chunk = os.read(server.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    continue
                line, _, remaining = buffer.partition(b'\n')
                buffer[:] = remaining
                response = json.loads(line)
                if response.get('id') == request_id:
                    return response['result']
            raise TimeoutError('Codex meter RPC unavailable')

        try:
            send('initialize', {'clientInfo': {'name': 'job-quota', 'version': '1.0'}}, 1)
            send('initialized')
            before = send('account/read', {'refreshToken': False}, 2)
            limits = send('account/rateLimits/read', request_id=3)
            after = send('account/read', {'refreshToken': False}, 4)
            return before, limits, after
        finally:
            server.terminate()
            try:
                server.wait(timeout=1)
            except subprocess.TimeoutExpired:
                server.kill()


def account_fingerprint(account_id):
    return hashlib.sha256(str(account_id).strip().encode()).hexdigest()


def read_live_meters(command=None, timeout=10):
    """Return an authenticated account's current meters and stable private fingerprint."""
    import os
    import time

    from .pace import _meters

    if command is None:
        command = [os.environ.get('JOB_AUTOMATION_CODEX_BIN') or 'codex',
                   '-c', 'forced_login_method="chatgpt"', '-c', 'model_provider="openai"',
                   'app-server', '--listen', 'stdio://']
    try:
        before, limits, after = _host_rpc(command, timeout)
        account = before['account']
        if account != after['account'] or before.get('userId') != after.get('userId'):
            return {}
        if account.get('type', '').lower() != 'chatgpt' or limits.get('ordinaryUsageAllowed') is False:
            return {}
        account_id = limits.get('accountId') or account.get('accountId')
        profile = before.get('userId') or account.get('email')
        if not (account_id or profile):
            return {}
        identity = account_id or json.dumps([profile, account.get('workspaceName')])
        mapped = limits.get('rateLimitsByLimitId') or {}
        codex = mapped.get('codex') or limits.get('rateLimits') or {}
        if codex.get('limitId', 'codex') != 'codex':
            return {}
        sampled_at = time.time()
        reading = _meters(None, codex)
        if not reading.get('week'):
            return {}
        reading['sampled_at'] = sampled_at
        reading['account'] = account_fingerprint(identity)
        reading['account_id_verified'] = bool(account_id)
        return reading
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return {}
