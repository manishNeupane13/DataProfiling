import json
import os
import requests
import streamlit as st
import plotly.express as px
from workbench.client import API

st.set_page_config(page_title='Data Workbench', page_icon='🔎',layout='wide')
st.title('Data profiling & reconciliation')
st.caption('Local files • explicit identity rules • human review')
api=API(os.environ.get('WORKBENCH_API','http://127.0.0.1:8000'))
pages=['Upload & Datasets','Data Profiling','Column Mapping','Data Validation','Matching Configuration','Reconciliation Results','Conflict Review','Export Reports']
page=st.sidebar.radio('Workflow',pages)

def editor(label,default,key):
    return json.loads(st.text_area(label,json.dumps(default,indent=2),height=240,key=key))

try:
    datasets=api.get('/datasets')
    labels={d['id']:f"{d['filename']} / {d['worksheet']} ({d['rows']} rows)" for d in datasets}
    if page=='Upload & Datasets':
        uploads=st.file_uploader('Add CSV or Excel workbooks',type=['csv','xlsx'],accept_multiple_files=True)
        if st.button('Upload files',disabled=not uploads):
            api.call('POST','/datasets',files=[('files',(f.name,f.getvalue())) for f in uploads]); st.rerun()
        st.dataframe(datasets,use_container_width=True)
        if datasets:
            key=st.selectbox('Preview dataset',labels,format_func=labels.get)
            st.dataframe(api.get('/datasets/'+key)['records'],use_container_width=True)
        with st.expander('Storage retention'):
            st.caption('Deletes files older than this age across uploads, datasets, configurations, runs, and reports. Referenced files may become unavailable. Export first.')
            days=st.number_input('Retention days',min_value=0,value=30)
            confirm=st.checkbox('I understand this permanently deletes local workbench files')
            if st.button('Apply retention',disabled=not confirm):
                st.success(api.post('/cleanup',{'older_than_days':days})); st.rerun()
    elif not datasets:
        st.info('Upload datasets to begin.')
    elif page=='Data Profiling':
        key=st.selectbox('Dataset',labels,format_func=labels.get)
        p=api.get(f'/datasets/{key}/profile')
        a,b,c=st.columns(3); a.metric('Rows',p['rows']); b.metric('Columns',p['columns_count']); c.metric('Duplicate rows',p['duplicate_rows'])
        st.dataframe(p['columns'],use_container_width=True)
        st.plotly_chart(px.bar(p['columns'],x='column',y='missing',title='Missing values by column'),use_container_width=True)
        col=st.selectbox('Inspect values',[c['column'] for c in p['columns']])
        detail=next(c for c in p['columns'] if c['column']==col)
        st.plotly_chart(px.bar(detail['frequent_values'],x='value',y='count',title='Frequent values'),use_container_width=True) if detail['frequent_values'] else st.info('No values')
        st.write('Numeric distribution',detail['numeric_distribution'])
    elif page in ('Column Mapping','Matching Configuration'):
        left=st.selectbox('Reference dataset',labels,format_func=labels.get,key='left')
        right=st.selectbox('Comparison dataset',labels,format_func=labels.get,key='right')
        if left==right: st.warning('Choose different datasets.'); st.stop()
        pair=f'{left}:{right}'
        if page=='Column Mapping':
            suggested=api.post('/mapping-suggestions',{'left_id':left,'right_id':right})
            st.caption('Suggestions use column-name similarity. Check every mapping before saving.')
            mapping={}
            for col,options in suggested.items():
                chosen=st.selectbox(col,['(skip)']+[o['column'] for o in options],index=1,key=pair+col)
                if chosen!='(skip)': mapping[col]=chosen
            if st.button('Save mapping'):
                body={'left_id':left,'right_id':right,'mapping':mapping}
                api.post('/mappings',body); st.session_state[pair]=mapping; st.success('Mapping saved')
        else:
            saved=[m for m in api.get('/mappings') if m.get('left_id')==left and m.get('right_id')==right and 'mapping' in m]
            mapping=st.session_state.get(pair,saved[-1]['mapping'] if saved else {})
            if not mapping: st.info('Save a column mapping first.'); st.stop()
            st.write('Mapped columns',mapping)
            identity=st.multiselect('Stable identity attributes',list(mapping))
            st.caption('Use names, license numbers, or other stable attributes. Vehicle and safety assignments are not identity.')
            attrs=[{'column':c,'weight':st.number_input('Weight: '+c,min_value=0.1,value=1.0)} for c in identity]
            mode=st.selectbox('Matching method',['fuzzy','normalized','exact'])
            threshold=st.slider('Candidate threshold',0,100,80)
            margin=st.slider('Ambiguity margin',0,30,5)
            if st.button('Match and reconcile',disabled=not attrs):
                with st.spinner('Comparing records…'):
                    run=api.post('/runs',dict(left_id=left,right_id=right,mapping=mapping,attributes=attrs,mode=mode,threshold=threshold,ambiguity_margin=margin))
                st.session_state['run']=run['id']; st.success('Run created. Open Reconciliation Results or Conflict Review.')
    elif page=='Data Validation':
        key=st.selectbox('Dataset',labels,format_func=labels.get)
        st.caption('Rules support required, type (string/integer/float/boolean/date), regex, allowed, min/max, unique, and compare_to with operator eq/ne/lt/le/gt/ge.')
        columns=next(d['columns'] for d in datasets if d['id']==key)
        advanced=st.checkbox('Advanced JSON rules')
        if advanced:
            rules=editor('Validation rules (JSON)',[{'column':columns[0],'required':True}],key='rules'+key)
        else:
            col=st.selectbox('Column to validate',columns)
            required=st.checkbox('Require a value',value=True)
            unique=st.checkbox('Require unique values')
            kind=st.selectbox('Expected type',['any','string','integer','float','boolean','date'])
            pattern=st.text_input('Regular expression (optional)')
            allowed=st.text_input('Allowed values (comma separated, optional)')
            rule={'column':col,'required':required,'unique':unique}
            if kind!='any': rule['type']=kind
            if pattern: rule['regex']=pattern
            if allowed: rule['allowed']=[v.strip() for v in allowed.split(',')]
            if st.checkbox('Check numeric range'):
                rule['min']=st.number_input('Minimum',value=0.0)
                rule['max']=st.number_input('Maximum',value=100.0)
            if st.checkbox('Compare with another column'):
                rule['compare_to']=st.selectbox('Other column',columns)
                rule['operator']=st.selectbox('Comparison',['eq','ne','lt','le','gt','ge'])
            rules=[rule]
        if st.button('Validate'):
            report=api.post(f'/datasets/{key}/validate',{'rules':rules})
            st.metric('Violations',report['count']); st.dataframe(report['errors'],use_container_width=True)
    else:
        runs=api.get('/runs')
        if not runs: st.info('Create a matching run first.'); st.stop()
        ids=[r['id'] for r in runs]
        key=st.selectbox('Reconciliation run',ids,index=ids.index(st.session_state['run']) if st.session_state.get('run') in ids else len(ids)-1)
        output=api.get('/runs/'+key)
        if page=='Reconciliation Results':
            st.caption('Missing and extra mean no approved match yet; pending candidates may account for them. Neither upload order nor reference role establishes correctness.')
            for label in ('matched_records','possible_matches','missing_records','extra_records','field_conflicts','duplicate_candidates','invalid_values','possible_outdated_records'):
                with st.expander(f"{label.replace('_',' ').title()} ({len(output[label])})"):
                    st.dataframe(output[label],use_container_width=True)
        elif page=='Conflict Review':
            status=st.selectbox('Show candidates',['pending','approved','rejected','all'])
            candidates=output['possible_matches']+output['matched_records']+output['rejected_matches']
            candidates=[c for c in candidates if status=='all' or c['status']==status]
            if not candidates: st.info('No candidates in this view.')
            else:
                c=st.selectbox('Candidate',candidates,format_func=lambda c:f"Rows {c['left_row']} ↔ {c['right_row']} · {c['score']}% · {'ambiguous' if c['ambiguous'] else c['status']}")
                a,b=st.columns(2); a.write('Reference'); a.json(c['left']); b.write('Comparison'); b.json(c['right'])
                note=st.text_input('Review rationale')
                action=st.selectbox('Decision',['approved','rejected','pending'])
                if st.button('Save review decision'):
                    api.post(f'/runs/{key}/decisions',{'match_id':c['id'],'status':action,'note':note}); st.rerun()
            st.dataframe(output['field_conflicts'],use_container_width=True)
            st.dataframe(output['approved_review_decisions'],use_container_width=True)
        else:
            for fmt,label in [('xlsx','Excel workbook'),('csv','CSV report bundle')]:
                if st.button('Generate '+label):
                    response=api.call('GET',f'/runs/{key}/export?format={fmt}')
                    st.download_button('Download '+label,response.content,file_name='reconciliation.'+('xlsx' if fmt=='xlsx' else 'zip'))
except (requests.RequestException,ValueError,KeyError) as exc:
    if isinstance(exc,requests.HTTPError): st.error(exc.response.text)
    else: st.error(f'Unable to complete action: {exc}')
