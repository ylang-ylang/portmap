"""Control only owned fixture SSH processes; no runtime socket is mounted in clients."""
import json
from pathlib import Path
import subprocess
import sys
import time

artifacts, project, root, service, *arguments = sys.argv[1:]
artifacts, root = Path(artifacts), Path(root)
if service not in ('browser', 'android') or not project.startswith('portmap-browser-'):
    raise SystemExit('Invalid isolated browser test identity')
sys.path.insert(0, str(root / 'tools'))
from build_firefox_extension import EVIDENCE_SCHEMA, source_provenance

# Observe real checkout/dirtiness and exact runtime+harness bytes before launch.
# A base HEAD alone must never be presented as the uncommitted feature source.
(artifacts / 'source-provenance.json').write_text(json.dumps({
    'evidence_schema': EVIDENCE_SCHEMA,
    'source_provenance': source_provenance(root),
}, indent=2))
compose = ['docker', 'compose', '--project-name', project,
           '--file', str(root / 'tests/browser/compose.yaml'),
           '--file', str(root / 'tests/browser/compose.android.yaml')]
for filename in ('control-request.json', 'control-response.json'):
    (artifacts / filename).unlink(missing_ok=True)
script = '/app/tests/browser/android.py' if service == 'android' else '/app/tests/browser/runner.py'
process = subprocess.Popen(compose + ['--profile', 'android', 'run', '--rm', '--no-deps', service, 'python', script, *arguments])
handled = set()
deadline = time.monotonic() + 1800
try:
    while process.poll() is None:
        if time.monotonic() > deadline:
            raise TimeoutError('Actual browser test deadline exceeded')
        request = artifacts / 'control-request.json'
        if request.exists():
            try:
                value = json.loads(request.read_text())
            except json.JSONDecodeError:
                time.sleep(.1)
                continue
            if value['gate'] not in handled:
                if value.get('action') != 'terminate-ssh' or value.get('service') not in ('node-a', 'node-b'):
                    raise RuntimeError('Unknown owned-fixture control action')
                code = """
import os,pwd,signal,json
from pathlib import Path
uid=pwd.getpwnam('probe').pw_uid
terminated=[]
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit(): continue
    try:
        fields={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in (entry/'status').read_text().splitlines() if ':' in line}
        if int(fields['Uid'].split()[0])==uid:
            os.kill(int(entry.name),signal.SIGKILL);terminated.append(int(entry.name))
    except (FileNotFoundError,ProcessLookupError): pass
print(json.dumps({'terminated':len(terminated),'container_pids':terminated}))
"""
                completed = subprocess.run(compose + ['exec','-T',value['service'],'python','-c',code],
                                           check=True, capture_output=True, text=True)
                result = json.loads(completed.stdout)
                result.update(gate=value['gate'], service=value['service'])
                (artifacts / 'control-response.json').write_text(json.dumps(result))
                handled.add(value['gate'])
        time.sleep(.1)
    raise SystemExit(process.returncode)
finally:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
