"""Atomic native call/result provenance; no inference or worker dispatch here."""
import json
import uuid
from .chats import current_chat
from .errors import HarnessError
from .storage import canonical


def _owner(store, task_id):
    chat = current_chat(store)
    if not store.db.execute('SELECT 1 FROM task_chats WHERE task_id=? AND chat_id=?', (task_id, chat)).fetchone():
        raise HarnessError('chat_scope', 'Native history belongs to another task or chat.')
    return chat


def record_call(store, task_id, action):
    """Store a previously validated action and host-issued ID atomically."""
    if not isinstance(action, dict) or not isinstance(action.get('tool'), str) or not action['tool']:
        raise HarnessError('native_history', 'A validated action with a tool name is required.')
    function = {'name': action['tool'], 'arguments': {k: v for k, v in action.items() if k != 'tool'}}
    with store.transaction():
        chat = _owner(store, task_id)
        if store.db.execute('SELECT 1 FROM native_calls WHERE task_id=? AND result_message IS NULL', (task_id,)).fetchone():
            raise HarnessError('native_pending', 'Reconcile the pending call before proposing another operation.')
        call_id = 'call_' + uuid.uuid4().hex
        message_id = store.message('assistant', canonical(action), task_id)
        store.db.execute('INSERT INTO native_calls VALUES(?,?,?,?,?,NULL)',
                         (call_id, task_id, chat, message_id, canonical(function)))
    return call_id


def record_result(store, task_id, call_id, result, *, recovery_state=None):
    with store.transaction():
        chat = _owner(store, task_id)
        row = store.db.execute('SELECT * FROM native_calls WHERE call_id=? AND task_id=? AND chat_id=?',
                               (call_id, task_id, chat)).fetchone()
        if not row:
            raise HarnessError('native_history', 'Result has no matching call in this task and chat.')
        if row['result_message'] is not None:
            raise HarnessError('native_history', 'A result is already recorded for this call.')
        message_id = store.message('tool', canonical(result), task_id, recovery_state=recovery_state)
        store.db.execute('UPDATE native_calls SET result_message=? WHERE call_id=?', (message_id, call_id))
    return message_id


def exchanges(store, task_id, *, limit=8):
    """Return complete pairs for bounded context; never replay an incomplete call."""
    if not 1 <= limit <= 16:
        raise HarnessError('history_limit', 'Choose between one and sixteen native exchanges.')
    chat = _owner(store, task_id)
    if store.db.execute('SELECT 1 FROM native_calls WHERE task_id=? AND result_message IS NULL', (task_id,)).fetchone():
        raise HarnessError('native_pending', 'Pending call needs ledger reconciliation; do not repeat the edit.')
    rows = store.db.execute('''SELECT n.*,m.content FROM native_calls n JOIN messages m ON m.id=n.result_message
        WHERE n.task_id=? AND n.chat_id=? ORDER BY n.assistant_message DESC LIMIT ?''', (task_id, chat, limit)).fetchall()
    result = []
    for row in reversed(rows):
        from .word.evidence import model_receipt
        receipt = model_receipt(json.loads(row['content']))
        result.append([
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': row['call_id'], 'type': 'function', 'function': json.loads(row['function'])}]},
            {'role': 'tool', 'tool_call_id': row['call_id'], 'content': canonical(receipt)},
        ])
    return result


def operation_key(call_id):
    """The execution ledger must use this identity for native mutations."""
    import re
    if not re.fullmatch(r'call_[0-9a-f]{32}', call_id):
        raise HarnessError('native_history', 'Expected a host-issued native call identity.')
    return 'native:' + call_id


def _reconcile_prepared_creation(store, task_id, row, function):
    """Recover the immutable exact-call receipt; never infer preparation."""
    from .word.creation_submission import result as submission_result,presence
    from .word.creation_repair import get,pending
    receipt=submission_result(store,task_id,native_call_id=row['call_id'])
    if receipt is None:
        return {'state':'unresolved','reason':'No exact native preparation submission receipt; execution is not inferred.'}
    state=get(store,task_id)
    routing=None
    if state is not None and state.get('review_required') is True and state['ref']==receipt['ref']:
        marker=state.get('submission_presence',{})
        if (marker.get('status')=='verified' and marker.get('native_call_id')==row['call_id']
                and marker.get('proposal_ref')==state['ref']
                and marker.get('payload_hash')==state['payload_hash']
                and presence(store,state) is not None):
            current=pending(store,task_id)
            if current and current.get('status') in ('prepared','invalid_design') and current.get('ref')==receipt['ref']:
                routing=current
    message_id=record_result(store,task_id,row['call_id'],receipt,
        recovery_state={'unresolved':False,'mutation':False,'resolved_by':receipt['ref']})
    if routing is not None:_restore_proposal_routing(store,task_id,routing)
    return {'state':'reconciled','call_id':row['call_id'],'message_id':message_id,
            'ref':receipt['ref'],'routing_restored':routing is not None}


