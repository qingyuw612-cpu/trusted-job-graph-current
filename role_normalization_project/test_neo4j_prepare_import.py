import csv, tempfile, unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from neo4j_prepare_import import cypher, cypher_statements, prepare

class TestPrepare(unittest.TestCase):
    def test_real_strict_counts(self):
        roles,jobs,edges,rejected,stats=prepare()
        self.assertEqual(stats['relationships'],2132)
        self.assertEqual(stats['input_mappings'],17230)
        self.assertTrue(all(e['jd_id'] and e['role_id'] for e in edges))
        self.assertFalse(any(r['approval_status']=='PENDING' for r in edges))
    def test_ai_is_opt_in(self):
        _,_,strict,_,_=prepare(); _,_,expanded,_,_=prepare(include_ai=True)
        self.assertLess(len(strict),len(expanded))

    def test_cypher_preview_splits_into_executable_statements(self):
        statements = cypher_statements(cypher(True))
        self.assertEqual(len(statements), 5)
        self.assertTrue(all(not statement.startswith("//") for statement in statements))
        self.assertTrue(statements[0].startswith("CREATE CONSTRAINT"))

if __name__=='__main__': unittest.main()
