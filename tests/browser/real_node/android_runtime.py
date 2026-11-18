"""Fresh AOSP/KVM runtime using the existing native Android browser/ADB helpers."""
import os
from pathlib import Path
import subprocess
import tempfile
import time

import android

from evidence import file_sha256

APK_SHA256 = '63c38a072a25e15f1947f29fb3b1ffd352722ac86478062112b5426428754ebe'


class AndroidRuntime:
    def __init__(self, raw):
        self.raw = raw
        self.process = None
        self.log = None

    def prepare(self):
        if android.CHANNEL != 'nightly':
            raise RuntimeError('The experimental addon requires the official pinned Nightly Android APK')
        if os.environ.get('ACCEPT_ANDROID_SDK_LICENSE') != '1':
            raise RuntimeError('Set ACCEPT_ANDROID_SDK_LICENSE=1 only after accepting the Android SDK license')
        if not Path('/dev/kvm').exists():
            raise RuntimeError('Actual Android requires /dev/kvm, never a privileged-container fallback')
        apk_sha256 = file_sha256('/opt/android/firefox.apk')
        if apk_sha256 != APK_SHA256:
            raise RuntimeError('The cached official Android APK does not match its pinned SHA256')
        with (self.raw / 'android-acceleration.txt').open('w') as output:
            subprocess.run(['emulator', '-accel-check'], check=True, stdout=output, stderr=subprocess.STDOUT)
        avds = Path(tempfile.mkdtemp(prefix='real-node-android-avd-'))
        os.environ['ANDROID_AVD_HOME'] = str(avds)
        avd = avds / 'portmap.avd'
        avd.mkdir()
        (avds / 'portmap.ini').write_text('avd.ini.encoding=UTF-8\npath=' + str(avd) + '\ntarget=android-30\n')
        (avd / 'config.ini').write_text(
            'AvdId=portmap\navd.ini.encoding=UTF-8\nabi.type=x86_64\nhw.cpu.arch=x86_64\n'
            'hw.cpu.ncore=2\nhw.ramSize=2048\nhw.gpu.enabled=yes\nhw.gpu.mode=swiftshader_indirect\n'
            'hw.lcd.width=1080\nhw.lcd.height=1920\nhw.lcd.density=420\n'
            'image.sysdir.1=system-images/android-30/default/x86_64/\ntag.id=default\n'
            'tag.display=Default\ndisk.dataPartition.size=3G\n')
        subprocess.run(['adb', 'start-server'], check=True)
        command = ['emulator', '-avd', 'portmap', '-port', '5554', '-accel', 'on',
                   '-no-window', '-no-audio', '-no-snapshot', '-no-boot-anim',
                   '-gpu', 'swiftshader_indirect', '-camera-back', 'none', '-camera-front', 'none',
                   '-tcpdump', str(self.raw / 'android-network.pcap')]
        # The owned container's resolver handles ordinary names only. No .portmap
        # DNS record or hosts override is installed; the addon owns those routes.
        resolvers = [line.split()[1] for line in Path('/etc/resolv.conf').read_text().splitlines()
                     if line.startswith('nameserver ')]
        if resolvers:
            command += ['-dns-server', ','.join(resolvers)]
        self.log = (self.raw / 'android-emulator.log').open('w')
        self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 300
        adb_timeouts = 0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('Actual Android emulator exited; inspect raw/android-emulator.log')
            try:
                boot = android.adb('shell', 'getprop', 'sys.boot_completed', check=False,
                                   capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired:
                adb_timeouts += 1
                continue
            if boot.returncode == 0 and boot.stdout.strip() == '1':
                break
            time.sleep(2)
        else:
            raise RuntimeError('Actual Android emulator did not complete boot within 300 seconds')
        root = android.adb('root', check=False, capture_output=True, text=True, timeout=30)
        android.adb('wait-for-device', timeout=30)
        uid = android.adb('shell', 'id', '-u', capture_output=True, text=True, timeout=10).stdout.strip()
        (self.raw / 'android-root-transition.txt').write_text(
            'request_exit=' + str(root.returncode) + '\n' + root.stdout + root.stderr + 'actual_uid=' + uid + '\n')
        if uid != '0':
            raise RuntimeError('The disposable AOSP guest adbd did not reconnect as root')
        with (self.raw / 'android-install.log').open('w') as output:
            android.adb('install', '--no-streaming', '-r', '/opt/android/firefox.apk', timeout=120,
                        stdout=output, stderr=subprocess.STDOUT)
        android.adb('shell', 'input', 'keyevent', '82')
        with (self.raw / 'android-properties.txt').open('w') as output:
            android.adb('shell', 'getprop', stdout=output)
        return {'kind': 'actual AOSP x86_64 KVM emulator, not a physical phone',
                'package': android.PACKAGE, 'apk_sha256': apk_sha256,
                'api': 30, 'systemImageRevision': 11, 'emulatorVersion': '37.2.13',
                'platformToolsVersion': '37.0.1', 'bootAdbTimeouts': adb_timeouts,
                'freshAvd': True}

    def stop(self):
        if not self.process:
            return
        if self.process.poll() is None:
            android.adb('emu', 'kill', check=False, capture_output=True, timeout=15)
        try:
            code = self.process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=15)
            raise RuntimeError('The native Android recorder did not shut down cleanly')
        finally:
            if self.log:
                self.log.close()
        self.process = None
        if code != 0:
            raise RuntimeError('The native Android emulator recorder exited with status ' + str(code))
