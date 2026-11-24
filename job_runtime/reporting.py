"""Prepare stable dashboard updates, including terminal jobs and uncertain outcomes."""
from itertools import groupby
import json
import uuid

from .adapters import DATE_HEADERS, EDITABLE, legacy, mirror_patch, sheet_value
from .policy import digest
from .store import Denied

MAX_BATCH_CELLS = 1500
IDENTITY = {'Applied': ('job_key', 'Date'), 'Rejected': ('Company', 'Title', 'Link'),
            'History': ('Date', 'Company', 'Role')}



def exports(store, snapshots, summary):
    tracker = legacy('tracker')
    result = {tab: tracker.export(store.con, tab)['values'] for tab in snapshots}
    jobs = {r['job_key']: dict(r) for r in store.con.execute('SELECT * FROM jobs')}
    applications = {r['job_key']: dict(r) for r in store.con.execute('SELECT * FROM applications ORDER BY applied_at')}
    uncertain = {r['job_key'] for r in store.con.execute("SELECT job_key FROM jr_attempts WHERE state='submit_started'")}
    uncertain.update(h['job_key'] for h in store.legacy_unknown_holds())
    for tab in ('Today', 'Jobs'):
        if tab not in snapshots:
            continue
        header = snapshots[tab][0]
        keycol = header.index('job_key')
        rows = {r[keycol]: r for r in result[tab][1:] if len(r) > keycol and r[keycol]}
        # Existing rows stay in place even when they leave an active-only export.
        for old in snapshots[tab][1:]:
            key = old[keycol] if len(old) > keycol else ''
            if not key or key in rows or key not in jobs:
                continue
            row = list(old) + [''] * (len(header) - len(old))
            j = jobs[key]
            if tab == 'Jobs':
                row[header.index('Status')] = j['status']
            else:
                app = applications.get(key)
                row[0] = 'Applied' if app else 'Archived'
                row[3] = 'Already submitted. Do not apply again.' if app else ('No action: ' + (j['reason'] or j['status']))
            rows[key] = row
        if tab == 'Today':
            from .canary import state as rollout_state
            from .opening_history import recovery_holds
            from .policy import opening_identity
            cohort = rollout_state(store)
            if cohort['phase'] in ('active','halted'):
                holds = recovery_holds(store)
                for key in dict.fromkeys([*cohort.get('deferred_eligibility',{}), *cohort['jobs']]):
                    if key not in jobs or key in applications:
                        continue
                    job = jobs[key]
                    row = store.con.execute("SELECT payload FROM jr_backlog WHERE provider='application_handoff' AND item_key=? AND state='pending'",(key,)).fetchone()
                    reasons = (json.loads(row[0]).get('questions') or []) if row else []
                    recovery = holds.get(opening_identity(job['link']))
                    if recovery:
                        reasons = [recovery['reason'], *reasons]
                    if reasons:
                        rows[key] = ['Draft on hold',job['company'],job['title'],' '.join(reasons),job['link'],job['posted_at'],'',key]
            for key,app in applications.items():
                if key in rows and str(app.get('confirmed_via','')).endswith('date first observed') and app['submitted_by']=='unattributed':
                    rows[key][0] = 'Prior application confirmed'
                    rows[key][3] = 'Platform history confirms this earlier application. The date shown is when the receipt was observed.'
            for key in uncertain:
                if key not in jobs:
                    continue
                j = jobs[key]
                rows[key] = ['Outcome uncertain', j['company'], j['title'],
                             'Do not submit again. The previous attempt needs an exact employer or platform receipt.',
                             j['link'], j['posted_at'], '', key]
            if store.codex_production():
                from .shortlist import report_day, summary as daily_summary
                summary = daily_summary(report_day(store)) + (' ' + summary if summary else '')
            from .learning import short_summary
            learned=short_summary(store)
            if learned['open_issues']:
                issues,questions=learned['open_issues'],learned['owner_questions']
                summary=(summary+' · ' if summary else '')+f'Learning log has {issues} open issue'+('' if issues==1 else 's')+f' and {questions} unanswered personal question'+('' if questions==1 else 's')+'.'
            result[tab] = [header, ['Last run', '', '', summary, '', store.clock().astimezone(tracker.ET).strftime('%Y-%m-%d %H:%M'), '', ''], *rows.values()]
        else:
            result[tab] = [header, *rows.values()]
    return result



