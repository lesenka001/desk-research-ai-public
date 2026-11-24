"""Owner-only server-rendered CRM routes for the existing HTTP server."""

from collections import defaultdict
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from http.cookies import SimpleCookie, CookieError
import base64
import hmac
import html
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from crm import domain, store

COOKIE = 'ir_crm_owner'
TTL = 8 * 60 * 60
CSS = Path(__file__).with_name('crm.css').read_text(encoding='utf-8')
ATTEMPTS = {}
ATTEMPT_LOCK = threading.Lock()
SESSIONS = {}
SESSION_LOCK = threading.Lock()


def esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def secret():
    value = os.environ.get('CRM_OWNER_PASSWORD', '')
    return value if len(value) >= 12 else ''


def signature(value):
    return hmac.new(sha256(('crm-session:' + secret()).encode()).digest(), value.encode(), sha256).hexdigest()


def new_session():
    value = f'{int(time.time())}.{secrets.token_hex(24)}'
    token = value + '.' + signature(value)
    now = time.time()
    with SESSION_LOCK:
        for key in list(SESSIONS):
            if SESSIONS[key] <= now: del SESSIONS[key]
        if len(SESSIONS) >= 128:
            del SESSIONS[min(SESSIONS, key=SESSIONS.get)]
        SESSIONS[sha256(token.encode()).hexdigest()] = now + TTL
    return token


def authenticated(handler):
    if not secret(): return None
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get('Cookie', ''))
        token = cookie[COOKIE].value
        stamp, nonce, digest = token.split('.')
        age = time.time() - int(stamp)
        if len(nonce) == 48 and 0 <= age < TTL and hmac.compare_digest(digest, signature(stamp + '.' + nonce)):
            with SESSION_LOCK:
                if SESSIONS.get(sha256(token.encode()).hexdigest(), 0) > time.time():
                    return token
    except (ValueError, KeyError, CookieError):
        pass
    return None


def csrf(token):
    return signature('csrf:' + token)


def hidden(token):
    return f'<input type="hidden" name="csrf" value="{csrf(token)}">'


def response(handler, body='', status=200, headers=()):
    data = body.encode('utf-8')
    handler.send_response(status)
    for key, value in headers:
        handler.send_header(key, value)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    handler.end_headers()
    handler.wfile.write(data)


def redirect(handler, location, cookie=None):
    headers = [('Location', location)]
    if cookie: headers.append(('Set-Cookie', cookie))
    response(handler, status=303, headers=headers)


def session_cookie(token, clear=False):
    secure = os.environ.get('CRM_COOKIE_SECURE', 'true').lower() != 'false'
    return (f'{COOKIE}={token}; Path=/crm; HttpOnly; SameSite=Strict; '
            f'Max-Age={0 if clear else TTL}' + ('; Secure' if secure else ''))


def shell(body, token=None):
    exit_button = (f'<form action="/crm/logout" method="post">{hidden(token)}'
                   '<button class="link">Выйти</button></form>') if token else ''
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>База проектов · Навигатор IR</title><style>{CSS}</style></head><body>'
            '<header><a class="wordmark" href="/crm">Навигатор IR<small>История исследований</small></a>'
            f'<div class="header-right"><a href="/">Открытые исследования</a>{exit_button}</div></header>'
            f'<main>{body}</main></body></html>')


def login_page(message=''):
    error = f'<div class="notice error" role="alert">{esc(message)}</div>' if message else ''
    return shell('<div class="login"><div class="eyebrow">Доступ владельца</div>'
                 '<h1>База проектов</h1><p class="muted">Загрузка CRM и проверка волн трекеров.</p>'
                 f'{error}<div class="card"><form action="/crm/login" method="post">'
                 '<label for="password">Пароль</label><input id="password" name="password" '
                 'type="password" autocomplete="current-password" required autofocus maxlength="512">'
                 '<button>Войти</button></form></div></div>')


def bounded_int(value, default=0):
    try: return max(0, min(int(value), 10**12))
    except (ValueError, TypeError): return default


