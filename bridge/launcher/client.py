import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parent
GAME = ROOT / 'versions' / '和白的机械动力冒险'
VERSION = json.loads((GAME / '和白的机械动力冒险.json').read_text(encoding='utf-8-sig'))
FEATURES = {'has_custom_resolution': True, 'is_quick_play_multiplayer': True}


def allowed(item):
    rules = item.get('rules')
    if rules is None:
        return True
    result = False
    for rule in rules:
        system = rule.get('os', {})
        if system.get('name', 'linux') != 'linux':
            continue
        if 'arch' in system and not re.search(system['arch'], platform.machine()):
            continue
        if 'version' in system and not re.search(system['version'], platform.release()):
            continue
        if any(FEATURES.get(k, False) != v for k, v in rule.get('features', {}).items()):
            continue
        result = rule['action'] == 'allow'
    return result


def valid(path, info):
    if not path.is_file():
        return False
    if 'size' in info and path.stat().st_size != info['size']:
        return False
    if info.get('sha1'):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha1').hexdigest() == info['sha1']
    return True


def fetch(path, info):
    if valid(path, info):
        return
    if not info.get('url'):
        raise RuntimeError(f'Missing local generated library: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.part')
    with urllib.request.urlopen(info['url'], timeout=45) as response, temporary.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    if not valid(temporary, info):
        raise RuntimeError(f'Checksum failed: {path}')
    temporary.replace(path)


def libraries():
    for item in VERSION['libraries']:
        if not allowed(item):
            continue
        artifact = item.get('downloads', {}).get('artifact')
        if artifact is None:
            parts = item['name'].split(':')
            group, name, version = parts[:3]
            suffix = '-' + parts[3] if len(parts) > 3 else ''
            relative = f'{group.replace(".", "/")}/{name}/{version}/{name}-{version}{suffix}.jar'
            artifact = {'path': relative, 'url': item.get('url', '') + relative if item.get('url') else ''}
        yield ROOT / 'libraries' / artifact['path'], artifact


def base_files():
    return list(libraries()) + [(GAME / (VERSION['id'] + '.jar'), VERSION['downloads']['client'])]


def assets(download):
    index_info = VERSION['assetIndex']
    index_path = ROOT / 'assets' / 'indexes' / (index_info['id'] + '.json')
    if download:
        fetch(index_path, index_info)
    elif not valid(index_path, index_info):
        raise RuntimeError('Asset index missing; run prepare first')
    objects = json.loads(index_path.read_text())['objects']
    unique = {v['hash']: v['size'] for v in objects.values()}
    jobs = [(ROOT / 'assets' / 'objects' / digest[:2] / digest,
             {'sha1': digest, 'size': size, 'url': f'https://resources.download.minecraft.net/{digest[:2]}/{digest}'})
            for digest, size in unique.items()]
    missing = [(p, i) for p, i in jobs if not valid(p, i)]
    print(f'Assets missing: {len(missing)}; bytes: {sum(i["size"] for _, i in missing)}', flush=True)
    if not download and missing:
        raise RuntimeError('Assets incomplete; run prepare first')
    if download:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for count, _ in enumerate(pool.map(lambda pair: fetch(*pair), missing), 1):
                if count % 100 == 0:
                    print(f'Assets downloaded: {count}/{len(missing)}', flush=True)


def command():
    natives = ROOT / 'natives-linux'
    natives.mkdir(exist_ok=True)
    values = {
        'auth_player_name': 'Nanako', 'auth_uuid': '2c30c089ff233c0998f68f06538a5aa7',
        'auth_access_token': '0', 'clientid': '', 'auth_xuid': '', 'user_type': 'legacy',
        'version_name': VERSION['id'], 'version_type': VERSION.get('type', 'release'),
        'game_directory': str(GAME), 'assets_root': str(ROOT / 'assets'),
        'assets_index_name': VERSION['assetIndex']['id'], 'natives_directory': str(natives),
        'library_directory': str(ROOT / 'libraries'), 'classpath_separator': ':',
        'classpath': ':'.join(dict.fromkeys(str(p) for p, _ in base_files())),
        'launcher_name': 'mc-brain', 'launcher_version': '1',
        'quickPlayMultiplayer': os.environ.get('MC_TARGET', '127.0.0.1:25565'),
        'resolution_width': '640', 'resolution_height': '360', 'user_properties': '{}',
    }
    def expand(items):
        result = []
        for item in items:
            if isinstance(item, dict):
                if not allowed(item):
                    continue
                item = item['value']
            for text in item if isinstance(item, list) else [item]:
                result.append(re.sub(r'\$\{([^}]+)\}', lambda m: values[m[1]], text))
        return result
    return ['java', '-Xms4G', '-Xmx16G'] + expand(VERSION['arguments']['jvm']) + [VERSION['mainClass']] + expand(VERSION['arguments']['game'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['audit', 'libraries', 'prepare', 'launch'])
    mode = parser.parse_args().mode
    with (ROOT / '.client-setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        files = base_files()
        missing = [(p, i) for p, i in files if not valid(p, i)]
        print(f'Base files: {len(files)}; missing/invalid: {len(missing)}', flush=True)
        for path, info in missing:
            print(f'{path.relative_to(ROOT)} | {info.get("size", "unknown")} bytes', flush=True)
        if mode in ('libraries', 'prepare'):
            for path, info in missing:
                fetch(path, info)
            print('LIBRARIES_OK', flush=True)
        elif mode == 'launch' and missing:
            raise RuntimeError('Base dependencies incomplete')
        args = command()
        print(f'Launch arguments expanded: {len(args)}', flush=True)
        if mode == 'prepare':
            assets(True)
            print('CLIENT_PREPARED_OK', flush=True)
        elif mode == 'launch':
            assets(False)
            subprocess.run(args, cwd=GAME, check=True)


if __name__ == '__main__':
    main()
