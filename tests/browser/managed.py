"""Real browser-generated keys, pinned SSH setup and disposable server negatives."""
import hashlib
import http.client
import json
import time
import uuid

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

import runner


def fixture(account, action='status', public_key=None):
    connection = http.client.HTTPConnection('node-b', 8080, timeout=5)
    try:
        if action == 'status':
            connection.request('GET', '/managed/status?account=' + account)
        else:
            value = {'account': account}
            if public_key is not None:
                value['publicKey'] = public_key
            connection.request('POST', '/managed/' + action, json.dumps(value),
                               {'Content-Type': 'application/json'})
        response = connection.getresponse()
        runner.require(response.status == 200, 'Disposable SSH fixture control failed')
        return json.loads(response.read())
    finally:
        connection.close()


def identity(browser):
    # Read the background's actual database without exporting any private bytes.
    # Abort an upgrade rather than creating a database during an absent-key probe.
    observed = browser.driver.execute_async_script('''
      const done=arguments[arguments.length-1];
      (async()=>{
        const local=await browser.storage.local.get(null);
        const reference=local.managedSshIdentityReference;
        const request=indexedDB.open('portmap-managed-ssh',1);
        let absent=false;
        request.onupgradeneeded=()=>{absent=true;request.transaction.abort();};
        request.onerror=()=>done(absent?{present:false,reference_present:!!reference,
          localPrivateCopy:'managedSshIdentity' in local}:{error:'Identity database read failed'});
        request.onsuccess=()=>{
          const db=request.result;
          if(!db.objectStoreNames.contains('identity')){db.close();done({error:'Missing identity store'});return;}
          const read=db.transaction('identity','readonly').objectStore('identity').get('managedSshIdentity');
          read.onerror=()=>{db.close();done({error:'Identity record read failed'});};
          read.onsuccess=async()=>{
            try{
              const record=read.result;
              if(!record){done({present:false,reference_present:!!reference,
                localPrivateCopy:'managedSshIdentity' in local});return;}
              const jwk=record.jwk,fields=['d','p','q','dp','dq','qi'];
              const bytes=Uint8Array.from(atob(jwk.n.replace(/-/g,'+').replace(/_/g,'/')+
                '='.repeat((4-jwk.n.length%4)%4)),c=>c.charCodeAt(0));
              const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(jwk.n)));
              const serialized=JSON.stringify(local);
              done({present:true,rsaBits:bytes.length*8-Math.clz32(bytes[0])+24,
                privateFieldsPresent:fields.every(field=>typeof jwk[field]==='string'&&jwk[field].length>0),
                publicReferenceMatches:reference?.schemaVersion===1&&reference.n===jwk.n,
                localPrivateCopy:'managedSshIdentity' in local||fields.some(field=>serialized.includes(jwk[field])),
                keyFingerprint:Array.from(digest,b=>b.toString(16).padStart(2,'0')).join('')});
            }catch(_){done({error:'Identity metadata observation failed'});}
            finally{db.close();}
          };
        };
      })().catch(()=>done({error:'Identity observation failed'}));
    ''')
    runner.require('error' not in observed, 'Cannot observe browser key metadata safely')
    return observed