def _reconcile_revised_creation(store, task_id, row, function):
    """Recover the exact retained revision without reapplying its patch."""
    from .storage import digest
    from .word.contracts import ReviseCreation
    from .word.creation_repair import get, pending
    action = ReviseCreation.model_validate({'tool': function['name'], **function['arguments']})
    request = action.model_dump(exclude={'tool'})
    state = get(store, task_id)  # Verify original state and payload hashes before comparison.
    if state is None or state.get('review_required') is not True:
        return {'state': 'unresolved', 'reason': 'No durable revised creation proposal requiring review.'}
    origin = state.get('revision_origin')
    if (not isinstance(origin, dict) or origin.get('ref') != action.ref
            or origin.get('request_hash') != digest(canonical(request).encode())):
        return {'state': 'unresolved', 'reason': 'Current proposal does not record this exact revision request.'}
    summary = pending(store, task_id)
    if summary is None or summary.get('status') not in ('prepared','invalid_design') or summary.get('ref') != state['ref']:
        return {'state': 'unresolved', 'reason': 'Revised proposal is stale or unavailable; execution is not inferred.'}
    from .word.creation_revision import result as revision_result
    receipt=revision_result(store,task_id,action.ref,request['changes'])
    if receipt is None:
        return {'state':'unresolved','reason':'No matching retained revision receipt.'}
    message_id = record_result(store, task_id, row['call_id'], receipt,
                               recovery_state={'unresolved': False, 'mutation': False, 'resolved_by': state['ref']})
    _restore_proposal_routing(store,task_id,summary)
    return {'state': 'reconciled', 'call_id': row['call_id'], 'message_id': message_id,
            'ref': state['ref']}


def _restore_proposal_routing(store,task_id,summary):
    """Restore only tool exposure after the exact proposal receipt is recovered."""
    from .word.guidance import build_state
    from .word.creation_routing import next_operations
    operations=next_operations(summary)
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                     ('word_tools:'+task_id,canonical(build_state(operations))))


def _restore_requirements_routing(store,task_id,row,receipt):
    """Restore a lost automatic transition, never a newer explicit selection."""
    mode=store.db.execute('SELECT value FROM meta WHERE key=?',('task_content_mode:'+task_id,)).fetchone()
    from .word.requirements import get,PLANNING_MODES
    if mode is None or mode[0] not in PLANNING_MODES:return False
    from .word.content_plan import source
    from .word.guidance import build_state
    state=get(store,task_id)
    if state is None or state['generation']!=receipt.get('generation'):return False
    _,_,source_hash=source(store,task_id)
    if state['request']['source_hash']!=source_hash:return False
    target=state['owner']['target']
    if not target or store.candidate(target) or store.file_hash(target) is not None:return False
    selected=store.db.execute('SELECT value FROM meta WHERE key=?',('word_tools:'+task_id,)).fetchone()
    if selected is not None and selected[0]!=canonical(build_state(['plan_requirements'])):return False
    # Tool selection metadata has no timestamp. Preserve any recorded explicit
    # load after the recovered call, even if it chose plan_requirements again.
    for message in store.db.execute(
            "SELECT m.content FROM messages m JOIN message_chats c ON c.message_id=m.id WHERE m.task_id=? AND c.chat_id=? AND m.role='tool' AND m.id>?",
            (task_id,row['chat_id'],row['assistant_message'])):
        try:content=json.loads(message[0])
        except (ValueError,TypeError):continue
        if isinstance(content,dict) and 'loaded_operations' in content:return False
    next_operation='prepare_creation'
    if mode[0]=='isolated_draft_planning_v1:focused_drafting_v1:requirements_reviewed_source_planning':
        from .word.draft_fragments import get as get_draft
        if get_draft(store,task_id) is None:next_operation='plan_draft'
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                     ('word_tools:'+task_id,canonical(build_state([next_operation]))))
    return True


