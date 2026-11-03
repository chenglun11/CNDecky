import importlib.util
import hashlib
import io
import itertools
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr

spec = importlib.util.spec_from_file_location('client', Path(__file__).resolve().parents[1]/'scripts/client.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class DownloadProgressTests(unittest.TestCase):
    def download(self, payload, headers=None, expected=None, total=None, output=None, progress=True):
        response = io.BytesIO(payload)
        response.headers = headers or {}
        output = output if output is not None else io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'download'
            with patch.object(client, 'build_opener') as opener, redirect_stderr(output):
                opener.return_value.open.return_value = response
                client.download('https://decky.example.com/file', target,
                                expected=expected, progress=progress, total=total)
            self.assertEqual(target.read_bytes(), payload)
        return output.getvalue()

    def test_known_size_shows_bytes_percent_speed_eta_and_verified_completion(self):
        payload = b'x' * (128 * 1024)
        log = self.download(payload, expected=hashlib.sha256(payload).hexdigest(), total=len(payload))
        for text in ('正在连接', '100%', '0.1 MiB / 0.1 MiB', 'MiB/s', '预计剩余', 'SHA-256 校验通过'):
            self.assertIn(text, log)

    def test_content_length_is_used_when_release_size_is_missing(self):
        self.assertIn('100%', self.download(b'x' * 1024, {'Content-Length': '1024'}))

    def test_unknown_or_invalid_length_shows_bytes_without_inventing_percent(self):
        for headers in ({}, {'Content-Length': 'unknown'}, {'Content-Length': '-1'}):
            log = self.download(b'x' * 1024, headers)
            self.assertIn('已下载', log)
            self.assertIn('MiB/s', log)
            self.assertNotIn('%', log)
            self.assertNotIn('预计剩余', log)

    def test_hash_failure_never_reports_success(self):
        output = io.StringIO()
        with self.assertRaisesRegex(client.InstallError, 'SHA-256'):
            self.download(b'broken', expected='0' * 64, output=output)
        self.assertIn('正在校验', output.getvalue())
        self.assertNotIn('校验通过', output.getvalue())

    def test_progress_updates_before_download_finishes(self):
        with patch.object(client.time, 'monotonic', side_effect=itertools.count()):
            log = self.download(b'x' * (256 * 1024), total=256 * 1024)
        for percent in ('25%', '50%', '75%', '100%'):
            self.assertIn(percent, log)

    def test_other_downloads_remain_quiet(self):
        self.assertEqual(self.download(b'metadata', progress=False), '')

    def test_timeout_preserves_progress_and_never_reports_success(self):
        class StalledResponse(io.BytesIO):
            headers = {}

            def read1(self, size):
                chunk = super().read1(size)
                if not chunk:
                    raise TimeoutError('timed out')
                return chunk

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(client, 'build_opener') as opener, redirect_stderr(output):
                opener.return_value.open.return_value = StalledResponse(b'partial')
                with self.assertRaises(TimeoutError):
                    client.download('https://decky.example.com/file', Path(directory) / 'download', progress=True)
        self.assertIn('已下载', output.getvalue())
        self.assertTrue(output.getvalue().endswith('\n'))
        self.assertNotIn('下载完成', output.getvalue())


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root/'home/deck'
        self.home.mkdir(parents=True)
        self.hb = self.home/'homebrew'
        (self.hb/'settings').mkdir(parents=True)
        (self.hb/'services').mkdir()
        (self.home/'.steam/steam').mkdir(parents=True)
        self.settings = self.hb/'settings/loader.json'
        self.settings.write_text(json.dumps({'store':0,'pluginOrder':['CSS Loader'],'other':{'key':True}}))
        self.binary = self.hb/'services/PluginLoader'
        self.binary.write_bytes(b'old binary')
        self.staged = self.root/'staged'
        self.staged.write_bytes(b'new binary')
        self.calls=[]
        def runner(*args, check=True):
            self.calls.append(args)
            return subprocess.CompletedProcess(args,0)
        self.runner=runner

    def tearDown(self): self.temp.cleanup()

    def test_configure_preserves_other_settings_and_stops_before_writing(self):
        backup = client.apply(self.home,'https://decky.example.com',runner=self.runner,system_root=self.root)
        result=json.loads(self.settings.read_text())
        self.assertEqual(result['store'],2)
        self.assertEqual(result['store-url'],'https://decky.example.com/plugins')
        self.assertEqual(result['pluginOrder'],['CSS Loader'])
        self.assertEqual(result['other'],{'key':True})
        self.assertEqual(self.binary.read_bytes(),b'old binary')
        self.assertEqual(json.loads((backup/'0').read_text())['store'],0)
        self.assertIn(('systemctl','stop','plugin_loader'),self.calls)

    def test_restore_official_keeps_plugins_and_other_settings(self):
        self.settings.write_text(json.dumps({'store':2,'store-url':'https://old.example/plugins','pluginOrder':['A']}))
        client.apply(self.home,'https://decky.example.com',restore=True,runner=self.runner,system_root=self.root)
        data=json.loads(self.settings.read_text())
        self.assertEqual(data,{'store':0,'pluginOrder':['A']})

    def test_repeat_install_keeps_plugin_files_and_makes_backups(self):
        plugin=self.hb/'plugins/Test/main.py'; plugin.parent.mkdir(parents=True); plugin.write_text('existing')
        for _ in range(2):
            client.apply(self.home,'https://decky.example.com',self.staged,'v3.3.1',runner=self.runner,system_root=self.root)
        self.assertEqual(plugin.read_text(),'existing')
        self.assertEqual(self.binary.read_bytes(),b'new binary')
        self.assertEqual((self.hb/'services/.loader.version').read_text(),'v3.3.1\n')
        self.assertEqual(len(list((self.hb/'.cndecky-backups').iterdir())),2)
        self.assertTrue((self.home/'.steam/steam/.cef-enable-remote-debugging').exists())

    def test_failed_start_rolls_back_binary_settings_service_and_flags(self):
        original=self.settings.read_bytes()
        failures=[]
        def runner(*args,check=True):
            if args==('systemctl','start','plugin_loader') and not failures:
                failures.append(True)
                raise subprocess.CalledProcessError(1,args)
            return subprocess.CompletedProcess(args,0)
        with self.assertRaises(subprocess.CalledProcessError):
            client.apply(self.home,'https://decky.example.com',self.staged,'v3.3.1',runner=runner,system_root=self.root)
        self.assertEqual(self.binary.read_bytes(),b'old binary')
        self.assertEqual(self.settings.read_bytes(),original)
        self.assertFalse((self.root/'etc/systemd/system/plugin_loader.service').exists())
        self.assertFalse((self.home/'.steam/steam/.cef-enable-remote-debugging').exists())

    def test_malformed_settings_and_symlinks_fail_before_service_stop(self):
        self.settings.write_text('broken')
        with self.assertRaises(client.InstallError):
            client.apply(self.home,'https://decky.example.com',runner=self.runner,system_root=self.root)
        self.assertEqual(self.calls,[])
        self.settings.unlink(); self.settings.symlink_to(self.staged)
        with self.assertRaises(client.InstallError):
            client.apply(self.home,'https://decky.example.com',runner=self.runner,system_root=self.root)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.staged.read_bytes(),b'new binary')

    def test_dry_run_restore_has_no_side_effects(self):
        result=subprocess.run(['python3',str(Path(client.__file__)),'--mirror','https://decky.example.com','--restore-official','--dry-run'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('没有修改文件',result.stdout)

    def test_same_origin_redirect_rejects_foreign_domain(self):
        from urllib.request import Request
        with self.assertRaises(client.InstallError):
            client.SameOriginRedirect().redirect_request(Request('https://decky.example.com/x'),None,302,'',{},'https://evil.example/file')

    def test_interrupted_install_rolls_back(self):
        original = self.settings.read_bytes()
        interrupted = []
        def runner(*args, check=True):
            if args == ('systemctl', 'start', 'plugin_loader') and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt()
            return subprocess.CompletedProcess(args, 0)
        with self.assertRaises(KeyboardInterrupt):
            client.apply(self.home, 'https://decky.example.com', self.staged, 'v3.3.1',
                         runner=runner, system_root=self.root)
        self.assertEqual(self.binary.read_bytes(), b'old binary')
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse((self.root/'etc/systemd/system/plugin_loader.service').exists())

    def test_disk_full_rollback_uses_original_inode_without_allocating(self):
        original = self.settings.read_bytes()
        original_inode = self.binary.stat().st_ino
        write = client.atomic_write
        def fail_after_binary(path, *args, **kwargs):
            if path != self.binary:
                raise OSError(28, 'No space left on device')
            return write(path, *args, **kwargs)
        with patch.object(client, 'atomic_write', side_effect=fail_after_binary):
            with self.assertRaises(OSError):
                client.apply(self.home, 'https://decky.example.com', self.staged, 'v3.3.1',
                             runner=self.runner, system_root=self.root)
        self.assertEqual(self.binary.read_bytes(), b'old binary')
        self.assertEqual(self.binary.stat().st_ino, original_inode)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(self.calls[-1], ('systemctl', 'start', 'plugin_loader'))

    def test_interrupt_during_initial_stop_restores_running_service(self):
        original = self.settings.read_bytes()
        def runner(*args, check=True):
            self.calls.append(args)
            if args == ('systemctl', 'stop', 'plugin_loader'):
                raise KeyboardInterrupt()
            return subprocess.CompletedProcess(args, 0)
        with self.assertRaises(KeyboardInterrupt):
            client.apply(self.home, 'https://decky.example.com', runner=runner, system_root=self.root)
        self.assertEqual(self.calls[-1], ('systemctl', 'start', 'plugin_loader'))
        self.assertEqual(self.settings.read_bytes(), original)


if __name__=='__main__': unittest.main()
