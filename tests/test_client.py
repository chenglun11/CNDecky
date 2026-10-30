import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('client', Path(__file__).resolve().parents[1]/'scripts/client.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


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
