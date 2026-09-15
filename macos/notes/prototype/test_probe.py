import unittest
from unittest.mock import patch

import probe


class MutationGuardTests(unittest.TestCase):
    def setUp(self):
        self.state = {"revision": "v1", "text": "Verdigris Checklist Probe test\nitem\n",
                      "items": [{"id": "first", "text": "item", "checked": False,
                                 "location": 31, "length": 4, "indent": 0}],
                      "attachmentRuns": []}

    def test_stale_revision_never_dispatches(self):
        with patch.object(probe, "show"), patch.object(probe, "snapshot", return_value=self.state), \
             patch.object(probe, "job") as dispatch:
            with self.assertRaisesRegex(RuntimeError, "Revision conflict"):
                probe.set_checked("first", True, "old")
            dispatch.assert_not_called()

    def test_repeated_desired_state_is_noop(self):
        with patch.object(probe, "show"), patch.object(probe, "snapshot", return_value=self.state), \
             patch.object(probe, "job") as dispatch:
            self.assertFalse(probe.set_checked("first", False, "v1")["changed"])
            dispatch.assert_not_called()

    def test_change_during_target_lookup_never_dispatches(self):
        changed = {**self.state, "revision": "v2"}
        with patch.object(probe, "show"), patch.object(probe, "snapshot", side_effect=[self.state, changed]), \
             patch.object(probe, "editor_text", return_value=self.state["text"]), \
             patch.object(probe, "job") as dispatch:
            with self.assertRaisesRegex(RuntimeError, "Revision changed"):
                probe.set_checked("first", True, "v1")
            dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
