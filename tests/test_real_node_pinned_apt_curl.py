"""Exercise the guest curl boundary without host package/network side effects."""
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from tests.browser.real_node import base_bootstrap


URL = ('https://snapshot.ubuntu.com/ubuntu/20261118T000000Z/pool/main/f/'
       'fixture-pinned/fixture-pinned_2.3.4-0ubuntu1~24.04.1_amd64.deb')
BODY = b'bounded adapter bytes; real Debian archive verification is covered separately'


@pytest.fixture(autouse=True)
def no_unplanned_command(monkeypatch):
    def reject(argv, **kwargs):
        pytest.fail('Unexpected external command at the curl unit boundary: ' + repr(argv))
    monkeypatch.setattr(base_bootstrap.subprocess, 'run', reject)


@pytest.fixture
def curl(monkeypatch):
    boundary = SimpleNamespace(calls=[], responses=[], temporary=[])

    def run(argv, **kwargs):
        boundary.calls.append((argv, kwargs))
        response = boundary.responses.pop(0)
        if 'error' in response:
            raise response['error']
        body = Path(argv[argv.index('--output') + 1])
        headers = Path(argv[argv.index('--dump-header') + 1])
        boundary.temporary.append(body.parent)
        body.write_bytes(response.get('body', BODY))
        status = response.get('status', 200)
        headers.write_bytes(response.get('headers', f'HTTP/1.1 {status} fixture\r\nContent-Length: {len(BODY)}\r\n\r\n'.encode()))
        stdout = response.get('stdout', f"{status}\n{response.get('effective', argv[-1])}\n{response.get('redirect', '')}")
        return subprocess.CompletedProcess(argv, response.get('exit', 0), stdout=stdout,
                                           stderr='fixture-credential-must-not-be-logged')

    monkeypatch.setattr(base_bootstrap.subprocess, 'run', run)
    return boundary


def assert_clean(boundary):
    assert all(not path.exists() for path in boundary.temporary)


@pytest.mark.parametrize('headers', [None, b'HTTP/2 200\r\n\r\n',
                                    b'HTTP/1.1 103 Early Hints\r\nLink: fixture\r\n\r\nHTTP/1.1 200 OK\r\nContent-Encoding: identity\r\n\r\n'])
def test_curl_is_bounded_verified_and_independent_of_credentials(monkeypatch, curl, headers):
    for name in ('HTTP_PROXY', 'https_proxy', 'FTP_PROXY', 'ALL_PROXY'):
        monkeypatch.setenv(name, 'https://fixture-user:fixture-secret@proxy.invalid')
    monkeypatch.setenv('CURL_CA_BUNDLE', '/fixture/untrusted-ca')
    monkeypatch.setenv('CURL_HOME', '/fixture/credential-home')
    curl.responses.append({} if headers is None else {'headers': headers})
    with base_bootstrap._open_package(URL, 1024) as response:
        assert response.status == 200
        assert response.geturl() == URL
        assert response.read() == BODY
    command, options = curl.calls[0]
    assert command[:2] == ['curl', '--disable']
    assert command[command.index('--proxy') + 1] == ''
    assert command[command.index('--noproxy') + 1] == '*'
    assert command[command.index('--proto') + 1] == '=https'
    assert command[command.index('--max-filesize') + 1] == '1024'
    assert 0 < float(command[command.index('--connect-timeout') + 1]) <= 30
    assert 0 < float(command[command.index('--max-time') + 1]) <= 120
    assert 0 < options['timeout'] <= 121
    assert options['capture_output'] and options['text']
    assert not {'--retry', '--retry-all-errors', '--insecure', '-k', '--location', '-L', '--netrc'} & set(command)
    environment = options['env']
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'FTP_PROXY', 'NO_PROXY', 'ALL_PROXY'):
        assert environment[name] == environment[name.lower()] == ''
    assert set(environment) == {'PATH', 'LC_ALL', 'HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy',
                                'FTP_PROXY', 'ftp_proxy', 'NO_PROXY', 'no_proxy', 'ALL_PROXY', 'all_proxy'}
    assert 'fixture-secret' not in repr(environment)
    assert len(curl.calls) == 1
    assert_clean(curl)


def test_only_canonical_same_provider_and_path_redirect_is_followed(monkeypatch, curl):
    times = iter((0, 0, 30))
    monkeypatch.setattr(base_bootstrap, 'time', SimpleNamespace(monotonic=lambda: next(times)))
    canonical = URL.replace('~', '%7E')
    curl.responses.extend([{'status': 302, 'redirect': canonical}, {}])
    with base_bootstrap._open_package(URL, 1024) as response:
        assert response.geturl() == canonical
        assert response.read() == BODY
    assert [command[-1] for command, _ in curl.calls] == [URL, canonical]
    assert [float(command[command.index('--max-time') + 1]) for command, _ in curl.calls] == [120, 90]
    assert_clean(curl)


