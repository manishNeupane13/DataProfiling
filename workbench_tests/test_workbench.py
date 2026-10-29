import io
from pathlib import Path
import json
import threading
import socket
import zipfile
import pandas as pd
import pytest
import uvicorn
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
from workbench.api import create_app
from workbench.core import profile, validate, reconcile
from workbench.client import API
from workbench.sample_data import generate

@pytest.fixture
def client(tmp_path): return TestClient(create_app(tmp_path/'data'))

def upload(client, name, text):
    return client.post('/datasets',files={'files':(name,text)}).json()[0]['id']

def test_ingestion_profiles_and_validation(client,tmp_path):
    generate(tmp_path)
    response=client.post('/datasets',files={'files':('history.xlsx',(tmp_path/'historical_2024.xlsx').read_bytes())})
    assert response.status_code==200 and len(response.json())==2
    key=response.json()[0]['id']
    p=client.get(f'/datasets/{key}/profile').json()
    assert p['duplicate_rows']==1
    assert p['columns'][0]['missing']==1
    assert next(c for c in p['columns'] if c['column']=='Start Date')['date_format_inconsistencies']
    errors=client.post(f'/datasets/{key}/validate',json={'rules':[{'column':'Driver Name','required':True,'unique':True},{'column':'License','regex':'^IL[0-9]{5}$'}]}).json()['errors']
    assert errors and all({'file','worksheet','row','column','value','rule'} <= e.keys() for e in errors)
    assert client.post('/datasets',files={'files':('x.exe',b'bad')}).status_code==400
    assert client.get('/datasets/not-an-id').status_code==400

def test_rules():
    df=pd.DataFrame({'n':['1','bad','3'],'end':['2','1','2'],'flag':['true','false','oops'],'date':['2024-01-01','bad',None]})
    errors=validate(df,[{'column':'n','type':'integer','min':2,'max':4,'compare_to':'end','operator':'le'}, {'column':'flag','type':'boolean','allowed':['true','false']},{'column':'date','type':'date','required':True}],{'filename':'a.csv','worksheet':'CSV'})
    assert len(errors)>=7
    p=profile(df)
    assert p['columns'][0]['mixed_types']


def config(a,b,mode='fuzzy'):
    return dict(left_id=a,right_id=b,mapping={'Name':'Full Name','Truck':'Vehicle'},attributes=[{'column':'Name','weight':1}],mode=mode,threshold=70)

def test_matching_review_export(client):
    a=upload(client,'a.csv',b'Name,Truck\nMaria Lopez,T1\nAisha Patel,T2\nAisha Patel,T3\nLost Person,T4\n')
    b=upload(client,'b.csv',b'Full Name,Vehicle\nMaria Lopez,T9\nAisha Patel,T2\nNew Person,T5\n')
    run=client.post('/runs',json=config(a,b)).json(); key=run['id']
    assert all(c['status']=='pending' for c in run['candidates'])
    assert any(c['ambiguous'] for c in run['candidates'])
    maria=next(c for c in run['candidates'] if c['left_row']==2)
    assert client.post(f'/runs/{key}/decisions',json={'match_id':maria['id'],'status':'approved','note':'Verified license'}).status_code==200
    output=client.get('/runs/'+key).json()
    assert len(output['matched_records'])==1 and output['field_conflicts'][0]['category']=='conflicting_assignment'
    assert len(output['missing_records'])==3 and len(output['extra_records'])==2
    aishas=[c for c in run['candidates'] if c['right_row']==3]
    client.post(f'/runs/{key}/decisions',json={'match_id':aishas[0]['id'],'status':'approved'})
    assert client.post(f'/runs/{key}/decisions',json={'match_id':aishas[1]['id'],'status':'approved'}).status_code==400
    excel=client.get(f'/runs/{key}/export').content
    sheets=pd.read_excel(io.BytesIO(excel),sheet_name=None)
    assert {'profiling','matched_records','field_conflicts'} <= sheets.keys()
    zipped=client.get(f'/runs/{key}/export?format=csv').content
    assert 'possible_matches.csv' in zipfile.ZipFile(io.BytesIO(zipped)).namelist()
    bad=config(a,b); bad['attributes']=[{'column':'Truck'}]
    assert client.post('/runs',json=bad).status_code==400


def test_exact_normalized_and_weighted():
    a=pd.DataFrame({'Name':[' MARIA! '],'License':['A1']}); b=pd.DataFrame({'Full Name':['maria'],'ID':['A1']})
    c=dict(mapping={'Name':'Full Name','License':'ID'},attributes=[{'column':'Name','weight':1},{'column':'License','weight':3}],threshold=70,mode='normalized')
    assert reconcile(a,b,c)['candidates'][0]['status']=='approved'
    c['mode']='fuzzy'; assert reconcile(a,b,c)['candidates'][0]['status']=='pending'
    c['mode']='exact'; assert reconcile(a,b,c)['candidates'][0]['score']==75


def test_http_and_streamlit(tmp_path,monkeypatch):
    app=create_app(tmp_path/'data')
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); thread.start()
    import time
    for _ in range(100):
        if server.started: break
        time.sleep(.05)
    try:
        base=f'http://127.0.0.1:{port}'; monkeypatch.setenv('WORKBENCH_API',base)
        api=API(base)
        assert api.get('/health')['status']=='ok'
        api.call('POST','/datasets',files={'files':('a.csv',b'Name,Truck\nMaria,T1\n')})
        assert len(api.get('/datasets'))==1
        at=AppTest.from_file(Path(__file__).resolve().parents[1]/'workbench'/'ui.py').run(timeout=30)
        assert not at.exception
        at.sidebar.radio[0].set_value('Data Profiling').run(timeout=30)
        assert not at.exception and at.metric[0].value=='1'
    finally:
        server.should_exit=True; thread.join(timeout=10); sock.close()


def test_reopen_persistence_cleanup_and_bad_requests(client):
    a=upload(client,'a.csv',b'Name,Truck\nMaria,T1\n')
    b=upload(client,'b.csv',b'Full Name,Vehicle\nMaria,T2\n')
    run=client.post('/runs',json=config(a,b)).json(); key=run['id']
    c=run['candidates'][0]
    for status in ['rejected','pending','approved']:
        assert client.post(f'/runs/{key}/decisions',json={'match_id':c['id'],'status':status}).status_code==200
    persisted=client.app.state.store.read('runs',key)
    assert len(persisted['decisions'])==3
    assert client.post(f'/datasets/{a}/validate',json={'rules':[{'column':'Name','regex':'['}]}).status_code==400
    assert client.post('/runs',json={}).status_code==400
    assert client.get('/datasets/'+'a'*32).status_code==404
    assert client.post('/cleanup',json={'older_than_days':-1}).status_code==400
    assert client.post('/cleanup',json={'older_than_days':0}).json()['deleted_files']>0
    assert client.get('/datasets').json()==[]
