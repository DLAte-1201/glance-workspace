"""Verify original character bytes and narrowly allowed HTTP image routes."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from urllib.error import HTTPError
from urllib.request import urlopen
from server import LocalServer
from workroom import Workroom


def main():
    originals = json.loads(Path('CHARACTER-LOCAL-VERIFIED.json').read_text('utf-8'))['files']
    checked = []
    with tempfile.TemporaryDirectory(prefix='glance-assets-') as folder:
        room = Workroom(folder)
        server = LocalServer(('127.0.0.1', 0), room)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            for role, url, mime in [('Codex','/assets/codex-reference.png','image/png'),
                                   ('Claude','/assets/claude-reference.png','image/png'),
                                   ('Gemini','/assets/gemini-reference.jpg','image/jpeg')]:
                source = next(x for x in originals if x['role'] == role and (role != 'Gemini' or x.get('format') == 'JPEG'))
                expected = source['sha256']
                assert hashlib.sha256(Path(source['path']).read_bytes()).hexdigest() == expected
                with urlopen(base + url, timeout=5) as response:
                    assert response.headers.get_content_type() == mime
                    assert hashlib.sha256(response.read()).hexdigest() == expected
                checked.append({'role':role, 'url':url, 'mime':mime, 'original_sha256':expected})
            denied = ['/reference-images/images.jpg', '/assets/../server.py', '/assets/%2e%2e/server.py', '/assets/unregistered.png']
            for url in denied:
                try:
                    urlopen(base + url, timeout=5)
                    raise AssertionError('Unregistered asset route allowed')
                except HTTPError as exc:
                    assert exc.code == 404
            report = {'result':'passed','originals_unchanged':True,'served_originals':checked,'unregistered_routes_rejected':denied,'external_calls':0}
            Path('CHARACTER-ASSET-QA.json').write_text(json.dumps(report,indent=2)+'\n','utf-8')
            print(json.dumps(report))
        finally:
            server.shutdown()
            server.server_close()
            room.close()
            thread.join(timeout=3)


if __name__ == '__main__':
    main()
