"""Read-only native preparation constraints from complete model-selected plans.

This projection is not intent extraction, a document edit or semantic approval.
The caller gates exposure and rechecks the returned stamp just before dispatch.
"""
from copy import deepcopy

from ..errors import HarnessError
from ..storage import canonical, digest
from . import requirements
from .content_plan import _plan_owner, source
from .contracts import PageSettings

MAX_STAMP_BYTES = 4096
MAX_DIAGNOSTIC_BYTES = 1024
FIELDS = {'width': 'width_twips', 'height': 'height_twips', 'margin': 'margin_twips'}


def _bounded(stamp):
    if (len(stamp['diagnostic'].encode('utf-8')) > MAX_DIAGNOSTIC_BYTES
            or len(canonical(stamp).encode('utf-8')) > MAX_STAMP_BYTES):
        raise HarnessError('page_requirement_limit', 'Page requirement evidence exceeds its bounded stamp size.')
    return stamp


def _stamp(store, task_id):
    owner = _plan_owner(store, task_id)
    state = requirements.get(store, task_id)
    _, objective, current_hash = source(store, task_id)
    stamp = {'version': 1, 'status': 'missing', 'diagnostic':
             'No selected requirements plan. Load plan_requirements and declare the requested checks before preparation.',
             'interpretation': 'model_selected', 'owner': owner, 'generation': None,
             'plan_hash': None, 'source_hash': None, 'current_source_hash': current_hash,
             'fields': {}}
    if state is None:
        return _bounded(stamp)
    stamp.update(generation=state['generation'], plan_hash=digest(canonical(state).encode('utf-8')),
                 source_hash=state['request']['source_hash'])
    if state['source_text'] != objective or stamp['source_hash'] != current_hash:
        stamp.update(status='stale', diagnostic=
                     'Selected requirements refer to an earlier objective. Replan from the current source before preparation.')
        return _bounded(stamp)
    selected = {}
    for entry in state['request']['requirements']:
        if entry['kind'] != 'page_geometry':
            continue
        field = FIELDS[entry['field']]
        selected.setdefault(field, set()).add(requirements.twips(entry['equals'], entry['unit']))
    conflicts = [field + '=' + '/'.join(str(value) for value in sorted(values))
                 for field, values in sorted(selected.items()) if len(values) > 1]
    if conflicts:
        stamp.update(status='conflict', diagnostic=
                     'Conflicting selected page values in twips: ' + '; '.join(conflicts) +
                     '. Explicitly revise the requirements plan; no value was chosen and preparation is unavailable.')
        return _bounded(stamp)
    fields = {field: next(iter(values)) for field, values in sorted(selected.items())}
    if 'margin_twips' in fields:
        required = 2 * fields['margin_twips'] + 1440
        invalid = [field for field in ('width_twips', 'height_twips')
                   if field in fields and fields[field] < required]
        if invalid:
            stamp.update(status='conflict', diagnostic=
                         'Selected page geometry conflicts: ' + ', '.join(invalid) +
                         ' is less than 2*margin_twips+1440 (' + str(required) +
                         '). Explicitly revise the plan; no unselected dimension was assumed.')
            return _bounded(stamp)
    stamp.update(status='ready', fields=fields, diagnostic=
                 ('Preparation requires the selected page values in twips: ' + canonical(fields) +
                  '. Values are model-selected requirements, not certified source interpretation.' if fields else
                  'The current requirements plan selects no page geometry; preparation fields are unchanged.'))
    return _bounded(stamp)


def _clean(value):
    if isinstance(value, dict):
        return {key: ({name: _clean(schema) for name, schema in child.items()}
                      if key in ('properties', '$defs', 'definitions') else _clean(child))
                for key, child in value.items() if key not in ('title', 'description', 'default')}
    if isinstance(value, list):
        return [_clean(child) for child in value]
    return value


def _page_schema(parameters):
    """Validate the exact canonical page definition and its sole reference."""
    try:
        if parameters['type'] != 'object' or parameters['additionalProperties'] is not False:
            raise ValueError('root shape')
        if canonical(_clean(parameters['properties']['page'])) != canonical({'$ref': '#/$defs/PageSettings'}):
            raise ValueError('page reference')
        page = parameters['$defs']['PageSettings']
        if canonical(_clean(page)) != canonical(_clean(PageSettings.model_json_schema())):
            raise ValueError('page definition')
        required = parameters.get('required', [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ValueError('required')

        def references(value):
            if isinstance(value, dict):
                return int(value.get('$ref') == '#/$defs/PageSettings') + sum(references(child) for child in value.values())
            if isinstance(value, list):
                return sum(references(child) for child in value)
            return 0

        if references(parameters) != 1:
            raise ValueError('shared page definition')
        return page
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError('native_schema', 'Preparation page schema differs from the registered PageSettings contract.') from exc


def project_tools(store, task_id, tools):
    """Return independent tools and a bounded authoritative freshness stamp.

    Missing, stale or conflicting plans remove only word.prepare_creation.
    Empty/non-page plans are ready, but still bound to the complete generation.
    No default dimensions, source interpretation or storage writes are applied.
    """
    from ..native_tools import tool_names
    result = deepcopy(tools)
    tool_names(result)
    with store.transaction():
        stamp = _stamp(store, task_id)
    if stamp['status'] != 'ready':
        return [tool for tool in result if tool['function']['name'] != 'word.prepare_creation'], stamp
    for tool in result:
        if tool['function']['name'] != 'word.prepare_creation':
            continue
        parameters = tool['function']['parameters']
        page = _page_schema(parameters)
        if not stamp['fields']:
            continue
        parameters['required'] = list(dict.fromkeys([*parameters.get('required', []), 'page']))
        page['required'] = list(dict.fromkeys([*page.get('required', []), *stamp['fields']]))
        for field, value in stamp['fields'].items():
            # Preserve type/range constraints; eliminate contradictory default hints.
            page['properties'][field].pop('default', None)
            page['properties'][field]['const'] = value
    return result, stamp


def require_fresh(store, task_id, stamp):
    """Reject stale/changed/disallowed preparation authority without any writes."""
    with store.transaction():
        current = _stamp(store, task_id)
        if current['status'] != 'ready':
            raise HarnessError('page_requirements_' + current['status'], current['diagnostic'], recovery=current)
        if canonical(current) != canonical(stamp):
            raise HarnessError('page_requirements_changed',
                               'Selected requirements, source or target changed; rebuild preparation tools before dispatch.',
                               recovery=current)
