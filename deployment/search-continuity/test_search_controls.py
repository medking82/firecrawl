import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
import search_controls as controls


def success():
    return {'success': True, 'data': {'web': [{'url': 'https://example.org', 'title': 'Public', 'description': 'Evidence'}]},
            'searchContinuity': {'provider': 'firecrawl', 'fallback': False}}


class Clock:
    value = 100.0
    def __call__(self):return self.value


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.budget=Mock();self.budget.run.side_effect=lambda f:f()
        self.control=controls.SearchControls(self.budget,ttl=30,capacity=2,clock=self.clock,wall=lambda:1700000000)
        self.body={'query':'public subject','limit':1}
        self.fetch=Mock(side_effect=success)

    def call(self,**kwargs):return self.control.run(self.body,self.fetch,scope=b'user-session-a',**kwargs)

    def test_ttl_origin_timestamp_and_expiry(self):
        a=self.call();self.clock.value+=9
        b=self.call()
        self.assertEqual(b['searchContinuity']['cacheStatus'],'hit')
        self.assertEqual(b['searchContinuity']['cacheAgeSeconds'],9)
        self.assertEqual(a['searchContinuity']['retrievedAt'],b['searchContinuity']['retrievedAt'])
        self.fetch.assert_called_once()
        self.clock.value+=21
        self.assertEqual(self.call()['searchContinuity']['cacheStatus'],'miss')
        self.assertEqual(self.fetch.call_count,2)

    def test_unknown_identity_never_caches_or_coalesces(self):
        for _ in range(2):
            result=self.control.run(self.body,self.fetch)
            self.assertEqual(result['searchContinuity']['cacheStatus'],'bypass_unscoped')
        self.assertEqual(self.fetch.call_count,2)
        self.assertEqual(len(self.control.cache),0)

    def test_sessions_and_accounts_are_isolated_and_context_resets(self):
        for account,session in [('a','s1'),('a','s2'),('b','s1')]:
            with controls.trusted_session(account,session):
                self.control.run(self.body,self.fetch)
                self.control.run(self.body,self.fetch)
        self.assertEqual(self.fetch.call_count,3)
        self.assertIsNone(controls.SCOPE.get())
        self.assertNotIn('s1',repr(self.control.cache))

    def test_complete_parameters_are_keyed(self):
        self.call()
        for key,value in [('limit',2),('lang','zh'),('country','my'),('includeDomains',['example.org']),('sources',['news']),('scrapeOptions',{'formats':['html']})]:
            self.control.run({**self.body,key:value},self.fetch,scope=b'user-session-a')
        self.assertEqual(self.fetch.call_count,7)

    def test_latest_explicit_fresh_and_time_constraint_bypass(self):
        self.call();self.call(fresh=True)
        self.control.run({**self.body,'query':'latest public subject'},self.fetch,scope=b'user-session-a')
        self.control.run({**self.body,'tbs':'d'},self.fetch,scope=b'user-session-a')
        with controls.freshness(True):self.call()
        self.assertEqual(self.fetch.call_count,5)
        self.assertFalse(controls.FRESH.get())

    def test_errors_empty_and_fallback_warnings_not_cached(self):
        for value in [ {'success':False}, {'success':True,'data':{}},
                       {'success':True,'data':{'web':[]},'searchContinuity':{'provider':'firecrawl','fallback':False}},
                       {**success(),'searchContinuity':{'provider':'exa','fallback':True,'primaryStatus':'primary_empty_unknown'}} ]:
            fetch=Mock(return_value=value)
            for _ in range(2):self.control.run(self.body,fetch,scope=b'x')
            self.assertEqual(fetch.call_count,2)
        fetch=Mock(side_effect=ValueError('exa_rate_limited'))
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'exa_rate_limited'):self.control.run(self.body,fetch,scope=b'x')
        self.assertEqual(fetch.call_count,2)

    def test_same_scoped_concurrent_query_has_one_fetch(self):
        entered,release=threading.Event(),threading.Event()
        def fetch():entered.set();release.wait(3);return success()
        call=Mock(side_effect=fetch)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            first=pool.submit(self.control.run,self.body,call,scope=b'x')
            self.assertTrue(entered.wait(1))
            second=pool.submit(self.control.run,self.body,call,scope=b'x')
            # Observe the waiter via a separate instrumented Event, without timing the upstream.
            deadline=time.monotonic()+1
            while not second.running() and time.monotonic()<deadline:time.sleep(.001)
            release.set()
            a,b=first.result(),second.result()
        call.assert_called_once()
        self.assertIn(b['searchContinuity']['cacheStatus'],('coalesced','hit'))
        b['data']['web'][0]['title']='mutated'
        self.assertEqual(self.control.run(self.body,call,scope=b'x')['data']['web'][0]['title'],'Public')

    def test_fallback_follower_runs_own_once_through_budget_after_full_wait(self):
        entered,release=threading.Event(),threading.Event()
        value={**success(),'searchContinuity':{'provider':'exa','fallback':True,'primaryStatus':'primary_empty_unknown'}}
        def fetch():entered.set();release.wait(3);return value
        call=Mock(side_effect=fetch)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            first=pool.submit(self.control.run,self.body,call,scope=b'x')
            self.assertTrue(entered.wait(1))
            event=next(iter(self.control.flights.values()))['event']
            event.wait=Mock(wraps=event.wait)
            second=pool.submit(self.control.run,self.body,call,scope=b'x')
            deadline=time.monotonic()+1
            while not event.wait.called and time.monotonic()<deadline:time.sleep(.001)
            event.wait.assert_called_once_with(120.0)
            release.set()
            a,b=first.result(),second.result()
        self.assertTrue(a['success']);self.assertTrue(b['success'])
        self.assertEqual(b['searchContinuity']['cacheStatus'],'bypass_uncoalesced')
        self.assertEqual(call.call_count,2);self.assertEqual(self.budget.run.call_count,2)
        self.assertFalse(self.control.cache);self.assertFalse(self.control.flights)

    def test_capacity_and_large_results_are_bounded(self):
        for query in ('a','b','c'):
            self.control.run({'query':query},self.fetch,scope=b'x')
        self.assertEqual(len(self.control.cache),2)
        huge=success();huge['data']['web'][0]['description']='x'*(1024*1024+1)
        fetch=Mock(return_value=huge)
        for _ in range(2):self.control.run({'query':'huge'},fetch,scope=b'x')
        self.assertEqual(fetch.call_count,2)


