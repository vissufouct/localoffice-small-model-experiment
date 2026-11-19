"""First-party extensions use this registry; capabilities never include host commit."""
from dataclasses import dataclass,asdict
from typing import Callable
import importlib.util
import shutil
from pathlib import Path
from .errors import HarnessError

@dataclass(frozen=True)
class Manifest:
    id:str
    version:str
    host_api:int
    capabilities:tuple[str,...]
    permissions:tuple[str,...]
    dependencies:tuple[str,...]=()
    trusted:bool=True

class PluginRegistry:
    api_version=1
    def __init__(self,store):
        self.store=store;self.plugins={};self.tools={};self.handlers={}
    def register(self,manifest:Manifest,handlers:dict[str,Callable]):
        if manifest.host_api!=self.api_version:raise HarnessError('plugin_api','Incompatible plugin API version.')
        if manifest.id in self.plugins:raise HarnessError('plugin_duplicate','Duplicate plugin identity.')
        if not manifest.trusted:raise HarnessError('plugin_trust','Arbitrary third-party executable plugins are deferred.')
        if set(handlers)!=set(manifest.capabilities):raise HarnessError('plugin_schema','Manifest must match exported tool capabilities.')
        if any(name in self.tools for name in handlers):raise HarnessError('plugin_duplicate','Duplicate tool capability.')
        self.plugins[manifest.id]=manifest
        for name,handler in handlers.items():self.tools[name]=manifest.id;self.handlers[name]=handler
    def enabled(self,key):
        row=self.store.db.execute('SELECT value FROM meta WHERE key=?',('plugin:'+key,)).fetchone()
        return not row or row[0]=='enabled'
    def set_enabled(self,key,enabled:bool):
        if key not in self.plugins:raise HarnessError('not_found','Plugin not found.')
        with self.store.transaction():self.store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('plugin:'+key,'enabled' if enabled else 'disabled'))
    def catalogue(self):
        items=[]
        for manifest in self.plugins.values():
            issues=[]
            if manifest.id=='assets' and importlib.util.find_spec('PIL') is None:
                issues.append('Missing image runtime: Pillow')
            if manifest.id=='word':
                from .word.plugin import ROOT
                for relative in ('.runtime/dotnet/dotnet','workers/openxml/bin/Debug/net10.0/LocalOffice.OpenXml.dll'):
                    if not (ROOT/relative).is_file():issues.append('Missing Word runtime: '+relative)
            if manifest.id=='pdf':
                for module in ('pypdf','pypdfium2','PIL'):
                    if importlib.util.find_spec(module) is None:issues.append('Missing runtime: '+module)
                if not shutil.which('tesseract'):issues.append('OCR executable unavailable: tesseract')
                for language in ('eng','por'):
                    if not (Path.home()/'.localoffice/tessdata'/(language+'.traineddata')).is_file():
                        issues.append('OCR language data unavailable: '+language)
            items.append({**asdict(manifest),'enabled':self.enabled(manifest.id),'health':'degraded' if issues else 'ready',
                          'issues':issues,'runtime':'host-mediated first-party','health_scope':'dependency availability; not model or output-quality qualification'})
        return items
    def call(self,name,**arguments):
        key=self.tools.get(name)
        if not key:raise HarnessError('tool_unavailable','Unknown tool capability.')
        if not self.enabled(key):raise HarnessError('plugin_disabled',f'{key} is disabled.')
        return self.handlers[name](**arguments)


def first_party_registry(store):
    registry=PluginRegistry(store)
    registry.register(Manifest('filesystem','1.0.0',1,('read_file',),('project.read',)),
                      {'read_file':lambda path:store.read(path,limit=24_000)})
    registry.register(Manifest('artifact-markdown','1.0.0',1,('stage_markdown',),('working_copy.stage',)),
                      {'stage_markdown':store.stage})
    from .pdf import PdfPlugin
    pdf=PdfPlugin(store)
    registry.register(Manifest('pdf','1.0.0',1,('read_pdf',),('project.read','source.snapshot','ocr.local')),
                      {'read_pdf':pdf.read})
    registry.pdf=pdf
    from .assets import AssetPlugin
    assets=AssetPlugin(store)
    registry.register(Manifest('assets','0.1.0',1,('assets.import_image',),('project.read','source.snapshot'),('Pillow',)),
                      {'assets.import_image':assets.import_async})
    registry.assets=assets
    from .word.plugin import WordPlugin
    word=WordPlugin(store)
    registry.register(Manifest('word','0.1.0',1,('word.list_objects','word.find_text','word.read_objects','word.format_text','format_text','word.replace_text','replace_text','word.stage_batch','word.create','word.repair_creation','word.prepare_creation','word.finalize_creation','word.revise_creation','word.edit_creation_blocks','word.edit_creation_styles','word.refresh_fields','word.load_tools','word.plan_requirements','word.plan_draft','word.submit_draft','word.plan_content','word.repair_content_plan','word.select_source_quotes'),
                      ('project.read','working_copy.stage'),('openxml-worker',)),
                      {name:(lambda method: lambda **args:word.call_async(method,**args))(method)
                       for name,method in [('word.list_objects','list_objects'),('word.find_text','find_text'),('word.read_objects','read_objects'),('word.format_text','format_text'),('format_text','format_text'),('word.replace_text','replace_text'),('replace_text','replace_text'),('word.stage_batch','stage_batch'),('word.create','create'),('word.repair_creation','repair_creation'),('word.prepare_creation','prepare_creation'),('word.finalize_creation','finalize_creation'),('word.revise_creation','revise_creation'),('word.edit_creation_blocks','edit_creation_blocks'),('word.edit_creation_styles','edit_creation_styles'),('word.refresh_fields','refresh_fields'),('word.load_tools','load_tools'),('word.plan_requirements','plan_requirements'),('word.plan_draft','plan_draft'),('word.submit_draft','submit_draft'),('word.plan_content','plan_content'),('word.repair_content_plan','repair_content_plan'),('word.select_source_quotes','select_source_quotes')]})
    registry.word=word
    from .word.render import WordRenderer
    registry.word_renderer=WordRenderer(store)
    return registry
