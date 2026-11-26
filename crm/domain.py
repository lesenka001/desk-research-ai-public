"""Deterministic XLSX intake and tracker calculations; no database or network."""

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO
import json
import math
import re
import unicodedata
from zipfile import BadZipFile, ZipFile

HEADERS = ('Number', 'Project #', 'Target Audience', 'IR (fact), %',
           'N (A.org)', 'Date FW Close (fact)')
MAX_FILE = 16 * 1024 * 1024
MAX_ROWS = 20000
REASONS = {
    'recontact': 'Реконтакт', 'ir': 'IR пустой, нулевой или некорректный',
    'n': 'N пустой, нулевой или некорректный', 'audience': 'Пустая ЦА',
    'aorg': 'Неверный номер аорг или PN вместо аорг',
    'pn': 'Неверный PN', 'date': 'Нет корректной даты завершения поля',
    'duplicate': 'Разные данные одного PN в одном файле',
}


class IntakeError(ValueError):
    pass


def identity(data):
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def audience_key(text):
    text = str(text or '').casefold()
    def protect(match):
        value = re.sub(r'\s+', '', match.group()).replace('–', '-').replace('—', '-').replace(',', '.')
        return '\x01' + value + '\x02'
    text = re.sub(r'\d+(?:[.,]\d+)?(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?', protect, text)
    result, protected = [], False
    for char in text:
        if char == '\x01':
            protected = True
        elif char == '\x02':
            protected = False
        elif not protected and unicodedata.category(char)[0] in ('P', 'S') and char not in '+%<=>≤≥':
            char = ' '
        result.append(char)
    return ' '.join(''.join(result).split())


def rounded(value):
    return int(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def pn_number(pn):
    return int(pn[2:])


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.').rstrip('%'))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def project(values, row_number, ir_percent_format=False):
    pn, aorg, audience, ir, n, end = values
    raw = [v.isoformat() if isinstance(v, (date, datetime)) else str(v) if v is not None else None for v in values]
    pn, aorg, audience = text(pn).upper(), text(aorg), text(audience)
    ir, n = number(ir), number(n)
    if ir is not None and ir_percent_format:
        ir *= 100
    if isinstance(end, datetime):
        end = end.date()
    elif isinstance(end, str):
        parsed = None
        for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
            try:
                parsed = datetime.strptime(end.strip(), fmt).date()
                break
            except ValueError:
                pass
        end = parsed
    reasons = []
    if re.search(r'реконтакт|ре[\s-]+контакт|re[\s-]?contact', audience, re.I):
        reasons.append('recontact')
    if not re.fullmatch(r'PN\d+', pn): reasons.append('pn')
    if not re.fullmatch(r'\d+', aorg) or aorg.upper() == pn: reasons.append('aorg')
    if not audience: reasons.append('audience')
    if ir is None or not 0 < ir <= 100: reasons.append('ir')
    if n is None or n <= 0 or n != int(n): reasons.append('n')
    if not isinstance(end, date): reasons.append('date')
    return dict(row=row_number, data=dict(pn=pn, aorg=aorg, audience=audience,
                ir=ir, n=int(n) if n is not None and n.is_integer() else n,
                date=end.isoformat() if isinstance(end, date) else None),
                reasons=reasons, raw=raw)


def read_xlsx(content):
    if not content or len(content) > MAX_FILE:
        raise IntakeError('Файл пуст или больше 16 МБ.')
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(e.file_size for e in entries) > 64 * 1024 * 1024:
                raise IntakeError('Слишком большой распакованный Excel.')
        import openpyxl
        workbook = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=False)
        try:
            matching = []
            for sheet in workbook:
                first = next(sheet.iter_rows(max_row=1), ())
                labels = [text(c.value) for c in first]
                if all(labels.count(h) == 1 for h in HEADERS):
                    matching.append((sheet, [labels.index(h) for h in HEADERS]))
            if len(matching) != 1:
                raise IntakeError('Нужен один лист с шестью колонками: ' + ', '.join(HEADERS))
            sheet, columns = matching[0]
            # Ignore potentially inflated worksheet dimensions; bound actual iteration.
            sheet.reset_dimensions()
            rows = []
            for line, cells in enumerate(sheet.iter_rows(min_row=2), 2):
                if line > MAX_ROWS + 1:
                    raise IntakeError('В файле больше 20 000 строк.')
                selected = [cells[i] if i < len(cells) else None for i in columns]
                values = [c.value if c is not None else None for c in selected]
                if not any(v is not None and text(v) for v in values):
                    continue
                row = project(values, line, bool(selected[3] and '%' in selected[3].number_format))
                if any(c is not None and c.data_type in ('f', 'e') for c in selected):
                    row['reasons'] = list(dict.fromkeys(row['reasons'] + ['formula']))
                rows.append(row)
            if not rows:
                raise IntakeError('В файле нет проектов.')
            return rows
        finally:
            workbook.close()
    except IntakeError:
        raise
    except Exception as error:
        # Do not expose workbook contents or XML fragments in HTTP error responses.
        raise IntakeError('Не удалось прочитать XLSX. Проверь формат и структуру файла.') from error


