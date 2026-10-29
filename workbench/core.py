import io
import math
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pandera.pandas as pa
from rapidfuzz.fuzz import ratio


def clean(value):
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def records(df):
    return json.loads(df.to_json(orient='records', date_format='iso'))


class Store:
    def __init__(self, root):
        self.root = Path(root)
        for folder in ('uploads', 'datasets', 'mappings', 'runs', 'reports'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    def path(self, folder, key):
        if not re.fullmatch(r'[a-f0-9]{32}', key):
            raise ValueError('Invalid identifier')
        return self.root / folder / f'{key}.json'

    def save(self, folder, data, key=None):
        key = key or uuid4().hex
        target = self.path(folder, key)
        fd, tmp = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out, default=str, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return key

    def read(self, folder, key):
        return json.loads(self.path(folder, key).read_text())

    def listing(self, folder):
        return [json.loads(p.read_text()) for p in sorted((self.root / folder).glob('*.json'))]

    def frame(self, key):
        return pd.DataFrame(self.read('datasets', key)['records'])

    def ingest(self, filename, content):
        suffix = Path(filename).suffix.lower()
        if suffix not in ('.csv', '.xlsx'):
            raise ValueError('Only CSV and XLSX are supported')
        sheets = {'CSV': pd.read_csv(io.BytesIO(content), dtype=object)} if suffix == '.csv' else pd.read_excel(io.BytesIO(content), sheet_name=None, dtype=object)
        upload_id = uuid4().hex
        (self.root / 'uploads' / (upload_id + suffix)).write_bytes(content)
        result = []
        for sheet, df in sheets.items():
            df.columns = [str(c) for c in df.columns]
            key = uuid4().hex
            entry = dict(id=key, upload_id=upload_id, filename=Path(filename).name, worksheet=sheet,
                         created_at=datetime.now(timezone.utc).isoformat(), rows=len(df), columns=list(df.columns),
                         dtypes={c: str(pd.api.types.infer_dtype(df[c].dropna())) for c in df}, records=records(df))
            self.save('datasets', entry, key)
            result.append({k: v for k, v in entry.items() if k != 'records'})
        return result


def profile(df):
    columns = []
    for c in df:
        s = df[c].dropna()
        numeric = pd.to_numeric(s, errors='coerce').replace([float('inf'),float('-inf')],float('nan')).dropna()
        kinds = set('number' if re.fullmatch(r'-?\d+(\.\d+)?', str(v)) else 'text' for v in s)
        date_shapes = set(re.sub(r'\d', 'D', str(v)) for v in s if re.search(r'\d[/-]\d', str(v)))
        columns.append(dict(column=c, missing=int(df[c].isna().sum()), unique=int(s.nunique()),
                            duplicate_values=int(s.duplicated().sum()), dtype=str(pd.api.types.infer_dtype(s)),
                            mixed_types=len(kinds) > 1, date_format_inconsistencies=len(date_shapes) > 1,
                            potential_identifier=bool(len(s) == len(df) and len(s) and s.is_unique),
                            frequent_values=[{'value': str(k), 'count': int(v)} for k, v in s.value_counts().head(10).items()],
                            numeric_distribution={k: float(v) for k, v in numeric.describe().items() if pd.notna(v)} if len(numeric) else {}))
    return dict(rows=len(df), columns_count=len(df.columns), duplicate_rows=int(df.duplicated().sum()), columns=columns)


def validate(df, rules, metadata):
    errors = []
    def add(i, c, value, rule):
        errors.append(dict(file=metadata['filename'], worksheet=metadata['worksheet'], row=int(i)+2,
                           column=c, value=None if pd.isna(value) else str(value), rule=rule))
    for rule in rules:
        c = rule['column']
        if c not in df:
            add(-1, c, None, 'column_missing')
            continue
        s = df[c]
        checks = []
        if rule.get('required'):
            checks.append(pa.Check(lambda x: x.notna() & x.astype(str).str.strip().ne(''), name='required', ignore_na=False))
        if rule.get('unique'):
            checks.append(pa.Check(lambda x: ~x.duplicated(keep=False), name='unique'))
        if 'regex' in rule:
            try: re.compile(rule['regex'])
            except re.error as exc: raise ValueError('Invalid regular expression') from exc
            checks.append(pa.Check.str_matches(rule['regex']))
        if 'allowed' in rule:
            checks.append(pa.Check.isin(rule['allowed']))
        if 'min' in rule or 'max' in rule:
            lo, hi = rule.get('min', float('-inf')), rule.get('max', float('inf'))
            checks.append(pa.Check(lambda x: pd.to_numeric(x, errors='coerce').between(lo, hi), name='numeric_range'))
        kind = rule.get('type')
        if kind:
            def valid(v):
                if pd.isna(v): return True
                text = str(v).strip()
                if kind == 'string': return isinstance(v, str)
                if kind == 'integer': return bool(re.fullmatch(r'[+-]?\d+', text))
                if kind == 'float': return pd.notna(pd.to_numeric(text, errors='coerce'))
                if kind == 'boolean': return text.lower() in ('true', 'false', '0', '1')
                if kind == 'date': return pd.notna(pd.to_datetime(text, errors='coerce'))
                raise ValueError('Unknown validation type')
            checks.append(pa.Check(lambda x: x.map(valid), name='type_' + kind))
        try:
            pa.DataFrameSchema({c: pa.Column(checks=checks, nullable=True, required=True)}).validate(df, lazy=True)
        except pa.errors.SchemaErrors as exc:
            for failure in exc.failure_cases.to_dict('records'):
                i = failure['index']
                if pd.notna(i): add(i, c, s.loc[i], str(failure['check']))
        if 'compare_to' in rule:
            other = rule['compare_to']
            if other not in df: raise ValueError('Cross-column target is missing')
            op = rule.get('operator', 'eq')
            if op not in ('eq', 'ne', 'lt', 'le', 'gt', 'ge'): raise ValueError('Invalid comparison operator')
            a, b = s, df[other]
            if op in ('lt', 'le', 'gt', 'ge'):
                a, b = pd.to_numeric(a, errors='coerce'), pd.to_numeric(b, errors='coerce')
            mask = getattr(a, op)(b) & a.notna() & b.notna()
            for i in df.index[~mask]: add(i, c, s.loc[i], f'{op}:{other}')
    return errors


def normalize(v):
    if v is None: return ''
    return ' '.join(re.sub(r'[^\w\s]', '', unicodedata.normalize('NFKC', str(v)).casefold()).split())


def reconcile(left, right, config):
    mapping = config['mapping']
    attrs = config['attributes']
    if len(set(mapping.values())) != len(mapping): raise ValueError('Map each comparison column only once')
    if not attrs: raise ValueError('Select at least one identity attribute')
    for a in attrs:
        if a['column'] not in mapping: raise ValueError('Identity attribute must be mapped')
        if not math.isfinite(float(a.get('weight', 1))) or float(a.get('weight', 1)) <= 0: raise ValueError('Weights must be positive')
        if re.search(r'vehicle|truck|safety|supervisor|officer|assignment', a['column'], re.I):
            raise ValueError('Changing assignments cannot be identity attributes')
    if any(c not in left or d not in right for c, d in mapping.items()): raise ValueError('Mapped column is missing')
    threshold = config.get('threshold', 80)
    if not 0 <= threshold <= 100: raise ValueError('Threshold must be between 0 and 100')
    candidates = []
    mode = config.get('mode', 'fuzzy')
    if mode not in ('exact', 'normalized', 'fuzzy'): raise ValueError('Invalid match mode')
    for i, a in enumerate(records(left)):
        for j, b in enumerate(records(right)):
            scores = []
            for attr in attrs:
                c = attr['column']; x, y = a[c], b[mapping[c]]
                if mode != 'exact': x, y = normalize(x), normalize(y)
                score = 0 if x in (None, '') or y in (None, '') else (ratio(str(x), str(y)) if mode == 'fuzzy' else 100 * (x == y))
                scores.append((score, float(attr.get('weight', 1))))
            score = sum(s*w for s,w in scores) / sum(w for _,w in scores)
            if score >= threshold:
                candidates.append(dict(id=f'{i}:{j}', left_row=i+2, right_row=j+2, score=round(score, 2), left=a, right=b))
    for c in candidates:
        rivals = [d for d in candidates if d['id'] != c['id'] and (c['left_row']==d['left_row'] or c['right_row']==d['right_row'])]
        c['ambiguous'] = any(d['score'] >= c['score'] - config.get('ambiguity_margin', 5) for d in rivals)
        c['status'] = 'approved' if mode in ('exact', 'normalized') and c['score']==100 and not rivals else 'pending'
    return dict(candidates=candidates, decisions=[], config=config)


def results(run, left, right):
    approved = [c for c in run['candidates'] if c['status']=='approved']
    l_used = {c['left_row'] for c in approved}; r_used = {c['right_row'] for c in approved}
    conflicts = []
    for c in approved:
        for a,b in run['config']['mapping'].items():
            if normalize(c['left'][a]) != normalize(c['right'][b]):
                conflicts.append(dict(match_id=c['id'], left_row=c['left_row'], right_row=c['right_row'], column=a,
                                      left_value=c['left'][a], right_value=c['right'][b], category='conflicting_assignment' if re.search('vehicle|truck|officer|supervisor',a,re.I) else 'field_mismatch'))
    return dict(matched_records=approved, possible_matches=[c for c in run['candidates'] if c['status']=='pending'],
                missing_records=[dict(row=i+2, **r) for i,r in enumerate(records(left)) if i+2 not in l_used],
                extra_records=[dict(row=i+2, **r) for i,r in enumerate(records(right)) if i+2 not in r_used],
                field_conflicts=conflicts, duplicate_candidates=[c for c in run['candidates'] if c['ambiguous']],
                approved_review_decisions=run['decisions'])
