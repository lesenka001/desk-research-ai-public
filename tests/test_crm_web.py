from copy import deepcopy
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
import re
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from crm import domain
from crm.store import decision_report
from web import crm
from web.main import Handler
from tests.test_crm_domain import workbook, row


class MemoryStore:
    def __init__(self):
        self.state = domain.empty_state()
        self.events = []
    def load(self, before=None):
        events = [e for e in self.events[::-1] if before is None or e[0]<before]
        return deepcopy(self.state), len(self.events), deepcopy(events[:30])
    def upload(self, content, filename):
        self.state, report = domain.import_rows(self.state, domain.read_xlsx(content))
        self.events.append((len(self.events)+1, datetime.now(timezone.utc), 'import', filename, report))
        return len(self.events), report
    def decision(self, pn, action, fingerprint, target=None):
        before = self.state
        self.state = domain.decide(self.state,pn,action,fingerprint,target)
        self.events.append((len(self.events)+1,datetime.now(timezone.utc),'decision',None,decision_report(before,self.state,pn,action,fingerprint,target)))
        return len(self.events), self.events[-1][4]
    def rename(self, aorg, group, name, fingerprint):
        before = deepcopy(self.state['group_meta'][aorg][group])
        self.state = domain.rename_group(self.state,aorg,group,name,fingerprint)
        after = self.state['group_meta'][aorg][group]
        report = dict(action='rename_group',aorg=aorg,group=group,before=before,after=deepcopy(after),group_label=domain.group_label(after))
        self.events.append((len(self.events)+1,datetime.now(timezone.utc),'decision',None,report))
        return len(self.events), report
    def history(self, event_id):
        return next((deepcopy(e) for e in self.events if e[0]==event_id),None)


