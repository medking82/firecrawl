"""Actual Linux signal barrier for bootstrapping managers without an admission gate."""
import importlib.util,os,signal,subprocess,sys,tempfile,time,unittest
from pathlib import Path

@unittest.skipUnless(sys.platform=='linux','Requires Linux /proc and SIGSTOP')
class FrozenAdmission(unittest.TestCase):
    def test_no_legacy_admission_between_final_idle_check_and_termination(self):
        spec=importlib.util.spec_from_file_location('activation',Path(__file__).with_name('activate-manager.py'))
        activation=importlib.util.module_from_spec(spec);spec.loader.exec_module(activation)
        with tempfile.TemporaryDirectory(prefix='cdp-freeze-') as directory:
            root=Path(directory)
            program="""from pathlib import Path
import sys,time
r=Path(sys.argv[1]);(r/'ready').touch()
while True:
 if (r/'request').exists():
  (r/'request').unlink()
  with (r/'admitted').open('a') as out:out.write('session\\n')
 time.sleep(0.005)
"""
            child=subprocess.Popen([sys.executable,'-B','-c',program,str(root)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                def wait(predicate):
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline:
                        if predicate():return
                        if child.poll() is not None:self.fail('Admission fixture exited')
                        time.sleep(0.005)
                    self.fail('Admission fixture deadline exceeded')
                wait(lambda:(root/'ready').exists())
                # Legacy code does not understand a file-based gate.
                (root/'admission-paused').touch();(root/'request').touch()
                wait(lambda:(root/'admitted').exists())
                activation.freeze(child.pid)
                self.assertIn('\nState:\tT',Path(f'/proc/{child.pid}/status').read_text())
                # The activation idle check occurs here. A racing admission is
                # queued afterward, but the old process cannot execute it.
                (root/'request').touch();time.sleep(0.05)
                self.assertEqual((root/'admitted').read_text(),'session\n')
                os.kill(child.pid,signal.SIGKILL);child.wait(timeout=3)
                self.assertEqual((root/'admitted').read_text(),'session\n')
            finally:
                if child.poll() is None:
                    os.kill(child.pid,signal.SIGCONT);child.kill();child.wait(timeout=3)
                child.stderr.close()

if __name__=='__main__':unittest.main()
