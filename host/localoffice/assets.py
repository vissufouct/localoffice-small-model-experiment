"""Project-local image snapshots. Model receipts never contain image bytes."""
import asyncio
import io
import json
import time
import warnings
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from PIL import Image,UnidentifiedImageError
from .errors import HarnessError
from .paths import project_path
from .storage import canonical,digest

MAX_IMAGE_BYTES=25_000_000
MAX_IMAGE_PIXELS=20_000_000


class ImportImage(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    tool:Literal['assets.import_image']='assets.import_image'
    path:str=Field(min_length=1,max_length=1024)
    expected_hash:str|None=Field(default=None,pattern=r'^[0-9a-f]{64}$')


class AssetPlugin:
    def __init__(self,store):self.store=store

    def import_image(self,**arguments):
        request=ImportImage(**arguments)
        with self.store.lock:
            path=project_path(self.store.root,request.path,must_exist=True)
            # Read through a bounded stream even if the source grows after stat.
            with path.open('rb') as source:data=source.read(MAX_IMAGE_BYTES+1)
            if len(data)>MAX_IMAGE_BYTES:raise HarnessError('asset_limit','Image exceeds 25 MB.')
            content_hash=digest(data)
            if request.expected_hash and request.expected_hash!=content_hash:
                raise HarnessError('stale_source','Image changed since the expected source hash.')
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error',Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as image:
                        if image.format not in ('PNG','JPEG'):
                            raise HarnessError('asset_format','Use a PNG or JPEG image.')
                        width,height=image.size
                        if width*height>MAX_IMAGE_PIXELS or max(width,height)>20000:
                            raise HarnessError('asset_limit','Image exceeds 20 million pixels or 20,000 pixels per edge.')
                        if getattr(image,'n_frames',1)!=1:
                            raise HarnessError('asset_format','Animated images require an explicit frame selection, not silent flattening.')
                        fmt=image.format;mode=image.mode
                        orientation=image.getexif().get(274,1)
                        if orientation not in range(1,9):
                            raise HarnessError('asset_orientation','Image has an invalid EXIF orientation.')
                    with Image.open(io.BytesIO(data)) as checked:checked.verify()
                    # Verify container and actual pixel decoding before snapshotting.
                    with Image.open(io.BytesIO(data)) as image:image.load()
            except HarnessError:raise
            except (UnidentifiedImageError,OSError,ValueError,SyntaxError,Image.DecompressionBombWarning,Image.DecompressionBombError) as exc:
                raise HarnessError('asset_invalid','Image could not be safely decoded as PNG/JPEG.') from exc
            metadata={'asset_id':'image:'+content_hash,'content_hash':content_hash,
                      'mime_type':'image/png' if fmt=='PNG' else 'image/jpeg','bytes':len(data),
                      'width_px':width,'height_px':height,'display_width_px':height if orientation>=5 else width,
                      'display_height_px':width if orientation>=5 else height,'mode':mode,'exif_orientation':orientation,
                      'pixel_transform':'none; original bytes retained','license_status':'unknown'}
            # Blob writes precede DB publication. A crash can leave an unreferenced
            # blob, never a published handle whose bytes were not persisted.
            self.store.put_blob(data)
            with self.store.transaction():
                self.store.db.execute('INSERT OR IGNORE INTO image_assets VALUES(?,?,?)',(content_hash,canonical(metadata),time.time()))
                self.store.db.execute('INSERT OR IGNORE INTO image_asset_sources VALUES(?,?,?)',(content_hash,request.path,time.time()))
                self.store.node(metadata['asset_id'],'asset',metadata,content_hash)
                source_id='image-source:'+digest(request.path.encode())+':'+content_hash
                self.store.node(source_id,'source',{'path':request.path,'source_hash':content_hash},content_hash)
                self.store.edge(metadata['asset_id'],source_id,'imported-from','validated local image snapshot')
            return {**metadata,'source':{'path':request.path,'hash':content_hash},'status':'imported',
                    'document_changed':False}

    def resolve(self,asset_id):
        if not isinstance(asset_id,str) or not asset_id.startswith('image:'):
            raise HarnessError('asset_not_found','Use an imported image handle.')
        row=self.store.db.execute('SELECT metadata FROM image_assets WHERE hash=?',(asset_id[6:],)).fetchone()
        if not row:raise HarnessError('asset_not_found','Image is not imported in this project.')
        metadata=json.loads(row[0]);data=self.store.blob(metadata['content_hash'])
        if digest(data)!=metadata['content_hash']:raise HarnessError('asset_corrupt','Image snapshot hash mismatch.')
        return metadata,data

    def placement_bytes(self,asset_id):
        """Apply EXIF orientation to a placement copy; keep the asset untouched."""
        metadata,data=self.resolve(asset_id)
        if metadata['exif_orientation']==1:return metadata,data
        from PIL import ImageOps
        with Image.open(io.BytesIO(data)) as image:
            oriented=ImageOps.exif_transpose(image)
            if oriented.mode not in ('1','L','LA','P','RGB','RGBA','I','I;16'):
                oriented=oriented.convert('RGBA' if 'transparency' in oriented.info else 'RGB')
            oriented.info.pop('exif',None)
            out=io.BytesIO();oriented.save(out,format='PNG');data=out.getvalue()
        if len(data)>50_000_000:raise HarnessError('asset_limit','Oriented placement copy exceeds 50 MB.')
        return {**metadata,'mime_type':'image/png','width_px':metadata['display_width_px'],
                'height_px':metadata['display_height_px'],'exif_orientation':1},data

    async def import_async(self,**arguments):
        work=asyncio.create_task(asyncio.to_thread(self.import_image,**arguments))
        try:return await asyncio.shield(work)
        except asyncio.CancelledError:
            try:await work
            except Exception:pass
            raise


def model_import_receipt(receipt):
    """Small read-only projection; full hashes/provenance remain in stored receipts."""
    return {k:receipt[k] for k in ('asset_id','mime_type','display_width_px','display_height_px','status','document_changed')} | {
        'source_path':receipt['source']['path'],
        'instruction':'Use asset_id only as the asset reference. Edit expected_hash must come from the target Word object read, never from this image asset.'}
