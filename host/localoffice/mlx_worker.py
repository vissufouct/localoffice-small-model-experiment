"""Owned offline MLX worker. JSON-lines control protocol; no arbitrary code tools."""
import contextlib
import json
import os
import sys
from pathlib import Path

os.environ['HF_HUB_OFFLINE']='1'
os.environ['TRANSFORMERS_OFFLINE']='1'

def emit(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)

def main():
    from mlx_lm import load,stream_generate
    from mlx_lm.sample_utils import make_sampler
    path=Path(sys.argv[1]).resolve(strict=True)
    if not path.is_dir():raise ValueError('MLX weights must be an existing local directory.')
    with contextlib.redirect_stdout(sys.stderr):
        model,tokenizer=load(str(path),trust_remote_code=False)
        decoding=None
        if os.environ.get('LOCALOFFICE_MLX_STRUCTURAL_DECODING')=='1' or os.environ.get('LOCALOFFICE_MLX_NATIVE_DECODING')=='1':
            from .decoding import MLXDecoding
            decoding=MLXDecoding(model,tokenizer)
    emit({'type':'ready'})
    for line in sys.stdin:
        if len(line)>1_000_000:raise ValueError('Request exceeds worker limit')
        try:
            request=json.loads(line)
            messages=request['messages']
            if request.get('tools') is not None and request.get('schema') is not None:
                raise ValueError('Native tools cannot use the legacy standalone JSON grammar')
            with contextlib.redirect_stdout(sys.stderr):
                from .native_tools import render_prompt
                prompt=render_prompt(tokenizer,messages,tools=request.get('tools'))
                tokens=tokenizer.encode(prompt)
            if request['command']=='count':
                emit({'type':'count','tokens':len(tokens)})
                continue
            max_tokens=request.get('max_tokens',768)
            if not 1<=max_tokens<=2048:raise ValueError('Invalid output budget')
            if len(tokens)+max_tokens+128>request['context_limit']:raise ValueError('Context capacity exceeded')
            with contextlib.redirect_stdout(sys.stderr):
                if os.environ.get('LOCALOFFICE_MLX_NATIVE_DECODING')=='1':
                    if not request.get('tools') or decoding is None:raise ValueError('Native constrained decoding requires exposed tools and an available decoder')
                    from .decoding import native_processor
                    processors=[native_processor(decoding,request['tools'])]
                else:processors=[decoding.processor(request['schema'])] if decoding and request.get('schema') else None
            from .sampling import sampler_arguments
            for item in stream_generate(model,tokenizer,prompt=prompt,max_tokens=max_tokens,sampler=make_sampler(**sampler_arguments(request.get("sampling_profile","low_temperature"))),logits_processors=processors):
                if item.text:emit({'type':'token','text':item.text})
                if item.finish_reason:
                    emit({'type':'usage','usage':{'prompt_tokens':item.prompt_tokens,'completion_tokens':item.generation_tokens,'peak_memory_gb':item.peak_memory}})
                    emit({'type':'finish','reason':item.finish_reason})
            emit({'type':'done'})
        except Exception as exc:
            emit({'type':'error','message':str(exc)[:2000]})

if __name__=='__main__':
    try:main()
    except Exception as exc:
        emit({'type':'error','message':str(exc)[:2000]})
        sys.exit(1)
