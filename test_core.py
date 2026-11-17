"""Offline integration tests; python test_core.py"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import tarkov_profile_manager as app


def run_tests():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        game = root / 'game' / 'Settings'
        game.mkdir(parents=True)
        content = {'Graphics.ini': b'[Render]\nQuality=2\n', 'Controls.json': b'{"jump":"space"}\n'}
        for name, data in content.items():
            (game / name).write_bytes(data)
        with patch.object(app, 'app_home', lambda: root / 'local'):
            store = app.Store()
            store.set_settings(game)
            store.create_from_settings('FPS / PvP'.replace('/', '-'), list(content))
            folder, manifest, files = store.list_profiles()[0]
            export_root = root / 'manual_export'
            export_root.mkdir()
            exported = store.export(folder, manifest, export_root)
            assert exported.is_dir()
            assert sorted(x.name for x in exported.iterdir()) == sorted(content)
            for name, data in content.items():
                assert (exported / name).read_bytes() == data
            second = store.export(folder, manifest, export_root)
            assert second != exported and second.exists()
            store.import_folder(exported)
            assert len(store.list_profiles()) == 2
            shared = root / 'drive_shared'
            shared.mkdir()
            cloud = app.SharedFolderStorage(store)
            cloud.set_path(shared)
            uploaded = cloud.upload(store, folder, manifest)
            assert len(cloud.list_profiles()) == 1
            cloud.download(store, uploaded)
            assert len(store.list_profiles()) == 3
            (game / 'Graphics.ini').write_bytes(b'BAD')
            target = store.list_profiles()[0]
            backup = store.apply(target[0], target[1])
            assert backup is not None
            assert (game / 'Graphics.ini').read_bytes() == content['Graphics.ini']
            assert (backup / 'Graphics.ini').read_bytes() == b'BAD'
            assert not list(root.rglob('*.eftprofile'))
            print('PASS: native export, duplicates, import, shared-folder sync, backup, apply')


if __name__ == '__main__':
    run_tests()