def page_slice(rows, query):
    page = bounded_int(query.get('page', ['0'])[0])
    start = min(page * 30, len(rows))
    links = []
    for number, label in ((page - 1, 'Предыдущие'), (page + 1, 'Следующие')):
        if number >= 0 and number * 30 < len(rows):
            params = {k:v[0] for k,v in query.items()}
            params['page'] = number
            links.append(f'<a href="/crm?{esc(urlencode(params))}">{label}</a>')
    return rows[start:start + 30], '<div class="pagination">' + ''.join(links) + '</div>'


def table(headers, rows):
    return ('<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{esc(h)}</th>' for h in headers)
            + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows)
            + '</tbody></table></div>')


def percent(value):
    return f'{domain.rounded(value)}%'


def action_form(token, pn, record, action, label, extra='', css_class=''):
    return (f'<form method="post" action="/crm/decide">{hidden(token)}'
            f'<input type="hidden" name="pn" value="{esc(pn)}">'
            f'<input type="hidden" name="fingerprint" value="{domain.identity(record["data"])}">'
            f'<input type="hidden" name="action" value="{action}">{extra}'
            f'<button class="{css_class}">{label}</button></form>')


def review_cards(state, token, query):
    records = [(pn,r) for pn,r in state['projects'].items() if r.get('pending') and not r.get('excluded')]
    records.sort(key=lambda item: (item[1]['data']['aorg'], item[1]['data']['date'], domain.pn_number(item[0])))
    selected, pagination = page_slice(records, query)
    cards = []
    for pn, record in selected:
        data = record['data']
        common = (f'<div class="card-top"><h3>{esc(pn)} · аорг {esc(data["aorg"])}</h3>'
                  f'<span class="badge">IR {percent(data["ir"])}</span><span>N {data["n"]}</span>'
                  f'<span class="muted">Поле завершено {esc(data["date"])}</span></div>')
        fallback = '<p class="muted">До решения используются прежние принятые данные этого PN.</p>' if record.get('accepted') else ''
        if 'audience' in record['pending']:
            known = {}
            for other in state['projects'].values():
                if other.get('excluded'):
                    continue
                versions = [other['data']]
                if other.get('accepted'): versions.append(other['accepted']['data'])
                for d in versions:
                    if d['aorg'] == data['aorg']:
                        group = state['aliases'].get(d['aorg'], {}).get(domain.audience_key(d['audience']))
                        if group is not None: known[group] = d['audience']
            for group in set(state['aliases'].get(data['aorg'], {}).values()):
                known.setdefault(group, group.replace('\x01', '').replace('\x02', ''))
            options = ''.join(f'<option value="{k.encode().hex()}">{esc(v[:160])}</option>' for k,v in known.items())
            comparisons = ''.join(f'<details><summary>Существующая ЦА {i}</summary><div class="audience">{esc(v)}</div></details>' for i,v in enumerate(known.values(), 1))
            extra = f'<div><label for="target-{pn}">Существующая группа ЦА</label><select id="target-{pn}" name="target" required>{options}</select></div>'
            content = ('<span class="badge warn">Отличается описание ЦА</span><div class="compare">'
                       f'<div><h3>Новая волна</h3><div class="audience">{esc(data["audience"])}</div></div>'
                       f'<div><h3>Принятые группы</h3>{comparisons}</div></div>{fallback}<div class="actions">'
                       + action_form(token,pn,record,'same','Это та же ЦА',extra)
                       + action_form(token,pn,record,'different','Это другая ЦА',css_class='secondary') + '</div>')
        else:
            peers = [r['data'] for r in state['projects'].values() if not r.get('excluded')
                     and r['data']['aorg'] == data['aorg'] and r.get('group') == record['group']]
            peers.sort(key=lambda d:(d['date'],domain.pn_number(d['pn'])))
            peer_table = table(['PN','Дата поля','IR','N'], [[esc(d['pn']),esc(d['date']),percent(d['ir']),str(d['n'])] for d in peers])
            fences = record.get('fences')
            explanation = f'Границы проверки IQR: {percent(fences[0])} … {percent(fences[1])}.' if fences else ''
            content = (f'<span class="badge warn">Кандидат в выбросы IR</span><p>{explanation}</p>'
                       f'<div class="audience">{esc(data["audience"])}</div>{peer_table}{fallback}<div class="actions">'
                       + action_form(token,pn,record,'keep','Включить в расчет')
                       + action_form(token,pn,record,'exclude','Исключить как выброс',css_class='danger') + '</div>')
        cards.append(f'<article class="card">{common}{content}</article>')
    return ''.join(cards) + pagination if cards else '<div class="empty">Спорных строк нет. Корректные новые волны будут учитываться автоматически.</div>'