def reconcile_word_call(store, task_id):
    """Recover a missing receipt from an exact validated operation or fresh proposal.

    No worker invocation, inferred completion or automatic mutation replay.
    Unrecorded/no-op operations remain unresolved until explicitly reconciled.
    """
    from .storage import digest
    from .word.contracts import FormatTextCall, ReplaceTextCall
    from .runtime import WORD_ACTION
    from .word.plugin import WordPlugin
    with store.transaction():
        chat = _owner(store, task_id)
        row = store.db.execute('SELECT * FROM native_calls WHERE task_id=? AND chat_id=? AND result_message IS NULL',
                               (task_id, chat)).fetchone()
        if not row:
            return {'state': 'no_pending_call'}
        function = json.loads(row['function'])
        name = function['name']
        if name in ('word.plan_draft', 'word.submit_draft'):
            from .word.draft_fragments import result as draft_result
            receipt=draft_result(store,task_id,{'tool':name,**function['arguments']},
                                 native_call_id=row['call_id'])
            if receipt is None:
                return {'state':'unresolved','reason':'No exact completed draft receipt; execution is not inferred.'}
            # Draft metadata is not a document repair. Preserve unrelated recovery
            # and routing state, including when replaying a superseded generation.
            message_id=record_result(store,task_id,row['call_id'],receipt)
            return {'state':'reconciled','call_id':row['call_id'],'message_id':message_id,
                    'ref':receipt['ref']}
        if name in ('word.edit_creation_blocks','word.edit_creation_styles'):
            if name=='word.edit_creation_styles':
                from .word.creation_styles import result as block_result
            else:
                from .word.creation_blocks import result as block_result
            from .word.creation_repair import pending
            receipt=block_result(store,task_id,function['arguments'],native_call_id=row['call_id'])
            if receipt is None:
                return {'state':'unresolved','reason':'No exact completed proposal-edit receipt; execution is not inferred.'}
            message_id=record_result(store,task_id,row['call_id'],receipt,
                recovery_state={'unresolved':False,'mutation':False})
            current=pending(store,task_id)
            if (current and current.get('ref')==receipt.get('ref')
                    and current.get('status') in ('prepared','invalid_design')):
                _restore_proposal_routing(store,task_id,current)
            return {'state':'reconciled','call_id':row['call_id'],'message_id':message_id}
        if name == 'word.plan_requirements':
            from .word.requirements import result as requirements_result
            receipt=requirements_result(store,task_id,function['arguments'],native_call_id=row['call_id'])
            if receipt is None:
                return {'state':'unresolved','reason':'No exact completed requirements-plan receipt; execution is not inferred.'}
            routing_restored=_restore_requirements_routing(store,task_id,row,receipt)
            message_id=record_result(store,task_id,row['call_id'],receipt,
                recovery_state={'unresolved':False,'mutation':False})
            return {'state':'reconciled','call_id':row['call_id'],'message_id':message_id,
                    'routing_restored':routing_restored}
        if name == 'word.select_source_quotes':
            from .word.source_quotes import result as quote_result
            receipt=quote_result(store,task_id,function['arguments'],native_call_id=row['call_id'])
            if receipt is None:
                return {'state':'unresolved','reason':'No exact completed source-selection receipt; execution is not inferred.'}
            message_id=record_result(store,task_id,row['call_id'],receipt,
                recovery_state={'unresolved':False,'mutation':False})
            return {'state':'reconciled','call_id':row['call_id'],'message_id':message_id}
        if name == 'word.plan_content':
            from .word.content_plan_repair import failure_result
            receipt=failure_result(store,task_id,function['arguments'],native_call_id=row['call_id'])
            if receipt is None:
                return {'state':'unresolved','reason':'No exact retained failed selection receipt.'}
            message_id=record_result(store,task_id,row['call_id'],receipt,
                recovery_state={'unresolved':True,'mutation':False})
            return {'state':'reconciled','call_id':row['call_id'],'message_id':message_id,
                    'outcome':'recorded_failure'}
        if name == 'word.repair_content_plan':
            from .word.content_plan_repair import result as content_plan_result
            receipt=content_plan_result(store,task_id,function['arguments'])
            if receipt is None:
                return {'state':'unresolved','reason':'No exact completed content-plan correction receipt.'}
            message_id=record_result(store,task_id,row['call_id'],receipt,
                recovery_state={'unresolved':False,'mutation':False,'resolved_by':function['arguments']['ref']})
            return {'state':'reconciled','call_id':row['call_id'],'message_id':message_id,'receipt_id':function['arguments']['ref']}
        if name == 'word.revise_creation':
            return _reconcile_revised_creation(store, task_id, row, function)
        if name == 'word.prepare_creation':
            return _reconcile_prepared_creation(store, task_id, row, function)
        if name not in {'word.finalize_creation', 'word.stage_batch', 'word.create', 'word.repair_creation', 'word.refresh_fields', 'word.format_text', 'format_text', 'word.replace_text', 'replace_text'}:
            return {'state': 'unresolved', 'reason': 'No durable mutation receipt expected for this tool.'}
        operation = store.db.execute('SELECT o.* FROM operations o JOIN operation_chats c ON c.operation_id=o.id WHERE o.idempotency_key=? AND o.task_id=? AND c.chat_id=?',
                                     (operation_key(row['call_id']), task_id, chat)).fetchone()
        if not operation:
            return {'state': 'unresolved', 'reason': 'No exact ledger operation; execution is not inferred.'}
        if operation['tool'] != 'word.stage' or operation['status'] not in ('validated', 'committed'):
            return {'state': 'unresolved', 'reason': 'Exact operation has not completed validation.'}
        saved = json.loads(operation['args'])
        if digest(canonical(saved).encode()) != operation['args_hash']:
            raise HarnessError('state_corrupt', 'Native recovery ledger arguments failed their hash check.')
        action = WORD_ACTION.validate_python({'tool': name, **function['arguments']})
        if isinstance(action, (FormatTextCall, ReplaceTextCall)):
            action = action.as_batch()
        submitted = saved.get('repair_request') if name in {'word.repair_creation','word.finalize_creation'} else saved.get('submitted_request', saved.get('request'))
        proposed=action.model_dump(exclude={'literal_refs'}) if name=='word.create' and not action.literal_refs else action.model_dump()
        if name=='word.create':
            from .word.creation_identity import normalize_creation_identity
            proposed=normalize_creation_identity(proposed)
            submitted=normalize_creation_identity(submitted)
        elif name in {'word.repair_creation','word.finalize_creation'}:
            from .word.creation_identity import normalize_repair_identity
            # Compare the validated call representation, never the execution
            # binding map. Typed lists (including order) and legacy maps have
            # distinct identities. Only absent/empty containers are compatible;
            # the original ledger hash was checked above, before projection.
            proposed=normalize_repair_identity(action.model_dump())
            submitted=normalize_repair_identity(submitted)
        if canonical(proposed) != canonical(submitted) or saved.get('task_id') != task_id:
            raise HarnessError('native_history', 'Exact call identity has different recorded arguments.')
        target = store.db.execute('SELECT artifact FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not target or target[0] != operation['path'] or (action.path if name not in {'word.repair_creation','word.finalize_creation'} else saved.get('request',{}).get('path')) != target[0]:
            raise HarnessError('artifact_scope', 'Native recovery does not match the task artifact.')
        candidate = store.candidate(operation['path'])
        current = candidate['candidate_hash'] if candidate else store.file_hash(operation['path'])
        if current != operation['candidate_hash']:
            return {'state': 'unresolved', 'reason': 'The recorded result is not the current artifact revision.'}
        if candidate:
            store.blob(current)
        if not store.db.execute("SELECT 1 FROM events WHERE operation_id=? AND state='validated'", (operation['id'],)).fetchone():
            raise HarnessError('state_corrupt', 'Native recovery lacks a validation event.')
        receipt = WordPlugin(store)._receipt(dict(operation))
        message_id = record_result(store, task_id, row['call_id'], receipt,
                                   recovery_state={'unresolved': False, 'mutation': False, 'resolved_by': operation['id']})
        return {'state': 'reconciled', 'call_id': row['call_id'], 'message_id': message_id, 'receipt_id': operation['id']}


def project_message(store, message, content, *, pending_call_id=None):
    """Project stored IDs into canonical roles, retaining receipt compression."""
    message_id = message.get('id')
    row = store.db.execute('SELECT * FROM native_calls WHERE assistant_message=? OR result_message=?',
                           (message_id, message_id)).fetchone() if message_id is not None else None
    if row and message['role'] == 'assistant':
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': row['call_id'], 'type': 'function', 'function': json.loads(row['function'])}]}
    if message['role'] == 'tool' and (row or pending_call_id):
        return {'role': 'tool', 'tool_call_id': row['call_id'] if row else pending_call_id, 'content': content}
    return {'role': message['role'] if message['role'] in ('user', 'assistant') else 'user', 'content': content}