def empty_state():
    return dict(projects={}, aliases={}, known_waves={}, group_meta={}, metadata_version=5)



def remember_rows(state, rows):
    """Retain observed identities and source text even for rejected import rows."""
    known = state.setdefault('known_waves', {})
    for entry in rows:
        data = entry.get('data', {})
        pn, aorg = data.get('pn', ''), data.get('aorg', '')
        if not re.fullmatch(r'PN\d+', pn) or not re.fullmatch(r'\d+', aorg):
            continue
        waves = known.setdefault(aorg, {})
        previous = waves.get(pn, {})
        descriptions = list(previous.get('descriptions', []))
        audience = data.get('audience', '')
        if audience and audience not in descriptions:
            descriptions.append(audience)
        waves[pn] = dict(data=deepcopy(data), descriptions=descriptions,
                         status=entry.get('status', 'history'))


def ensure_metadata(state):
    """Add metadata without changing existing aliases, accepted data or decisions."""
    sources = []
    for record in state['projects'].values():
        if record.get('accepted'):
            sources.append(dict(data=record['accepted']['data']))
        sources.append(dict(data=record['data']))
    remember_rows(state, sources)
    metadata = state.setdefault('group_meta', {})
    for aorg, aliases in state['aliases'].items():
        groups = metadata.setdefault(aorg, {})
        for key in sorted(set(aliases.values())):
            if key not in groups:
                number = max((g['number'] for g in groups.values()), default=0) + 1
                groups[key] = dict(number=number, name='', descriptions=[])
        for wave in state.get('known_waves', {}).get(aorg, {}).values():
            for description in wave['descriptions']:
                key = aliases.get(audience_key(description))
                if key in groups and description not in groups[key]['descriptions']:
                    groups[key]['descriptions'].append(description)
    return state


def upgrade_state(state, reports=()):
    state = deepcopy(state)
    for report in reports:
        remember_rows(state, report.get('rows', []))
    ensure_metadata(state)
    state['metadata_version'] = 5
    return state


def project_info(state, aorg):
    total = len(state.get('known_waves', {}).get(aorg, {}))
    return dict(total=total, project_type='трекер' if total > 1 else 'эдхок')


def group_label(meta):
    label = f'ЦА {meta["number"]}'
    return label + (' — ' + meta['name'] if meta.get('name') else '')


def rename_group(state, aorg, group, name, fingerprint):
    state = upgrade_state(state)
    meta = state.get('group_meta', {}).get(aorg, {}).get(group)
    if meta is None or group not in set(state['aliases'].get(aorg, {}).values()):
        raise IntakeError('Группа ЦА не найдена. Обнови страницу.')
    if identity(meta) != fingerprint:
        raise IntakeError('Название или данные группы изменились. Обнови страницу.')
    name = name.strip()
    if len(name) > 200:
        raise IntakeError('Название ЦА должно содержать не больше 200 символов.')
    meta['name'] = name
    return state


def group_catalog(state):
    """All groups, including groups with no currently accepted IR."""
    state = upgrade_state(state)
    calculated = {(g['aorg'], g['group']): g for g in summaries(state)}
    result = []
    for aorg, groups in state['group_meta'].items():
        aliases = state['aliases'].get(aorg, {})
        for key, meta in groups.items():
            members = []
            for pn, wave in state.get('known_waves', {}).get(aorg, {}).items():
                if not any(aliases.get(audience_key(d)) == key for d in wave['descriptions']):
                    continue
                record = state['projects'].get(pn)
                status = 'Исключена / история'
                if record and record['data']['aorg'] == aorg:
                    accepted = record.get('accepted')
                    if record.get('excluded'):
                        status = 'Исключена при импорте'
                    elif accepted and accepted['data']['aorg'] == aorg and accepted['group'] == key:
                        status = 'В расчете (прежние данные)' if record.get('pending') else 'В расчете'
                    elif record.get('group') == key:
                        status = 'На проверке' if record.get('pending') else 'Исключена как выброс'
                    else:
                        status = 'История описания ЦА'
                members.append(dict(pn=pn, status=status))
            members.sort(key=lambda r: pn_number(r['pn']))
            item = dict(aorg=aorg, group=key, label=group_label(meta), meta=meta,
                        descriptions=meta['descriptions'], members=members,
                        summary=calculated.get((aorg, key)))
            item.update(project_info(state, aorg))
            result.append(item)
    return sorted(result, key=lambda g: (g['aorg'], g['meta']['number']))


def bounds(values):
    values = sorted(values)
    def quantile(p):
        h = (len(values) - 1) * p
        i = int(h)
        return values[i] + (values[min(i + 1, len(values) - 1)] - values[i]) * (h - i)
    q1, q3 = quantile(.25), quantile(.75)
    return q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)


def choice_key(data, group):
    return identity([data['aorg'], group, data['ir']])


