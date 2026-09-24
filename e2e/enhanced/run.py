#!/usr/bin/env python3
"""Disposable adoption lab. Official Enhanced artifacts are verified and never edited."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
import uuid
import zipfile
REPO = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
IMAGES = {'jf10': 'jellyfin/jellyfin:10.11.11@sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db', 'jf12': 'jellyfin/jellyfin:12.1@sha256:78d3ea1207d1322471fcac39a614f004f2ccf7e878f95ab2977d752f07e4dd7e'}

def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def require(condition, message):
    """A check that survives `python -O`; `assert` would silently vanish there."""
    if not condition:
        raise SystemExit('FATAL: ' + message)

def remove_container(name):
    # By name, never by a possibly-unset id: a `docker run` that created the
    # container but failed afterwards must still be cleaned up.
    subprocess.run(['docker', 'rm', '-f', name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def main():
    # A CI timeout delivers SIGTERM; turn it into an exception so the
    # `finally` blocks below still remove the containers and lab directories.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt('SIGTERM')))
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=pathlib.Path, default=REPO / 'plugin/build')
    parser.add_argument('--target', choices=IMAGES)
    args = parser.parse_args()
    snapshot = args.snapshot.resolve()
    require(snapshot.is_relative_to(REPO / 'plugin/.builds'), 'the snapshot must be an immutable plugin/.builds entry: ' + str(snapshot))
    run('python3', str(REPO / 'scripts/verify-package.py'), '--build-dir', str(snapshot), '--manifest', str(REPO / 'manifest.json'), '--manifest-mode', 'structure', '--require-immutable-snapshot')
    lock = json.loads((REPO / 'e2e/compat/ecosystem.lock.json').read_text())
    artifacts = {a['id']: a for a in lock['artifacts']}
    output = HERE / 'artifacts'
    output.mkdir(exist_ok=True)
    state = HERE / '.state'
    state.mkdir(exist_ok=True)
    dotnet = str(pathlib.Path(os.environ.get('DOTNET_ROOT', str(pathlib.Path.home() / '.dotnet'))) / 'dotnet')
    project = HERE / 'fixture/Fixture.csproj'
    # Compile separate assemblies; the browser compares their embedded runtime
    # against the immutable standalone candidate, not a simulated runtime.
    for adopter in ('true', 'false'):
        run(dotnet, 'build', str(project), '-c', 'Release', '--nologo', '-p:RestoreLockedMode=true', f'-p:Adopter={adopter}', f'-p:BaseIntermediateOutputPath={state}/builds/{adopter}/obj/', f'-p:BaseOutputPath={state}/builds/{adopter}/bin/', stdout=subprocess.DEVNULL)
    for target in [args.target] if args.target else IMAGES:
        stage = snapshot / ('stage' if target == 'jf10' else 'stage-jf12')
        meta = json.loads((stage / 'meta.json').read_text())
        tfm = meta['framework']
        artifact = artifacts['jellyfin-enhanced-' + target]
        archive = state / artifact['archive']['name']
        if not archive.exists() or digest(archive) != artifact['archive']['sha256']:
            urllib.request.urlretrieve(artifact['archive']['url'], archive)
        require(digest(archive) == artifact['archive']['sha256'], 'downloaded Enhanced archive does not match the locked SHA-256: ' + artifact['archive']['name'])
        traces = []
        for order in ('first', 'last'):
            (output / (target + '-' + order + '.json')).unlink(missing_ok=True)
        for order in ('first', 'last'):
            lab = pathlib.Path(tempfile.mkdtemp(prefix=target + '-' + order + '-', dir=state))
            plugins = lab / 'config/plugins'
            plugins.mkdir(parents=True)
            (lab / 'cache').mkdir()
            name = 'rk-enhanced-' + uuid.uuid4().hex[:12]
            container = None
            started = False

            def plugin_folder(folder, source, plugin_meta):
                dest = plugins / folder
                dest.mkdir()
                if isinstance(source, pathlib.Path):
                    shutil.copytree(source, dest, dirs_exist_ok=True)
                else:
                    for src in source:
                        shutil.copy2(src, dest / src.name)
                for directory, _, files in os.walk(dest):
                    pathlib.Path(directory).chmod(0o755)
                    for filename in files:
                        (pathlib.Path(directory) / filename).chmod(0o644)
                (dest / 'meta.json').write_text(json.dumps(plugin_meta))
            plugin_folder('RefreshKit', stage, meta)
            enhanced = plugins / 'Enhanced'
            enhanced.mkdir()
            # Read one verified assembly, never extract untrusted archive paths.
            with zipfile.ZipFile(archive) as z:
                (enhanced / artifact['plugin']['assembly']).write_bytes(z.read(artifact['plugin']['assembly']))
            (enhanced / 'meta.json').write_text(json.dumps(dict(name='Jellyfin Enhanced', guid=artifact['plugin']['guid'], version=artifact['plugin']['version'], targetAbi=meta['targetAbi'], status='Active', autoUpdate=False)))
            # Jellyfin registers plugins in ordinal name order, so the prefix
            # decides where the adopter sits relative to BOTH "Jellyfin Enhanced"
            # and "Jellyfin Refresh Kit": '000' registers first (outermost
            # middleware), 'zzz' registers after both (innermost). A digit
            # prefix for "last" would still sort before the letter J.
            for assembly, route, guid, sort in [('EnhancedAdoptionFixture', 'EnhancedAdoption', 'c39e6da2-71fe-49ac-96ce-8bd3ba780e03', '000' if order == 'first' else 'zzz'), ('SiblingRefreshFixture', 'SiblingRefresh', 'b9d81601-6a04-4a9a-95d5-08e250da36d0', '500')]:
                plugin_folder(sort + route, [state / 'builds' / ('true' if route == 'EnhancedAdoption' else 'false') / 'bin/Release' / tfm / (assembly + '.dll')], dict(name=sort + route, guid=guid, version='1.0.0.0', targetAbi=meta['targetAbi'], status='Active', autoUpdate=False))
            try:
                started = True
                container = run('docker', 'run', '-d', '--name', name, '--label', 'rk.enhanced-readiness=true', '--network', 'bridge', '--user', f'{os.getuid()}:{os.getgid()}', '-p', '127.0.0.1::8096', '-v', str(lab / 'config') + ':/config', '-v', str(lab / 'cache') + ':/cache', IMAGES[target], capture_output=True).stdout.strip()
                info = json.loads(run('docker', 'inspect', container, capture_output=True).stdout)[0]
                port = info['NetworkSettings']['Ports']['8096/tcp'][0]['HostPort']
                origin = 'http://127.0.0.1:' + port
                headers = {'Content-Type': 'application/json', 'Authorization': 'MediaBrowser Client="rk-lab", Device="rk-lab", DeviceId="rk-lab", Version="1"'}

                def api(path, data=None, method=None):
                    req = urllib.request.Request(origin + path, headers=headers, data=json.dumps(data).encode() if data is not None else None, method=method)
                    with urllib.request.urlopen(req, timeout=15) as r:
                        body = r.read()
                        return json.loads(body) if body else None
                for _ in range(120):
                    try:
                        api('/System/Info/Public')
                        api('/Startup/User')
                        break
                    except Exception:
                        time.sleep(1)
                else:
                    raise RuntimeError('Server startup failed')
                api('/Startup/Configuration', dict(UICulture='en-US', MetadataCountryCode='US', PreferredMetadataLanguage='en'))
                api('/Startup/User')
                api('/Startup/User', dict(Name='rk_admin', Password='ReadinessLab42!'))
                api('/Startup/RemoteAccess', dict(EnableRemoteAccess=True, EnableAutomaticPortMapping=False))
                api('/Startup/Complete', {})
                auth = api('/Users/AuthenticateByName', dict(Username='rk_admin', Pw='ReadinessLab42!'))
                token = auth.get('AccessToken', auth.get('accessToken'))
                headers['Authorization'] += ', Token="' + token + '"'
                cfg = api('/Plugins/515255fe333249b0b4710be58c8221d8/Configuration')
                cfg.update(IdleSeconds=0, PollSeconds=15, ReloadBudget=10, ConfigCooldownMinutes=0)
                api('/Plugins/515255fe333249b0b4710be58c8221d8/Configuration', cfg)
                result = output / (target + '-' + order + '.json')
                run('node', str(HERE / 'browser.cjs'), origin, str(result), cwd=REPO)
                d = json.loads(result.read_text())
                d.update(snapshot=snapshot.name, sourceRevision=meta['sourceRevision'], sourceTreeSha256=meta['sourceTreeSha256'], image=IMAGES[target], enhanced=artifact['plugin']['version'], enhancedSha256=artifact['archive']['sha256'], fixtureAssemblySha256=digest(state / 'builds/true/bin/Release' / tfm / 'EnhancedAdoptionFixture.dll'))
                d.update(harnessRevision=run('git', 'rev-parse', 'HEAD', cwd=REPO, capture_output=True).stdout.strip(), harnessDirty=bool(run('git', 'status', '--porcelain', '--untracked-files=all', cwd=REPO, capture_output=True).stdout.strip()), fixtureSources={str(p.relative_to(REPO)): digest(p) for p in [HERE / 'run.py', HERE / 'browser.cjs', project, project.parent / 'Plugin.cs', project.parent / 'packages.lock.json', REPO / 'RefreshKit.cs', REPO / 'jellyfin-refresh-kit.js']}, packageSha256={p.name: digest(p) for p in snapshot.glob('*.zip')})
                result.write_text(json.dumps(d, indent=2))
                traces.append(d['middlewareOrder'])
                print(target, order, 'PASS', flush=True)
            finally:
                try:
                    if container:
                        # Full server logs may contain authenticated request URLs.
                        # Retain only assembly/plugin startup identities.
                        log = subprocess.run(['docker', 'logs', container], check=False, text=True, capture_output=True)
                        (output / (target + '-' + order + '-startup.log')).write_text('\n'.join((x for x in (log.stdout + log.stderr).splitlines() if 'Loaded assembly' in x or 'Loaded plugin' in x)))
                        subprocess.run(['docker', 'stop', '--timeout', '10', container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                finally:
                    if started:
                        remove_container(name)
                    for directory, _, _ in os.walk(lab):
                        pathlib.Path(directory).chmod(0o755)
                    shutil.rmtree(lab, ignore_errors=True)
        require(traces[0] != traces[1], f'{target}: both orders must actually execute differently: {traces}')
    print('Enhanced adoption lab passed', flush=True)
if __name__ == '__main__':
    main()