def projects_view(state, query):
    search = query.get('q', [''])[0][:150].casefold()
    groups = domain.summaries(state)
    if search:
        groups = [g for g in groups if search in (g['aorg'] + ' ' + g['audience'] + ' ' + ' '.join(r['pn'] for r in g['rows'])).casefold()]
    selected, pagination = page_slice(groups, query)
    rows = []
    for g in selected:
        details = table(['PN', 'Дата поля','IR','N'], [[esc(r['pn']),esc(r['date']),percent(r['ir']),str(r['n'])] for r in sorted(g['rows'],key=lambda r:r['date'],reverse=True)])
        rows.append([f'<b>{esc(g["aorg"])}</b><br>{esc(g["latest"])}<br><span class="muted">{g["date"]}</span>',
                     f'<div class="audience">{esc(g["audience"])}</div><details><summary>Расчетные волны</summary>{details}</details>',
                     f'<b class="numeric">{percent(g["mean"])}</b>',
                     f'<span class="numeric">{percent(g["low"])} … {percent(g["high"])}</span>',
                     f'{g["count"]}<br><span class="muted">Всего в трекере: {g["total"]}</span>'])
    search_form = (f'<form class="search" method="get" action="/crm"><input type="hidden" name="tab" value="projects">'
                   f'<input name="q" aria-label="Поиск по PN, аорг или ЦА" placeholder="PN, аорг или описание ЦА" value="{esc(query.get("q",[""])[0][:150])}">'
                   '<button class="secondary">Найти</button></form>')
    excluded = []
    for pn,r in state['projects'].items():
        if r.get('outlier') and not r.get('excluded'):
            d=r['data']
            excluded.append([esc(pn),esc(d['aorg']),f'<div class="audience">{esc(d["audience"])}</div>',percent(d['ir'])])
    extras = ('<details><summary>Исключенные выбросы (' + str(len(excluded)) + ')</summary>'
              + table(['PN','Аорг','ЦА','IR'],excluded) + '</details>') if excluded else ''
    return search_form + (table(['Аорг / последний PN','Целевая аудитория','Средний IR','Диапазон IR','Волн в расчете'],rows) if rows else '<div class="empty">Расчетных проектов пока нет.</div>') + pagination + extras


def history_view(events, query):
    rows = []
    for event_id, created, kind, filename, report in events:
        summary = (f'Добавлено {report.get("added",0)} · заменено {report.get("updated",0)} · '
                   f'исключено {report.get("excluded",0)} · на проверке {report.get("pending",0)}') if kind=='import' else f'{report.get("pn", "")} · {report.get("action", "")}'
        rows.append([esc(str(created)[:19]),f'<a href="/crm?event={event_id}">{esc(filename or "Решение проверки")}</a>',esc(summary)])
    older = f'<a href="/crm?tab=history&amp;before={events[-1][0]}">Более ранние записи</a>' if len(events)==30 else ''
    return (table(['Когда','Загрузка / решение','Результат'],rows) if rows else '<div class="empty">История появится после первой загрузки.</div>') + f'<div class="pagination">{older}</div>'


