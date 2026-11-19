"""Derive scoped CSV result logs from the public, redacted JSON evidence."""
import csv
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'docs/results'

def read(name):
    return json.loads((ROOT/'docs/evidence'/name).read_text())['data']

def write(name, rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with (OUT/name).open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def main():
    OUT.mkdir(exist_ok=True)
    source='word-body-b1-isolated-plan-v2-attempt1/report.json'
    r=read(source);m=r['metrics']
    write('complete_task_runs.csv',[dict(run_id='b1_isolated_v2_attempt1',cohort=r['cohort'],fixture=r['fixture'],verifier_version=r['focused_verifier_version'],outcome='FAIL',complete_task_accepted=False,elapsed_seconds=r['elapsed_seconds'],model_calls=m['model_calls'],tool_calls=m['tool_calls'],repair_events=m['repair_events'],input_tokens=m['input_tokens'],output_tokens=m['output_tokens'],reason='Task did not finish within ten steps; submitted draft also differed from paragraph two.',source='../evidence/'+source,evidence_scope='Single included complete-task report; not all historical attempts')])
    source='draft-focus-objective-v1-attempt1-review.json'
    reviews=read(source)['rows'];raw={(x['case'],x['arm']):x for x in read('draft-focus-objective-v1-attempt1.json')['runs']}
    rows=[]
    for x in reviews:
        run=raw[x['case'],x['arm']]
        usage=next((e['usage'] for e in run['events'] if e['type']=='usage'),{})
        rows.append(dict(case=x['case'],arm=x['arm'],focus_origin=run['focus_origin'],criterion='draft_semantics_against_complete_objective',outcome='PASS' if x['semantic_pass'] else 'FAIL',text=x.get('text') or '',action=x['action']['tool'],reason=x['reason'],input_tokens=run['input_tokens'],output_tokens=usage.get('completion_tokens',''),elapsed_seconds=run['elapsed_seconds'],complete_task_acceptance='NOT_ASSESSED',source='../evidence/'+source))
    write('drafting_diagnostic.csv',rows)
    source='draft-scope-interface-v1-attempt1-review.json';rows=[]
    for x in read(source)['rows']:
        rows.append(dict(case=x['case'],arm=x['interface'],criterion='exact_scope',outcome='PASS' if x['exact_scope'] else 'FAIL',extra_segments=';'.join(x['extra_segments']),omitted_segments=';'.join(x['omitted_segments']),draft_assessment=x['draft_meaning'],complete_task_acceptance='NOT_ASSESSED',source='../evidence/'+source))
    write('scope_diagnostic.csv',rows)
    source='completion-review-selection-v1-attempt1-review.json'
    write('completion_diagnostic.csv',[dict(arm=x['variant'],criterion='select_finish',outcome='FAIL',action=x['selected_action'],reason=x['assessment'],actions_executed=0,complete_task_acceptance='NOT_ASSESSED',source='../evidence/'+source) for x in read(source)['rows']])
    write('validation_summary.csv',[
        dict(check='Historical full source regression suite',outcome='PASS',passed_tests=2799,elapsed_seconds=383,scope='Original configured workspace; historical reported result',model_accuracy_qualification=False,source='../EXPERIMENT.md'),
        dict(check='Export targeted regression selection',outcome='PASS',passed_tests=24,elapsed_seconds=1.98,scope='Export tests; not additive to historical full suite',model_accuracy_qualification=False,source='../VALIDATION.md'),
        dict(check='Export frontend and native debug builds',outcome='PASS',passed_tests='',elapsed_seconds='',scope='Build only',model_accuracy_qualification=False,source='../VALIDATION.md'),
        dict(check='Export native UI project creation',outcome='PASS',passed_tests='',elapsed_seconds='',scope='Observed UI interaction; no model inference',model_accuracy_qualification=False,source='../VALIDATION.md')])
    print('Wrote five scoped CSV files from public evidence and labelled validation summaries.')

if __name__=='__main__':main()
