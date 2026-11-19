import io
import time

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import create_database, migrate
from app.main import create_app
from app.schemas.agent import ExtractionResult

USER = '00000000-0000-4000-8000-000000000001'


class Agent:
    def __init__(self):
        self.requests = []
        self.failure = None
        self.empty = False

    async def extract_tasks(self, request):
        self.requests.append(request)
        if self.failure:
            raise self.failure
        if self.empty:
            return ExtractionResult.model_validate({'tasks': [], 'warnings': [{'code':'NO_TASK_FOUND','message':'没有任务'}]})
        return ExtractionResult.model_validate({'tasks':[{
            'title':'实验报告', 'description':None, 'course_name':'机器学习', 'external_ref':'ML-03',
            'due_kind':'DATE', 'due_date':'2026-11-23', 'due_at':None, 'timezone':request.timezone,
            'requirements':'上传 PDF', 'observed_fields':['title','course_name','external_ref','deadline','requirements'],
            'uncertain_fields':[], 'reason':'通知明确', 'source_excerpt':'实验报告',
        }], 'warnings':[]})

    async def match_tasks(self, request):
        raise AssertionError('identity match should not require model')


class OCR:
    def recognize(self, image_bytes):
        return ('实验报告 机器学习 ML-03 2026-11-23 上传 PDF 手机 13800138000', [])


@pytest.fixture
def setup(tmp_path):
    engine = create_database(f'sqlite:///{tmp_path}/test.db', enforce_sqlite_version=False)
    migrate(engine)
    with engine.begin() as c:
        c.execute(text('INSERT INTO users VALUES (:id,:name,:tz,:now)'), {'id':USER,'name':'演示','tz':'Asia/Shanghai','now':'2026-11-17T00:00:00Z'})
    agent = Agent()
    app = create_app(engine, USER, agent=agent, ocr=OCR())
    with TestClient(app) as client:
        yield client, agent, engine


def test_text_to_review_to_confirmation_and_duplicate(setup):
    client, agent, engine = setup
    raw = '机器学习 实验报告 ML-03 2026-11-23 上传 PDF 手机 13800138000 邮箱 student@example.com'
    created = client.post('/api/v1/imports/text', json={'text':raw,'source_time':'2026-11-17T16:00:00+08:00'})
    assert created.status_code == 201, created.text
    data = created.json()
    assert data['status'] == 'READY' and data['reference_time'] == '2026-11-17T08:00:00Z'
    assert '13800138000' not in data['sanitized_text'] and 'student@example.com' not in data['sanitized_text']
    assert 'PRIVACY_RULE_APPLIED' in [w['code'] for w in data['warnings']]
    iid=data['id']
    analyzed=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1})
    assert analyzed.status_code == 200, analyzed.text
    detail=analyzed.json()
    assert detail['status']=='REVIEW_READY' and detail['version']==3 and detail['pending_count']==1
    assert detail['items'][0]['suggested_action']=='CREATE'
    assert '13800138000' not in agent.requests[0].sanitized_text
    assert client.get('/api/v1/tasks').json()['total']==0
    item=detail['items'][0]
    confirmed=client.post(f'/api/v1/imports/{iid}/confirm',headers={'Idempotency-Key':'one'},json={'expected_import_version':3,'items':[{'draft_item_id':item['id'],'action':'CREATE','final_task':item['proposed_task']}]})
    assert confirmed.status_code == 200, confirmed.text
    after=client.get(f'/api/v1/imports/{iid}').json()
    assert after['status']=='COMPLETED' and after['items'][0]['final_task']['title']=='实验报告'
    second=client.post('/api/v1/imports/text',json={'text':raw}).json()
    repeat=client.post(f"/api/v1/imports/{second['id']}/analyze",json={'expected_version':1})
    assert repeat.status_code==200 and repeat.json()['items'][0]['suggested_action']=='DUPLICATE'
    with engine.connect() as conn:
        assert conn.execute(text('select count(*) from tasks')).scalar_one()==1
        assert '13800138000' not in conn.execute(text('select sanitized_text from imports where id=:id'), {'id':iid}).scalar_one()