def exercise(browser, case, proof, metadata, address):
    accounts = metadata['managedAccounts']
    public_key = None
    first_identity = None
    successful = ('manual', 'bootstrap', 'duplicate', 'nopassword')
    # The ordinary transport cases have finished. Release their parent claim
    # before using distinct *.b.portmap nodes covered by the real fixture CA.
    browser.options()
    if browser.cards('b.portmap'):
        browser.action('b.portmap', 'delete')

    def driver():
        # Restart replaces the actual WebDriver session, including on Android.
        return browser.driver

    def domain(account):
        return 'managed-' + account + '.b.portmap'

    def empty_secret_editor():
        return driver().execute_script('''
          return ['ssh-onetime-password','ssh-password','ssh-private-key','ssh-public-key','ssh-passphrase']
            .every(id=>document.getElementById(id).value==='');
        ''')

    def begin(account, *, expected_pin=''):
        browser.options()
        browser.sensitive_input = False
        browser.enter('name', 'Managed ' + account)
        browser.enter('domain', domain(account))
        Select(driver().find_element(By.ID, 'mode')).select_by_value('ssh')
        browser.enter('host', address)
        browser.enter('ssh-username', accounts[account]['username'])
        browser.enter('http-port', 8080)
        browser.enter('https-port', 8443)
        browser.advanced(False)
        browser.enter('ssh-port', metadata['sshPort'])
        browser.enter('ssh-target-host', '127.0.0.1')
        browser.enter('ssh-host-key', expected_pin)
        browser.ssh_advanced(False)
        runner.require(driver().find_element(By.ID, 'ssh-auth-type').get_attribute('value') == 'managed',
                       'New SSH editor did not default to the browser key')
        runner.require(not driver().find_element(By.ID, 'save').is_enabled() and
                       not driver().find_element(By.ID, 'install-managed-key').is_enabled(),
                       'Managed login was enabled before explicit fingerprint confirmation')
        runner.require(empty_secret_editor(), 'Fresh managed editor contains old credentials')

    def capture(name, field='managed-fields'):
        runner.require(empty_secret_editor(), 'Refusing a credential-bearing screenshot')
        browser.sensitive_input = False
        driver().execute_script('document.getElementById(arguments[0]).scrollIntoView({behavior:"instant",block:"center"})', field)
        browser.screenshot(name)

    def trust(account, *, screenshot=False):
        before = fixture(account)['events']
        driver().find_element(By.ID, 'inspect-host').click()
        WebDriverWait(driver(), 30).until(lambda d: d.find_element(By.ID, 'host-confirmation').is_displayed())
        runner.require(driver().find_element(By.ID, 'detected-fingerprint').text == metadata['hostKeySha256Hex'],
                       'Displayed fingerprint differs from the actual server host key')
        runner.require(not driver().find_element(By.ID, 'host-ack').is_selected() and
                       not driver().find_element(By.ID, 'confirm-host').is_enabled() and
                       not driver().find_element(By.ID, 'save').is_enabled(),
                       'Inspecting a host implicitly trusted or authenticated it')
        runner.require(fixture(account)['events'] == before, 'Host inspection attempted authentication')
        if screenshot:
            capture('managed-fingerprint-explicit-confirmation', 'host-confirmation')
        driver().find_element(By.ID, 'host-ack').click()
        driver().find_element(By.ID, 'confirm-host').click()
        WebDriverWait(driver(), 15).until(lambda d: d.find_element(By.ID, 'save').is_enabled())
        runner.require(fixture(account)['events'] == before, 'Confirming a fingerprint sent credentials')
        return {'fingerprint_confirmed': True, 'inspection_no_auth': True}

    def copy_key():
        browser.ssh_advanced()
        driver().find_element(By.ID, 'copy-managed-public-key').click()
        WebDriverWait(driver(), 30).until(lambda d: d.find_element(By.ID, 'managed-public-key').is_displayed() and
            d.find_element(By.ID, 'setup-status').text in
            ('Browser public key copied.', 'Select and copy the public key shown below.'))
        value = driver().find_element(By.ID, 'managed-public-key').get_attribute('value').strip()
        runner.require(value.startswith('ssh-rsa ') and '\n' not in value, 'Browser produced an invalid public-key line')
        runner.require(driver().find_element(By.ID, 'managed-public-key').get_property('readOnly') is True,
                       'Browser public-key output is editable')
        copied = driver().find_element(By.ID, 'setup-status').text == 'Browser public key copied.'
        runner.require(copied, 'Actual browser clipboard write did not complete')
        return value

    def saved(account):
        value = driver().execute_async_script('''
          const [domain,done]=arguments;
          browser.storage.local.get('config').then(({config})=>{
            const entry=config?.connections.find(value=>value.domain===domain);
            done(entry?{id:entry.id,domain:entry.domain,mode:entry.mode,host:entry.host,
              httpPort:entry.httpPort,httpsPort:entry.httpsPort,authType:entry.ssh?.auth.type,
              credentialFree:JSON.stringify(entry.ssh?.auth)===JSON.stringify({type:'managed'})}:null);
          }).catch(()=>done({error:'Saved state read failed'}));
        ''', domain(account))
        runner.require(not isinstance(value, dict) or 'error' not in value, 'Cannot observe saved managed auth metadata')
        return value

    def wait_saved(account):
        WebDriverWait(driver(), 40).until(lambda d: d.find_element(By.ID, 'name').get_attribute('value') == '' and
                                         len(browser.cards(domain(account))) == 1)
        runner.require(empty_secret_editor(), 'Successful setup retained the one-time password in the DOM')
        browser.sensitive_input = False
        value = saved(account)
        runner.require(value and value['authType'] == 'managed' and value['credentialFree'],
                       'Managed save persisted per-node credentials')
        return value

    def password_install(account, *, wrong=False):
        section = driver().find_element(By.ID, 'setup-password-section')
        if not section.get_property('open'):
            section.find_element(By.CSS_SELECTOR, 'summary').click()
        browser.sensitive_input = True
        password = accounts[account]['password']
        browser.enter('ssh-onetime-password', 'invalid-' + password if wrong else password)
        driver().find_element(By.ID, 'install-managed-key').click()

    def error_no_save(account):
        WebDriverWait(driver(), 40).until(lambda d: bool(d.find_element(By.ID, 'error').text) and
                                        d.find_element(By.ID, 'mode').is_enabled())
        runner.require(driver().execute_script("return document.getElementById('ssh-onetime-password').value==='';"),
                       'Failed setup retained the one-time password')
        browser.sensitive_input = False
        runner.require(not browser.cards(domain(account)) and saved(account) is None,
                       'Failed setup saved a managed connection')
        return driver().find_element(By.ID, 'error').text[:300]

    def route(account, *, tls=False):
        result = browser.page(('https' if tls else 'http') + '://' + domain(account) +
                              '/managed-' + uuid.uuid4().hex, 'node-b', loopback=True)
        runner.require(result['peerLoopback'], 'Managed node did not use the real SSH route')
        return result

    def watch(account):
        fixture(account, 'watch', public_key)

    def manual():
        nonlocal public_key, first_identity
        browser.options()
        before = identity(browser)
        runner.require(not before['present'] and not before['reference_present'],
                       'Managed key existed before its first explicit creation')
        begin('manual')
        capture('managed-default-simple-editor')
        confirmed = trust('manual', screenshot=True)
        # Confirmation is endpoint-bound, not a reusable global trust switch.
        browser.enter('ssh-port', metadata['sshPort'] + 1)
        runner.require(not driver().find_element(By.ID, 'save').is_enabled(), 'Endpoint edit retained host trust')
        browser.enter('ssh-port', metadata['sshPort'])
        runner.require(not driver().find_element(By.ID, 'save').is_enabled(), 'Restoring an endpoint silently restored trust')
        browser.ssh_advanced(False)
        trust('manual')
        public_key = copy_key()
        first_identity = identity(browser)
        runner.require(first_identity['present'] and first_identity['rsaBits'] >= 3072 and
                       first_identity['privateFieldsPresent'] and first_identity['publicReferenceMatches'] and
                       not first_identity['localPrivateCopy'], 'Browser key is not safely persisted in the real profile')
        capture('managed-copy-public-key-manual-setup', 'manual-managed-key')
        fixture('manual', 'install', public_key)
        browser.ssh_advanced(False)
        driver().find_element(By.ID, 'save').click()
        entry = wait_saved('manual')
        actual = fixture('manual')
        runner.require(actual['events']['passwordAccepted'] == 0 and actual['events']['keyAccepted'] >= 1,
                       'Manual browser-key setup did not authenticate with the generated key')
        page = route('manual')
        return {**confirmed, 'endpoint_change_invalidates_confirmation': True, 'identity_before': before,
                'identity_after': first_identity, 'public_key_copied': True, 'fresh_key_login': True,
                'route_loopback': page['peerLoopback'], 'saved': entry, 'fixture': actual}
    case('managed-browser-key-generate-copy-manual-login', manual, fatal=True)

    def bootstrap():
        begin('bootstrap')
        confirmed = trust('bootstrap')
        watch('bootstrap')
        before = fixture('bootstrap')['authorizedKeys']
        password_install('bootstrap')
        entry = wait_saved('bootstrap')
        actual = fixture('bootstrap')
        keys, events = actual['authorizedKeys'], actual['events']
        runner.require(events['passwordAccepted'] == 1 and events['keyAccepted'] >= 1 and events['execStarted'] >= 1,
                       'Bootstrap did not use password exec followed by a fresh key-auth connection')
        runner.require(keys['baselinePreserved'] and keys['managedOccurrences'] == 1 and
                       keys['sha256'] != before['sha256'] and keys['fileMode'] == '0o600' and
                       keys['directoryMode'] == '0o700', 'Bootstrap damaged existing authorized_keys or permissions')
        page, tls_page = route('bootstrap'), route('bootstrap', tls=True)
        browser.options()
        capture('managed-passwordless-connection-saved', 'connections')
        return {**confirmed, 'fresh_key_login': True, 'route_loopback': page['peerLoopback'],
                'https_route_loopback': tls_page['peerLoopback'], 'password_cleared': True,
                'existing_bytes_preserved': True, 'saved': entry, 'fixture': actual}
    case('managed-password-bootstrap-fresh-key-login', bootstrap, fatal=True)

    def duplicate():
        begin('duplicate')
        trust('duplicate')
        seeded = fixture('duplicate', 'install', public_key)
        password_install('duplicate')
        entry = wait_saved('duplicate')
        actual = fixture('duplicate')
        runner.require(actual['authorizedKeys']['sha256'] == seeded['authorizedKeys']['sha256'] and
                       actual['authorizedKeys']['unchanged'] and actual['authorizedKeys']['managedOccurrences'] == 1,
                       'Reinstall duplicated a restricted key or changed existing options/comments/bytes')
        runner.require(actual['events']['passwordAccepted'] == 1 and actual['events']['keyAccepted'] >= 1,
                       'Duplicate bootstrap did not verify a fresh key login')
        route('duplicate')
        return {'bytes_unchanged': True, 'restricted_options_preserved': True,
                'arbitrary_quoted_comment_preserved': True, 'fresh_key_login': True,
                'route_loopback': True, 'saved': entry, 'fixture': actual}
    case('managed-duplicate-preserves-bytes-and-restricted-options', duplicate, fatal=True)

    def wrong_pin():
        begin('wrongpin', expected_pin='0' * 64)
        watch('wrongpin')
        before = fixture('wrongpin')
        driver().find_element(By.ID, 'inspect-host').click()
        message = error_no_save('wrongpin')
        actual = fixture('wrongpin')
        runner.require('fingerprint' in message.lower() and actual['events'] == before['events'] and
                       actual['authorizedKeys']['unchanged'], 'Wrong host pin reached authentication or modified the account')
        runner.require(not driver().find_element(By.ID, 'save').is_enabled() and
                       not driver().find_element(By.ID, 'install-managed-key').is_enabled(), 'Wrong pin enabled login')
        capture('managed-wrong-pin-before-authentication')
        return {'inspection_no_auth': True, 'wrong_pin_before_auth': True, 'saved': None,
                'bytes_unchanged': True, 'ui_error': message, 'fixture': actual}
    case('managed-wrong-pin-rejected-before-authentication', wrong_pin, fatal=True)

    def rejected(account, *, wrong=False, expected=()):
        begin(account)
        trust(account)
        watch(account)
        before = fixture(account)
        password_install(account, wrong=wrong)
        message = error_no_save(account)
        actual = fixture(account)
        runner.require(actual['authorizedKeys'] == before['authorizedKeys'],
                       'Rejected setup changed existing authorized_keys, ownership, or permissions')
        runner.require(not expected or any(term in message.lower() for term in expected),
                       'Rejected setup omitted actionable failure information')
        if account in ('wrongpassword', 'nopassword'):
            runner.require(actual['events']['passwordAccepted'] == 0, 'Rejected password was authenticated')
        else:
            runner.require(actual['events']['passwordAccepted'] == 1, 'Safety negative did not reach real authenticated exec')
        runner.require(actual['events']['keyAccepted'] == 0, 'Rejected installer falsely verified a key login')
        return {'saved': None, 'bytes_unchanged': True, 'password_cleared': True,
                'ui_error': message, 'fixture': actual}

    case('managed-wrong-password-no-save',
         lambda: rejected('wrongpassword', wrong=True, expected=('authentication', 'password', 'auth')), fatal=True)
    case('managed-password-disabled-no-save',
         lambda: rejected('nopassword', expected=('authentication', 'password', 'auth')), fatal=True)

    def disabled_password_manual():
        begin('nopassword')
        trust('nopassword')
        fixture('nopassword', 'install', public_key)
        driver().find_element(By.ID, 'save').click()
        entry = wait_saved('nopassword')
        actual = fixture('nopassword')
        runner.require(actual['events']['passwordAccepted'] == 0 and actual['events']['keyAccepted'] >= 1,
                       'Password-disabled server did not accept manual browser-key onboarding')
        route('nopassword')
        return {'fresh_key_login': True, 'route_loopback': True, 'password_required': False,
                'saved': entry, 'fixture': actual}
    case('managed-password-disabled-manual-key-login', disabled_password_manual, fatal=True)
    case('managed-restricted-exec-no-save',
         lambda: rejected('restrictedexec', expected=('installer', 'exit')), fatal=True)

    def partial():
        begin('partial')
        trust('partial')
        watch('partial')
        password_install('partial')
        message = error_no_save('partial')
        actual = fixture('partial')
        runner.require(actual['events']['passwordAccepted'] == 1 and actual['events']['keyAccepted'] == 0 and
                       actual['authorizedKeys']['baselinePreserved'] and
                       actual['authorizedKeys']['managedOccurrences'] == 1,
                       'Partial-install negative did not append before a real new key-auth rejection')
        runner.require('install' in message.lower() and ('may' in message.lower() or 'added' in message.lower()),
                       'Partial remote change was not disclosed honestly')
        capture('managed-partial-install-honest-error')
        return {'saved': None, 'remote_key_installed': True, 'fresh_key_login': False,
                'partial_change_disclosed': True, 'password_cleared': True,
                'ui_error': message, 'fixture': actual}
    case('managed-partial-install-fresh-key-failure-no-save', partial, fatal=True)

    for name, account, terms in (
        ('managed-reject-authorized-keys-symlink', 'symlink', ('symlink', 'authorized_keys')),
        ('managed-reject-ssh-directory-symlink', 'symlinkdir', ('.ssh', 'unsafe')),
        ('managed-reject-world-writable-authorized-keys', 'permissions', ('writable', 'authorized_keys')),
        ('managed-reject-world-writable-ssh-directory', 'directorypermissions', ('.ssh', 'unsafe')),
        ('managed-reject-foreign-owned-authorized-keys', 'foreignowner', ('owned', 'authorized_keys')),
    ):
        case(name, lambda a=account, t=terms: rejected(a, expected=t), fatal=True)

    def abandon(account, *, close=False):
        begin(account)
        trust(account)
        watch(account)
        fixture(account, 'hold')
        try:
            password_install(account)
            WebDriverWait(driver(), 20).until(lambda _: fixture(account)['events']['passwordAccepted'] == 1 and
                                             fixture(account)['events']['execStarted'] >= 1)
            runner.require(fixture(account)['authorizedKeys']['managedOccurrences'] == 0,
                           'Cancellation barrier did not hold the real installer')
            if close:
                driver().get('about:blank')
            else:
                cancel = driver().find_element(By.ID, 'cancel-edit')
                runner.require(cancel.is_displayed() and cancel.is_enabled(), 'In-flight managed setup cannot be cancelled')
                cancel.click()
                WebDriverWait(driver(), 10).until(lambda d: d.find_element(By.ID, 'name').get_attribute('value') == '')
        finally:
            fixture(account, 'release')
        browser.options()
        WebDriverWait(driver(), 15).until(lambda d: d.find_element(By.ID, 'mode').is_enabled())
        # Allow the actual closed-channel/remote process completion to arrive;
        # then reload so this is a persistence assertion, not stale DOM state.
        time.sleep(2)
        driver().refresh()
        browser.wait_options()
        runner.require(saved(account) is None and not browser.cards(domain(account)) and empty_secret_editor(),
                       'Abandoned managed setup saved a stale draft or retained its password')
        browser.sensitive_input = False
        actual = fixture(account)
        return {'saved': None, 'no_stale_save': True, 'password_cleared': True,
                'options_closed': close, 'remote_key_present': actual['authorizedKeys']['managedOccurrences'] > 0,
                'fixture': actual}
    case('managed-cancel-inflight-no-stale-save', lambda: abandon('cancel'), fatal=True)
    case('managed-closed-options-no-stale-save', lambda: abandon('closed', close=True), fatal=True)

    def password_absent():
        browser.options()
        observed = driver().execute_async_script('''
          const [passwords,domains,done]=arguments;
          (async()=>{
            const local=await browser.storage.local.get(null);
            const response=await browser.runtime.sendMessage({channel:'portmap-connections',action:'get'});
            const all=JSON.stringify(local),snapshot=JSON.stringify(response);
            const entries=domains.map(domain=>local.config?.connections.find(entry=>entry.domain===domain));
            done({password_absent:passwords.every(value=>!all.includes(value)&&!snapshot.includes(value)),
              managed_auth_credential_free:entries.every(entry=>JSON.stringify(entry?.ssh.auth)==='{"type":"managed"}'),
              snapshot_has_no_private_material:!snapshot.includes('PRIVATE KEY')&&!snapshot.includes('"privateKey"')&&
                !snapshot.includes('"jwk"'),private_key_not_in_storage_local:!('managedSshIdentity' in local),
              password_input_empty:document.getElementById('ssh-onetime-password').value===''});
          })().catch(()=>done({error:'Credential persistence inspection failed'}));
        ''', [account['password'] for account in accounts.values()], [domain(account) for account in successful])
        runner.require('error' not in observed and all(observed.values()), 'Managed password or private key crossed persistence/public snapshot boundaries')
        return observed
    case('managed-password-not-persisted', password_absent, fatal=True)

    def restart():
        browser.options()
        before_identity = identity(browser)
        before = runner.observe(browser)
        url = 'http://' + domain('bootstrap') + '/managed-first-after-restart-' + uuid.uuid4().hex
        browser.restart(first_url=url)
        after = runner.observe(browser)
        runner.require(before['processID'] != after['processID'], 'Managed persistence did not restart the real native browser')
        page = browser.page(url, 'node-b', loopback=True, navigate=False)
        browser.options()
        after_identity = identity(browser)
        runner.require(before_identity == after_identity == first_identity,
                       'Restart replaced or lost the profile browser key')
        entry = saved('bootstrap')
        runner.require(entry and entry['credentialFree'], 'Restart lost managed auth metadata')
        capture('managed-restarted-profile-key-route', 'connections')
        return {'same_profile_key': True, 'native_process_changed': True, 'reinstalled': False,
                'first_route_loopback': page['peerLoopback'], 'route_loopback': True,
                'identity_after': after_identity, 'saved': entry, 'fixture': fixture('bootstrap')}
    case('managed-browser-restart-key-route', restart, fatal=True)

    def reset():
        browser.options()
        driver().find_element(By.ID, 'reset').click()
        browser.confirm_dialog()
        WebDriverWait(driver(), 15).until(lambda d: not d.find_elements(By.CSS_SELECTOR, 'article[data-domain]'))
        removed = identity(browser)
        runner.require(not removed['present'] and not removed['reference_present'] and not removed['localPrivateCopy'],
                       'Reset retained the private profile key or its public continuity reference')
        runner.require(driver().execute_async_script('''
          const done=arguments[0];browser.storage.local.get('config').then(({config})=>
            done(!config||config.connections.length===0)).catch(()=>done(false));
        '''), 'Reset retained local connections')
        browser.denied(domain('bootstrap'), 'managed-reset')
        remote = fixture('bootstrap')
        runner.require(remote['authorizedKeys']['managedOccurrences'] == 1,
                       'Local reset unexpectedly modified remote authorized_keys')
        begin('manual')
        replacement = copy_key()
        replacement_identity = identity(browser)
        runner.require(replacement != public_key and replacement_identity['keyFingerprint'] != first_identity['keyFingerprint'],
                       'Explicit reset did not permit creation of a distinct new profile key')
        driver().find_element(By.ID, 'reset').click()
        browser.confirm_dialog()
        WebDriverWait(driver(), 15).until(lambda _: not identity(browser)['present'])
        capture('managed-reset-destroys-profile-key', 'reset')
        return {'identity_removed': True, 'reference_removed': True, 'connections_removed': True,
                'new_key_differs': True, 'old_route_blocked': True, 'remote_key_retained': True,
                'identity_after': removed, 'fixture': remote}
    case('managed-reset-destroys-profile-key', reset, fatal=True)
    proof['managed_ssh'] = {'real_server': True, 'browser_webcrypto': True,
                            'disposable_accounts': len(accounts),
                            'private_material_exported_to_proof': False,
                            'profile_key_fingerprint': first_identity['keyFingerprint'],
                            'public_key_sha256': hashlib.sha256(public_key.encode()).hexdigest()}
