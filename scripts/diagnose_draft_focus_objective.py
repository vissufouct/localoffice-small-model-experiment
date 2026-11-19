"""Frozen drafting-only paired diagnostic; offline by default, no document dispatch."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from diagnose_draft_scope_interface import ROOT, PIN, sha, response
SPEC = ROOT / 'docs/benchmarks/draft-focus-objective-v1.json'
FIXTURE_SHA = 'b4430ae2137238f189491c30a38a3224f78f451ab46ca10152579e1ec6989b83'


def validate():
    from localoffice.word.objective_evidence import catalogue_text
    from localoffice.word.draft_context import build
    assert sha(SPEC.read_bytes()) == FIXTURE_SHA
    spec = json.loads(SPEC.read_text())
    assert spec['model_revision'] == PIN and len(spec['cases']) == 4
    assert spec['budget']['maximum_calls'] == 8
    for case in spec['cases']:
        assert catalogue_text(case['objective']) == case['catalogue']
        focus = case['focus']
        assert case['objective'][focus['span']['start']:focus['span']['end']] == focus['text']
        assert case['source_hash'] == case['catalogue']['source_hash']
        baseline = case['contexts']['selected_only']
        messages, tools = build(focus['text'], 'df_' + '0' * 32)
        assert baseline == {'messages': messages, 'tools': tools}
        alternate = case['contexts']['focus_and_objective']
        assert alternate['tools'] == tools
        assert alternate['messages'][0]['content'] == messages[0]['content'] + '\n' + spec['additional_instruction']
        assert json.loads(alternate['messages'][1]['content']) == {'selected_focus': focus['text'], 'complete_objective': case['objective']}
        provenance = case['provenance']
        if focus['origin'] == 'model':
            assert sha((ROOT / provenance['report']).read_bytes()) == provenance['report_sha256']
        else:
            assert focus['origin'] == 'operator' and not focus['scope_accuracy_eligible']
    assert spec['schedule'] == [[c['id'], arm] for c in spec['cases'] for arm in ('selected_only', 'focus_and_objective')]
    return spec


async def run(spec, output):
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    from localoffice.providers import MLXProvider
    from benchmark_word_creation_cases import runtime_sources
    approval = json.loads((ROOT / 'docs/benchmarks/approved-mlx-download.json').read_text())
    model = approval['model']
    assert approval['revision'] == PIN and Path(model['path']).name == PIN and Path(model['path']).is_dir()
    provider = MLXProvider(structural_decoding=False, native_tool_protocol=True, native_constrained=True, sampling_profile='low_temperature')
    assert provider.sampling == spec['sampling']
    sources = runtime_sources()
    report = dict(diagnostic=spec['diagnostic'], fixture_sha256=FIXTURE_SHA, script_sha256=sha(Path(__file__).read_bytes()), runtime_sources=sources, model=model, revision=PIN, sampling=provider.sampling, document_operations=0, ledger_writes=0, runs=[], acceptance='diagnostic only', semantic_review='pending')
    started = time.monotonic()
    with output.open('x') as stream:
        def save():
            stream.seek(0); json.dump(report, stream, ensure_ascii=False, indent=2); stream.write('\n'); stream.truncate(); stream.flush()
        save()
        try:
            await provider.start(model['path'], spec['budget']['context_limit'])
            for case_id, arm in spec['schedule']:
                assert runtime_sources() == sources and sha(SPEC.read_bytes()) == FIXTURE_SHA
                case = next(c for c in spec['cases'] if c['id'] == case_id)
                row = dict(case=case_id, arm=arm, focus_origin=case['focus']['origin'])
                report['runs'].append(row)
                began = time.monotonic()
                try:
                    ctx = case['contexts'][arm]
                    action = await asyncio.wait_for(response(provider, ctx['messages'], ctx['tools'], spec['budget'], row), timeout=spec['budget']['seconds_per_arm'])
                    row['status'] = 'submitted' if action['tool'] == 'word.submit_draft' else 'non_submission_exit'
                    if row['status'] == 'submitted':
                        text = action['text']; count = len(text.split()); bounds = case['gold']['word_count']
                        row['observations'] = dict(text=text, word_count=count, word_bound_pass=bounds['min'] <= count <= bounds['max'], copied_literal=text in case['copied_literals'])
                except Exception as exc:
                    row.update(status='failed', error=repr(exc))
                row['elapsed_seconds'] = time.monotonic() - began
                save()
        except Exception as exc:
            report['error'] = repr(exc)
        finally:
            await provider.stop()
            report.update(elapsed_seconds=time.monotonic()-started, runtime_sources_unchanged=runtime_sources()==sources, fixture_unchanged=sha(SPEC.read_bytes())==FIXTURE_SHA)
            save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-inference', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(); spec = validate()
    if args.run_inference:
        if args.output is None: parser.error('--output required')
        asyncio.run(run(spec, args.output))
    else:
        print(json.dumps(dict(status='offline_fixture_valid', cases=len(spec['cases']), fixture_sha256=FIXTURE_SHA, inference_calls=0)))
