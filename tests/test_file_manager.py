import io
import errno
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from agenthub import file_manager as fm, files, server


class FileManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.manager = fm.Manager(self.root / 'state')
        self.scope = 'fixture-scope'

    def wait(self, ident):
        for _ in range(500):
            job = self.manager.get(self.scope, ident)
            if job['state'] not in {'queued', 'running'}:
                return job
            time.sleep(.01)
        self.fail('job did not complete')

    def job(self, action, **kwargs):
        return self.wait(self.manager.start(self.scope, {'action': action, **kwargs})['id'])

    def good(self, action, **kwargs):
        job = self.job(action, **kwargs)
        self.assertEqual(job['state'], 'completed', job)
        return job

    def test_create_rename_copy_move_conflicts_and_trash_restore(self):
        self.good('mkdir', destination=str(self.work), name='子目录')
        self.good('new-file', destination=str(self.work), name='中文.txt')
        source = self.work / '中文.txt'
        source.write_text('content')
        self.good('rename', paths=[str(source)], name='重命名.txt')
        source = self.work / '重命名.txt'
        dest = self.work / '子目录'
        self.good('copy', paths=[str(source)], destination=str(dest))
        self.assertEqual((dest/source.name).read_text(), 'content')
        self.assertEqual(self.job('copy', paths=[str(source)], destination=str(dest))['state'], 'failed')
        self.good('copy', paths=[str(source)], destination=str(dest), conflict='keep')
        self.assertEqual((dest/'重命名 (1).txt').read_text(), 'content')
        source.write_text('changed')
        self.good('copy', paths=[str(source)], destination=str(dest), conflict='skip')
        self.assertEqual((dest/source.name).read_text(), 'content')
        self.good('move', paths=[str(source)], destination=str(dest), conflict='replace')
        self.assertFalse(source.exists())
        self.assertEqual((dest/source.name).read_text(), 'changed')
        trash = self.manager.trash_list(self.scope)
        self.assertEqual(len(trash), 1)
        self.good('restore', paths=[trash[0]['id']], conflict='keep')
        self.assertEqual((dest/'重命名 (2).txt').read_text(), 'content')
        self.good('trash', paths=[str(dest/source.name)])
        item = self.manager.trash_list(self.scope)[0]
        self.good('purge', paths=[item['id']])
        self.assertEqual(self.manager.trash_list(self.scope), [])

    def test_symlinks_are_operated_on_not_targets(self):
        target = self.work/'target'
        target.mkdir()
        (target/'important').write_text('keep')
        link = self.work/'link'
        link.symlink_to(target)
        dest = self.work/'destination'
        dest.mkdir()
        self.good('copy', paths=[str(link)], destination=str(dest))
        self.assertTrue((dest/'link').is_symlink())
        self.good('trash', paths=[str(link)])
        self.assertTrue((target/'important').is_file())
        self.assertFalse(os.path.lexists(link))
        broken = self.work/'broken'
        broken.symlink_to(self.work/'missing')
        self.good('rename', paths=[str(broken)], name='renamed-link')
        self.assertTrue((self.work/'renamed-link').is_symlink())

    def test_root_self_descendant_special_paths_rejected(self):
        directory = self.work/'directory'
        directory.mkdir()
        child = directory/'child'
        child.mkdir()
        for path in ['/', str(Path.home()), str(self.root/'state'), str(self.root)]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.manager.start(self.scope, {'action':'trash','paths':[path]})
        for action in ['copy','move']:
            with self.assertRaises(ValueError):
                self.manager.start(self.scope, {'action':action,'paths':[str(directory)],'destination':str(child)})
        fifo = self.work/'fifo'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            fm.path_for(str(fifo))
        for name in ['..', '', '/tmp', 'bad/name', 'bad\\name']:
            with self.assertRaises(ValueError):
                self.manager.start(self.scope, {'action':'mkdir','destination':str(self.work),'name':name})

    def test_cancel_preserves_sources_and_retry_only_remaining(self):
        source = self.work/'large.bin'
        source.write_bytes(b'x' * (3*fm.CHUNK))
        destination = self.work/'destination'
        destination.mkdir()
        original_check = self.manager.check
        def cancel(job, count=0):
            if count:
                self.manager.cancels.add(job['id'])
            original_check(job, count)
        with patch.object(self.manager,'check',side_effect=cancel):
            job = self.job('copy', paths=[str(source)], destination=str(destination))
        self.assertEqual(job['state'],'cancelled')
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(source.stat().st_size,3*fm.CHUNK)
        self.manager.control(self.scope,job['id'],'retry')
        self.assertEqual(self.wait(job['id'])['state'],'completed')
        self.assertEqual((destination/source.name).read_bytes(),source.read_bytes())
        second = self.work/'second.txt'
        second.write_text('second')
        job = self.job('copy',paths=[str(source),str(second)],destination=str(destination))
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['completed'],[str(second)])
        self.manager.control(self.scope,job['id'],'retry','keep')
        self.assertEqual(self.wait(job['id'])['state'],'completed')
        self.assertFalse((destination/'second (1).txt').exists())

    def test_upload_offsets_interruption_resume_conflict(self):
        spec = {'action':'upload','destination':str(self.work),'name':'上传.txt','size':6,'modified':1}
        job = self.manager.start(self.scope,spec)
        ident = job['id']
        self.manager.upload(self.scope,ident,0,b'abc')
        with self.assertRaises(ValueError):
            self.manager.upload(self.scope,ident,0,b'abc')
        self.assertFalse((self.work/'上传.txt').exists())
        self.manager = fm.Manager(self.root/'state')
        self.assertEqual(self.manager.get(self.scope,ident)['bytes'],3)
        self.manager.control(self.scope,ident,'cancel')
        with self.assertRaises(ValueError):
            self.manager.upload(self.scope,ident,3,b'def')
        self.manager.control(self.scope,ident,'retry')
        job = self.manager.upload(self.scope,ident,3,b'def')
        self.assertEqual(self.wait(ident)['state'],'completed')
        self.assertEqual((self.work/'上传.txt').read_bytes(),b'abcdef')
        job = self.manager.start(self.scope,spec)
        self.manager.upload(self.scope,job['id'],0,b'123456')
        self.assertEqual(self.wait(job['id'])['state'],'failed')
        self.manager.control(self.scope,job['id'],'retry','keep')
        self.manager.upload(self.scope,job['id'],6,b'')
        self.assertEqual(self.wait(job['id'])['state'],'completed')
        self.assertEqual((self.work/'上传 (1).txt').read_bytes(),b'123456')

    def test_archive_roundtrip_zip_slip_and_links(self):
        source = self.work/'source'
        source.mkdir()
        (source/'中文.txt').write_text('hello')
        (source/'empty').mkdir()
        self.good('compress',paths=[str(source)],destination=str(self.work),name='result.zip')
        self.good('extract',paths=[str(self.work/'result.zip')],destination=str(self.work))
        self.assertEqual((self.work/'result/source/中文.txt').read_text(),'hello')
        self.assertTrue((self.work/'result/source/empty').is_dir())
        job = self.good('bundle',paths=[str(source)])
        archive, name = self.manager.artifact(self.scope,job['id'])
        self.assertEqual(name,'source.zip')
        self.assertTrue(zipfile.is_zipfile(archive))
        with self.assertRaises(PermissionError):
            self.manager.artifact('other',job['id'])
        bad = self.work/'bad.zip'
        with zipfile.ZipFile(bad,'w') as archive:
            archive.writestr('../escaped.txt','bad')
        self.assertEqual(self.job('extract',paths=[str(bad)],destination=str(self.work))['state'],'failed')
        self.assertFalse((self.work/'escaped.txt').exists())
        self.assertFalse((self.work/'bad').exists())
        (source/'link').symlink_to(source/'中文.txt')
        self.assertEqual(self.job('bundle',paths=[str(source)])['state'],'failed')

    def test_durable_jobs_scope_and_no_clobber(self):
        job = self.good('new-file',destination=str(self.work),name='a.txt')
        self.manager.grant(self.scope,'./work')
        self.manager = fm.Manager(self.root/'state')
        self.assertEqual(self.manager.get(self.scope,job['id'])['state'],'completed')
        self.assertIn((self.scope,'./work'),self.manager.grants)
        self.assertEqual(self.manager.jobs('other'),[])
        with self.assertRaises(PermissionError):
            self.manager.control('other',job['id'],'retry')
        source = self.work/'source'
        source.write_text('source')
        destination = self.work/'a.txt'
        with self.assertRaises(FileExistsError):
            fm.rename_noreplace(source,destination)
        self.assertEqual(destination.read_text(),'')
        self.assertEqual(source.read_text(),'source')

    def test_sort_hidden_info_and_media_range(self):
        (self.work/'small.txt').write_text('x')
        (self.work/'large.txt').write_text('x'*10)
        (self.work/'.hidden').touch()
        listing = files.list_directory(self.work,sort='size',order='desc',hidden=False)
        self.assertEqual([e['name'] for e in listing['entries']],['large.txt','small.txt'])
        html = self.work/'unsafe.html'
        html.write_text('<script>alert(1)</script>')
        self.assertEqual(fm.describe(html)['preview'],'text')
        self.assertEqual(fm.describe(html)['text'],html.read_text())
        video = self.work/'video.mp4'
        video.write_bytes(b'0123456789')
        handler = object.__new__(server.Handler)
        headers, codes = {}, []
        handler.headers = {'Range':'bytes=2-5'}
        handler.send_response = codes.append
        handler.send_header = headers.__setitem__
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler._file_stream(video)
        self.assertEqual(codes,[206])
        self.assertEqual(headers['Content-Range'],'bytes 2-5/10')
        self.assertEqual(handler.wfile.getvalue(),b'2345')

    def test_cross_filesystem_move_and_restore_remain_recoverable(self):
        source = self.work/'original.txt'
        source.write_text('cross-device contents')
        destination = self.work/'destination'
        destination.mkdir()
        original_rename = fm.rename_noreplace
        def cross_device(src, dst):
            if src == source or src.name == 'data':
                raise OSError(errno.EXDEV, 'cross-device fixture')
            return original_rename(src, dst)
        with patch.object(fm,'rename_noreplace',side_effect=cross_device):
            self.good('move',paths=[str(source)],destination=str(destination))
            self.assertFalse(source.exists())
            self.assertEqual((destination/source.name).read_text(),'cross-device contents')
            item = self.manager.trash_list(self.scope)[0]
            self.good('restore',paths=[item['id']])
        self.assertEqual(source.read_text(),'cross-device contents')


if __name__ == '__main__':
    unittest.main()
