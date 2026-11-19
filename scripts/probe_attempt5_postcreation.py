"""Disposable-copy postcreation assembly and template-token ablations; no inference."""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'docs/benchmarks/word-body-b1-isolated-plan-attempt5/report.json'
OUTPUT = REPORT.parent / 'postcreation-token-diagnosis.json'
PIN = '50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def main(output=OUTPUT):
    if output.exists():
        raise RuntimeError('Retain existing diagnostic evidence')
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
    report = json.loads(REPORT.read_text())
    origin = Path(report['project'])
    db = origin / '.localoffice/state.sqlite3'
    before = sha(db)
    sources = {str(p.relative_to(ROOT)): sha(p) for p in (ROOT/'host/localoffice').rglob('*')
               if p.is_file() and p.suffix in ('.py', '.md')}
    with tempfile.TemporaryDirectory(prefix='attempt5-postcreation-') as tmp:
        tmp = Path(tmp)
        shutil.copytree(ROOT/'host/localoffice', tmp/'host/localoffice', ignore=shutil.ignore_patterns('__pycache__'))
        sys.path.insert(0, str(tmp/'host'))
        project = tmp/'project'
        shutil.copytree(origin, project, ignore=shutil.ignore_patterns('state.sqlite3*', 'writer.lock'))
        with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as source:
            with sqlite3.connect(project/'.localoffice/state.sqlite3') as dest:
                source.backup(dest)
        from transformers import AutoTokenizer
        approval = json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
        assert approval['revision'] == PIN and Path(approval['local_path']).name == PIN
        tokenizer = AutoTokenizer.from_pretrained(approval['local_path'], local_files_only=True, trust_remote_code=False)
        from localoffice.storage import ProjectStore, canonical
        from localoffice.runtime import TaskRunner
        from localoffice.native_tools import render_prompt, validate_history

        def count(messages, tools):
            validate_history(messages)
            return len(tokenizer.encode(render_prompt(tokenizer, messages, tools=tools)))

        class Provider:
            native_tool_protocol = True
            native_constrained = True
            context_limit = 4096

            def __init__(self):
                self.trace = []

            def status(self):
                return {'constrained_decoding': True}

            async def count_tokens(self, messages, *, tools):
                n = count(messages, tools)
                self.trace.append(copy.deepcopy({'tokens': n, 'messages': messages, 'tools': tools}))
                return n

            async def stream(self, *args, **kwargs):
                raise AssertionError('Inference forbidden')

        store = ProjectStore(project)
        key = report['task_id']
        boundary = store.db.execute('SELECT max(result_message) FROM native_calls WHERE task_id=?', (key,)).fetchone()[0]
        later = store.db.execute('SELECT id,role FROM messages WHERE task_id=? AND id>?', (key,boundary)).fetchall()
        assert all(r['role']=='error' for r in later)
        with store.transaction():
            for row in later:
                store.db.execute('DELETE FROM message_chats WHERE message_id=?', (row['id'],))
                store.db.execute('DELETE FROM messages WHERE id=?', (row['id'],))
        provider = Provider()
        runner = TaskRunner(store,provider,source_planning=True,creation_review=True,
                            requirements_planning=True,focused_drafting=True,isolated_draft_planning=True)
        result = {'source_report': str(REPORT), 'inference_performed': False, 'weights_loaded': False,
                  'workers_started': False, 'tokenizer_revision': PIN, 'input_limit': 3200,
                  'source_db_sha256_before': before, 'runtime_sources': sources,
                  'runtime_changes_since_attempt': [p for p,h in sources.items() if report['runtime_source_hashes'].get(p)!=h],
                  'removed_terminal_error_ids_on_copy': [r['id'] for r in later]}
        try:
            await runner.assemble(key)
            result['assembly_status'] = 'admitted'
        except Exception as exc:
            result.update(assembly_status='rejected', error=str(exc), error_code=getattr(exc,'code',None))
        result.update(count_trace=provider.trace, context_info=runner.context_info)
        last = provider.trace[-1]
        messages, tools, baseline = last['messages'], last['tools'], last['tokens']
        ablations = {}

        def measure(name, msgs, schemas):
            n = count(msgs, schemas)
            ablations[name] = {'tokens': n, 'saved_vs_baseline': baseline-n, 'headroom': 3200-n}

        reads = {'word.list_objects','word.find_text','word.read_objects'}
        reduced = [t for t in tools if t['function']['name'] not in reads]
        measure('remove_only_three_read_schemas', messages, reduced)
        navigation = copy.deepcopy(messages)
        navigation[0]['content'] += '\nLoad operations=["inspect"] with word.load_tools for document inspection.'
        measure('remove_read_schemas_add_load_inspect_instruction', navigation, reduced)
        for tool in tools:
            name = tool['function']['name']
            measure('remove_schema:'+name,messages,[t for t in tools if t is not tool])
        marker = 'Current authoritative state: '
        system = messages[0]['content']
        offset = system.index(marker)+len(marker)
        exact, length = json.JSONDecoder().raw_decode(system[offset:])
        for field in exact:
            altered = copy.deepcopy(messages)
            state = dict(exact); state.pop(field)
            altered[0]['content'] = system[:offset]+canonical(state)+system[offset+length:]
            measure('remove_state_field:'+field,altered,tools)
        result['authoritative_state'] = exact
        result['system_sections'] = []
        for line in system.splitlines():
            if not line: continue
            altered = copy.deepcopy(messages)
            altered[0]['content'] = system.replace(line,'',1)
            result['system_sections'].append({'text':line,'saved_if_removed':baseline-count(altered,tools)})
        # Latest pair is removed together only as a diagnostic upper bound.
        if messages[-1]['role']=='tool' and messages[-2]['role']=='assistant':
            measure('remove_latest_pair_DIAGNOSTIC_ONLY',messages[:-2],tools)
        measure('remove_all_schemas_DIAGNOSTIC_ONLY',messages,[])
        result['ablations'] = ablations
        result['token_note'] = 'Template-measured marginal differences; not additive. Ablations are measurements, not approved projections.'
        result['source_db_sha256_after'] = sha(db)
        result['source_database_unchanged'] = sha(db)==before
        result['runtime_sources_unchanged_during_probe'] = all(sha(ROOT/p)==h for p,h in sources.items())
        store.close()
        assert result['source_database_unchanged'] and result['runtime_sources_unchanged_during_probe']
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'trace':[t['tokens'] for t in provider.trace], 'ablations':ablations,
                          'db_unchanged':result['source_database_unchanged'], 'output':str(output)},indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    asyncio.run(main(parser.parse_args().output))
