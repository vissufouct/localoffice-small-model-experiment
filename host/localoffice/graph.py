"""Bounded, chat-aware graph navigation; never an execution-state authority."""
import json
from .chats import current_chat
from .errors import HarnessError


def record_source_read(store,task_id,tool,result,message_id):
    """Project only successful host read receipts, never model-proposed claims."""
    path=result['path']
    revision=result['source_hash'] if tool=='read_pdf' else result['revision']
    provenance='message:'+str(message_id)
    with store.transaction():
        # A working-copy read must not relabel the on-disk file's revision.
        if not result.get('working'):
            store.node('file:'+path,'source',{'path':path},revision)
        store.node('revision:'+revision,'revision',{'path':path},revision)
        store.edge('file:'+path,'revision:'+revision,'contains',provenance)
        store.edge('task:'+task_id,'revision:'+revision,'read-source',provenance)
        for obj in result.get('objects',result.get('matches',[])):
            view=f'word:{revision}:{obj["id"]}'
            store.node(view,'source_view',{'path':path,'object_id':obj['id'],'object_hash':obj['hash']},revision)
            store.edge('revision:'+revision,view,'contains',provenance)
            store.edge('task:'+task_id,view,'read-object',provenance)
        for page in result.get('pages',[]):
            view=f'pdf:{revision}:{page["page"]}:read'
            store.node(view,'source_view',{'path':path,'page':page['page'],'kind':'read',
                       'extraction':page.get('extraction','unknown')},revision)
            store.edge('revision:'+revision,view,'contains',provenance)
            store.edge('task:'+task_id,view,'read-page',provenance)


def neighborhood(store,seed,*,depth=2,max_nodes=20,max_edges=40,max_chars=8000):
    if not isinstance(seed,str) or len(seed)>1200:raise HarnessError('graph_arguments','Use a bounded node identity.')
    if not 0<=depth<=3 or not 1<=max_nodes<=50 or not 1<=max_edges<=100 or not 512<=max_chars<=16000:
        raise HarnessError('graph_arguments','Graph request exceeds retrieval limits.')
    chat=current_chat(store)
    def visible(row):
        key=row['id'];kind=row['kind'];body=json.loads(row['body'])
        if kind=='task':
            return bool(store.db.execute('SELECT 1 FROM task_chats WHERE task_id=? AND chat_id=?',(key.removeprefix('task:'),chat)).fetchone())
        if kind=='decision':
            return bool(store.db.execute("SELECT 1 FROM decision_scopes WHERE decision_id=? AND (scope='project' OR chat_id=?)",(key.removeprefix('decision:'),chat)).fetchone())
        if kind=='receipt':
            return bool(store.db.execute('SELECT 1 FROM operation_chats WHERE operation_id=? AND chat_id=?',(body.get('operation'),chat)).fetchone())
        if body.get('chat_id') and body['chat_id']!=chat:return False
        return kind in ('task','decision','receipt','file','source','revision','source_view','visual_review','asset')
    result={'seed':seed,'nodes':[],'edges':[],'limited':False,'authority':'navigation only; verify current revisions and ledger before acting'}
    frontier=[seed];visited=set();accepted=set();edge_keys=set();characters=0
    with store.lock:
        for level in range(depth+1):
            next_frontier=[]
            for key in frontier:
                if key in visited:continue
                visited.add(key)
                row=store.db.execute('SELECT * FROM nodes WHERE id=?',(key,)).fetchone()
                if not row or not visible(row):continue
                node={'id':row['id'],'kind':row['kind'],'revision':row['revision'],'body_excerpt':row['body'][:600],'body_truncated':len(row['body'])>600}
                cost=len(json.dumps(node,ensure_ascii=False))
                if len(result['nodes'])>=max_nodes or characters+cost>max_chars:
                    result['limited']=True;continue
                result['nodes'].append(node);accepted.add(key);characters+=cost
                if level==depth:continue
                rows=store.db.execute('SELECT * FROM edges WHERE source=? OR target=? ORDER BY created DESC LIMIT ?',(key,key,max_edges+1)).fetchall()
                if len(rows)>max_edges:result['limited']=True
                for edge in rows[:max_edges]:
                    other=edge['target'] if edge['source']==key else edge['source']
                    if other not in visited and other not in next_frontier:next_frontier.append(other)
            frontier=next_frontier[:max_edges+1]
        for key in sorted(accepted):
            for row in store.db.execute('SELECT * FROM edges WHERE source=? ORDER BY target,relation LIMIT ?',(key,max_edges+1)):
                if row['target'] not in accepted:continue
                edge=dict(row);identity=(edge['source'],edge['target'],edge['relation'])
                if identity in edge_keys:continue
                cost=len(json.dumps(edge,ensure_ascii=False))
                if len(result['edges'])>=max_edges or characters+cost>max_chars:
                    result['limited']=True;continue
                result['edges'].append(edge);characters+=cost;edge_keys.add(identity)
    return result
