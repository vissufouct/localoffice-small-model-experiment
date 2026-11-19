import fcntl
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from .errors import HarnessError
from .storage import atomic_write,canonical

class ProjectRegistry:
    def __init__(self,settings):
        self.path=settings/'projects.json';self.lock=settings/'projects.lock'
    @contextmanager
    def locked(self):
        with self.lock.open('a+b') as file:
            fcntl.flock(file.fileno(),fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(file.fileno(),fcntl.LOCK_UN)
    def records(self):
        if not self.path.exists():return []
        try:return json.loads(self.path.read_text())
        except (ValueError,OSError):raise HarnessError('registry_invalid','Project registry needs recovery; it was not overwritten.')
    def register(self,root,name=None):
        root=Path(root).resolve()
        with self.locked():
            records=self.records()
            existing=next((item for item in records if item['root']==str(root)),None)
            if existing:
                if name:existing['name']=name.strip()[:120]
            else:
                existing={'id':str(uuid.uuid4()),'root':str(root),'name':(name or root.name).strip()[:120]}
                records.append(existing)
            atomic_write(self.path,canonical(records).encode())
            return existing
    def listing(self):
        with self.locked():records=self.records()
        result=[]
        for record in records:
            root=Path(record['root']);db=root/'.localoffice/state.sqlite3';chats=[]
            if db.is_file() and not db.is_symlink() and not db.parent.is_symlink():
                try:
                    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=1) as connection:
                        connection.row_factory=sqlite3.Row
                        chats=[dict(row) for row in connection.execute('SELECT id,title,updated FROM chats ORDER BY updated DESC LIMIT 200')]
                except sqlite3.Error:pass
            result.append({**record,'available':root.is_dir(),'chats':chats})
        return result
