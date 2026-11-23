"""A missing page banner must fail rather than silently skip."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_page_warning


class PageWarningRegression(unittest.TestCase):
    def test_missing_banner_is_a_failure(self):
        with tempfile.TemporaryDirectory() as root:
            page=Path(root)/'page.html';page.write_text('TEST missing banner')
            case=test_page_warning.WarningBannerTests('test_page_script_keeps_the_banner_up_to_date')
            with patch.object(test_page_warning,'PAGE',page):
                result=unittest.TestResult();case.run(result)
            self.assertEqual([],result.skipped)
            self.assertEqual(1,len(result.failures))


if __name__=='__main__':unittest.main()
