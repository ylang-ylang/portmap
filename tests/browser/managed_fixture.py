"""Disposable real-sshd accounts and public-only observations for onboarding."""
import base64
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import re
import secrets
import subprocess
import threading


SCENARIOS = ('manual', 'bootstrap', 'duplicate', 'wrongpin', 'wrongpassword',
             'nopassword', 'restrictedexec', 'partial', 'symlink', 'symlinkdir',
             'permissions', 'directorypermissions', 'foreignowner', 'cancel', 'closed')


class ManagedAccounts:
    def __init__(self, existing_public_key):
        self.lock = threading.Lock()
        self.accounts = {}
        self.credentials = {}
        seed = ('# owned disposable account; preserve comments and options\r\n'
                'no-agent-forwarding,no-X11-forwarding ' + existing_public_key.strip() +
                ' existing-fixture-key\n# final comment without LF').encode()
        for index, scenario in enumerate(SCENARIOS):
            username = 'pm' + scenario
            subprocess.run(['useradd', '--create-home', '--uid', str(11000 + index),
                            '--shell', '/bin/sh', username], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            password = secrets.token_urlsafe(32)
            subprocess.run(['chpasswd'], input=username + ':' + password + '\n',
                           text=True, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            user = pwd.getpwnam(username)
            home = Path(user.pw_dir)
            directory = home / '.ssh'
            directory.mkdir(mode=0o700)
            authorized = directory / 'authorized_keys'
            authorized.write_bytes(seed)
            # Set modes while root still owns these test paths. The target
            # deliberately has CHOWN but not FOWNER in its narrow capability set.
            os.chmod(authorized, 0o666 if scenario == 'permissions' else 0o600)
            os.chmod(directory, 0o777 if scenario == 'directorypermissions' else 0o700)
            for path in (directory, authorized):
                os.chown(path, user.pw_uid, user.pw_gid)
            sentinel = None
            if scenario == 'symlink':
                sentinel = home / 'owned-sentinel'
                authorized.rename(sentinel)
                authorized.symlink_to(sentinel)
            elif scenario == 'symlinkdir':
                sentinel = home / 'owned-ssh-sentinel'
                directory.rename(sentinel)
                directory.symlink_to(sentinel, target_is_directory=True)
                sentinel = sentinel / 'authorized_keys'
            elif scenario == 'foreignowner':
                os.chown(authorized, 0, 0)
            self.accounts[scenario] = {
                'username': username, 'directory': directory, 'authorized': authorized,
                'baseline': seed, 'managed_blob': None, 'sentinel': sentinel,
                'events': dict(passwordAccepted=0, passwordFailed=0, keyAccepted=0, execStarted=0),
                'held': None,
            }
            self.credentials[scenario] = {'username': username, 'password': password}

    def configuration(self):
        users = ' '.join(account['username'] for account in self.accounts.values())
        return ('\nMatch User pmnopassword\n    PasswordAuthentication no\n'
                'Match User pmrestrictedexec\n    ForceCommand /bin/false\n'
                'Match User pmpartial\n    PubkeyAuthentication no\n', users)

    def observe_logs(self, stream):
        for raw in iter(stream.readline, b''):
            line = raw.decode('utf-8', errors='replace')
            with self.lock:
                for account in self.accounts.values():
                    user = re.escape(account['username'])
                    if re.search(r'Accepted password for ' + user + r'\b', line):
                        account['events']['passwordAccepted'] += 1
                    if re.search(r'Failed password for ' + user + r'\b', line):
                        account['events']['passwordFailed'] += 1
                    if re.search(r'Accepted publickey for ' + user + r'\b', line):
                        account['events']['keyAccepted'] += 1
                    if re.search(r'Starting session: (?:forced-command .*|command) for ' + user + r'\b', line):
                        account['events']['execStarted'] += 1
        # Never persist raw server debug output or any credential values.

    @staticmethod
    def validate_public_key(value):
        if not isinstance(value, str) or len(value) > 8192 or not re.fullmatch(r'ssh-rsa [A-Za-z0-9+/]+={0,2}', value):
            raise ValueError('Only one canonical browser RSA public key is accepted')
        blob = value.split()[1]
        decoded = base64.b64decode(blob, validate=True)
        if base64.b64encode(decoded).decode() != blob or not decoded.startswith(b'\x00\x00\x00\x07ssh-rsa'):
            raise ValueError('Invalid browser RSA public key')
        return blob

    def status(self, name):
        account = self.accounts[name]
        authorized, directory = account['authorized'], account['directory']
        data = authorized.read_bytes()
        sentinel = account['sentinel']
        blob = account['managed_blob']
        return {'scenario': name, 'username': account['username'],
                'events': dict(account['events']), 'held': account['held'] is not None,
                'authorizedKeys': {'sha256': hashlib.sha256(data).hexdigest(), 'length': len(data),
                    'baselinePreserved': data.startswith(account['baseline']),
                    'unchanged': data == account['baseline'],
                    'managedOccurrences': data.count(blob.encode()) if blob else 0,
                    'fileMode': oct(authorized.lstat().st_mode & 0o777),
                    'directoryMode': oct(directory.lstat().st_mode & 0o777),
                    'fileOwner': authorized.lstat().st_uid,
                    'fileSymlink': authorized.is_symlink(), 'directorySymlink': directory.is_symlink(),
                    'sentinelUnchanged': sentinel is None or sentinel.read_bytes() == account['baseline']}}

    def release(self, name):
        account = self.accounts[name]
        if account['held'] is not None:
            descriptor = account['held']
            account['held'] = None
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def control(self, action, value):
        name = value.get('account')
        if name not in self.accounts or set(value) - {'account', 'publicKey'}:
            raise ValueError('Unknown disposable account control')
        with self.lock:
            account = self.accounts[name]
            if action == 'install':
                if name not in ('manual', 'duplicate', 'nopassword'):
                    raise ValueError('Manual installation is not allowed for this scenario')
                blob = self.validate_public_key(value.get('publicKey'))
                options = ('restrict,port-forwarding,permitopen="127.0.0.1:8080",'
                           'permitopen="127.0.0.1:8443" ' if name == 'duplicate' else '')
                comment = ' trailing " unmatched comment' if name == 'duplicate' else ' owned-manual-install'
                entry = ('\n' + options + value['publicKey'] + comment + '\n# preserve suffix without LF').encode()
                with account['authorized'].open('ab') as output:
                    output.write(entry)
                account['managed_blob'] = blob
                account['baseline'] = account['authorized'].read_bytes()
            elif action == 'watch':
                account['managed_blob'] = self.validate_public_key(value.get('publicKey'))
            elif action == 'hold':
                if name not in ('cancel', 'closed') or account['held'] is not None:
                    raise ValueError('Only one disposable cancellation lock may be held')
                descriptor = os.open(account['authorized'], os.O_RDWR)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                account['held'] = descriptor
                timer = threading.Timer(30, self.expire, args=(name,))
                timer.daemon = True
                timer.start()
            elif action == 'release':
                self.release(name)
            elif action != 'status':
                raise ValueError('Unknown disposable control operation')
            return self.status(name)

    def expire(self, name):
        with self.lock:
            self.release(name)
