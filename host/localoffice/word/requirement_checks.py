"""Compare declared requirements to a resolved creation request, never parse intent."""
from decimal import Decimal
from ..errors import HarnessError
from ..storage import canonical,digest
from .contracts import CreateDocument
from .content_plan import source,_plan_owner
from . import requirements as plans


def _bounded(status,items=(),**metadata):
    result={'status':status,'coverage':'unassessed','interpretation':'model_selected',
            **metadata,'items':[],'omitted_count':len(items)}
    for item in items:
        trial={**result,'items':result['items']+[item],'omitted_count':result['omitted_count']-1}
        if len(canonical(trial).encode())<=640:result=trial
    return result


def check(store,task_id,document):
    """Return <=640 UTF-8 bytes; all-selected pass never means full acceptance."""
    with store.transaction():
        owner=_plan_owner(store,task_id)
        request=CreateDocument.model_validate(document)
        if request.path!=owner['target']:
            raise HarnessError('requirements_scope','Requirement check targets another artifact.')
        state=plans.get(store,task_id)
        if state is None:return _bounded('unassessed',count=0)
        declared=state['request']['requirements']
        metadata={'generation':state['generation'],'count':len(declared)}
        _,_,current=source(store,task_id)
        if current!=state['request']['source_hash']:
            return _bounded('stale',**metadata)
        if not declared:return _bounded('unassessed',**metadata)
        paragraphs=[]
        for i,block in enumerate(request.blocks):
            if block.kind=='paragraph':
                preceding=i>0 and request.blocks[i-1].kind=='page_break'
                paragraphs.append((block,block.page_break_before or preceding,i))
        styles={style.id:style for style in request.styles}
        items=[]
        for index,entry in enumerate(declared):
            kind=entry['kind'];item={'index':index,'kind':kind}
            actual=None;expected=None
            if kind=='page_geometry':
                actual=getattr(request.page,entry['field']+'_twips')
                expected=plans.twips(entry['equals'],entry['unit'])
                item.update(field=entry['field'],path='page.'+entry['field']+'_twips',unit='twip')
            else:
                ordinal=entry['paragraph_index'];item['paragraph_index']=ordinal
                if ordinal>=len(paragraphs):
                    items.append({**item,'status':'unverified','reason':'paragraph_missing'});continue
                paragraph,effective_break,block_index=paragraphs[ordinal]
                item['block_index']=block_index
                if kind=='paragraph_words':
                    item['path']=f'blocks.{block_index}.text'
                    if not isinstance(paragraph.text,str):
                        items.append({**item,'status':'unverified','reason':'text_unresolved'});continue
                    actual=len(paragraph.text.split());expected={'min':entry['min'],'max':entry['max']}
                    item['basis']='unicode_whitespace_split'
                elif kind=='paragraph_page_break':actual=effective_break;expected=entry['equals']
                else:
                    style=styles.get(paragraph.style)
                    if style is None and paragraph.style!='Normal':
                        items.append({**item,'status':'unverified','reason':'style_unresolved'});continue
                    actual=(style.size_points if style and style.size_points is not None else
                            style.size_half_points/2 if style and style.size_half_points is not None else 11)
                    expected=entry['equals'];item['unit']='pt'
            passed=(expected['min']<=actual<=expected['max'] if kind=='paragraph_words'
                    else actual is expected if kind=='paragraph_page_break'
                    else Decimal(str(actual))==Decimal(str(expected)))
            items.append({**item,'status':'pass' if passed else 'mismatch','expected':expected,'actual':actual})
        failures=[item for item in items if item['status']!='pass']
        status=('mismatch' if any(item['status']=='mismatch' for item in failures)
                else 'unverified' if failures else 'all_selected_pass')
        # Keep all selected checks in the immutable plan. When failures exist,
        # focus correction evidence on those; matched still counts successes.
        ordered=failures if failures else items
        return _bounded(status,ordered,**metadata,matched=len(items)-len(failures),
                        failed=len(failures),items_scope='failures' if failures else 'selected',
                        quantity_rule='nearest_twip_half_up')


def require(store,task_id,document):
    evidence=check(store,task_id,document)
    if evidence['status'] in ('stale','mismatch','unverified'):
        raise HarnessError('requirements_'+evidence['status'],
            'Selected requirements are not verified as satisfied. Review expected/actual facts; explicitly revise the proposal or requirement plan.',
            recovery=evidence)
    return evidence


def check_prepared(store,task_id,payload):
    """Read-only preview; never mistake an unready proposal for a checked pass."""
    from pydantic import ValidationError
    from .content_plan import bind_creation
    with store.transaction():
        state=plans.get(store,task_id)
        if state is None:return None
        declared=state['request']['requirements']
        metadata={'generation':state['generation'],'count':len(declared)}
        _,_,current=source(store,task_id)
        if current!=state['request']['source_hash']:return _bounded('stale',**metadata)
        if not declared:return None
        owner=_plan_owner(store,task_id)
        path=payload.get('path') if isinstance(payload,dict) else getattr(payload,'path',None)
        if path!=owner['target']:
            raise HarnessError('requirements_scope','Requirement preview targets another artifact.')
        try:
            request=CreateDocument.model_validate(payload)
            resolved,_=bind_creation(store,task_id,request)
        except ValidationError:
            return _bounded('unverified',**metadata,reason='current_proposal_not_ready')
        except HarnessError as exc:
            if exc.code not in ('content_reference','content_source','content_conflict','content_slot'):
                raise
            return _bounded('unverified',**metadata,reason='current_proposal_not_ready')
        return check(store,task_id,resolved)
