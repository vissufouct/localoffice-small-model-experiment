"""Local portable skills: metadata first, body only when selected; never run scripts."""
import hashlib
import json
import shutil
import time
import re
import uuid
from pathlib import Path
import yaml
from .errors import HarnessError
from .storage import canonical,atomic_write

class SkillLoader:
    def __init__(self,store,registry=None):
        self.store=store
        if registry is None:
            from .plugins import first_party_registry
            registry=first_party_registry(store)
        self.registry=registry
        self.builtin_root=Path(__file__).parent/'builtin_skills'
        self.roots=[store.root/'.agents/skills',store.root/'.claude/skills',store.state/'skills',
                    Path.home()/'.agents/skills',Path.home()/'.codex/skills',Path.home()/'.claude/skills',self.builtin_root]
        self.catalog={}
    def inspect(self,path):
        if path.stat().st_size>100_000:raise HarnessError('skill_limit','Skill metadata/body exceeds 100 KB.')
        source=path.read_text()
        if source.startswith('---'):
            parts=source.split('---',2)
            metadata=yaml.safe_load(parts[1]) if len(parts)==3 else {}
            body=parts[2].strip() if len(parts)==3 else source
        else:metadata={};body=source
        if not isinstance(metadata,dict):raise HarnessError('skill_metadata','Skill frontmatter must be a mapping.')
        name=str(metadata.get('name',path.parent.name));description=str(metadata.get('description',''))
        required=metadata.get('allowed-tools',metadata.get('tools',[]))
        if isinstance(required,str):required=required.replace(',',' ').split()
        if not isinstance(required,list):required=[]
        unsupported=[str(x) for x in required if x not in set(self.registry.tools)|{'ask_user','finish'}]
        host_specific=any(key in metadata for key in ['hooks','context','agent'])
        status='adaptation needed' if unsupported or host_specific else 'ready'
        return {'id':hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24],
                'name':name,'description':description,'path':str(path),'status':status,
                'required_tools':required,'unsupported_tools':unsupported,'scripts_present':(path.parent/'scripts').is_dir(),
                'scripts_execution':'disabled','source_hash':hashlib.sha256(source.encode()).hexdigest(),
                'origin':'bundled' if path.resolve().is_relative_to(self.builtin_root.resolve()) else 'local',
                'body':body,'metadata':metadata}
    def default_word(self,creating):
        # Deterministic first-party choice; never auto-select a similarly named import.
        path=self.builtin_root/('word-create' if creating else 'word-edit')/'SKILL.md'
        item=self.inspect(path)
        self.catalog[item['id']]=path
        return self.load(item['id'])
    def discover(self):
        results=[];self.catalog={}
        for root in self.roots:
            if not root.is_dir():continue
            # Bounded discovery: skills directly below roots, plus one category level.
            paths=sorted(set(root.glob('*/SKILL.md'))|set(root.glob('*/*/SKILL.md')))
            for path in paths[:300]:
                try:
                    item=self.inspect(path);self.catalog[item['id']]=path
                    results.append({k:v for k,v in item.items() if k not in ['body','metadata']})
                except Exception as exc:results.append({'path':str(path),'status':'unsupported','error':str(exc)[:200]})
        return results
    def load(self,key):
        if key not in self.catalog:self.discover()
        if key not in self.catalog:raise HarnessError('not_found','Skill not found in the local catalogue.')
        item=self.inspect(self.catalog[key])
        if item['status']!='ready':raise HarnessError('skill_incompatible','This skill requires adaptation before use.')
        return item
    def managed_root(self):
        root=self.store.state/'skills'
        if root.is_symlink():raise HarnessError('skill_symlink','Managed skill storage cannot be a symbolic link.')
        root.mkdir(exist_ok=True)
        return root
    def create_draft(self,name,description,body,required_tools):
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>80:
            raise HarnessError('skill_name','Use a lowercase skill name with letters, numbers and hyphens, up to 80 characters.')
        if not description.strip() or len(description)>1000 or not body.strip() or len(body)>12000:
            raise HarnessError('skill_limit','Provide a description (up to 1,000 characters) and instructions (up to 12,000 characters).')
        supported=set(self.registry.tools)|{'ask_user','finish'}
        if not isinstance(required_tools,list) or any(tool not in supported for tool in required_tools):
            raise HarnessError('skill_capabilities','Declare only registered tool capabilities.')
        root=self.managed_root()
        dest=root/(name+'-'+uuid.uuid4().hex[:10])
        dest.mkdir()
        metadata={'name':name,'description':description.strip(),'allowed-tools':sorted(set(required_tools))}
        source='---\n'+yaml.safe_dump(metadata,sort_keys=False,allow_unicode=True)+'---\n\n'+body.strip()+'\n'
        atomic_write(dest/'SKILL.md',source.encode())
        atomic_write(dest/'LOCALOFFICE_PROVENANCE.json',canonical({'origin':'user-authored draft','created':time.time(),'scripts_executed':False,'automatically_selected':False}).encode())
        return self.inspect(dest/'SKILL.md')
    def import_local(self,folder):
        source=Path(folder).expanduser().resolve(strict=True)
        if not source.is_dir() or not (source/'SKILL.md').is_file():raise HarnessError('skill_format','Select a local directory containing SKILL.md.')
        self.inspect(source/'SKILL.md')
        paths=list(source.rglob('*'))
        if len(paths)>2000 or sum(p.stat().st_size for p in paths if p.is_file())>10_000_000:
            raise HarnessError('skill_limit','Initial local import limit: 2,000 entries and 10 MB.')
        if any(p.is_symlink() for p in paths):raise HarnessError('skill_symlink','Skill imports cannot contain symbolic links.')
        key=hashlib.sha256(str(source).encode()).hexdigest()[:16]
        dest=self.managed_root()/key
        if dest.exists():raise HarnessError('skill_exists','This source is already imported; adaptations must be explicit.')
        dest.parent.mkdir(exist_ok=True)
        shutil.copytree(source,dest)
        (dest/'LOCALOFFICE_PROVENANCE.json').write_text(canonical({'source':str(source),'imported':time.time(),'scripts_executed':False}))
        return {k:v for k,v in self.inspect(dest/'SKILL.md').items() if k not in ['body','metadata']}