def test_image_input_validation_and_no_original_stored(setup):
    client, _, engine = setup
    output=io.BytesIO()
    Image.new('RGB',(20,20),'white').save(output,format='PNG')
    response=client.post('/api/v1/imports/image',files={'file':('screenshot.png',output.getvalue(),'image/png')})
    assert response.status_code==201, response.text
    assert response.json()['input_type']=='IMAGE' and '13800138000' not in response.text
    assert client.post('/api/v1/imports/image',files={'file':('bad.png',b'not an image','image/png')}).status_code==422
    assert client.post('/api/v1/imports/image',files={'file':('a.txt',b'text','text/plain')}).status_code==415
    with engine.connect() as conn:
        assert conn.execute(text('select count(*) from imports')).scalar_one()==1


def test_analysis_failure_and_retry(setup):
    client, agent, engine=setup
    item=client.post('/api/v1/imports/text',json={'text':'实验报告'}).json()
    iid=item['id']
    agent.failure=ConnectionError('sensitive upstream detail')
    failed=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1})
    assert failed.status_code==502 and failed.json()['error']['code']=='LLM_UPSTREAM_ERROR'
    assert 'sensitive upstream detail' not in failed.text
    detail=client.get(f'/api/v1/imports/{iid}').json()
    assert detail['status']=='FAILED' and detail['version']==3 and detail['items']==[]
    with engine.connect() as conn:
        assert conn.execute(text('select count(*) from tasks')).scalar_one()==0
    agent.failure=None
    retry=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':3})
    assert retry.status_code==200 and retry.json()['status']=='REVIEW_READY' and retry.json()['version']==5


def test_empty_result_and_reanalysis_is_idempotent(setup):
    client, agent, _=setup
    agent.empty=True
    iid=client.post('/api/v1/imports/text',json={'text':'普通通知'}).json()['id']
    result=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1})
    assert result.status_code==200 and result.json()['status']=='COMPLETED' and result.json()['items']==[]
    assert 'NO_TASK_FOUND' in [w['code'] for w in result.json()['warnings']]
    assert client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1}).status_code==200
    assert len(agent.requests)==1


def test_privacy_filter_does_not_remove_task_number_or_dates():
    from app.services.privacy_filter import PrivacyFilter
    value='学号：2026123456，手机13800138000，邮箱 a@example.com，身份证 440101199001011234。任务 ML-03，2026-11-23。'
    filtered, changed=PrivacyFilter().apply(value)
    assert changed and '2026123456' not in filtered and '13800138000' not in filtered and 'a@example.com' not in filtered and '440101199001011234' not in filtered
    assert 'ML-03' in filtered and '2026-11-23' in filtered

def test_analysis_timeout_marks_failed_and_leaves_no_drafts(setup):
    import asyncio
    client, agent, engine = setup
    async def slow(request):
        await asyncio.sleep(1)
    agent.extract_tasks=slow
    client.app.state.analysis.timeout=0.02
    iid=client.post('/api/v1/imports/text',json={'text':'实验报告'}).json()['id']
    response=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1})
    assert response.status_code==504 and response.json()['error']['code']=='ANALYSIS_TIMEOUT'
    detail=client.get(f'/api/v1/imports/{iid}').json()
    assert detail['status']=='FAILED' and detail['last_error']['code']=='ANALYSIS_TIMEOUT'
    with engine.connect() as conn:
        assert conn.execute(text('select count(*) from draft_items')).scalar_one()==0


def test_concurrent_analyze_returns_conflict(setup):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor
    client, agent, _=setup
    started=threading.Event()
    release=threading.Event()
    previous=agent.extract_tasks
    async def slow(request):
        started.set()
        await asyncio.to_thread(release.wait, 3)
        return await previous(request)
    agent.extract_tasks=slow
    iid=client.post('/api/v1/imports/text',json={'text':'实验报告'}).json()['id']
    url=f'/api/v1/imports/{iid}/analyze'
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending=executor.submit(client.post,url,json={'expected_version':1})
        assert started.wait(2)
        conflict=client.post(url,json={'expected_version':1})
        assert conflict.status_code==409 and conflict.json()['error']['code']=='ANALYSIS_IN_PROGRESS'
        release.set()
        assert pending.result(timeout=3).status_code==200


