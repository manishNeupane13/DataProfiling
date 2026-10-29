import io
import json
import os
import threading
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import Response
from rapidfuzz.fuzz import ratio

from workbench.core import Store, profile, validate, reconcile, results


def create_app(root=None):
    app = FastAPI(title='Local Data Workbench')
    store = Store(root or os.environ.get('WORKBENCH_DATA', 'local_data'))
    lock = threading.RLock()
    app.state.store = store

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={'detail': str(exc)})

    @app.exception_handler(TypeError)
    async def invalid_type(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={'detail': 'Invalid request value or rule structure'})

    @app.exception_handler(KeyError)
    async def missing_field(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={'detail': 'Missing or unknown field: ' + str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={'detail': 'Resource not found'})

    @app.get('/health')
    def health(): return {'status': 'ok'}

    @app.post('/datasets')
    async def upload(files: list[UploadFile] = File(...)):
        output = []
        for file in files:
            content = await file.read(50 * 1024 * 1024 + 1)
            if len(content) > 50 * 1024 * 1024: raise HTTPException(413, 'Maximum file size is 50 MB')
            try: output.extend(store.ingest(file.filename, content))
            except (ValueError, KeyError, zipfile.BadZipFile, UnicodeError) as exc: raise ValueError('Unable to read file: check format and headers') from exc
        return output

    @app.get('/datasets')
    def datasets(): return [{k:v for k,v in d.items() if k!='records'} for d in store.listing('datasets')]

    @app.get('/datasets/{key}')
    def dataset(key: str):
        d = store.read('datasets', key)
        return {**d, 'records': d['records'][:100]}

    @app.get('/datasets/{key}/profile')
    def profiling(key: str): return profile(store.frame(key))

    @app.post('/mapping-suggestions')
    def suggestions(body: dict):
        a, b = store.frame(body['left_id']), store.frame(body['right_id'])
        return {c: sorted([{'column': d, 'score': ratio(c.casefold(), d.casefold())} for d in b], key=lambda v:v['score'], reverse=True) for c in a}

    @app.post('/mappings')
    def save_mapping(body: dict):
        key = store.save('mappings', body)
        return {'id': key}

    @app.get('/mappings')
    def mappings(): return store.listing('mappings')

    @app.post('/datasets/{key}/validate')
    def validation(key: str, body: dict):
        errors = validate(store.frame(key), body.get('rules', []), store.read('datasets', key))
        store.save('mappings', {'kind':'validation', 'dataset_id':key, 'rules':body.get('rules', []), 'errors':errors})
        return {'errors':errors, 'count':len(errors)}

    @app.post('/runs')
    def matching(body: dict):
        run = reconcile(store.frame(body['left_id']), store.frame(body['right_id']), body)
        run.update(id=uuid4().hex, left_id=body['left_id'], right_id=body['right_id'], created_at=datetime.now(timezone.utc).isoformat())
        store.save('runs', run, run['id'])
        return run

    @app.get('/runs')
    def runs(): return [{'id':r['id'], 'created_at':r['created_at']} for r in store.listing('runs')]

    @app.get('/runs/{key}')
    def run_results(key: str):
        run = store.read('runs', key)
        output = results(run, store.frame(run['left_id']), store.frame(run['right_id']))
        output['rejected_matches'] = [c for c in run['candidates'] if c['status']=='rejected']
        output['invalid_values'] = [e for v in store.listing('mappings') if v.get('kind')=='validation' and v.get('dataset_id') in (run['left_id'],run['right_id']) for e in v['errors']]
        output['possible_outdated_records'] = [dict(c, note='Values differ; verify effective dates. Upload order is not authority.') for c in output['field_conflicts']]
        return output

    @app.post('/runs/{key}/decisions')
    def decision(key: str, body: dict):
        with lock:
            run = store.read('runs', key)
            candidate = next((c for c in run['candidates'] if c['id']==body.get('match_id')), None)
            if candidate is None: raise ValueError('Unknown match')
            status = body.get('status')
            if status not in ('approved', 'rejected', 'pending'): raise ValueError('Invalid status')
            if status == 'approved' and any(c['id']!=candidate['id'] and c['status']=='approved' and (c['left_row']==candidate['left_row'] or c['right_row']==candidate['right_row']) for c in run['candidates']):
                raise ValueError('This record already has an approved match; revoke it first')
            candidate['status'] = status
            run['decisions'].append(dict(match_id=candidate['id'], status=status, note=body.get('note',''), at=datetime.now(timezone.utc).isoformat()))
            store.save('runs', run, key)
        return {'status':status}

    @app.get('/runs/{key}/export')
    def export(key: str, format: str = 'xlsx'):
        run = store.read('runs',key)
        tables = run_results(key)
        tables['profiling'] = [dict(dataset_id=d, **c) for d in (run['left_id'], run['right_id']) for c in profile(store.frame(d))['columns']]
        def frame(rows):
            return pd.DataFrame([{k:json.dumps(v,default=str) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in rows]).map(lambda v: "'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v)
        output = io.BytesIO()
        if format == 'xlsx':
            with pd.ExcelWriter(output,engine='openpyxl') as writer:
                for name, rows in tables.items(): frame(rows).to_excel(writer,sheet_name=name[:31], index=False)
            media='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'; ext='xlsx'
        elif format == 'csv':
            with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                for name,rows in tables.items(): archive.writestr(name+'.csv',frame(rows).to_csv(index=False))
            media='application/zip'; ext='zip'
        else: raise ValueError('Use xlsx or csv')
        content=output.getvalue()
        (store.root/'reports'/f'{key}.{ext}').write_bytes(content)
        return Response(content,media_type=media,headers={'Content-Disposition':f'attachment; filename="report.{ext}"'})

    @app.post('/cleanup')
    def cleanup(body: dict):
        days = float(body.get('older_than_days',30))
        if days < 0: raise ValueError('Retention must be nonnegative')
        cutoff = datetime.now(timezone.utc).timestamp()-days*86400
        count=0
        with lock:
            # Runs and configurations reference datasets, so retention operates on the full workspace.
            for folder in ('runs','mappings','reports','datasets','uploads'):
                for p in (store.root/folder).iterdir():
                    if p.is_file() and p.stat().st_mtime < cutoff:
                        p.unlink(); count+=1
        return {'deleted_files':count}
    return app

app = create_app()
