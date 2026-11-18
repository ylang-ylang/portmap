"""Command-edge regressions for source-only, explicitly networked image builds."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.browser.real_node import image_builder


class CommandEdges(image_builder.Resources):
    def __init__(self, recipe, *, fail_step=False, fail_cleanup=False, bad_proxy=False):
        self.recipe = recipe
        self.run_id = 'unit-image-owner'
        self.prefix = 'portmap-real-node-unit'
        self.network = {'id': 'a' * 64, 'name': self.prefix + '-egress', 'subnet': '10.247.32.0/24'}
        self.resources = {'containers': [], 'networks': [self.network]}
        self.removed = {'containers': [], 'networks': []}
        self.commands = []
        self.snapshots = []
        self.fail_step = fail_step
        self.fail_cleanup = fail_cleanup
        self.bad_proxy = bad_proxy
        self.base_id = 'sha256:' + 'b' * 64
        self.image_id = 'sha256:' + 'c' * 64
        self.container_id = 'd' * 64

    def _checkpoint(self):
        self.snapshots.append(copy.deepcopy(self.resources))

    def _docker(self, arguments, **options):
        self.commands.append((arguments, options))
        result = SimpleNamespace(returncode=0, stdout='', stderr='')
        if arguments[:2] == ['image', 'inspect']:
            if arguments[2].startswith('unit-image:'):
                environment = {'PATH': '/usr/local/bin:/usr/bin', 'HTTPS_PROXY': 'https://private:credential@proxy.invalid'}
                environment.update(self.recipe['environment'])
                environment['PATH'] = '/opt/tools:/usr/local/bin:/usr/bin'
                environment.update(image_builder.PROXY_ENV)
                if self.bad_proxy:
                    environment['FTP_PROXY'] = 'ftp://private:credential@proxy.invalid'
                config = {'User': self.recipe['user'], 'WorkingDir': self.recipe['workdir'],
                          'Entrypoint': self.recipe['entrypoint'], 'Cmd': self.recipe['command'],
                          'Env': [key + '=' + value for key, value in environment.items()]}
                value = {'Id': self.image_id, 'Config': config}
            else:
                value = {'Id': self.base_id, 'Config': {'Env': ['PATH=/usr/local/bin:/usr/bin',
                    'HTTPS_PROXY=https://private:credential@proxy.invalid']}}
            result.stdout = json.dumps([value])
        elif arguments[0] == 'create':
            result.stdout = self.container_id
        elif arguments[0] == 'exec':
            assert self.resources['containers'] == [{'id': self.container_id, 'name': self.resources['containers'][0]['name']}]
            result.stdout = 'installed dependency packages\n'
            if self.fail_step:
                result.returncode = 23
        elif arguments == ['inspect', self.container_id]:
            create = next(command for command, _ in self.commands if command[0] == 'create')
            source = create[create.index('--mount') + 1].split(',')[1].removeprefix('src=')
            result.stdout = json.dumps([{'Id': self.container_id, 'Image': self.base_id,
                'Config': {'Labels': {image_builder.LABEL: self.run_id}, 'User': '0:0',
                           'Env': [key + '=' + value for key, value in env_arguments(create).items()]},
                'HostConfig': {'Privileged': False, 'NetworkMode': self.network['name'],
                               'CapAdd': list(image_builder.BUILD_CAPABILITIES), 'CapDrop': ['ALL'],
                               'SecurityOpt': ['no-new-privileges:true'], 'PortBindings': {}},
                'NetworkSettings': {'Networks': {self.network['name']: {}}},
                'Mounts': [{'Source': source, 'Destination': image_builder.INPUT_MOUNT, 'RW': False}]}])
        elif arguments[:2] == ['rm', '--force'] and self.fail_cleanup:
            raise ValueError('secondary-owned-container-cleanup')
        return result


@pytest.fixture
def recipe(tmp_path):
    (tmp_path / 'setup.sh').write_text('set -eu\n')
    (tmp_path / 'unrelated-private-input').write_text('not a construction input')
    value = {'schemaVersion': 1, 'base': 'debian:bookworm-slim@sha256:' + '1' * 64,
             'inputs': {'setup.sh': 'setup.sh'}, 'steps': [{'script': 'setup.sh', 'arguments': ['dependencies']}],
             'buildArguments': {'ACCEPT_ANDROID_SDK_LICENSE': '0', 'FIREFOX_ANDROID_CHANNEL': 'release'},
             'standardArguments': {'ACCEPT_ANDROID_SDK_LICENSE': '1', 'FIREFOX_ANDROID_CHANNEL': 'nightly'},
             'environment': {'PYTHONPATH': '/app/src', 'PATH': '/opt/tools:${PATH}'},
             'user': '1000:1000', 'workdir': '/app', 'entrypoint': [],
             'command': ['python', '/app/tests/browser/runner.py']}
    path = tmp_path / 'image.json'
    path.write_text(json.dumps(value))
    return path, value


def build(edges, recipe_path, tmp_path, **kwargs):
    return edges._build_image(recipe_path, root=tmp_path, tag='unit-image:tools', network=edges.network,
                              work=tmp_path / 'readonly-inputs', log=tmp_path / 'build.log', **kwargs)


def env_arguments(arguments):
    return dict(arguments[index + 1].split('=', 1) for index, item in enumerate(arguments) if item == '--env')


def test_ordinary_builder_never_uses_default_bridge_buildkit_privilege_or_host_socket(recipe, tmp_path):
    recipe_path, value = recipe
    edges = CommandEdges(value)
    receipt = build(edges, recipe_path, tmp_path)
    commands = [command for command, _ in edges.commands]
    create = next(command for command in commands if command[0] == 'create')
    assert create[create.index('--network') + 1] == edges.network['name']
    assert create[create.index('--entrypoint') + 1] == ''
    assert create[-4:] == [value['base'], '/bin/sh', '-c', 'exec sleep infinity']
    assert create[create.index('--cap-drop') + 1] == 'ALL'
    capabilities = {create[index + 1] for index, item in enumerate(create) if item == '--cap-add'}
    assert capabilities == set(image_builder.BUILD_CAPABILITIES)
    assert not capabilities.intersection({'NET_ADMIN', 'SYS_ADMIN', 'NET_RAW', 'SYS_PTRACE'})
    assert 'no-new-privileges:true' in create
    mounts = [create[index + 1] for index, item in enumerate(create) if item == '--mount']
    assert mounts == [f'type=bind,src={tmp_path / "readonly-inputs"},dst={image_builder.INPUT_MOUNT},readonly']
    assert not any(command[0] in ('build', 'buildx', 'builder') for command in commands)
    assert not any(argument in ('--privileged', '--publish', '-p') or 'docker.sock' in argument for command in commands for argument in command)
    assert receipt['method'] == 'ordinary-container-owned-network'
    assert receipt['image_id'] == edges.image_id
    assert any(snapshot['containers'] for snapshot in edges.snapshots)
    assert edges.resources['containers'] == [] and edges.removed['containers'] == [edges.container_id]
    assert not (tmp_path / 'readonly-inputs').exists()
    assert 'private:credential' not in json.dumps(receipt) + (tmp_path / 'build.log').read_text()


def test_proxy_environment_is_blank_in_all_ten_cases_and_build_arguments_are_not_committed(recipe, tmp_path):
    recipe_path, value = recipe
    edges = CommandEdges(value)
    build(edges, recipe_path, tmp_path)
    commands = [command for command, _ in edges.commands]
    create = next(command for command in commands if command[0] == 'create')
    create_env = env_arguments(create)
    assert len(image_builder.PROXY_ENV) == 10
    assert {key: create_env[key] for key in image_builder.PROXY_ENV} == image_builder.PROXY_ENV
    assert create_env['PATH'] == '/opt/tools:/usr/local/bin:/usr/bin'
    assert 'ACCEPT_ANDROID_SDK_LICENSE' not in create_env and 'FIREFOX_ANDROID_CHANNEL' not in create_env
    execute = next(command for command in commands if command[0] == 'exec')
    assert env_arguments(execute) == value['standardArguments']
    commit = next(command for command in commands if command[0] == 'commit')
    changes = [commit[index + 1] for index, item in enumerate(commit) if item == '--change']
    assert 'USER 1000:1000' in changes and 'WORKDIR /app' in changes
    assert 'ENTRYPOINT []' in changes and 'CMD ["python", "/app/tests/browser/runner.py"]' in changes
    assert all('ENV ' + key + '=""' in changes for key in image_builder.PROXY_ENV)
    assert not any('ACCEPT_ANDROID_SDK_LICENSE' in change or 'FIREFOX_ANDROID_CHANNEL' in change or image_builder.INPUT_MOUNT in change for change in changes)
    assert commands.index(next(command for command in commands if command[0] == 'stop')) < commands.index(commit)


@pytest.mark.parametrize('failure', ['setup', 'committed-proxy'])
def test_failed_setup_or_committed_proxy_never_returns_an_image_and_still_removes_owned_container(recipe, tmp_path, failure):
    recipe_path, value = recipe
    edges = CommandEdges(value, fail_step=failure == 'setup', bad_proxy=failure == 'committed-proxy')
    with pytest.raises(ValueError, match='Dependency setup failed|exact intended runtime configuration'):
        build(edges, recipe_path, tmp_path)
    assert edges.resources['containers'] == [] and edges.removed['containers'] == [edges.container_id]
    assert not (tmp_path / 'readonly-inputs').exists()
    if failure == 'setup':
        assert not any(command[0] == 'commit' for command, _ in edges.commands)


def test_primary_setup_failure_survives_cleanup_failure_with_ownership_retained(recipe, tmp_path):
    recipe_path, value = recipe
    edges = CommandEdges(value, fail_step=True, fail_cleanup=True)
    with pytest.raises(ValueError, match='Dependency setup failed') as caught:
        build(edges, recipe_path, tmp_path)
    assert caught.value.__notes__ == ['Dependency build cleanup also failed: ValueError']
    assert edges.resources['containers'][0]['id'] == edges.container_id
    assert not (tmp_path / 'readonly-inputs').exists()


@pytest.mark.parametrize('invalid', ['host', 'bridge', 'none', 'not-owned'])
def test_unowned_or_default_network_is_rejected_before_any_docker_command(recipe, tmp_path, invalid):
    recipe_path, value = recipe
    edges = CommandEdges(value)
    if invalid == 'not-owned':
        edges.resources['networks'] = []
    else:
        edges.network['name'] = invalid
    with pytest.raises(ValueError, match='explicitly owned network'):
        build(edges, recipe_path, tmp_path)
    assert edges.commands == []


def test_inherited_browser_base_requires_exact_immutable_id(recipe, tmp_path):
    recipe_path, value = recipe
    value['base'] = '${BROWSER_BASE}'
    recipe_path.write_text(json.dumps(value))
    edges = CommandEdges(value)
    with pytest.raises(ValueError, match='immutable local image ID'):
        build(edges, recipe_path, tmp_path, base_image='some-mutable-desktop-tag')
    assert edges.commands == []


@pytest.mark.parametrize('endpoint', ['tcp://production.invalid:2376', 'ssh://production.invalid', 'unix://relative'])
def test_independent_builder_rejects_nonlocal_endpoint_before_state_or_resources(tmp_path, endpoint):
    with pytest.raises(ValueError, match='frozen local Unix Docker endpoint'):
        image_builder.ImageBuilder(state_dir=tmp_path / 'private', docker=('docker', '--host', endpoint),
                                   cleanup_path=tmp_path / 'cleanup.json')
    assert not (tmp_path / 'private').exists()


class NetworkEdges(image_builder.Resources):
    def __init__(self, routes, subnets):
        self.routes, self.subnets = routes, subnets
        self.run_id = 'unit-network-owner'
        self.prefix = 'portmap-real-node-network-unit'
        self.resources = {'containers': [], 'networks': []}
        self.commands = []

    def _checkpoint(self):
        self.checkpoint = copy.deepcopy(self.resources)

    def _run(self, arguments):
        assert arguments == ['ip', '-j', 'route', 'show', 'table', 'all']
        return SimpleNamespace(stdout=json.dumps(self.routes))

    def _docker(self, arguments):
        self.commands.append(arguments)
        if arguments == ['network', 'ls', '-q']:
            return SimpleNamespace(stdout='existing-network')
        if arguments[:2] == ['network', 'inspect']:
            return SimpleNamespace(stdout=json.dumps([{'IPAM': {'Config': [{'Subnet': value} for value in self.subnets]}}]))
        assert arguments[:2] == ['network', 'create']
        return SimpleNamespace(stdout='e' * 64)


def test_owned_egress_network_rejects_all_host_policy_routes_and_existing_docker_subnets():
    edges = NetworkEdges([{'dst': 'default'}, {'dst': '10.247.0.0/16', 'table': 52},
                          {'dst': '192.168.0.0/16'}, {'dst': '100.64.0.0/10'}], ['172.30.0.0/16'])
    network = edges._new_network('egress', internal=False)
    assert network['subnet'] == '198.18.32.0/24'
    assert network['name'] == edges.prefix + '-egress'
    create = edges.commands[-1]
    assert create == ['network', 'create', '--label', image_builder.LABEL + '=' + edges.run_id,
                      '--subnet', '198.18.32.0/24', network['name']]
    assert edges.checkpoint['networks'] == [network]


def test_exhausted_safe_subnets_fail_before_creating_any_network():
    edges = NetworkEdges([{'dst': value} for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '198.18.0.0/15')], [])
    with pytest.raises(ValueError, match='No non-overlapping disposable IPv4 subnet'):
        edges._new_network('egress', internal=False)
    assert not any(command[:2] == ['network', 'create'] for command in edges.commands)
    assert edges.resources == {'containers': [], 'networks': []}


@pytest.mark.parametrize('change', ['privileged', 'default-bridge', 'extra-capability', 'writable-inputs',
                                  'proxy-credential', 'host-port', 'wrong-owner'])
def test_observed_builder_policy_rejects_docker_configuration_drift(recipe, tmp_path, change):
    _, value = recipe
    edges = CommandEdges(value)
    actual = {'Id': edges.container_id, 'Image': edges.base_id,
              'Config': {'Labels': {image_builder.LABEL: edges.run_id}, 'User': '0:0',
                         'Env': [key + '=' + value for key, value in image_builder.PROXY_ENV.items()]},
              'HostConfig': {'Privileged': False, 'NetworkMode': edges.network['name'],
                             'CapAdd': list(image_builder.BUILD_CAPABILITIES), 'CapDrop': ['ALL'],
                             'SecurityOpt': ['no-new-privileges:true'], 'PortBindings': {}},
              'NetworkSettings': {'Networks': {edges.network['name']: {}}},
              'Mounts': [{'Source': str(tmp_path), 'Destination': image_builder.INPUT_MOUNT, 'RW': False}]}
    if change == 'privileged':
        actual['HostConfig']['Privileged'] = True
    elif change == 'default-bridge':
        actual['HostConfig']['NetworkMode'] = 'bridge'
    elif change == 'extra-capability':
        actual['HostConfig']['CapAdd'].append('SYS_ADMIN')
    elif change == 'writable-inputs':
        actual['Mounts'][0]['RW'] = True
    elif change == 'proxy-credential':
        actual['Config']['Env'] = ['FTP_PROXY=ftp://user:secret@private.invalid']
    elif change == 'host-port':
        actual['HostConfig']['PortBindings'] = {'80/tcp': [{'HostPort': '80'}]}
    else:
        actual['Config']['Labels'][image_builder.LABEL] = 'not-our-owner'
    with pytest.raises(ValueError, match='owned-network isolation or proxy policy'):
        image_builder._builder_isolation(actual, network=edges.network, run_id=edges.run_id, work=tmp_path)


def test_independent_builder_preserves_primary_error_and_records_each_cleanup_failure(tmp_path, monkeypatch):
    builder = image_builder.ImageBuilder(state_dir=tmp_path / 'private',
        docker=('docker', '--host', 'unix:///var/run/docker.sock'), cleanup_path=tmp_path / 'cleanup.json')
    assert (builder.state / 'owner.json').stat().st_mode & 0o777 == 0o600
    builder.resources = {'containers': [{'id': 'f' * 64, 'name': 'owned-container'}],
                         'networks': [{'id': 'a' * 64, 'name': 'owned-network'}]}
    calls = []
    def fail(operation):
        calls.append(operation)
        raise ValueError('secondary-private-detail')
    monkeypatch.setattr(builder, '_discover_owned', lambda: fail('discover'))
    monkeypatch.setattr(builder, '_remove_container', lambda resource: fail('container'))
    monkeypatch.setattr(builder, '_remove_network', lambda resource: fail('network'))
    with pytest.raises(ValueError, match='primary-build-error') as caught:
        with builder:
            raise ValueError('primary-build-error')
    assert calls == ['discover', 'container', 'network']
    assert caught.value.__notes__ == ['Dependency image cleanup also failed; see cleanup.json']
    receipt = json.loads((tmp_path / 'cleanup.json').read_text())
    assert receipt['status'] == 'failed' and len(receipt['errors']) == 3
    assert receipt['remainingResources'] == builder.resources
    assert 'secondary-private-detail' not in json.dumps(receipt)