def test_recover_interrupted_analysis_at_startup(tmp_path):
    engine=create_database(f'sqlite:///{tmp_path}/db.sqlite',enforce_sqlite_version=False)
    migrate(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users VALUES (:id,'demo','Asia/Shanghai','2026-11-17T00:00:00Z')"), {'id':USER})
        c.execute(text("INSERT INTO imports (id,user_id,input_type,sanitized_text,reference_time,timezone,status,version,created_at,updated_at) VALUES ('00000000-0000-4000-8000-000000000101',:u,'TEXT','safe','2026-11-17T00:00:00Z','Asia/Shanghai','ANALYZING',2,'2026-11-17T00:00:00Z','2026-11-17T00:00:00Z')"),{'u':USER})
    with TestClient(create_app(engine, USER, agent=Agent(), ocr=OCR())) as client:
        response=client.get('/api/v1/imports/00000000-0000-4000-8000-000000000101')
        assert response.json()['status']=='FAILED' and response.json()['version']==3
        assert response.json()['last_error']['code']=='PROCESS_INTERRUPTED'


def test_long_text_gets_payload_too_large(setup):
    client, _, _=setup
    response=client.post('/api/v1/imports/text',json={'text':'a'*20001})
    assert response.status_code==413 and response.json()['error']['code']=='INPUT_TOO_LARGE'


def test_queued_ocr_timeout_does_not_start_later(setup):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    client, _, _=setup
    started=threading.Event()
    release=threading.Event()
    calls=[]
    class SlowOCR:
        def recognize(self, data):
            calls.append(1)
            started.set()
            release.wait(2)
            return ('实验报告', [])
    client.app.state.ocr=SlowOCR()
    client.app.state.ocr_timeout=0.08
    out=io.BytesIO()
    Image.new('RGB',(10,10),'white').save(out,format='PNG')
    files={'file':('image.png',out.getvalue(),'image/png')}
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending=executor.submit(client.post,'/api/v1/imports/image',files=files)
        assert started.wait(1)
        second=client.post('/api/v1/imports/image',files=files)
        assert second.status_code==502 and pending.result(timeout=2).status_code==502
        release.set()
        time.sleep(0.2)
    assert len(calls)==1

def test_database_write_failure_during_analysis_is_not_reported_as_model_error(setup, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app.services import analysis_service
    client, _, _=setup
    iid=client.post('/api/v1/imports/text',json={'text':'实验报告'}).json()['id']
    def broken(*args):
        raise OperationalError('write', {}, Exception('database is locked'))
    monkeypatch.setattr(analysis_service, '_finish', broken)
    response=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1})
    assert response.status_code==503 and response.json()['error']['code']=='DATABASE_BUSY'
    detail=client.get(f'/api/v1/imports/{iid}').json()
    assert detail['status']=='FAILED' and detail['last_error']['code']=='DATABASE_BUSY'

def test_import_detail_does_not_require_write_lock(setup):
    client, _, engine=setup
    iid=client.post('/api/v1/imports/text',json={'text':'实验报告'}).json()['id']
    with engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        response=client.get(f'/api/v1/imports/{iid}')
        connection.rollback()
    assert response.status_code==200 and response.json()['status']=='READY'

def test_image_analysis_to_user_confirmation(setup):
    client, agent, _ = setup
    output=io.BytesIO()
    Image.new('RGB',(20,20),'white').save(output,format='PNG')
    created=client.post('/api/v1/imports/image',files={'file':('image.png',output.getvalue(),'image/png')})
    iid=created.json()['id']
    analyzed=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1})
    assert analyzed.status_code==200 and analyzed.json()['items'][0]['suggested_action']=='CREATE'
    assert '13800138000' not in agent.requests[0].sanitized_text
    item=analyzed.json()['items'][0]
    saved=client.post(f'/api/v1/imports/{iid}/confirm',headers={'Idempotency-Key':'image-one'},json={'expected_import_version':3,'items':[{'draft_item_id':item['id'],'action':'CREATE','final_task':item['proposed_task']}]})
    assert saved.status_code==200 and client.get('/api/v1/tasks').json()['total']==1


def test_invalid_agent_output_fails_without_partial_drafts(setup):
    client, agent, engine=setup
    async def invalid(request):
        return {'tasks':[{'title':'invalid'}],'warnings':[]}
    agent.extract_tasks=invalid
    iid=client.post('/api/v1/imports/text',json={'text':'实验报告'}).json()['id']
    response=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1})
    assert response.status_code==502 and response.json()['error']['code']=='LLM_INVALID_OUTPUT'
    detail=client.get(f'/api/v1/imports/{iid}').json()
    assert detail['status']=='FAILED' and detail['items']==[]
    with engine.connect() as conn:
        assert conn.execute(text('select count(*) from draft_items')).scalar_one()==0


