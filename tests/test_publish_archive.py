import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publish_archive
from test_backfill import backfill
import server


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.stage, self.target, self.live = [root / name for name in ('stage.db', 'published.db', 'live.db')]
        with sqlite3.connect(self.live) as db:
            db.execute('CREATE TABLE blocks(number,hash,sample_time)')
            db.execute("INSERT INTO blocks VALUES(13,'anchor',13000)")
        self.db = backfill.database(self.stage)
        self.db.executemany('INSERT INTO metadata VALUES(?,?)',
                            dict(start='10', anchor='13', anchor_hash='anchor', anchor_time='13000').items())
        self.db.commit()
        backfill.store_batch(self.db, [(12,'h12',12000,1,'[]'), (11,'h11',11000,1.01,'[]')],15000)
        self.db.close()
        shutil.copy2(self.stage, self.target)
        self.db = sqlite3.connect(self.stage)
        backfill.store_batch(self.db, [(10,'h10',8000,.999,'[]')],10000)
        self.db.close()
        self.start = patch.object(publish_archive, 'START', 10)
        self.start.start()
        self.addCleanup(self.start.stop)

    def test_atomic_append_preserves_reader_cursor_and_backup(self):
        before = self.target.read_bytes()
        worker = server.HistoryWorker(server.HistoryCache(), 'http://unused', archive=self.target)
        worker.load_archive()
        self.assertEqual(worker.archive_seq,2)
        result = publish_archive.publish(self.stage,self.target,self.live)
        self.assertIn('Published 3',result)
        self.assertEqual(Path(str(self.target)+'.previous').read_bytes(),before)
        self.assertTrue(worker.load_archive())
        self.assertEqual(worker.archive_seq,3)
        with publish_archive.readonly(self.target) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0],3)
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')

    def test_identical_run_does_not_touch_production(self):
        publish_archive.publish(self.stage,self.target,self.live)
        before = self.target.stat(), self.target.read_bytes()
        self.assertIn('untouched',publish_archive.publish(self.stage,self.target,self.live))
        self.assertEqual((self.target.stat(),self.target.read_bytes()),before)

    def test_failed_validation_leaves_live_file_unchanged(self):
        before = self.target.read_bytes()
        with sqlite3.connect(self.stage) as db:
            db.execute('UPDATE samples SET price=.5 WHERE seq=1')
        with self.assertRaisesRegex(ValueError,'existing history'):
            publish_archive.publish(self.stage,self.target,self.live)
        self.assertEqual(self.target.read_bytes(),before)
        with sqlite3.connect(self.stage) as db:
            db.execute('DELETE FROM samples WHERE number=10')
        with self.assertRaisesRegex(ValueError,'contiguous'):
            publish_archive.publish(self.stage,self.target,self.live)
        self.assertEqual(self.target.read_bytes(),before)

    def test_backfill_cannot_open_published_path_or_alias(self):
        alias = self.target.parent / 'alias.db'
        alias.symlink_to(self.target)
        with patch.object(backfill,'PUBLISHED',self.target):
            for path in (self.target,alias):
                with self.assertRaisesRegex(ValueError,'must not write'):
                    backfill.database(path)

    def test_seed_preserves_production_and_refuses_to_overwrite_work(self):
        work = self.stage.parent / 'new-stage.db'
        before = self.target.stat(), self.target.read_bytes()
        publish_archive.seed(work,self.target)
        self.assertEqual((self.target.stat(),self.target.read_bytes()),before)
        with publish_archive.readonly(work) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0],2)
        with self.assertRaises(FileExistsError):
            publish_archive.seed(work,self.target)
