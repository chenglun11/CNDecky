import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from cndecky.launcher import BOOTSTRAP, MODES, desktop_entry


def decode_desktop_exec(text):
    """Test-side Desktop Entry string decoding, followed by quoted argv parsing."""
    value = next(line[5:] for line in text.splitlines() if line.startswith('Exec='))
    # Desktop Entry string unescaping precedes command-line argument unquoting.
    value = value.replace('\\\\', '\\')
    return shlex.split(value.replace('%%', '%'))


class LauncherTests(unittest.TestCase):
    def test_launcher_opens_konsole_with_a_separate_origin_and_selected_mode(self):
        for mode, (_, _, flag) in MODES.items():
            with self.subTest(mode=mode):
                text = desktop_entry('https://decky.example.com', mode)
                args = decode_desktop_exec(text)
                self.assertEqual(args[:6], ['konsole', '--separate', '--hold', '-e', 'sh', '-c'])
                # shlex keeps backslashes before $ inside double quotes; desktop parsers remove them.
                body = args[6].replace('\\$', '$').replace('\\`', '`')
                self.assertEqual(body, BOOTSTRAP)
                self.assertEqual(args[7:], ['cndecky-launcher', 'https://decky.example.com', flag])
                self.assertIn('Terminal=false', text)

    def test_no_http_launcher_or_arbitrary_action(self):
        with self.assertRaises(ValueError): desktop_entry('http://localhost:8080', 'install')
        with self.assertRaises(KeyError): desktop_entry('https://decky.example.com', 'delete-everything')

    def test_bootstrap_downloads_before_running_and_passes_only_fixed_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = root / 'curl'
            stub.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
args=sys.argv[1:]
pathlib.Path(os.environ['CN_TEST_CURL_LOG']).write_text(json.dumps(args))
target=pathlib.Path(args[args.index('-o')+1])
target.write_text('#!/bin/sh\\nprintf "%s\\\\n" "$@" >"$CN_TEST_ARGS"\\n')
sys.exit(int(os.environ.get('CN_TEST_CURL_EXIT', '0')))
''')
            stub.chmod(0o755)
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                       CN_TEST_CURL_LOG=str(root/'curl.json'), CN_TEST_ARGS=str(root/'args'))
            for mode, (_, _, flag) in MODES.items():
                result = subprocess.run(['sh','-c',BOOTSTRAP,'cndecky-launcher','https://decky.example.com',flag],
                                        env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual((root/'args').read_text().strip(),flag)
                args=json.loads((root/'curl.json').read_text())
                self.assertIn('https://decky.example.com/install.sh',args)
                self.assertEqual(args[args.index('--proto')+1], '=https')
                self.assertEqual(args[args.index('--proto-redir')+1], '=https')
                self.assertFalse(Path(args[args.index('-o')+1]).exists(), 'temporary script must be cleaned up')
            (root/'args').unlink()
            # Even if curl leaves a partial executable file, it must never be run.
            result = subprocess.run(['sh','-c',BOOTSTRAP,'cndecky-launcher','https://decky.example.com',''],
                                    env=dict(env,CN_TEST_CURL_EXIT='22'),capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((root/'args').exists())
            self.assertIn('Download failed',result.stdout)


if __name__ == '__main__': unittest.main()
