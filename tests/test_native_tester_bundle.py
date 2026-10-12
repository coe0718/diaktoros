"""Evidence archives include diagnostics without following runtime paths or links."""
import _home_guard  # noqa: F401
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import native_tester_bundle


class EvidenceBundleTests(unittest.TestCase):
    def test_selected_evidence_is_packaged_without_downloads_images_or_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'result.json').write_text('{"passed":false}')
            (root/'failed.log').write_text('failure')
            (root/'hermes').mkdir()
            (root/'hermes/private.log').write_text('exclude')
            (root/'turn.dmg').write_text('exclude')
            (root/'linked.log').symlink_to(root/'turn.dmg')
            (root/'soak/receipts').mkdir(parents=True)
            (root/'soak/events.jsonl').write_text('event')
            (root/'soak/receipts/dk-volume-fixture.json').write_text('{}')
            with zipfile.ZipFile(native_tester_bundle.bundle(root)) as archive:
                names = archive.namelist()
                self.assertIn('failed.log', names)
                self.assertIn('soak/receipts/dk-volume-fixture.json', names)
                self.assertNotIn('linked.log', names)
                self.assertNotIn('turn.dmg', names)
                self.assertNotIn('hermes/private.log', names)
                manifest = json.loads(archive.read('bundle-manifest.json'))
                self.assertEqual(manifest['omitted'][0]['path'], 'linked.log')

    def test_limits_record_truncation_and_omission_and_rebuild_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'a.log').write_text('0123456789')
            (root/'b.log').write_text('0123456789')
            with mock.patch.object(native_tester_bundle, 'MAX_FILE', 4), \
                    mock.patch.object(native_tester_bundle, 'MAX_TOTAL', 4):
                path = native_tester_bundle.bundle(root)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.read('a.log'), b'6789')
                manifest = json.loads(archive.read('bundle-manifest.json'))
                self.assertTrue(manifest['files'][0]['truncated'])
                self.assertEqual(manifest['omitted'][0]['path'], 'b.log')
            native_tester_bundle.bundle(root)
            with zipfile.ZipFile(path) as archive:
                self.assertIn('b.log', archive.namelist())
                self.assertNotIn('evidence.zip', archive.namelist())


if __name__ == '__main__':
    unittest.main()