def grid_snapshots(response, sheet_ids=None):
    """Decode a grouped Sheets CellData read from A1 on each requested tab."""
    if not isinstance(response, dict) or response.get('isError'):
        raise Denied('grouped dashboard read failed')
    data = response.get('structuredContent') or response
    if 'sheets' not in data:
        if not data or any(not isinstance(rows, list) for rows in data.values()):
            raise Denied('dashboard snapshots must contain tab rows')
        return data
    result = {}
    try:
        for sheet in data['sheets']:
            properties = sheet['properties']
            name = properties['title']
            if sheet_ids and name in sheet_ids and properties.get('sheetId') not in (None, sheet_ids[name]):
                raise ValueError
            grid, = sheet['data']
            if name in result or grid.get('startRow', 0) != 0 or grid.get('startColumn', 0) != 0:
                raise ValueError
            rows = []
            for row in grid['rowData']:
                values = []
                for i, cell in enumerate(row.get('values', [])):
                    value = cell.get('effectiveValue') or cell.get('userEnteredValue') or {}
                    scalar = next((value[k] for k in ('stringValue', 'numberValue', 'boolValue') if k in value), '')
                    if rows and i < len(rows[0]) and rows[0][i] in DATE_HEADERS:
                        scalar = cell.get('formattedValue', scalar)
                    values.append(scalar)
                rows.append(values)
            while rows and not any(value not in ('', None) for value in rows[-1]):
                rows.pop()
            result[name] = rows
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise Denied('grouped dashboard snapshot is partial or malformed') from exc
    return result


def _at(rows, row, col):
    return rows[row][col] if row < len(rows) and col < len(rows[row]) else ''


def _layout(tab, rows):
    header = rows[0]
    editable, key = EDITABLE.get(tab, (None, None))
    identity = [header.index(name) for name in ((key,) if key else IDENTITY[tab])]
    layout = []
    for i in range(1, len(rows)):
        keys = [sheet_value(header[col], _at(rows, i, col)) for col in identity]
        if any(keys):
            layout.append(['key', *keys])
            continue
        # Summary rows and blank gaps can move too. Ignore user edits to existing command cells.
        values = [sheet_value(name, _at(rows, i, col)) for col, name in enumerate(header) if name != editable]
        layout.append(['row', *values] if any(values) else None)
    while layout and layout[-1] is None:
        layout.pop()
    return digest(layout)


def _cell_data(value):
    if isinstance(value, bool):
        entered = {'boolValue': value}
    elif isinstance(value, (int, float)):
        entered = {'numberValue': value}
    else:
        entered = {'stringValue': '' if value is None else str(value)}
    return {'userEnteredValue': entered}


def _requests(rows, sheet_ids):
    blocks = []
    latest = {}
    for tab, row, cells in rows:
        # A run of consecutive columns can never jump over a command or an unchanged cell.
        for _, items in groupby(enumerate(cells), key=lambda pair: pair[1]['col'] - pair[0]):
            span = [cell for _, cell in items]
            column = span[0]['col']
            key = tab, column, len(span)
            values = {'values': [_cell_data(cell['value']) for cell in span]}
            block = latest.get(key)
            if block is not None and block['start']['rowIndex'] + len(block['rows']) == row:
                block['rows'].append(values)
            else:
                block = {'start': {'sheetId': sheet_ids[tab], 'rowIndex': row, 'columnIndex': column},
                         'rows': [values], 'fields': 'userEnteredValue'}
                latest[key] = block
                blocks.append({'updateCells': block})
    return blocks


def _put(rows, row, col, value):
    while len(rows) <= row:
        rows.append([])
    while len(rows[row]) <= col:
        rows[row].append('')
    rows[row][col] = value


def _column_name(width):
    letters = ''
    while width:
        width, index = divmod(width - 1, 26)
        letters = chr(65 + index) + letters
    return letters


