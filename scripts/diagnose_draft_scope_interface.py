"""Frozen paired scope diagnostic. Default: offline validation, no model/tokenizer.

After coordinator approval only: --run-inference --output NEW.json.
No ProjectStore, ledger dispatch, document workers, downloads or production edits.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))
SPEC = ROOT / 'docs/benchmarks/draft-scope-interface-v1.json'
PIN = '50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'
FIXTURE_SHA256 = 'b94864a0191fe5f2f4273237a28875758ab41daf965a8b5464f1cbd589a72406'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def selection_context(case, arm):
    from localoffice.word.draft_context import planning_tool, build
    from localoffice.word.draft_planning_context import SYSTEM, format_catalogue
    from localoffice.word.contracts import LoadTools

    catalogue = case['catalogue']
    tool = planning_tool(catalogue)
    system = SYSTEM
    if arm == 'segment_or_range':
        original = tool['function']['parameters']
        single = copy.deepcopy(original)
        single['properties'].pop('source_range')
        single['properties']['source_segment'] = {'enum': list(catalogue['segments'])}
        single['required'] = ['source_hash', 'source_segment']
        tool['function']['parameters'] = {'type': 'object', 'oneOf': [single, original]}
        system = system.replace('smallest inclusive range', 'smallest single segment or inclusive range')
        system = system.replace('Call word.plan_draft with that range.',
                                'Call word.plan_draft with source_segment for one segment or source_range for multiple segments.')
        tool['function']['description'] = tool['function']['description'].replace(
            'Use inclusive source range IDs.',
            'Use source_segment for one segment or inclusive source_range IDs for multiple segments.')
    elif arm != 'range':
        raise ValueError('Unknown interface')
    loader = LoadTools.model_json_schema()
    loader['properties'].pop('tool')
    loader['required'] = [name for name in loader.get('required', []) if name != 'tool']
    tools = [tool, {'type': 'function', 'function': {'name': 'word.load_tools', 'parameters': loader}},
             build('Draft', 'df_' + '0' * 32)[1][1]]
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': format_catalogue(catalogue)}]
    return messages, tools


def validate_spec():
    from localoffice.word.objective_evidence import catalogue_text
    from benchmark_word_body import prompt_for
    assert sha(SPEC.read_bytes()) == FIXTURE_SHA256, 'Frozen fixture changed; version the diagnostic'
    spec = json.loads(SPEC.read_text())
    assert spec['model_revision'] == PIN
    assert spec['status'] == 'frozen_before_inference'
    assert len(spec['cases']) == 3
    assert spec['cases'][0]['objective'] == prompt_for()
    for case in spec['cases']:
        assert catalogue_text(case['objective']) == case['catalogue']
        expected = case['expected']
        segments = case['catalogue']['segments']
        ids = list(segments)
        start, end = expected['range']['start'], expected['range']['end']
        assert expected['segments'] == ids[ids.index(start):ids.index(end) + 1]
        assert expected['span'] == {'start': segments[start]['start'], 'end': segments[end]['end']}
        assert expected['text'] == ''.join(segments[key]['text'] for key in expected['segments'])
        assert expected['requires_multiple_segments'] == (start != end)
        baseline, tools = selection_context(case, 'range')
        alternate, other_tools = selection_context(case, 'segment_or_range')
        assert baseline[1] == alternate[1]  # Complete identical objective; labels never injected.
        assert tools[1:] == other_tools[1:]
    assert spec['schedule'] == [[case['id'], arm] for case in spec['cases']
                                for arm in ('range', 'segment_or_range')]
    return spec


def selected_span(case, action):
    """Resolve model choices mechanically; never substitute expected selections."""
    if action['tool'] != 'word.plan_draft':
        return None
    if action['source_hash'] != case['catalogue']['source_hash']:
        raise ValueError('Source hash mismatch')
    segments = case['catalogue']['segments']
    if 'source_segment' in action:
        first = last = action['source_segment']
    else:
        first, last = action['source_range']['start'], action['source_range']['end']
    ids = list(segments)
    start, end = ids.index(first), ids.index(last)
    if end < start:
        raise ValueError('Reversed range')
    chosen = ids[start:end + 1]
    expected = case['expected']['segments']
    return {'segments': chosen, 'span': {'start': segments[first]['start'], 'end': segments[last]['end']},
            'text': ''.join(segments[key]['text'] for key in chosen),
            'exact_expected_span': chosen == expected,
            'extra_segments': [key for key in chosen if key not in expected],
            'omitted_segments': [key for key in expected if key not in chosen]}


async def response(provider, messages, tools, budget, record):
    from localoffice.native_tools import parse_qwen_call
    record.update(messages=messages, tools=tools, raw='', events=[])
    record['input_tokens'] = await provider.count_tokens(messages, tools=tools)
    if record['input_tokens'] + budget['max_tokens_per_response'] + budget['safety_reserve'] > budget['context_limit']:
        raise ValueError('context_capacity; unchanged budget, no pruning')
    async for event in provider.stream(messages, tools=tools, max_tokens=budget['max_tokens_per_response']):
        if event['type'] == 'token':
            record['raw'] += event['text']
        else:
            record['events'].append(event)
    record['action'] = parse_qwen_call(record['raw'], tools=tools)
    return record['action']


async def run(spec, output):
    # Model initialization is unreachable from the default offline command.
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    from localoffice.providers import MLXProvider
    from localoffice.word.draft_context import build
    from benchmark_word_creation_cases import runtime_sources
    approval = json.loads((ROOT / 'docs/benchmarks/approved-mlx-download.json').read_text())
    model = approval['model']
    if approval['revision'] != PIN or Path(model['path']).name != PIN or not Path(model['path']).is_dir():
        raise ValueError('Pinned local weights unavailable; no substitute or download')
    provider = MLXProvider(structural_decoding=False, native_tool_protocol=True,
                           native_constrained=True, sampling_profile='low_temperature')
    if provider.sampling != spec['sampling']:
        raise ValueError('Sampling differs from frozen specification')
    sources = runtime_sources()
    fixture_bytes = SPEC.read_bytes()
    report = {'diagnostic': spec['diagnostic'], 'spec': spec, 'fixture_sha256': sha(fixture_bytes),
              'script_sha256': sha(Path(__file__).read_bytes()), 'runtime_sources': sources,
              'model': model, 'revision': PIN, 'sampling': provider.sampling,
              'document_operations': 0, 'ledger_writes': 0, 'runs': [],
              'semantic_review': 'pending', 'acceptance': 'diagnostic only; no document/full-task acceptance'}
    started = time.monotonic()
    # Exclusive creation prevents overwriting prior evidence, even on interruption.
    with output.open('x') as stream:
        def save():
            stream.seek(0)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.truncate(); stream.flush()
        save()
        try:
            await provider.start(model['path'], spec['budget']['context_limit'])
            for case_id, arm in spec['schedule']:
                if runtime_sources() != sources or SPEC.read_bytes() != fixture_bytes:
                    raise ValueError('Sources or frozen fixture changed; stop diagnostic')
                case = next(item for item in spec['cases'] if item['id'] == case_id)
                row = {'case': case_id, 'interface': arm, 'planning': {}, 'semantic_review': 'pending'}
                report['runs'].append(row)
                began = time.monotonic()

                async def pair():
                    messages, tools = selection_context(case, arm)
                    action = await response(provider, messages, tools, spec['budget'], row['planning'])
                    chosen = selected_span(case, action)
                    row['selection'] = chosen
                    if chosen is None:
                        row['status'] = 'non_selection_exit'
                        return
                    row['drafting'] = {}
                    messages, tools = build(chosen['text'], 'df_' + '0' * 32)
                    action = await response(provider, messages, tools, spec['budget'], row['drafting'])
                    row['status'] = 'completed' if action['tool'] == 'word.submit_draft' else 'draft_exit'
                    if action['tool'] == 'word.submit_draft':
                        text = action['text']
                        row['draft_observations'] = {'text': text, 'whitespace_word_count': len(text.split()),
                            'equals_copied_literal': text in case['copied_literals'],
                            'semantic_compliance': 'unassessed'}

                try:
                    await asyncio.wait_for(pair(), timeout=spec['budget']['seconds_per_arm'])
                except Exception as exc:
                    row.update(status='failed', error=repr(exc))
                finally:
                    row['elapsed_seconds'] = time.monotonic() - began
                    save()
        except Exception as exc:
            report['error'] = repr(exc)
        finally:
            await provider.stop()
            report.update(elapsed_seconds=time.monotonic() - started,
                          runtime_sources_unchanged=runtime_sources() == sources,
                          fixture_unchanged=SPEC.read_bytes() == fixture_bytes)
            save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-inference', action='store_true', help='Only after coordinator approves a serial model slot.')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    spec = validate_spec()
    if args.run_inference:
        if args.output is None:
            parser.error('--output is required for inference')
        asyncio.run(run(spec, args.output))
    else:
        if args.output is not None:
            parser.error('Offline validation writes no report; omit --output')
        print(json.dumps({'status': 'offline_fixture_valid', 'fixture_sha256': sha(SPEC.read_bytes()),
                          'cases': [{'id': c['id'], **c['expected']} for c in spec['cases']],
                          'inference_calls': 0}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
