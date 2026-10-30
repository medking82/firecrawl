import os, signal, subprocess, sys, time, unittest
from pathlib import Path

@unittest.skipUnless(sys.platform.startswith('linux'), 'requires Linux process signals')
class WatchdogTest(unittest.TestCase):
    def test_parent_death_thaws_same_process(self):
        target = subprocess.Popen(['sleep', '30'])
        def cleanup():
            if target.poll() is None: target.kill()
            target.wait(timeout=5)
        self.addCleanup(cleanup)
        code = '''import importlib.util, os, signal, sys
s=importlib.util.spec_from_file_location('promote',sys.argv[1]); p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
w=p.start_thaw_watchdog(int(sys.argv[2])); os.kill(int(sys.argv[2]),signal.SIGSTOP)
print('stopped',flush=True); os._exit(0)
'''
        parent = subprocess.run([sys.executable, '-B', '-c', code, str(Path(__file__).with_name('promote.py')), str(target.pid)],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertIn('stopped', parent.stdout)
        for _ in range(100):
            state = Path(f'/proc/{target.pid}/status').read_text()
            if '\nState:\tT' not in state: break
            time.sleep(.02)
        else: self.fail('watchdog failed to resume process')

if __name__ == '__main__': unittest.main()