def event_view(event, query):
    if not event: return '<div class="notice error">Запись истории не найдена.</div>'
    event_id, created, kind, filename, report = event
    heading = f'<h2 class="filename">{esc(filename or "Решение проверки")}</h2><p class="muted">{esc(str(created)[:19])}</p>'
    if kind != 'import':
        return heading + f'<p>{esc(report.get("pn"))}: {esc(report.get("action"))}</p>'
    labels = {'added':'Добавлено','updated':'Заменено','unchanged':'Без изменений','excluded':'Исключено','pending':'На проверке'}
    summary = '<div class="notice">' + ' · '.join(f'{label}: {report.get(key,0)}' for key,label in labels.items()) + '</div>'
    selected, pagination = page_slice(report.get('rows', []), query)
    rows=[]
    for entry in selected:
        d=entry['data']
        reason = ', '.join(domain.REASONS.get(r, 'Формула или ошибка Excel') for r in entry['reasons'])
        if entry.get('pending'): reason = (reason + ' · ' if reason else '') + 'Ожидала проверки при загрузке'
        rows.append([str(entry['row']),esc(d['pn']),esc(d['aorg']),labels.get(entry['status'],entry['status']),esc(reason)])
    return heading + summary + table(['Строка Excel','PN','Аорг','Результат','Причина'],rows) + pagination


def dashboard(token, query):
    before = bounded_int(query.get('before', [None])[0]) or None
    state, revision, events = store.load(before)
    count = sum(not r.get('excluded') for r in state['projects'].values())
    pending = sum(bool(r.get('pending')) and not r.get('excluded') for r in state['projects'].values())
    groups = domain.summaries(state)
    latest_import = next((e for e in events if e[2]=='import'),None)
    last = f'Последняя загрузка: {esc(str(latest_import[1])[:19])}' if latest_import else 'Загрузок пока нет'
    top = ('<div class="top"><div><div class="eyebrow">Рабочая база CRM</div><h1>История проектов</h1>'
           '<p class="muted intro">Фактические результаты исследований и волны трекеров. '
           'Корректные данные обновляются сразу; решения нужны только для спорных ЦА и выбросов.</p>'
           f'<div class="metrics"><div class="metric"><b>{count}</b><span>Проектов и волн</span></div>'
           f'<div class="metric"><b>{len(groups)}</b><span>Расчетных групп</span></div>'
           f'<div class="metric"><b>{pending}</b><span>На проверке</span></div></div><p class="muted">{last}</p></div>'
           '<section class="upload"><h2>Загрузить выгрузку</h2><p class="muted">Полная база или новые проекты. '
           'Строки с пустыми/нулевыми IR и N, пустой ЦА, неверным аорг и реконтакты исключаются.</p>'
           f'<form method="post" action="/crm/upload" enctype="multipart/form-data">{hidden(token)}'
           '<label for="file">Excel из CRM · до 16 МБ</label><input id="file" type="file" name="file" '
           'accept=".xlsx" required><br><button>Загрузить и обновить базу</button></form>'
           '<p class="muted">Данные и история сохраняются. Сам Excel-файл не хранится.</p></section></div>')
    tab = query.get('tab',['review'])[0]
    if 'event' in query: tab='history'
    nav = '<nav>' + ''.join(f'<a class="{"active" if tab==key else ""}" href="/crm?tab={key}">{name}</a>'
                            for key,name in [('review',f'Проверка ({pending})'),('projects','Проекты и волны'),('history','История')]) + '</nav>'
    if 'event' in query: content=event_view(store.history(bounded_int(query['event'][0])),query)
    elif tab=='projects': content=projects_view(state,query)
    elif tab=='history': content=history_view(events,query)
    else: content=review_cards(state,token,query)
    return shell(top + nav + '<section class="section">' + content + '</section>'
                 '<p class="footnote">Средний IR и диапазон относятся к принятым волнам одной ЦА. '
                 'Показатели округлены до целого процента; расчеты используют исходную точность.</p>',token)


def read_form(handler, upload=False):
    length = handler.headers.get('Content-Length', '')
    limit = domain.MAX_FILE + 65536 if upload else 16384
    if not length.isdigit() or not 0 < int(length) <= limit:
        raise domain.IntakeError('Некорректный размер запроса или превышен лимит файла.')
    handler.connection.settimeout(15)
    body = handler.rfile.read(int(length))
    if len(body) != int(length): raise domain.IntakeError('Загрузка файла прервалась.')
    if not upload:
        return {key:values[0] for key,values in parse_qs(body.decode('utf-8'),max_num_fields=20).items()}
    kind = handler.headers.get('Content-Type','')
    if not kind.startswith('multipart/form-data'):
        raise domain.IntakeError('Выбери Excel-файл через форму загрузки.')
    message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+kind+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
    result={}
    for part in message.iter_parts():
        name=part.get_param('name',header='content-disposition')
        if name not in ('file','csrf') or name in result: raise domain.IntakeError('Некорректная форма загрузки.')
        payload=part.get_payload(decode=True)
        if name=='file': result[name]=(part.get_filename() or '',payload)
        else: result[name]=payload.decode('utf-8')
    return result