def refresh(state):
    """Resolve known audiences, then flag candidates, preserving prior accepted replacements."""
    records = state['projects']
    groups = defaultdict(list)
    for pn, record in sorted(records.items(), key=lambda item: (item[1]['data'].get('date') or '', pn_number(item[0]))):
        record['pending'] = []
        record['outlier'] = False
        if record.get('excluded'):
            record['accepted'] = None
            continue
        data = record['data']
        aliases = state['aliases'].setdefault(data['aorg'], {})
        key = audience_key(data['audience'])
        if not aliases:
            aliases[key] = key
        group = aliases.get(key)
        record['group'] = group
        if group is None:
            record['pending'].append('audience')
        else:
            groups[(data['aorg'], group)].append((pn, record))
    for (aorg, group), members in groups.items():
        values = [r['data']['ir'] for _, r in members
                  if r.get('choice') != {'key': choice_key(r['data'], group), 'value': 'exclude'}]
        low, high = bounds(values) if len(values) >= 2 else (-math.inf, math.inf)
        for pn, record in members:
            data = record['data']
            decision = record.get('choice', {})
            current = decision.get('key') == choice_key(data, group)
            record['fences'] = [low, high] if len(values) >= 2 else None
            record['outlier'] = bool(current and decision.get('value') == 'exclude')
            if not current and not low <= data['ir'] <= high:
                record['pending'].append('outlier')
    for record in records.values():
        if record.get('excluded'):
            continue
        if record['pending']:
            previous = record.get('accepted')
            if previous and previous['data'] == record['data']:
                record['accepted'] = None
        elif record.get('outlier'):
            record['accepted'] = None
        else:
            record['accepted'] = dict(data=deepcopy(record['data']), group=record['group'])
    ensure_metadata(state)
    return state


def import_rows(state, rows):
    state = upgrade_state(state)
    remember_rows(state, rows)
    report = dict(added=0, updated=0, unchanged=0, excluded=0, pending=0, rows=[])
    by_pn = defaultdict(list)
    for row in rows:
        by_pn[row['data']['pn']].append(row)
    for pn, duplicates in by_pn.items():
        conflict = len({identity(r['data']) for r in duplicates}) > 1
        for index, row in enumerate(duplicates):
            entry = deepcopy(row)
            if conflict:
                entry['reasons'] = list(dict.fromkeys(entry['reasons'] + ['duplicate']))
            record = state['projects'].get(pn)
            if entry['reasons']:
                entry['status'] = 'excluded'
                report['excluded'] += 1
                # Permanent exclusions also deactivate an older accepted PN.
                # A conflicting duplicate alone rejects this file's rows, not the old project.
                if record and {'recontact', 'ir', 'n', 'audience', 'aorg'} & set(entry['reasons']):
                    record.update(excluded=True, accepted=None)
            elif index or (record and record['data'] == entry['data'] and not record.get('excluded')):
                entry['status'] = 'unchanged'
                report['unchanged'] += 1
            else:
                entry['status'] = 'updated' if record else 'added'
                report[entry['status']] += 1
                new = dict(data=deepcopy(entry['data']), accepted=deepcopy(record.get('accepted')) if record else None,
                           choice=deepcopy(record.get('choice', {})) if record else {}, excluded=False)
                state['projects'][pn] = new
            report['rows'].append(entry)
    refresh(state)
    for entry in report['rows']:
        record = state['projects'].get(entry['data']['pn'])
        if entry['status'] != 'excluded' and record and record.get('pending'):
            report['pending'] += 1
            entry['pending'] = record['pending'][:]
    return state, report


def decide(state, pn, action, fingerprint, target=None):
    state = upgrade_state(state)
    record = state['projects'].get(pn)
    if not record or record.get('excluded') or identity(record['data']) != fingerprint:
        raise IntakeError('Данные изменились. Обнови страницу проверки.')
    data = record['data']
    if action in ('same', 'different') and 'audience' in record.get('pending', []):
        aliases = state['aliases'][data['aorg']]
        if action == 'same' and target not in set(aliases.values()):
            raise IntakeError('Выбери существующую группу ЦА этого трекера.')
        key = audience_key(data['audience'])
        aliases[key] = target if action == 'same' else key
    elif action in ('keep', 'exclude') and 'outlier' in record.get('pending', []):
        record['choice'] = dict(key=choice_key(data, record['group']), value=action)
    else:
        raise IntakeError('Этот вопрос уже решен или действие недоступно.')
    return refresh(state)


def summaries(state):
    groups = defaultdict(list)
    for record in state['projects'].values():
        if record.get('excluded'):
            continue
        if record.get('accepted'):
            accepted = record['accepted']
            groups[(accepted['data']['aorg'], accepted['group'])].append(accepted['data'])
    result = []
    for (aorg, group), rows in groups.items():
        latest = max(rows, key=lambda r: (r['date'], pn_number(r['pn'])))
        values = [r['ir'] for r in rows]
        result.append(dict(aorg=aorg, group=group, audience=latest['audience'], latest=latest['pn'],
                           date=latest['date'], count=len(rows), **project_info(state, aorg),
                           mean=sum(values) / len(values), low=min(values), high=max(values), rows=rows))
    return sorted(result, key=lambda g: (g['date'], pn_number(g['latest'])), reverse=True)
