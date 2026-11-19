"""Project duplicate failure facts only when the exact native result matches."""
import json
from ..storage import canonical
from ..chats import current_chat


def project(store,task_id,message_id,receipt,proposal):
    if not isinstance(proposal,dict) or proposal.get('status') not in ('prepared','invalid_design'):
        return receipt
    checks=proposal.get('selected_requirements')
    if (not isinstance(checks,dict) or receipt.get('code') not in
            ('requirements_mismatch','requirements_unverified','requirements_stale')
            or receipt.get('recovery')!=checks):
        return receipt
    row=store.db.execute('''SELECT n.function,m.content FROM native_calls n
        JOIN messages m ON m.id=n.result_message
        WHERE n.task_id=? AND n.chat_id=? AND n.result_message=?''',
        (task_id,current_chat(store),message_id)).fetchone()
    if row is None or canonical(json.loads(row['content']))!=canonical(receipt):return receipt
    function=json.loads(row['function'])
    if (function.get('name') not in ('word.finalize_creation','word.repair_creation')
            or function.get('arguments',{}).get('ref')!=proposal.get('ref')):
        return receipt
    return {**receipt,'recovery':{'details_in_current_state':'creation_repair.selected_requirements'}}