def routes(handler):
    parsed=urlsplit(handler.path)
    if parsed.path != '/crm' and not parsed.path.startswith('/crm/'):
        return False
    if not secret():
        response(handler,shell('<div class="notice error">Доступ владельца еще не настроен.</div>'),503)
        return True
    token=authenticated(handler)
    if handler.command=='GET':
        if parsed.path not in ('/crm','/crm/','/crm/login'):
            response(handler,'Страница не найдена',404)
        elif not token:
            response(handler,login_page())
        else:
            try: response(handler,dashboard(token,parse_qs(parsed.query,max_num_fields=10)))
            except Exception:
                response(handler,shell('<div class="notice error">База временно недоступна. Повтори попытку позже.</div>',token),503)
        return True
    if handler.command!='POST':
        response(handler,'Метод недоступен',405)
        return True
    origin=handler.headers.get('Origin')
    if origin and urlsplit(origin).netloc != handler.headers.get('Host'):
        response(handler,'Запрос с другого сайта отклонен',403)
        return True
    try:
        if parsed.path=='/crm/login':
            address=handler.client_address[0]
            now=time.time()
            with ATTEMPT_LOCK:
                for key in list(ATTEMPTS):
                    if now - ATTEMPTS[key][0] >= 600: del ATTEMPTS[key]
                start, count=ATTEMPTS.get(address,(now,0))
                if count>=5:
                    response(handler,login_page('Слишком много попыток. Повтори через 10 минут.'),429)
                    return True
                if len(ATTEMPTS)>1024: ATTEMPTS.clear()
                ATTEMPTS[address]=(start,count+1)
            fields=read_form(handler)
            if not hmac.compare_digest(fields.get('password','').encode(),secret().encode()):
                response(handler,login_page('Неверный пароль.'),401)
            else:
                with ATTEMPT_LOCK: ATTEMPTS.pop(address,None)
                redirect(handler,'/crm',session_cookie(new_session()))
            return True
        if not token:
            response(handler,login_page('Войди, чтобы изменить базу.'),401)
            return True
        fields=read_form(handler,parsed.path=='/crm/upload')
        if not hmac.compare_digest(fields.get('csrf','').encode(),csrf(token).encode()):
            response(handler,'Проверка формы не пройдена. Обнови страницу.',403)
            return True
        if parsed.path=='/crm/logout':
            with SESSION_LOCK:
                SESSIONS.pop(sha256(token.encode()).hexdigest(), None)
            redirect(handler,'/crm',session_cookie('',clear=True))
        elif parsed.path=='/crm/upload':
            filename,content=fields.get('file',('',b''))
            if not filename.lower().endswith('.xlsx'): raise domain.IntakeError('Нужен файл .xlsx.')
            event_id,_=store.upload(content,filename)
            redirect(handler,f'/crm?event={event_id}')
        elif parsed.path=='/crm/decide':
            target=fields.get('target')
            if target: target=bytes.fromhex(target).decode('utf-8')
            store.decision(fields.get('pn',''),fields.get('action',''),fields.get('fingerprint',''),target)
            redirect(handler,'/crm')
        else:
            response(handler,'Страница не найдена',404)
    except (domain.IntakeError,ValueError,UnicodeError) as error:
        message=str(error) if isinstance(error,domain.IntakeError) else 'Некорректная форма.'
        response(handler,shell(f'<div class="notice error" role="alert">{esc(message)}</div><a href="/crm">Вернуться к базе проектов</a>',token),400)
    except Exception:
        response(handler,shell('<div class="notice error">Изменения не сохранены. База временно недоступна.</div><a href="/crm">Вернуться</a>',token),503)
    return True
