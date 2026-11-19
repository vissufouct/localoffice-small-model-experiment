from localoffice.storage import canonical
from localoffice.word.creation_repair import capture
from localoffice.word.creation_revision import revise
from test_word_content_plan import fixture


def test_layout_reports_complete_snapshot_positions_before_and_after_revision(tmp_path):
    store=fixture(tmp_path,'Create First and Second.')
    try:
        raw={'tool':'word.create','path':'report.docx','blocks':[
            {'kind':'paragraph','text':'First'},
            {'kind':'table','rows':[['Cell']]},
            {'kind':'paragraph','text':'Second','page_break_before':True},
            {'kind':'page_break'}]}
        invalid=capture(store,'task',raw,allow_valid=True)
        assert invalid['status']=='invalid_design'
        assert invalid['block_layout']=={'block_count':4,'paragraphs':[0,2],
                'page_break_before':[2],'standalone_breaks':[3]}
        assert len(canonical(invalid['block_layout']).encode())<256
        repaired=revise(store,'task',invalid['ref'],[{'op':'remove','path':'blocks.3'}])
        assert repaired['block_layout']=={'block_count':3,'paragraphs':[0,2],
                'page_break_before':[2],'standalone_breaks':[]}
        assert repaired['status']=='prepared'
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()
