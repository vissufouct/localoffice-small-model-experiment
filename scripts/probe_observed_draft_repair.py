"""Replay observed planning/creation through one rejected repair; tokenizer only."""
import argparse
import asyncio
import copy
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from benchmark_word_creation_cases import ROOT, PIN, sha, runtime_sources


async def run(args):
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
    snapshot = runtime_sources()
    report = json.loads(args.report.read_text())
    source = Path(report['project']) / '.localoffice/state.sqlite3'
    before = sha(source.read_bytes())
    with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True) as db:
        rows = [(json.loads(a), json.loads(b)) for a, b in db.execute(
            'SELECT n.function,m.content FROM native_calls n JOIN messages m ON m.id=n.result_message '
            'WHERE n.task_id=? ORDER BY n.rowid', (report['task_id'],))]
    expected = ['word.select_source_quotes', 'word.plan_requirements', 'word.plan_draft',
                'word.submit_draft', 'word.prepare_creation', 'word.revise_creation']
    assert [a['name'] for a, _ in rows[:6]] == expected
    assert rows[5][1].get('code') == 'creation_revision'
    typed_changes = json.loads(args.typed_style_repair.read_text()) if args.typed_style_repair else None
    assert 'ct_' not in json.dumps(rows[4][0]), 'This replay supports plain-text proposals only.'
    from transformers import AutoTokenizer
    approval = json.loads((ROOT / 'docs/benchmarks/approved-mlx-download.json').read_text())
    assert approval['revision'] == PIN
    tokenizer = AutoTokenizer.from_pretrained(approval['local_path'], local_files_only=True, trust_remote_code=False)
    from localoffice.storage import ProjectStore
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import render_prompt
    from localoffice.word import draft_fragments, creation_repair
    from localoffice.word.contracts import PrepareCreation

    class Provider:
        native_tool_protocol = True
        native_constrained = True
        context_limit = 4096
        sampling = {'temperature': 0.2, 'top_p': 1.0, 'top_k': 0, 'min_p': 0.0}

        def __init__(self):
            self.presented = []
            self.counts = []
            self.mappings = []

        def status(self):
            return {'loaded': True, 'constrained_decoding': True}

        async def count_tokens(self, messages, *, tools):
            count = len(tokenizer.encode(render_prompt(tokenizer, messages, tools=tools)))
            self.counts.append(count)
            return count

        async def stream(self, messages, *, tools, **kwargs):
            step = len(self.presented)
            assert step <= 6
            self.presented.append({'step': step + 1, 'tokens': self.counts[-1],
                                   'tools': [t['function']['name'] for t in tools]})
            action = copy.deepcopy(rows[step][0]) if step < 6 else {
                'name': 'ask_user', 'arguments': {'text': 'Tokenizer replay complete; no inference.'}}
            task_id = store.db.execute('SELECT id FROM tasks ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            if step == 3:
                state = draft_fragments.get(store, task_id)
                assert state['request'] == draft_fragments.PlanDraft(**rows[2][0]['arguments']).model_dump(), 'Retained draft instruction differs.'
                assert action['arguments']['ref'] == rows[2][1]['ref']
                self.mappings.append({'kind': 'draft', 'old': action['arguments']['ref'], 'new': state['ref']})
                action['arguments']['ref'] = state['ref']
            if step == 5:
                state = creation_repair.get(store, task_id)
                wanted = PrepareCreation(**rows[4][0]['arguments']).model_dump()
                wanted['tool'] = 'word.create'
                assert state['payload'] == wanted
                assert action['arguments']['ref'] == rows[4][1]['ref']
                self.mappings.append({'kind': 'proposal', 'old': action['arguments']['ref'], 'new': state['ref']})
                action['arguments']['ref'] = state['ref']
                if typed_changes is not None:
                    action = {'name': 'word.edit_creation_styles', 'arguments': {
                        'ref': state['ref'], 'changes': copy.deepcopy(typed_changes)}}
            yield {'type': 'token', 'text': '<tool_call>' + json.dumps(action, ensure_ascii=False) + '</tool_call>'}
            yield {'type': 'finish', 'reason': 'stop'}

    with tempfile.TemporaryDirectory(prefix='office-observed-repair-') as directory:
        store = ProjectStore(Path(directory))
        provider = Provider()
        runner = TaskRunner(store, provider, source_planning=True, creation_review=True,
                            requirements_planning=True, focused_drafting=True, isolated_draft_planning=True)
        workers = []

        def forbidden(*a, **kw):
            workers.append(True)
            raise RuntimeError('Document workers forbidden in tokenizer replay.')

        try:
            with patch('localoffice.word.plugin.WordPlugin._execute', forbidden):
                await runner.start(report['prompt'], artifact='report.docx', mode='background')
                await runner.job
            result = {'diagnostic': 'Observed six-call replay with authenticated reference remapping; not model behavior',
                      'source_report': str(args.report), 'model_revision': PIN,
                      'inference_performed': False, 'weights_loaded': False,
                      'runtime_sources_unchanged': runtime_sources() == snapshot,
                      'source_database_unchanged': sha(source.read_bytes()) == before,
                      'presented': provider.presented, 'count_trace': provider.counts,
                      'reference_mappings': provider.mappings, 'events': runner.events,
                      'scripted_typed_style_repair': typed_changes,
                      'runtime_fingerprint': sha(json.dumps(snapshot,sort_keys=True).encode()),
                      'finalization_admitted': bool(typed_changes is not None and len(provider.presented)==7
                          and 'word.finalize_creation' in provider.presented[-1]['tools']),
                      'worker_attempts': len(workers),
                      'operations': store.db.execute('SELECT count(*) FROM operations').fetchone()[0],
                      'retry_admitted': len(provider.presented) == 7}
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({k: v for k, v in result.items() if k not in ('events', 'count_trace', 'reference_mappings')}))
        finally:
            await runner.pause()
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--typed-style-repair', type=Path,
                        help='Explicit operator-supplied changes JSON replacing observed failed repair; not model behavior.')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Output exists; preserve evidence.')
    asyncio.run(run(args))
