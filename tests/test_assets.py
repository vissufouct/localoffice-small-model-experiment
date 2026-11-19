import json
import pytest
from PIL import Image
from localoffice.storage import ProjectStore,digest
from localoffice.assets import AssetPlugin
from localoffice.errors import HarnessError
from localoffice.plugins import first_party_registry


def picture(path,fmt='PNG',orientation=None):
    image=Image.new('RGB',(30,20),'blue')
    if orientation:
        exif=Image.Exif();exif[274]=orientation;image.save(path,format=fmt,exif=exif)
    else:image.save(path,format=fmt)
    return path.read_bytes()


@pytest.mark.parametrize('fmt',['PNG','JPEG'])
def test_import_deduplicates_and_keeps_original_snapshot_across_restart(tmp_path,fmt):
    original=picture(tmp_path/'source',fmt);store=ProjectStore(tmp_path);assets=AssetPlugin(store)
    receipt=assets.import_image(path='source',expected_hash=digest(original))
    assert len(json.dumps(receipt))<1500 and receipt['width_px']==30
    assert receipt['document_changed'] is False and receipt['license_status']=='unknown'
    (tmp_path/'copy').write_bytes(original);assert assets.import_image(path='copy')['asset_id']==receipt['asset_id']
    assert store.db.execute('SELECT count(*) FROM image_assets').fetchone()[0]==1
    assert store.db.execute('SELECT count(*) FROM image_asset_sources').fetchone()[0]==2
    (tmp_path/'source').write_bytes(b'changed');store.close();store=ProjectStore(tmp_path)
    try:
        assert AssetPlugin(store).resolve(receipt['asset_id'])[1]==original
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        from localoffice.graph import neighborhood
        assert len(neighborhood(store,receipt['asset_id'])['nodes'])==3
    finally:store.close()


def test_exif_orientation_is_recorded_without_reencoding(tmp_path):
    original=picture(tmp_path/'photo.jpg','JPEG',6);store=ProjectStore(tmp_path)
    try:
        assets=AssetPlugin(store);r=assets.import_image(path='photo.jpg')
        assert r['exif_orientation']==6 and r['display_width_px']==20 and r['display_height_px']==30
        assert assets.resolve(r['asset_id'])[1]==original
    finally:store.close()


def test_placement_orientation_retains_palette_transparency(tmp_path):
    import io
    image=Image.new('P',(30,20),0);image.putpalette([255,0,0]+[0]*765)
    exif=Image.Exif();exif[274]=6
    image.save(tmp_path/'palette.png',exif=exif,transparency=0)
    store=ProjectStore(tmp_path)
    try:
        assets=AssetPlugin(store);r=assets.import_image(path='palette.png');meta,data=assets.placement_bytes(r['asset_id'])
        with Image.open(io.BytesIO(data)) as placed:
            assert placed.size==(20,30) and placed.convert('RGBA').getpixel((0,0))[3]==0
        assert meta['exif_orientation']==1
    finally:store.close()


def test_rejects_bad_stale_animated_and_escaped_sources(tmp_path,monkeypatch):
    original=picture(tmp_path/'good.png');store=ProjectStore(tmp_path);assets=AssetPlugin(store)
    try:
        (tmp_path/'bad.png').write_bytes(b'not an image')
        with pytest.raises(HarnessError):assets.import_image(path='bad.png')
        with pytest.raises(HarnessError,match='changed'):assets.import_image(path='good.png',expected_hash='0'*64)
        with pytest.raises(HarnessError):assets.import_image(path='../good.png')
        (tmp_path/'link.png').symlink_to(tmp_path/'good.png')
        with pytest.raises(HarnessError):assets.import_image(path='link.png')
        Image.new('RGB',(10,10),'blue').save(tmp_path/'animated.png',save_all=True,append_images=[Image.new('RGB',(10,10),'red')])
        with pytest.raises(HarnessError,match='Animated'):assets.import_image(path='animated.png')
        import localoffice.assets as module
        monkeypatch.setattr(module,'MAX_IMAGE_PIXELS',500)
        with pytest.raises(HarnessError,match='pixels'):assets.import_image(path='good.png')
        monkeypatch.setattr(module,'MAX_IMAGE_BYTES',10)
        with pytest.raises(HarnessError,match='25 MB'):assets.import_image(path='good.png')
        assert store.db.execute('SELECT count(*) FROM image_assets').fetchone()[0]==0
        assert (tmp_path/'good.png').read_bytes()==original
    finally:store.close()


@pytest.mark.asyncio
async def test_registry_disable_and_project_isolation_and_forget(tmp_path):
    one=tmp_path/'one';two=tmp_path/'two';one.mkdir();two.mkdir();original=picture(one/'image.png')
    a=ProjectStore(one);b=ProjectStore(two)
    try:
        registry=first_party_registry(a);r=await registry.call('assets.import_image',path='image.png')
        with pytest.raises(HarnessError):AssetPlugin(b).resolve(r['asset_id'])
        registry.set_enabled('assets',False)
        with pytest.raises(HarnessError,match='disabled'):registry.call('assets.import_image',path='image.png')
        from localoffice.memory import export_memory,forget_project_memory
        exported=export_memory(a)
        try:assert len(json.loads(exported.read_text())['tables']['image_assets'])==1
        finally:exported.unlink()
        forget_project_memory(a,'FORGET PROJECT MEMORY')
        assert a.db.execute('SELECT count(*) FROM image_assets').fetchone()[0]==0
        assert not list(a.blobs.iterdir()) and (one/'image.png').read_bytes()==original
    finally:a.close();b.close()


@pytest.mark.asyncio
async def test_word_runtime_loads_import_schema_and_returns_only_asset_handle(tmp_path):
    from localoffice.runtime import TaskRunner
    from localoffice.word.guidance import prompt_schemas
    from test_word_tables import saved_table
    store,plugin,original,rev=saved_table(tmp_path);data=picture(tmp_path/'image.png')
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 500
        async def stream(self,messages,**kwargs):
            self.step+=1
            if self.step==1:
                assert 'ImportImage' not in kwargs['schema']['$defs']
                action={'tool':'word.load_tools','operations':['import_image']}
            elif self.step==2:
                assert 'ImportImage' in kwargs['schema']['$defs'] and 'StageBatch' not in kwargs['schema']['$defs']
                action={'tool':'assets.import_image','path':'image.png','expected_hash':digest(data)}
            else:
                receipt=json.loads(messages[-1]['content'].split('TOOL RECEIPT / SOURCE EVIDENCE: ',1)[1])
                assert receipt['asset_id']=='image:'+digest(data)
                assert 'content_hash' not in receipt and 'source' not in receipt
                assert receipt['source_path']=='image.png' and 'expected_hash' in receipt['instruction']
                action={'tool':'finish','text':'Image imported; no document placement performed.'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        schemas=prompt_schemas(['import_image']);assert set(schemas)=={'assets.import_image'}
        task=await runner.start('Import image.png as a reusable asset without changing report.docx.',artifact='report.docx');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        result=next(e['result'] for e in runner.events if e['type']=='tool' and e['tool']=='assets.import_image')
        assert result['content_hash']==digest(data) and len(json.dumps(result))<1500
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()
