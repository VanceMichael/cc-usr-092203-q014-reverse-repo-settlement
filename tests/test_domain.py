import unittest
from pathlib import Path
from src.domain import load_domain

class DomainTest(unittest.TestCase):
    def test_fixture_matches_domain(self):
        value = load_domain(Path("fixtures/domain.json"))
        self.assertEqual(value["domain"], "reverse-repo-settlement")
        self.assertGreaterEqual(len(value["constraints"]), 2)

if __name__ == "__main__":
    unittest.main()
