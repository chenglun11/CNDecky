import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cndecky.server import Cache, CDN, GITHUB, STORE, Gateway, GatewayError, Server, check_upstream, validate_origin


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.responses = {}
        self.calls = []
        def downloader(url):
            self.calls.append(url)
            response = self.responses[url]
            if isinstance(response, Exception):
                raise response
            return io.BytesIO(response)
        self.cache = Cache(self.temp.name, downloader=downloader)
        self.gateway = Gateway('https://decky.example.com', self.cache)
        self.digest = hashlib.sha256(b'plugin zip fixture').hexdigest()

    def tearDown(self):
        self.cache.db.close()
        self.temp.cleanup()

    def put_json(self, url, value):
        self.responses[url] = json.dumps(value).encode()

    def test_lists_and_updates_rewrite_downloads_without_changing_hash(self):
        plugin = {'id': 1, 'name': 'Test', 'versions': [{'name':'1.0', 'hash':self.digest}],
                  'image_url':CDN + '/artifact_images/Test%20Plugin.png'}
        self.put_json(STORE+'/plugins', [plugin])
        self.put_json(STORE+'/plugins/versions', {'Test':['1.0',self.digest]})
        result, _ = self.gateway.plugins({})
        versions, _ = self.gateway.versions()
        self.assertEqual(result[0]['versions'][0]['hash'], self.digest)
        expected = f'https://decky.example.com/artifacts/{self.digest}.zip'
        self.assertEqual(result[0]['versions'][0]['artifact'], expected)
        self.assertEqual(versions['Test'][2], expected)
        self.assertEqual(result[0]['image_url'], 'https://decky.example.com/images/Test%20Plugin.png')
        self.assertFalse(result[0]['versions'][0]['cached'])

    def test_hash_verification_and_repeated_download_hit(self):
        url = CDN + '/versions/' + self.digest + '.zip'
        self.responses[url] = b'plugin zip fixture'
        path, state = self.cache.get(url, expected=self.digest)
        self.assertEqual(state, 'MISS')
        self.assertEqual(path.read_bytes(), b'plugin zip fixture')
        self.assertEqual(self.cache.get(url, expected=self.digest)[1], 'HIT')
        self.assertEqual(self.calls, [url])
        self.assertEqual(self.cache.stats()['hits'], 1)
        self.assertEqual(self.cache.recent()[0]['url'], url)
        reopened = Cache(self.temp.name)
        self.assertEqual(reopened.stats()['hits'], 1)
        reopened.db.close()

    def test_corrupted_download_never_published(self):
        self.responses['test'] = b'wrong'
        with self.assertRaisesRegex(GatewayError, 'SHA-256'):
            self.cache.get('test', expected=self.digest)
        self.assertEqual(self.cache.stats()['files'], 0)
        self.assertEqual(self.cache.recent(), [])
        self.assertEqual(list(Path(self.temp.name).glob('.part-*')), [])

    def test_transient_failure_can_use_recent_metadata_but_404_cannot(self):
        self.responses['metadata'] = b'[]'
        path, _ = self.cache.get('metadata')
        self.responses['metadata'] = URLError('timeout')
        self.assertEqual(self.cache.get('metadata', ttl=0, stale_for=60)[1], 'STALE')
        self.responses['metadata'] = HTTPError('metadata',404,'gone',{},None)
        with self.assertRaises(GatewayError) as caught:
            self.cache.get('metadata', ttl=0, stale_for=60)
        self.assertEqual(caught.exception.status,404)

    def test_stale_cache_expires(self):
        self.responses['metadata'] = b'[]'
        path, _ = self.cache.get('metadata')
        import os
        os.utime(path, (0,0))
        self.responses['metadata'] = URLError('timeout')
        with self.assertRaises(GatewayError):
            self.cache.get('metadata', ttl=0, stale_for=60)

    def test_cache_limit_evicts_files_and_sqlite_index(self):
        self.cache.limit = 8
        self.responses.update(a=b'12345',b=b'67890')
        self.cache.get('a')
        self.cache.get('b')
        self.assertFalse(self.cache.contains('a'))
        self.assertTrue(self.cache.contains('b'))
        self.assertEqual(self.cache.stats()['bytes'], 5)
        self.assertEqual(len(self.cache.recent()), 1)

    def test_oversized_and_invalid_metadata_do_not_replace_cache(self):
        self.responses['x'] = b'12345'
        with self.assertRaises(GatewayError):
            self.cache.get('x', max_bytes=4)
        self.assertFalse(self.cache.contains('x'))
        self.responses[STORE+'/plugins'] = b'<html>error</html>'
        with self.assertRaises(GatewayError):
            self.gateway.plugins({})
        self.assertFalse(self.cache.contains(STORE+'/plugins'))

    def test_same_file_concurrent_fill_happens_once(self):
        self.responses['x'] = b'bytes'
        threads = [threading.Thread(target=lambda:self.cache.get('x')) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(self.calls.count('x'), 1)

    def test_slow_download_does_not_block_status(self):
        entered, release = threading.Event(), threading.Event()
        def downloader(url):
            entered.set()
            release.wait(3)
            return io.BytesIO(b'payload')
        self.cache.downloader = downloader
        thread = threading.Thread(target=lambda: self.cache.get('slow'))
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            start = time.monotonic()
            self.assertEqual(self.cache.stats()['files'], 0)
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            release.set(); thread.join()

    def test_open_download_survives_cache_eviction(self):
        self.cache.limit = 8
        self.responses.update(a=b'12345', b=b'67890')
        stream, _ = self.cache.get('a', open_file=True)
        try:
            self.cache.get('b')
            self.assertFalse(self.cache.contains('a'))
            self.assertEqual(stream.read(), b'12345')
        finally:
            stream.close()

    def test_unexpected_artifact_not_silently_replaced(self):
        self.put_json(STORE+'/plugins', [{'name':'Test','versions':[{'hash':self.digest,'artifact':'https://evil.example/test'}]}])
        with self.assertRaises(GatewayError): self.gateway.plugins({})

    def test_release_requires_official_sha_and_exact_asset_url(self):
        release = {'tag_name':'v3.3.1','assets':[{'name':'PluginLoader','size':10,
            'browser_download_url':'https://github.com/SteamDeckHomebrew/decky-loader/releases/download/v3.3.1/PluginLoader',
            'digest':'sha256:'+self.digest}]}
        self.put_json(GITHUB+'/releases/latest', release)
        manifest, _ = self.gateway.release()
        self.assertEqual(manifest['assets']['PluginLoader']['sha256'],self.digest)
        self.assertEqual(manifest['assets']['PluginLoader']['url'],'https://decky.example.com/loader/v3.3.1/PluginLoader')

    def test_origin_and_upstream_boundaries(self):
        self.assertEqual(validate_origin('https://decky.example.com/'),'https://decky.example.com')
        for value in ['http://example.com','https://evil.example/@x','https://u:p@example.com','https://example.com/;x']:
            with self.assertRaises(ValueError): validate_origin(value)
        for value in ['http://github.com/x','https://127.0.0.1/x','https://github.com.evil.example/x','https://github.com:444/x','https://x@github.com/x']:
            with self.assertRaises(GatewayError): check_upstream(value)

    def test_http_contract_cors_downloads_and_errors(self):
        self.put_json(STORE+'/plugins', [])
        self.responses[CDN+'/versions/'+self.digest+'.zip'] = b'plugin zip fixture'
        server = Server(('127.0.0.1',0),self.gateway)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(Request(base+'/plugins',method='OPTIONS',headers={'Origin':'https://steamloopback.host'})) as response:
                self.assertEqual(response.status,204)
                self.assertIn('X-Decky-Version',response.headers['Access-Control-Allow-Headers'])
            with urlopen(base+'/plugins') as response:
                self.assertEqual(json.load(response),[])
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(base+'/plugins',headers={'X-Decky-Version':'v2.0.0'}))
            self.assertEqual(caught.exception.code,426)
            with urlopen(base+'/artifacts/'+self.digest+'.zip') as response:
                self.assertEqual(hashlib.sha256(response.read()).hexdigest(),self.digest)
            with urlopen(Request(base+'/plugins/Test/versions/1.0/increment',method='POST')) as response:
                self.assertFalse(json.load(response)['forwarded'])
            with self.assertRaises(HTTPError) as caught: urlopen(base+'/proxy?url=http://127.0.0.1')
            self.assertEqual(caught.exception.code,404)
            with urlopen(base+'/install.sh') as response:
                script = response.read().decode()
                self.assertIn('MIRROR=https://decky.example.com',script)
                self.assertNotIn('@ORIGIN@',script)
            with urlopen(base+'/api/client') as response:
                expected = json.load(response)['sha256']
            with urlopen(base+'/client.py') as response:
                self.assertEqual(hashlib.sha256(response.read()).hexdigest(),expected)
            for mode in ('install', 'configure', 'restore'):
                url = base + f'/launchers/{mode}.desktop'
                with urlopen(Request(url, headers={'Host': 'untrusted.example'})) as response:
                    self.assertEqual(response.headers.get_content_type(), 'application/x-desktop')
                    self.assertEqual(response.headers['Content-Disposition'],
                                     f'attachment; filename="CNDecky-{mode}.desktop"')
                    launcher = response.read()
                    self.assertIn(b'https://decky.example.com', launcher)
                    self.assertNotIn(b'untrusted.example', launcher)
                with urlopen(Request(url, method='HEAD')) as response:
                    self.assertEqual(int(response.headers['Content-Length']), len(launcher))
                    self.assertEqual(response.read(), b'')
            with self.assertRaises(HTTPError) as caught:
                urlopen(base+'/launchers/custom.desktop?command=whoami')
            self.assertEqual(caught.exception.code, 404)
            self.gateway.origin = 'http://localhost:8080'
            with self.assertRaises(HTTPError) as caught:
                urlopen(base+'/launchers/install.desktop')
            self.assertEqual(caught.exception.code, 409)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