def prepare(store, snapshots, sheet_ids, summary):
    snapshots = grid_snapshots(snapshots, sheet_ids)
    output = exports(store, snapshots, summary)
    patches = {}
    row_writes = []
    expected = {'schema': 1, 'plan_id': uuid.uuid4().hex, 'tabs': {}, 'layouts': [], 'sheet_ids': {tab: sheet_ids[tab] for tab in snapshots}}
    states = {tab: [list(row) for row in rows] for tab, rows in snapshots.items()}
    for tab, rows in output.items():
        original = snapshots[tab]
        header = original[0]
        patch = mirror_patch(tab, original, rows)
        if tab == 'Today':
            for i, row in enumerate(original[1:], 1):
                if row and row[0] == 'Last run':
                    patch['cells'].extend({'row': i, 'col': col, 'value': output[tab][1][col]} for col in (3, 5))
                elif row and row[0] == 'Problems':
                    patch['cells'].append({'row': i, 'col': 0, 'value': 'Previous problem'})
        patches[tab] = patch
        cells = [cell for cell in patch['cells'] if sheet_value(header[cell['col']], cell['value']) !=
                 sheet_value(header[cell['col']], _at(original, cell['row'], cell['col']))]
        patch['cells'] = sorted(cells, key=lambda cell: (cell['row'], cell['col']))
        row_writes.extend((tab, row, list(cells)) for row, cells in groupby(patch['cells'], key=lambda cell: cell['row']))
        expected['tabs'][tab] = {'header': header, 'cells': [], 'appends': []}

    expected['layouts'].append({tab: _layout(tab, rows) for tab, rows in states.items()})
    batches = []
    pending = []
    count = 0
    for tab, row, cells in row_writes:
        if len(cells) > MAX_BATCH_CELLS:
            raise Denied('dashboard row exceeds the grouped write limit: ' + tab)
        if count and count + len(cells) > MAX_BATCH_CELLS:
            batches.append({'requests': _requests(pending, sheet_ids), 'cell_count': count})
            expected['layouts'].append({name: _layout(name, rows) for name, rows in states.items()})
            pending, count = [], 0
        # A fresh row and its key always land in the same batch.
        batch = len(batches) + 1
        if row >= len(snapshots[tab]):
            expected['tabs'][tab]['appends'].append([row, batch])
        for cell in cells:
            col, value = cell['col'], cell['value']
            before = _at(snapshots[tab], row, col)
            expected['tabs'][tab]['cells'].append([row, col, before, value, batch])
            _put(states[tab], row, col, value)
        pending.append((tab, row, cells))
        count += len(cells)
    if pending:
        batches.append({'requests': _requests(pending, sheet_ids), 'cell_count': count})
        expected['layouts'].append({tab: _layout(tab, rows) for tab, rows in states.items()})
    ranges = {tab: f"'{tab}'!A1:{_column_name(len(rows[0]))}" for tab, rows in states.items()}
    plan = {'patches': patches, 'batches': batches, 'expected': expected, 'ranges': ranges, 'exports': output}
    if len(batches) <= 1:
        plan['requests'] = batches[0]['requests'] if batches else []
    return plan


def verify(snapshots, expected, *, batches_applied=None):
    """Verify row identity and every target cell before writes, between batches, or after readback."""
    if not isinstance(expected, dict) or expected.get('schema') != 1 or not expected.get('layouts'):
        raise Denied('dashboard verification requires a prepared plan')
    snapshots = grid_snapshots(snapshots, expected.get('sheet_ids'))
    snapshots_expected = expected['tabs']
    final = len(expected['layouts']) - 1
    done = final if batches_applied is None else batches_applied
    if type(done) is not int or not 0 <= done <= final:
        raise Denied('invalid verified dashboard batch count')
    errors = []
    checked = 0
    for tab, target in snapshots_expected.items():
        rows = snapshots.get(tab)
        if not rows or rows[0][:len(target['header'])] != target['header'] or any(rows[0][len(target['header']):]):
            errors.append(f'{tab}: dashboard headers changed or read incomplete')
            continue
        if _layout(tab, rows) != expected['layouts'][done][tab]:
            errors.append(f'{tab}: dashboard row identities or positions changed')
        for row, col, before, after, batch in target['cells']:
            checked += 1
            wanted = after if batch <= done else before
            name = target['header'][col]
            if sheet_value(name, _at(rows, row, col)) != sheet_value(name, wanted):
                errors.append(f'{tab}!{_column_name(col + 1)}{row + 1}: dashboard value changed')
        editable = EDITABLE.get(tab, (None,))[0]
        for row, batch in target['appends']:
            if batch > done and row < len(rows) and any(v not in ('', None) for v in rows[row]):
                errors.append(f'{tab} row {row + 1}: appended row is occupied')
            elif editable and _at(rows, row, target['header'].index(editable)) not in ('', None):
                errors.append(f'{tab} row {row + 1}: command appeared on an appended row')
    return {'ok': not errors, 'errors': errors[:25], 'error_count': len(errors), 'batches_applied': done,
            'checked_cells': checked}