def test_ready_and_input_version_validation(setup):
    client, _, _=setup
    page=client.get('/')
    assert page.status_code == 200 and 'Deadline Keeper 测试台' in page.text
    assert client.get('/health/live').json()=={'status':'ok'}
    assert client.get('/health/ready').json()=={'status':'ready'}
    created=client.post('/api/v1/imports/text',json={'text':'实验报告','source_time':'2026-11-17T08:00:00'})
    assert created.status_code==422
    iid=client.post('/api/v1/imports/text',json={'text':'实验报告'}).json()['id']
    conflict=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':2})
    assert conflict.status_code==409 and conflict.json()['error']['code']=='IMPORT_VERSION_CONFLICT'
    assert client.get('/api/v1/imports/00000000-0000-4000-8000-000000000999').status_code==404

def test_full_flow_with_real_agent_adapter_and_mock_http(setup):
    import asyncio
    import json
    import httpx
    from app.adapters.agent_adapter import AgentAdapter, AgentSettings
    client, _, _ = setup
    seen=[]
    def respond(request):
        body=json.loads(request.content)
        data=json.loads(body['messages'][1]['content'])
        seen.append(data)
        if 'sanitized_text' in data:
            assert '13800138000' not in data['sanitized_text']
            result={'tasks':[{'title':'实验报告','description':None,'course_name':'机器学习','external_ref':None,'due_kind':'NONE','due_date':None,'due_at':None,'timezone':'Asia/Shanghai','requirements':None,'observed_fields':['title','course_name'],'uncertain_fields':[],'reason':'课程通知','source_excerpt':'实验报告'}],'warnings':[]}
        else:
            candidate=data['items'][0]['candidate_tasks'][0]
            assert '13800138000' not in candidate['requirements']
            result={'items':[{'draft_item_id':data['items'][0]['draft_item_id'],'relation':'SAME','matched_task_id':candidate['id'],'reason':'同一任务','uncertain_fields':[]}]}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}]})
    adapter=AgentAdapter(AgentSettings('https://model.example/v1','test','model'),transport=httpx.MockTransport(respond))
    client.app.state.analysis.agent=adapter
    first=client.post('/api/v1/imports/text',json={'text':'机器学习实验报告 联系手机 13800138000'}).json()
    analyzed=client.post(f"/api/v1/imports/{first['id']}/analyze",json={'expected_version':1}).json()
    item=analyzed['items'][0]
    final={**item['proposed_task'],'requirements':'联系手机 13800138000'}
    response=client.post(f"/api/v1/imports/{first['id']}/confirm",headers={'Idempotency-Key':'real-adapter'},json={'expected_import_version':3,'items':[{'draft_item_id':item['id'],'action':'CREATE','final_task':final}]})
    assert response.status_code==200
    second=client.post('/api/v1/imports/text',json={'text':'机器学习实验报告'}).json()
    matched=client.post(f"/api/v1/imports/{second['id']}/analyze",json={'expected_version':1})
    assert matched.status_code==200,matched.text
    assert matched.json()['items'][0]['suggested_action']=='DUPLICATE'
    assert len(seen)==3
    asyncio.run(adapter.aclose())

def test_source_time_must_be_iso_datetime_not_numeric_epoch(setup):
    client, _, _=setup
    response=client.post('/api/v1/imports/text',json={'text':'实验报告','source_time':'1795132800'})
    assert response.status_code==422

def test_unexpected_internal_failure_is_not_reported_as_model_failure(setup, monkeypatch):
    from app.services import analysis_service
    client, _, _=setup
    iid=client.post('/api/v1/imports/text',json={'text':'实验报告'}).json()['id']
    def broken(*args):
        raise RuntimeError('internal detail')
    monkeypatch.setattr(analysis_service, '_finish', broken)
    response=client.post(f'/api/v1/imports/{iid}/analyze',json={'expected_version':1})
    assert response.status_code==500 and response.json()['error']['code']=='INTERNAL_ERROR'
    assert 'internal detail' not in response.text

def test_startup_rejects_invalid_timeout_config(tmp_path, monkeypatch):
    engine=create_database(f'sqlite:///{tmp_path}/invalid-config.db',enforce_sqlite_version=False)
    migrate(engine)
    monkeypatch.setenv('OCR_TIMEOUT_SECONDS','0')
    with pytest.raises(RuntimeError, match='OCR_TIMEOUT_SECONDS'):
        with TestClient(create_app(engine, USER, agent=Agent(), ocr=OCR())):
            pass
