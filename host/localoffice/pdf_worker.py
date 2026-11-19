"""One bounded PDF job per owned process. Input (including password) stays on stdin."""
import base64
import io
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path


def execute(request):
    from pypdf import PdfReader
    path=Path(request['snapshot'])
    password=request.get('password') or None
    reader=PdfReader(path)
    if reader.is_encrypted:
        if password is None or not reader.decrypt(password):
            return {'error':'pdf_encrypted','message':'This PDF is encrypted. Supply its password for this request only.'}
    total=len(reader.pages)
    if total>2000:return {'error':'page_limit','message':'Initial source limit is 2,000 pages.'}
    first=request['page'];count=request.get('count',1)
    if not 1<=first<=total:return {'error':'page_out_of_range','message':'Requested page does not exist.'}
    if not 1<=count<=3:return {'error':'page_limit','message':'Read at most three pages per call.'}
    document=None
    def render(page_number,scale=1.8):
        nonlocal document
        import pypdfium2 as pdfium
        if document is None:document=pdfium.PdfDocument(path,password=password)
        page=document[page_number-1]
        width,height=page.get_size()
        scale=min(scale,math.sqrt(8_000_000/max(width*height,1)))
        bitmap=page.render(scale=scale)
        image=bitmap.to_pil().copy()
        bitmap.close();page.close()
        return image
    try:
        if request['command']=='render':
            image=render(first)
            output=io.BytesIO();image.save(output,format='PNG')
            return {'page':first,'page_count':total,'width':image.width,'height':image.height,
                    'png':base64.b64encode(output.getvalue()).decode(),'renderer':'PDFium','visual_reviewed':False}
        pages=[]
        for number in range(first,min(total+1,first+count)):
            text=reader.pages[number-1].extract_text() or ''
            status='embedded' if text.strip() else 'no_text'
            if not text.strip() and request.get('ocr'):
                image=render(number,2.2)
                with tempfile.TemporaryDirectory(prefix='localoffice-ocr-') as folder:
                    source=Path(folder)/'page.png';image.save(source)
                    command=['tesseract',str(source),'stdout','--tessdata-dir',request['tessdata'],'-l',request.get('languages','eng+por'),'--psm','3']
                    completed=subprocess.run(command,capture_output=True,timeout=35)
                    if completed.returncode:
                        return {'error':'ocr_failed','message':'OCR failed. Check the installed language data and runtime.'}
                    text=completed.stdout.decode('utf-8',errors='replace');status='ocr'
            pages.append({'page':number,'text':text[:2000],'truncated':len(text)>2000,'extraction':status,
                          'uncertainty':'OCR can misread characters and layout.' if status=='ocr' else 'Text order and table structure may differ from visual layout.'})
        return {'page_count':total,'pages':pages}
    finally:
        if document is not None:document.close()


def main():
    try:
        request=json.loads(sys.stdin.buffer.readline(1_000_000))
        response=execute(request)
    except subprocess.TimeoutExpired:
        response={'error':'ocr_timeout','message':'The bounded OCR job timed out.'}
    except Exception as exc:
        # Never echo parser exception content, passwords or document text into logs.
        response={'error':'pdf_invalid','message':'Unable to parse or render this PDF.','exception_type':type(exc).__name__}
    print(json.dumps(response,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
