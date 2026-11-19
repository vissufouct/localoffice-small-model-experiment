"""Current-revision literal occurrence checks, not intent or visual certification."""
from ..errors import HarnessError
from ..paths import project_path
from ..storage import canonical, digest
from .creation_evidence import inspect, MAX_PACKAGE


def _literal_segments(paragraph, *, page_fields=False):
    """Join adjacent text runs, never match field caches or across field barriers."""
    if not paragraph.get('literal_coverage_safe',False):return []
    segments=[];current='';inside=False
    for event in paragraph['sequence']:
        kind=event['kind']
        if event.get('form')=='complex':
            return []
        if kind=='field_begin':
            if inside or not page_fields or event.get('instruction','').strip() not in ('PAGE','NUMPAGES'):
                return []
            segments.append(current);current='';inside=True
        elif kind=='field_end':
            if not inside:return []
            inside=False
        elif inside:
            continue
        elif kind=='text':current+=event['text']
        elif kind=='tab':current+='\t'
        elif kind in ('break','carriage_return'):current+='\n'
        elif kind=='cached_page_break':continue
        else:return []
    if inside:return []
    segments.append(current)
    return segments


def occurrences(evidence):
    values={role:set() for role in ('paragraph','table_cell','header','footer')}

    def scan(blocks, role):
        for block in blocks:
            if block['kind']=='paragraph':
                values[role].update(_literal_segments(block,page_fields=role in ('header','footer')))
            elif block['kind']=='table':
                if not block.get('literal_coverage_safe',False):continue
                for row in block['rows']:
                    for cell in row:
                        if not cell.get('literal_coverage_safe',False):continue
                        # A selected cell is one exact literal, not a substring in
                        # a larger cell or a paragraph hidden among other content.
                        if len(cell['blocks'])==1 and cell['blocks'][0]['kind']=='paragraph':
                            values['table_cell' if role=='paragraph' else role].update(
                                _literal_segments(cell['blocks'][0],page_fields=False))
                        for nested in cell['blocks']:
                            if nested['kind']=='table':scan([nested],role)
    # Cross-paragraph complex fields have no trustworthy local cache boundary.
    if not evidence['body_descendant_counts']['complex_field_begins']:
        scan(evidence['body'],'paragraph')
    for section in evidence['sections']:
        for role in ('header','footer'):
            for variant in ('default','first','even'):
                story=section.get(variant+'_'+role)
                if story and story.get('active') is True and not story['descendant_counts']['complex_field_begins']:
                    scan(story['blocks'],role)
    return values


def check(store, task_id):
    """Read the active target bytes anew; never trust an earlier binding receipt."""
    from .content_plan import active_plan, resolve
    plan=active_plan(store,task_id)
    if plan is None:return None
    row=store.db.execute('SELECT artifact FROM tasks WHERE id=?',(task_id,)).fetchone()
    target=row['artifact']
    if not target or not target.lower().endswith('.docx'):
        raise HarnessError('content_coverage','An active literal plan requires its Word target.')
    candidate=store.candidate(target)
    if candidate:
        data=store.blob(candidate['candidate_hash'])
    else:
        path=project_path(store.root,target)
        if not path.is_file():raise HarnessError('content_coverage','Planned content has no current document.')
        if path.stat().st_size>MAX_PACKAGE:raise HarnessError('content_coverage','Document exceeds bounded coverage inspection.')
        with path.open('rb') as stream:data=stream.read(MAX_PACKAGE+1)
    evidence=inspect(data);values=occurrences(evidence)
    pending=[];matched=[]
    for item in plan['refs']:
        text=resolve(store,task_id,item['ref'],role=item['role'])
        (matched if text in values[item['role']] else pending).append({'ref':item['ref'],'role':item['role']})
    return {'revision':evidence['revision'],'plan_hash':digest(canonical(plan).encode()),'plan_generation':plan['generation'],
            'basis':'exact current XML literal occurrences; not binding, multiplicity, intent or visual certification',
            'matched':matched,'pending':pending,
            'observed_literal_samples':{
                role:sorted(value for value in values[role] if value and len(value)<=64)[:3]
                for role in {item['role'] for item in pending}}, 
            'limitation':'Only active explicit band variants are observed; inherited stories and unsupported fields are unverified. Activation does not prove rendered page use or complete professional coverage.'}


