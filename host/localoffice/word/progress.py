"""Bounded projections of real Word receipts, independent of disposable chat history."""
import json
from ..storage import canonical


def without_nulls(value):
    if isinstance(value,dict):return {k:without_nulls(v) for k,v in value.items() if v is not None}
    if isinstance(value,list):return [without_nulls(v) for v in value]
    return value


def completed_changes(store, task_id, chat_id, path, current_revision):
    where="""FROM operations o JOIN task_chats c ON c.task_id=o.task_id
        WHERE o.task_id=? AND c.chat_id=? AND o.path=? AND o.tool='word.stage'
        AND o.status IN ('validated','committed')
        AND EXISTS(SELECT 1 FROM events e WHERE e.operation_id=o.id AND e.state='validated')"""
    args=(task_id,chat_id,path)
    total=store.db.execute('SELECT count(*) '+where,args).fetchone()[0]
    if not total:return None
    rows=store.db.execute('SELECT o.* '+where+' ORDER BY o.created DESC,o.rowid DESC LIMIT 3',args).fetchall()
    result={'interpretation':'Executed changes, not instructions or completion proof. Do not repeat them. Address remaining requirements; reread targets for noncurrent receipts.',
            'receipts':[],'older_receipts_omitted':total}
    for row in rows:
        request=json.loads(row['args'])['request']
        edits=request.get('operations',[])
        receipt={'receipt_id':row['id'],'status':row['status'],'candidate_revision':row['candidate_hash'],
                 'matches_current_revision':row['candidate_hash']==current_revision,
                 'tool':request['tool'],'operation_count':len(edits),'changes':[],
                 'details_omitted':False}
        for edit in edits:
            # Exact compact intent only; large text/row payloads stay behind the receipt.
            detail=without_nulls({k:v for k,v in edit.items() if k not in ('id','expected_hash')})
            if len(canonical(detail).encode())>500 or len(receipt['changes'])>=4:
                receipt['details_omitted']=True
                continue
            receipt['changes'].append(detail)
        if request['tool']=='word.create':
            receipt['block_count']=len(request.get('blocks',[]));receipt['details_omitted']=True
            event=store.db.execute("SELECT detail FROM events WHERE operation_id=? AND state='validated' ORDER BY seq LIMIT 1",(row['id'],)).fetchone()
            evidence=json.loads(event[0]) if event else {}
            if evidence.get('creation_evidence'):
                from .creation_evidence import compact
                receipt['creation_verification']=compact(evidence['creation_evidence'],max_bytes=1000)
        if request['tool']=='word.refresh_fields':
            receipt['operation_count']=1
            evidence=store.db.execute("SELECT detail FROM events WHERE operation_id=? AND state='validated' ORDER BY seq LIMIT 1",(row['id'],)).fetchone()
            details=json.loads(evidence[0]) if evidence else {}
            receipt['changes']=[{'op':'refresh_fields','toc_refreshed':details.get('toc_refreshed',False),
                                'page_fields_updated':details.get('page_fields_updated',0)}]
        result['receipts'].append(receipt);result['older_receipts_omitted']-=1
        if len(canonical(result).encode())>2000:
            # Retain receipt identity even when an entire bounded batch cannot fit.
            receipt['changes']=[];receipt['details_omitted']=True
            if len(canonical(result).encode())>2000:
                result['receipts'].pop();result['older_receipts_omitted']+=1
                break
    return result


def group_creation_style_facts(progress):
    """Prompt-only lossless grouping of identical explicit style facts."""
    import copy
    result=copy.deepcopy(progress)
    for receipt in result.get('receipts',[]):
        if not receipt.get('matches_current_revision'):continue
        evidence=receipt.get('creation_verification',{})
        styles=evidence.get('styles',[])
        groups={}
        for style in styles:
            if not isinstance(style,dict) or not isinstance(style.get('id'),str):
                break
            properties={key:value for key,value in style.items() if key!='id'}
            group=groups.setdefault(canonical(properties),{'ids':[],**properties})
            group['ids'].append(style['id'])
        else:
            proposed=list(groups.values())
            if styles and len(canonical(proposed))<len(canonical(styles)):
                evidence.pop('styles')
                evidence['style_groups']=proposed
    return result