class QuietHandler(Handler):
    def log_message(self,*args): pass


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryStore()
        self.env = patch.dict('os.environ', {'CRM_OWNER_PASSWORD':'test-owner-passphrase', 'CRM_COOKIE_SECURE':'false'})
        self.env.start()
        self.mock_store = patch.object(crm,'store',self.memory)
        self.mock_store.start()
        crm.ATTEMPTS.clear()
        crm.SESSIONS.clear()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.cookie=''
        self.host=f'127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.mock_store.stop()
        self.env.stop()
    def request(self, path='/crm', method='GET', data=None, headers=None):
        conn=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        h={'Cookie':self.cookie}
        if headers: h.update(headers)
        conn.request(method,path,body=data,headers=h)
        r=conn.getresponse()
        body=r.read().decode('utf-8')
        result=(r.status,dict(r.getheaders()),body)
        conn.close()
        return result
    def login(self):
        status,headers,_=self.request('/crm/login','POST',urlencode({'password':'test-owner-passphrase'}),{'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(status,303)
        self.cookie=headers['Set-Cookie'].split(';')[0]
        return self.cookie.split('=',1)[1]
    def post(self,path,fields):
        return self.request(path,'POST',urlencode(fields),{'Content-Type':'application/x-www-form-urlencoded'})
    def upload(self,token,content,filename='projects.xlsx'):
        boundary='crm-test-boundary'
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{crm.csrf(token)}\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
              'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()+content+f'\r\n--{boundary}--\r\n'.encode()
        return self.request('/crm/upload','POST',body,{'Content-Type':f'multipart/form-data; boundary={boundary}'})

    def test_closed_without_password_and_private_before_login(self):
        self.memory.state,_=domain.import_rows(domain.empty_state(),[row(audience='PRIVATE AUDIENCE')])
        status,headers,body=self.request('/crm?tab=projects')
        self.assertEqual(status,200)
        self.assertNotIn('PRIVATE AUDIENCE',body)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        with patch.dict('os.environ',{'CRM_OWNER_PASSWORD':''}):
            self.assertEqual(self.request()[0],503)
    def test_login_cookie_forgery_expiry_password_rotation_and_logout(self):
        token=self.login()
        self.assertTrue(crm.authenticated(type('H',(),{'headers':{'Cookie':self.cookie}})()))
        self.assertIn('HttpOnly',crm.session_cookie(token))
        with patch.dict('os.environ',{'CRM_COOKIE_SECURE':'true'}): self.assertIn('; Secure',crm.session_cookie(token))
        self.cookie=self.cookie[:-1] + ('1' if self.cookie[-1]!='1' else '2')
        self.assertIn('name="password"',self.request()[2])
        self.cookie=f'{crm.COOKIE}={token}'
        with patch.object(crm.time,'time',return_value=int(token.split('.')[0])+crm.TTL+1):
            self.assertIn('name="password"',self.request()[2])
        with patch.dict('os.environ',{'CRM_OWNER_PASSWORD':'another-owner-passphrase'}):
            self.assertIn('name="password"',self.request()[2])
        status,headers,_=self.post('/crm/logout',{'csrf':crm.csrf(token)})
        self.assertEqual(status,303)
        self.assertIn('Max-Age=0',headers['Set-Cookie'])
        self.assertIn('name="password"',self.request()[2])
    def test_login_rate_limit_and_origin(self):
        for _ in range(5): self.assertEqual(self.post('/crm/login',{'password':'bad'})[0],401)
        self.assertEqual(self.post('/crm/login',{'password':'bad'})[0],429)
        self.assertEqual(self.request('/crm/login','POST','password=x',{'Origin':'https://evil.test'})[0],403)
    def test_upload_requires_login_csrf_and_valid_xlsx(self):
        self.assertEqual(self.post('/crm/upload',{'csrf':'x'})[0],401)
        token=self.login()
        self.assertEqual(self.post('/crm/decide',{'csrf':'подделка'})[0],403)
        self.assertEqual(self.upload(token,b'invalid')[0],400)
        self.assertFalse(self.memory.events)
    def test_end_to_end_upload_review_decision_projects_and_history(self):
        token=self.login()
        content=workbook([['PN1','100','Мужчины',20,100,datetime(2026,11,1)],
                          ['PN2','100','Женщины <script>alert(1)</script>',40,100,datetime(2026,11,2)],
                          ['PN3','101','Нет N',20,0,datetime(2026,11,3)]])
        status,headers,_=self.upload(token,content)
        self.assertEqual(status,303)
        self.assertEqual(headers['Location'],'/crm?event=1')
        status,_,body=self.request()
        self.assertEqual(status,200)
        self.assertIn('Создать отдельную группу ЦА',body)
        self.assertNotIn('<script>',body)
        self.assertIn('&lt;script&gt;',body)
        self.assertNotIn('PN3',body)
        record=self.memory.state['projects']['PN2']
        fields=dict(csrf=crm.csrf(token),pn='PN2',action='different',fingerprint=domain.identity(record['data']))
        self.assertEqual(self.post('/crm/decide',fields)[0],303)
        self.assertEqual(self.post('/crm/decide',fields)[0],400)
        self.assertIn('Спорных строк нет',self.request()[2])
        self.assertIn('PN2',self.request('/crm?tab=projects')[2])
        self.assertIn('Исключено: 1',self.request('/crm?event=1')[2])
        self.assertIn('N пустой',self.request('/crm?event=1')[2])
        self.assertIn('projects.xlsx',self.request('/crm?tab=history')[2])
        self.assertEqual(self.upload(token,content)[0],303)
        self.assertIn('Спорных строк нет',self.request()[2])
    def test_unknown_private_path_and_bad_query_do_not_leak(self):
        self.assertEqual(self.request('/crm/nonsense')[0],404)
        self.login()
        self.assertEqual(self.request('/crm?'+ '&'.join(f'a{i}=1' for i in range(12)))[0],503)

    def test_pending_replacement_offers_previous_accepted_audience(self):
        self.memory.state,_=domain.import_rows(domain.empty_state(),[row(audience='Прежняя ЦА')])
        self.memory.state,_=domain.import_rows(self.memory.state,[row(audience='Новая ЦА')])
        self.login()
        status,_,body=self.request()
        self.assertEqual(status,200)
        self.assertIn('Прежняя ЦА',body)
        self.assertRegex(body,r'<option value="[a-f0-9]+">ЦА 1</option>')

    def test_browser_form_origin_policy_and_same_origin_login(self):
        # Browsers send Origin:null for a POST form served with no-referrer.
        # The policy must preserve Origin on a same-origin form submission.
        status,headers,_=self.request()
        self.assertEqual(status,200)
        self.assertEqual(headers['Referrer-Policy'],'same-origin')
        fields=urlencode({'password':'test-owner-passphrase'})
        headers={'Content-Type':'application/x-www-form-urlencoded',
                 'Origin':f'http://{self.host}'}
        status,result,_=self.request('/crm/login','POST',fields,headers)
        self.assertEqual(status,303)
        self.assertIn('Set-Cookie',result)

    def test_opaque_and_foreign_form_origins_still_rejected(self):
        for origin in ('null','https://evil.test',f'http://{self.host}.evil.test'):
            with self.subTest(origin=origin):
                status,_,_=self.request('/crm/login','POST',urlencode({'password':'test-owner-passphrase'}),
                                       {'Content-Type':'application/x-www-form-urlencoded','Origin':origin})
                self.assertEqual(status,403)



    def test_explicit_second_group_selection_result_and_no_default(self):
        token=self.login()
        state,_=domain.import_rows(domain.empty_state(),[row(),row(2,audience='Женщины',ir=20)])
        state=domain.decide(state,'PN2','different',domain.identity(state['projects']['PN2']['data']))
        state,_=domain.import_rows(state,[row(3,audience='Покупательницы',ir=40)])
        self.memory.state=state
        _,_,body=self.request()
        self.assertIn('<option value="" selected disabled>',body)
        self.assertIn('ЦА 2',body)
        self.assertIn('Всего волн проекта: 3',body)
        self.assertIn('трекер',body)
        self.assertIn('PN2',body)
        fields=dict(csrf=crm.csrf(token),pn='PN3',action='same',fingerprint=domain.identity(state['projects']['PN3']['data']))
        before=deepcopy(state)
        self.assertEqual(self.post('/crm/decide',fields)[0],400)
        self.assertEqual(self.memory.state,before)
        fields['target']=domain.group_catalog(state)[1]['group'].encode().hex()
        status,headers,_=self.post('/crm/decide',fields)
        self.assertEqual(status,303)
        result=self.request(headers['Location'])[2]
        self.assertIn('PN3 · Отнесено к группе · ЦА 2',result)
        groups=domain.group_catalog(self.memory.state)
        self.assertEqual(groups[1]['summary']['mean'],30)
        self.assertEqual(groups[0]['summary']['count'],1)

    def test_names_require_auth_csrf_and_escape_and_survive_import(self):
        self.memory.state,_=domain.import_rows(domain.empty_state(),[row()])
        g=domain.group_catalog(self.memory.state)[0]
        fields=dict(aorg='100',group=g['group'].encode().hex(),fingerprint=domain.identity(g['meta']),name='<script>Покупатели</script>')
        self.assertEqual(self.post('/crm/rename',fields)[0],401)
        token=self.login()
        self.assertEqual(self.post('/crm/rename',fields)[0],403)
        fields['csrf']=crm.csrf(token)
        status,headers,_=self.post('/crm/rename',fields)
        self.assertEqual(status,303)
        self.assertIn('&lt;script&gt;',self.request(headers['Location'])[2])
        self.assertEqual(self.post('/crm/rename',fields)[0],400)
        self.memory.state,_=domain.import_rows(self.memory.state,[row()])
        body=self.request('/crm?tab=projects')[2]
        self.assertIn('ЦА 1 — &lt;script&gt;Покупатели&lt;/script&gt;',body)
        self.assertNotIn('<script>',body)
        g=domain.group_catalog(self.memory.state)[0]
        fields.update(fingerprint=domain.identity(g['meta']),name='')
        self.assertEqual(self.post('/crm/rename',fields)[0],303)
        self.assertEqual(domain.group_catalog(self.memory.state)[0]['label'],'ЦА 1')

    def test_project_without_ir_and_filter_keeps_type(self):
        token=self.login()
        self.memory.state,_=domain.import_rows(domain.empty_state(),[row(),row(2,ir=0),row(3,aorg='200',ir=0)])
        body=self.request('/crm?tab=projects&q=PN1')[2]
        self.assertIn('трекер',body)
        self.assertIn('Всего волн проекта: 2',body)
        self.memory.state,_=domain.import_rows(self.memory.state,[row(ir=0)])
        body=self.request('/crm?tab=projects')[2]
        self.assertIn('ЦА 1',body)
        self.assertIn('Нет расчетных волн',body)
        self.assertIn('аорг 200',body)
        self.assertIn('Изменить название ЦА',body)

if __name__=='__main__': unittest.main()
