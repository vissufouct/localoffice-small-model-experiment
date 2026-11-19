"""Bounded stored-proposal net effects; not rendering or task completion."""
from ..storage import canonical

_MISSING=object()


def _same(a,b):
    if a is _MISSING or b is _MISSING:return a is b
    return canonical(a)==canonical(b)


def _at(value,parts):
    for key in parts:
        if isinstance(value,dict):value=value.get(key,_MISSING)
        elif isinstance(value,list) and key.isdecimal() and int(key)<len(value):value=value[int(key)]
        else:return _MISSING
    return value


def summarize(before,after,changes):
    """Compare final payloads; use submitted paths only to identify no-ops/shifts."""
    structural=set()
    touched=set()
    for change in changes:
        parts=change['path'].split('.')
        parent=parts[:-1]
        if change['op'] in ('insert','remove') and (
                isinstance(_at(before,parent),list) or isinstance(_at(after,parent),list)):
            structural.add('.'.join(parent));touched.add('.'.join(parent))
        else:
            touched.add(change['path'])
            if isinstance(_at(before,parts),list) or isinstance(_at(after,parts),list):
                structural.add(change['path'])
    touched={next((root for root in sorted(structural,key=len)
                   if path==root or path.startswith(root+'.')),path) for path in touched}
    changed=[]
    def walk(a,b,path):
        if _same(a,b):return
        if path in structural:
            changed.append(path);return
        if isinstance(a,dict) and isinstance(b,dict):
            for key in sorted(a.keys()|b.keys()):
                walk(a.get(key,_MISSING),b.get(key,_MISSING),f'{path}.{key}' if path else key)
        elif isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
            # Reordering cannot yield stable child locators.
            if sorted(map(canonical,a))==sorted(map(canonical,b)):
                changed.append(path);return
            for i,(left,right) in enumerate(zip(a,b)):walk(left,right,f'{path}.{i}')
        else:changed.append(path)
    walk(before,after,'')
    unchanged=[]
    for path in sorted(touched):
        if _same(_at(before,path.split('.')),_at(after,path.split('.'))):
            unchanged.append(path)
    result={'scope':'proposal_data_only','proposal_changed':not _same(before,after),
            'changed':[],'unchanged':[],'omitted_count':len(changed)+len(unchanged)}
    for key,paths in (('changed',changed),('unchanged',unchanged)):
        for path in paths:
            trial={**result,key:result[key]+[path],'omitted_count':result['omitted_count']-1}
            if len(canonical(trial).encode())<=256:result=trial
    return result