@unittest.skipUnless(os.name=='posix','GA401 cross-process owner')
class SharedBudgetTests(unittest.TestCase):
    def test_processes_share_one_concurrency_and_start_spacing(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);events=root/'events';state=root/'state'
            script="""import sys,time,os
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from search_controls import SharedBudget
out=Path(sys.argv[3])
def operation():
 with out.open('a') as f:f.write('start '+str(time.monotonic())+'\\n');f.flush()
 time.sleep(.04)
 with out.open('a') as f:f.write('end '+str(time.monotonic())+'\\n')
SharedBudget(Path(sys.argv[2]),spacing=.10,wait=4).run(operation)
"""
            args=[sys.executable,'-B','-c',script,str(Path(controls.__file__).parent),str(state),str(events)]
            children=[subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(4)]
            for child in children:
                out,err=child.communicate(timeout=8)
                self.assertEqual(child.returncode,0,err)
            rows=[line.split() for line in events.read_text().splitlines()]
            self.assertEqual([r[0] for r in rows],['start','end']*4)
            starts=[float(r[1]) for r in rows if r[0]=='start']
            self.assertTrue(all(b-a>=.09 for a,b in zip(starts,starts[1:])))
            self.assertNotIn('query',(state/'admission.lock').read_text())

    def test_busy_budget_does_not_call_upstream_and_exception_releases(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)/'state';gate=controls.SharedBudget(state,spacing=0,wait=.03)
            with self.assertRaisesRegex(ValueError,'test-error'):gate.run(lambda:(_ for _ in ()).throw(ValueError('test-error')))
            with (state/'admission.lock').open('rb') as held:
                fcntl.flock(held,fcntl.LOCK_EX)
                fetch=Mock()
                with self.assertRaises(controls.BudgetBusy):gate.run(fetch)
                fetch.assert_not_called()
            self.assertEqual(gate.run(lambda:7),7)

    def test_nonfinite_state_fails_without_calling_upstream(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)/'state';gate=controls.SharedBudget(state,spacing=0)
            gate.run(lambda:None)
            for value in ('nan','inf','-inf'):
                (state/'admission.lock').write_text(value)
                fetch=Mock()
                with self.assertRaisesRegex(ValueError,'invalid_state'):gate.run(fetch)
                fetch.assert_not_called()

    def test_crashed_process_releases_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)/'state'
            script='import sys,os;sys.path.insert(0,sys.argv[1]);from search_controls import SharedBudget;SharedBudget(sys.argv[2],spacing=0).run(lambda:os._exit(7))'
            result=subprocess.run([sys.executable,'-B','-c',script,str(Path(controls.__file__).parent),str(state)],timeout=5)
            self.assertEqual(result.returncode,7)
            self.assertEqual(controls.SharedBudget(state,spacing=0,wait=.1).run(lambda:1),1)

    def test_linked_or_unsafe_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);actual=root/'actual';actual.mkdir(mode=0o700)
            link=root/'linked';link.symlink_to(actual,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'unsafe_path'):controls.SharedBudget(link).run(lambda:None)
            unsafe=root/'unsafe';unsafe.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError,'unsafe_owner'):controls.SharedBudget(unsafe).run(lambda:None)
            target=root/'target';target.touch();(actual/'admission.lock').symlink_to(target)
            with self.assertRaises(OSError):controls.SharedBudget(actual).run(lambda:None)


if __name__=='__main__':unittest.main()