import os
import subprocess
import unittest
from unittest import mock

from epubcast import power


class FakeProc:
    def __init__(self, *args, **kwargs):
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class KeepAwakeTest(unittest.TestCase):
    def run_on(self, system, which='/usr/bin/tool'):
        """Enter and leave the context on a pretended platform, returning the spawned args."""
        proc = FakeProc()
        with mock.patch.object(power.platform, 'system', return_value=system), \
             mock.patch.object(power.shutil, 'which', return_value=which), \
             mock.patch.object(power.subprocess, 'Popen', return_value=proc) as popen:
            with power.keep_awake():
                pass
        return popen, proc

    def test_mac_holds_caffeinate_and_ties_it_to_our_pid(self):
        popen, proc = self.run_on('Darwin')
        args = popen.call_args[0][0]
        self.assertEqual(args[0], 'caffeinate')
        self.assertIn('-i', args)  # idle sleep is the one that kills a long run
        self.assertNotIn('-d', args)  # but the display is free to go dark
        # -w <our pid>: the lock dies with us even if we are killed rather than exiting.
        self.assertEqual(args[args.index('-w') + 1], str(os.getpid()))
        self.assertTrue(proc.terminated)

    def test_linux_holds_systemd_inhibit(self):
        popen, proc = self.run_on('Linux')
        args = popen.call_args[0][0]
        self.assertEqual(args[0], 'systemd-inhibit')
        self.assertIn('--what=idle:sleep', args)
        self.assertTrue(proc.terminated)

    def test_a_missing_helper_is_a_warning_not_a_crash(self):
        popen, _ = self.run_on('Darwin', which=None)
        popen.assert_not_called()

    def test_an_unknown_platform_is_a_warning_not_a_crash(self):
        popen, _ = self.run_on('Haiku')
        popen.assert_not_called()

    def test_the_lock_is_released_when_the_body_raises(self):
        proc = FakeProc()
        with mock.patch.object(power.platform, 'system', return_value='Darwin'), \
             mock.patch.object(power.shutil, 'which', return_value='/usr/bin/caffeinate'), \
             mock.patch.object(power.subprocess, 'Popen', return_value=proc):
            with self.assertRaises(ValueError):
                with power.keep_awake():
                    raise ValueError('synthesis blew up')
        self.assertTrue(proc.terminated)

    def test_a_helper_that_ignores_terminate_is_killed(self):
        proc = FakeProc()
        proc.wait = mock.Mock(side_effect=subprocess.TimeoutExpired('caffeinate', 5))
        with mock.patch.object(power.platform, 'system', return_value='Darwin'), \
             mock.patch.object(power.shutil, 'which', return_value='/usr/bin/caffeinate'), \
             mock.patch.object(power.subprocess, 'Popen', return_value=proc):
            with power.keep_awake():
                pass
        self.assertTrue(proc.killed)

    def test_it_works_as_a_decorator(self):
        proc = FakeProc()
        with mock.patch.object(power.platform, 'system', return_value='Darwin'), \
             mock.patch.object(power.shutil, 'which', return_value='/usr/bin/caffeinate'), \
             mock.patch.object(power.subprocess, 'Popen', return_value=proc):
            @power.keep_awake()
            def synthesise():
                return 'done'

            # Twice: a @contextmanager generator is single-use, so the decorator must make a
            # fresh one per call or the second run of the GUI would raise instead of locking.
            self.assertEqual(synthesise(), 'done')
            self.assertEqual(synthesise(), 'done')


class MainIsWrappedTest(unittest.TestCase):
    def test_core_main_holds_the_lock_for_the_whole_run(self):
        import epubcast.core
        held = []
        with mock.patch.object(power, '_acquire', return_value=lambda: held.append('released')) as acquire:
            with self.assertRaises(Exception):
                # Fails on the missing file, long after the lock should have been taken.
                epubcast.core.main('no-such-book.epub', voice='af_sky', pick_manually=False, speed=1.0)
        acquire.assert_called_once()
        self.assertEqual(held, ['released'])


if __name__ == '__main__':
    unittest.main()