def require_complete(store, task_id):
    report=check(store,task_id)
    if report is None:return None
    if report['pending']:
        pending=report['pending']
        from .content_plan import resolve
        previews=[]
        for item in pending[:4]:
            text=resolve(store,task_id,item['ref'],role=item['role'])
            previews.append({**item,'preview':text[:80],'truncated':len(text)>80})
        detail={'revision':report['revision'],'pending':previews,'remaining':max(0,len(pending)-4)}
        raise HarnessError('content_coverage','Selected source literals are missing or unverified in the current document. Inspect and correct through registered tools; do not recreate the document or silently drop selected content. '+canonical(detail))
    return report


def feedback(store,task_id,*,max_bytes=640):
    """Bound current coverage for the next model turn; no writes or inferred fix."""
    from .content_plan import resolve
    if type(max_bytes) is not int or max_bytes<256:
        raise ValueError('Coverage feedback requires at least 256 bytes.')
    try:
        report=check(store,task_id)
    except HarnessError as exc:
        if exc.code not in ('content_reference','content_source','content_coverage'):
            raise
        from .content_plan import active_navigation
        navigation=active_navigation(store,task_id)
        result={'status':'unverified','reason':exc.code,'scope':'XML only; no approval.',
                'plan_generation':navigation['generation'] if navigation else None,
                'pending_count':None,'references':[],'truncated':bool(navigation and navigation['truncated'])}
        if navigation:
            for item in navigation['references']:
                entry={'ref':item['ref'],'role':item['role'],'status':'unverified'}
                trial={**result,'references':result['references']+[entry]}
                if len(canonical(trial).encode())>max_bytes:
                    result['truncated']=True;break
                result['references'].append(entry)
        return result
    if report is None:return None
    result={'revision':report['revision'],'plan_generation':report['plan_generation'],'status':'pending' if report['pending'] else 'observed',
            'matched':len(report['matched']),'pending_count':len(report['pending']),
            'scope':'XML only; pending is absent/unverified, not missing fields. Samples are not locators or approval.',
            'pending':[],'truncated':False}
    if report['pending']:
        result['instruction']='Compare expected literal with observed samples, not field values. Read targets before edits; do not ask to repeat supplied text.'
    if len(canonical(result).encode())>max_bytes:
        result.pop('instruction',None)
        result['scope']='XML only; no approval.'
    # The prompt already contains the task and active target. Do not repeat
    # matched source text, entire provenance or potentially stale locators.
    for item in report['pending']:
        text=resolve(store,task_id,item['ref'],role=item['role'])
        entry={**item,'preview':text[:64],'truncated':len(text)>64}
        trial={**result,'pending':result['pending']+[entry]}
        while entry['preview'] and len(canonical(trial).encode())>max_bytes:
            entry['preview']=entry['preview'][:len(entry['preview'])//2]
            entry['truncated']=True
            trial={**result,'pending':result['pending']+[entry]}
        if len(canonical(trial).encode())>max_bytes:
            result['truncated']=True;break
        result['pending'].append(entry)
    # Pending source identities take precedence over optional observed samples.
    # Otherwise the first role's samples can hide a second missing requirement.
    for index, entry in enumerate(result['pending']):
        for sample in report['observed_literal_samples'].get(entry['role'],[]):
            enriched={**entry,'observed_samples':entry.get('observed_samples',[])+[sample]}
            pending=list(result['pending']);pending[index]=enriched
            if len(canonical({**result,'pending':pending}).encode())>max_bytes:break
            entry=enriched
            result['pending'][index]=entry
    if len(canonical(result).encode())>max_bytes:
        result.pop('instruction',None)
        result['scope']='XML only; no approval.'
    return result
