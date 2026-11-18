"""Dependency images built by ordinary containers on explicitly owned networks.

No BuildKit worker, default-bridge build container, host socket mount, host
network, or privileged daemon is involved. JSON recipes and shared setup scripts
also produce the legacy Dockerfiles; this module never parses a Dockerfile.
"""
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import string
import subprocess
import time
import uuid

LABEL = 'portmap.real-node.run'
PROXY_ENV = {name: '' for kind in ('HTTP', 'HTTPS', 'FTP', 'NO', 'ALL')
             for name in (kind + '_PROXY', kind.lower() + '_proxy')}
BUILD_CAPABILITIES = ('CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'FSETID', 'SETGID', 'SETUID')
INPUT_MOUNT = '/tmp/portmap-image-inputs'


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _relative(value):
    path = PurePosixPath(value)
    if not isinstance(value, str) or not value or path.is_absolute() or '..' in path.parts or str(path) != value:
        raise ValueError('Image recipe inputs must be exact relative file paths')
    return path


def _builder_isolation(item, *, network, run_id, work):
    config, host = item['Config'], item['HostConfig']
    environment = dict(value.split('=', 1) for value in config.get('Env') or [])
    mounts = item.get('Mounts') or []
    added = {value.removeprefix('CAP_') for value in host.get('CapAdd') or []}
    dropped = {value.removeprefix('CAP_') for value in host.get('CapDrop') or []}
    if (host.get('Privileged') is not False or host.get('NetworkMode') != network['name'] or
            config.get('User') != '0:0' or config.get('Labels', {}).get(LABEL) != run_id or
            added != set(BUILD_CAPABILITIES) or dropped != {'ALL'} or
            not any(value in ('no-new-privileges', 'no-new-privileges:true') for value in host.get('SecurityOpt') or []) or
            host.get('PortBindings') or set(item['NetworkSettings']['Networks']) != {network['name']} or
            len(mounts) != 1 or mounts[0].get('Destination') != INPUT_MOUNT or
            mounts[0].get('Source') != str(work) or mounts[0].get('RW') is not False or
            any(environment.get(key) != value for key, value in PROXY_ENV.items())):
        raise ValueError('Actual dependency build container violates its owned-network isolation or proxy policy')
    return {'id': item['Id'], 'image': item['Image'], 'user': config['User'],
            'privileged': host['Privileged'], 'networkMode': host['NetworkMode'],
            'capDrop': host['CapDrop'], 'capAdd': host['CapAdd'], 'securityOpt': host['SecurityOpt'],
            'mountTargets': [INPUT_MOUNT], 'readOnlyInputs': True, 'portBindings': {},
            'proxyEnvironmentCleared': list(PROXY_ENV)}


class Resources:
    """Ownership primitives shared with the real-node runtime resource ledger."""

    def _new_network(self, suffix, internal):
        # Check before creating any bridge, including policy-routing tables used
        # by Tailscale. Never alter host routes to accommodate a fixture subnet.
        occupied = []
        for route in json.loads(self._run(['ip', '-j', 'route', 'show', 'table', 'all']).stdout):
            destination = route.get('dst')
            if destination and destination != 'default':
                network = ipaddress.ip_network(destination, strict=False)
                if network.version == 4 and network.prefixlen:
                    occupied.append(network)
        ids = self._docker(['network', 'ls', '-q']).stdout.split()
        if ids:
            for network in json.loads(self._docker(['network', 'inspect', *ids]).stdout):
                for configuration in network.get('IPAM', {}).get('Config') or []:
                    subnet = configuration.get('Subnet')
                    if subnet and ':' not in subnet:
                        occupied.append(ipaddress.ip_network(subnet))
        candidates = (ipaddress.ip_network(prefix + str(index) + '.0/24')
                      for prefix in ('10.247.', '172.30.', '192.168.', '198.18.')
                      for index in range(32, 240))
        subnet = next((candidate for candidate in candidates
                       if not any(candidate.overlaps(other) for other in occupied)), None)
        if subnet is None:
            raise ValueError('No non-overlapping disposable IPv4 subnet; do not change host or Tailscale routes')
        name = self.prefix + '-' + suffix
        arguments = ['network', 'create', '--label', LABEL + '=' + self.run_id, '--subnet', str(subnet)]
        if internal:
            arguments.append('--internal')
        identifier = self._docker([*arguments, name]).stdout.strip()
        resource = {'id': identifier, 'name': name, 'subnet': str(subnet)}
        self.resources['networks'].append(resource)
        self._checkpoint()
        return resource

    def _discover_owned(self):
        # A create command can be interrupted after daemon acceptance but before
        # its ID reaches us. Only our unpredictable ownership label is queried.
        for kind in ('containers', 'networks'):
            arguments = (['ps', '-aq', '--no-trunc'] if kind == 'containers' else ['network', 'ls', '-q'])
            ids = self._docker([*arguments, '--filter', 'label=' + LABEL + '=' + self.run_id]).stdout.split()
            known = {resource['id'] for resource in self.resources[kind]}
            if ids:
                inspect = ['inspect'] if kind == 'containers' else ['network', 'inspect']
                for item in json.loads(self._docker([*inspect, *ids]).stdout):
                    if item['Id'] not in known:
                        self.resources[kind].append({'id': item['Id'], 'name': item['Name'].lstrip('/')})
        self._checkpoint()

    def _remove_container(self, resource):
        result = self._docker(['inspect', resource['id']], check=False)
        if result.returncode:
            raise ValueError('Owned container disappeared before verified cleanup: ' + resource['name'])
        actual = json.loads(result.stdout)[0]
        if actual['Id'] != resource['id'] or actual.get('Config', {}).get('Labels', {}).get(LABEL) != self.run_id:
            raise ValueError('Refusing to remove a container with a different ownership label')
        self._docker(['rm', '--force', resource['id']])
        self.removed['containers'].append(resource['id'])
        self.resources['containers'].remove(resource)
        self._checkpoint()

    def _remove_network(self, resource):
        actual = json.loads(self._docker(['network', 'inspect', resource['id']]).stdout)[0]
        if actual['Id'] != resource['id'] or actual.get('Labels', {}).get(LABEL) != self.run_id:
            raise ValueError('Refusing to remove a network with a different ownership label')
        self._docker(['network', 'rm', resource['id']])
        self.removed['networks'].append(resource['name'])
        self.resources['networks'].remove(resource)
        self._checkpoint()

    def _build_image(self, recipe_path, *, root, tag, network, work, log, base_image=None):
        started = time.monotonic()
        recipe = json.loads(Path(recipe_path).read_text())
        if recipe.get('schemaVersion') != 1:
            raise ValueError('Unsupported dependency image recipe schema')
        if network not in self.resources['networks'] or network['name'] in ('host', 'bridge', 'none'):
            raise ValueError('Dependency image construction requires an explicitly owned network')
        arguments = {**recipe['buildArguments'], **recipe['standardArguments']}
        if recipe['base'] == '${BROWSER_BASE}':
            if not base_image or not re.fullmatch(r'sha256:[0-9a-f]{64}', base_image):
                raise ValueError('Inherited browser base must be an immutable local image ID')
            base = base_image
            arguments['BROWSER_BASE'] = base_image
        else:
            if base_image is not None or not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', recipe['base']):
                raise ValueError('Dependency base must be a digest-pinned image')
            base = recipe['base']
        probe = self._docker(['image', 'inspect', base], check=False)
        if probe.returncode:
            if base_image is not None:
                raise ValueError('Prepared desktop dependency image is unavailable')
            self._docker(['pull', '--platform', 'linux/amd64', base], timeout=900)
            probe = self._docker(['image', 'inspect', base])
        base_identity = json.loads(probe.stdout)[0]
        base_env = dict(item.split('=', 1) for item in base_identity['Config'].get('Env') or [])
        environment = {key: string.Template(value).substitute({**base_env, **arguments})
                       for key, value in recipe['environment'].items()}
        environment.update(PROXY_ENV)
        work = Path(work)
        work.mkdir(mode=0o700)
        for source_name, destination_name in recipe['inputs'].items():
            source_relative, destination_relative = _relative(source_name), _relative(destination_name)
            source = Path(root) / source_relative
            if not source.is_file() or source.is_symlink():
                raise ValueError('Dependency setup inputs must be ordinary source files')
            destination = work / destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        name = self.prefix + '-image-' + uuid.uuid4().hex[:8]
        # Commit merges an empty ENTRYPOINT with the container configuration.
        # Reset it at create time; put the setup shell in Cmd, not Entrypoint.
        create = ['create', '--name', name, '--label', LABEL + '=' + self.run_id,
                  '--network', network['name'], '--user', '0:0', '--cap-drop', 'ALL',
                  '--security-opt', 'no-new-privileges:true', '--pids-limit', '256',
                  '--workdir', recipe['workdir'], '--entrypoint', '',
                  '--mount', f'type=bind,src={work},dst={INPUT_MOUNT},readonly']
        for capability in BUILD_CAPABILITIES:
            create += ['--cap-add', capability]
        for key, value in environment.items():
            create += ['--env', key + '=' + value]
        identifier = self._docker([*create, base, '/bin/sh', '-c', 'exec sleep infinity']).stdout.strip()
        resource = {'id': identifier, 'name': name}
        self.resources['containers'].append(resource)
        self._checkpoint()
        failure = None
        try:
            self._docker(['start', identifier])
            isolation = _builder_isolation(json.loads(self._docker(['inspect', identifier]).stdout)[0],
                                           network=network, run_id=self.run_id, work=work)
            for step in recipe['steps']:
                script = _relative(step['script'])
                if step['script'] not in recipe['inputs'].values():
                    raise ValueError('Dependency setup script must be a declared read-only source input')
                command = ['exec']
                for key, value in arguments.items():
                    command += ['--env', key + '=' + value]
                command += [identifier, '/bin/sh', INPUT_MOUNT + '/' + str(script), *step['arguments']]
                result = self._docker(command, timeout=3600, check=False)
                with Path(log).open('a') as stream:
                    stream.write(result.stdout)
                    stream.write(result.stderr)
                if result.returncode:
                    raise ValueError('Dependency setup failed (exit ' + str(result.returncode) + '); see ' + Path(log).name)
            self._docker(['stop', '--time', '10', identifier], timeout=30)
            changes = ['USER ' + recipe['user'], 'WORKDIR ' + recipe['workdir'],
                       'ENTRYPOINT ' + json.dumps(recipe['entrypoint']), 'CMD ' + json.dumps(recipe['command'])]
            changes += ['ENV ' + key + '=' + json.dumps(value) for key, value in environment.items()]
            changes.append('LABEL ' + LABEL + '=""')
            commit = ['commit']
            for change in changes:
                commit += ['--change', change]
            self._docker([*commit, identifier, tag], timeout=600)
            actual = json.loads(self._docker(['image', 'inspect', tag]).stdout)[0]
            config = actual['Config']
            actual_env = dict(item.split('=', 1) for item in config.get('Env') or [])
            if (not re.fullmatch(r'sha256:[0-9a-f]{64}', actual['Id']) or
                    config.get('User') != recipe['user'] or config.get('WorkingDir') != recipe['workdir'] or
                    (config.get('Entrypoint') or []) != recipe['entrypoint'] or
                    (config.get('Cmd') or []) != recipe['command'] or
                    any(actual_env.get(key) != value for key, value in environment.items()) or
                    (config.get('Labels') or {}).get(LABEL) not in (None, '') or
                    any(key in actual_env and key not in base_env and key not in environment for key in arguments)):
                raise ValueError('Committed dependency image differs from its exact intended runtime configuration')
            return {'method': 'ordinary-container-owned-network', 'image_id': actual['Id'],
                    'base_image_id': base_identity['Id'], 'container_id': identifier,
                    'network': dict(network), 'capabilities': list(BUILD_CAPABILITIES),
                    'proxyEnvironmentCleared': list(PROXY_ENV),
                    'isolation': isolation,
                    'runtimeConfig': {'user': recipe['user'], 'workdir': recipe['workdir'],
                                      'entrypoint': recipe['entrypoint'], 'command': recipe['command'],
                                      'environmentKeys': sorted(environment)},
                    'elapsed_seconds': round(time.monotonic() - started, 3)}
        except BaseException as error:
            failure = error
            raise
        finally:
            cleanup_failure = None
            for cleanup in (lambda: self._remove_container(resource), lambda: shutil.rmtree(work)):
                try:
                    cleanup()
                except Exception as cleanup_error:
                    if failure is not None:
                        failure.add_note('Dependency build cleanup also failed: ' + type(cleanup_error).__name__)
                    elif cleanup_failure is None:
                        cleanup_failure = cleanup_error
                    else:
                        cleanup_failure.add_note('Additional dependency build cleanup failure: ' + type(cleanup_error).__name__)
            if cleanup_failure is not None:
                raise cleanup_failure


class ImageBuilder(Resources):
    """Independent owner for prewarming browser tools without creating test nodes."""

    def __init__(self, *, state_dir, docker, cleanup_path):
        if len(docker) != 3 or tuple(docker[:2]) != ('docker', '--host') or not docker[2].startswith('unix:///'):
            raise ValueError('Image construction requires a frozen local Unix Docker endpoint')
        self.docker = tuple(docker)
        self.run_id = uuid.uuid4().hex
        self.prefix = 'portmap-real-node-tools-' + self.run_id[:12]
        self.state = Path(state_dir)
        self.state.mkdir(mode=0o700)
        self.cleanup_path = Path(cleanup_path)
        self.resources = {'containers': [], 'networks': []}
        self.removed = {'containers': [], 'networks': []}
        self._checkpoint()

    def _checkpoint(self):
        _write_json(self.state / 'owner.json', {'runId': self.run_id, 'resources': self.resources})

    def _run(self, argv, *, timeout=120, check=True):
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise ValueError('Dependency image command failed (exit ' + str(result.returncode) + '): ' + ' '.join(argv[:3]))
        return result

    def _docker(self, argv, **kwargs):
        return self._run([*self.docker, *argv], **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        errors = []
        try:
            self._discover_owned()
        except Exception as error:
            errors.append(type(error).__name__ + ': ownership discovery failed')
        for kind, remove in (('containers', self._remove_container), ('networks', self._remove_network)):
            for resource in list(self.resources[kind]):
                try:
                    remove(resource)
                except Exception as error:
                    errors.append(type(error).__name__ + ': owned ' + kind + ' cleanup failed')
        receipt = {'status': 'failed' if errors else 'passed', 'runId': self.run_id,
                   'containersRemoved': self.removed['containers'], 'networksRemoved': self.removed['networks'],
                   'remainingResources': self.resources, 'errors': errors}
        _write_json(self.cleanup_path, receipt)
        if errors:
            if exception is None:
                raise ValueError('Dependency image cleanup failed; see ' + self.cleanup_path.name)
            exception.add_note('Dependency image cleanup also failed; see ' + self.cleanup_path.name)
        return False