@pytest.mark.parametrize('redirect', [
    URL.replace('snapshot.ubuntu.com', 'archive.ubuntu.com'),
    URL.replace('snapshot.ubuntu.com', 'snapshot.ubuntu.com.evil.invalid'),
    URL.replace('https:', 'http:', 1),
    URL.replace('https://', 'https://user:secret@', 1),
    URL.replace('snapshot.ubuntu.com', 'snapshot.ubuntu.com:443'),
    URL.replace('2.3.4', '2.3.5'), URL + '?download=1', URL + '#fragment',
    URL.replace('/pool/', '/pool%2f'), URL.replace('~', '%257E'), URL,
])
def test_unapproved_or_repeated_redirect_never_causes_another_get(curl, redirect):
    curl.responses.append({'status': 302, 'redirect': redirect})
    with pytest.raises(base_bootstrap.PackageBlobError):
        with base_bootstrap._open_package(URL, 1024):
            pytest.fail('Unsafe redirect was accepted')
    assert len(curl.calls) == 1
    assert_clean(curl)


def test_canonical_redirect_cycle_is_bounded(curl):
    canonical = URL.replace('~', '%7E')
    curl.responses.extend([{'status': 301, 'redirect': canonical}, {'status': 302, 'redirect': URL}])
    with pytest.raises(base_bootstrap.PackageBlobError):
        with base_bootstrap._open_package(URL, 1024):
            pytest.fail('Redirect cycle was accepted')
    assert len(curl.calls) == 2
    assert_clean(curl)


@pytest.mark.parametrize('status', [404, 410, 500, 502, 503, 599])
def test_http_availability_status_remains_available_to_exact_alternate_policy(curl, status):
    curl.responses.append({'status': status})
    with pytest.raises(HTTPError) as failure:
        with base_bootstrap._open_package(URL, 1024):
            pytest.fail('HTTP error was accepted as package bytes')
    assert failure.value.code == status
    assert failure.value.url == URL
    assert len(curl.calls) == 1
    assert_clean(curl)


@pytest.mark.parametrize('exit_code', [18, 28, 35, 60, 63])
def test_tls_timeout_size_and_body_errors_fail_closed_without_retry_or_stderr_leak(curl, exit_code):
    curl.responses.append({'exit': exit_code})
    with pytest.raises(base_bootstrap.PackageBlobError) as failure:
        with base_bootstrap._open_package(URL, 1024):
            pytest.fail('Curl failure was accepted')
    assert str(exit_code) in str(failure.value)
    assert 'fixture-credential' not in str(failure.value)
    assert len(curl.calls) == 1
    assert_clean(curl)


@pytest.mark.parametrize('error', [OSError('fixture execution error'), subprocess.TimeoutExpired(['curl'], 120)])
def test_execution_failures_are_not_http_availability_fallbacks(curl, error):
    curl.responses.append({'error': error})
    with pytest.raises(base_bootstrap.PackageBlobError):
        with base_bootstrap._open_package(URL, 1024):
            pytest.fail('Transport failure was accepted')
    assert len(curl.calls) == 1


@pytest.mark.parametrize('response', [
    {'stdout': '200\n' + URL}, {'stdout': 'invalid\n' + URL + '\n'},
    {'effective': URL.replace('snapshot.ubuntu.com', 'archive.ubuntu.com')},
    {'effective': URL + '?query=1'}, {'status': 200, 'redirect': URL},
    {'headers': b''}, {'headers': b'X' * 65537},
    {'headers': b'HTTP/1.1 201 Created\r\nContent-Length: 12\r\n\r\n'},
])
def test_unbound_or_inconsistent_response_metadata_is_rejected(curl, response):
    curl.responses.append(response)
    with pytest.raises(base_bootstrap.PackageBlobError):
        with base_bootstrap._open_package(URL, 1024):
            pytest.fail('Unbound response metadata was accepted')
    assert len(curl.calls) == 1
    assert_clean(curl)


@pytest.mark.parametrize('size', [0, -1, True, '1024', None])
def test_byte_bound_is_required_before_network_creation(curl, size):
    with pytest.raises(base_bootstrap.PackageBlobError):
        with base_bootstrap._open_package(URL, size):
            pytest.fail('Invalid byte bound was accepted')
    assert curl.calls == []


def test_overall_deadline_prevents_a_late_redirect_request(monkeypatch, curl):
    times = iter((0, 121))
    monkeypatch.setattr(base_bootstrap, 'time', SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(base_bootstrap.PackageBlobError, match='deadline'):
        with base_bootstrap._open_package(URL, 1024):
            pytest.fail('Expired request was issued')
    assert curl.calls == []
