# -*- coding: utf-8 -*-
"""Keep the machine awake for the length of a run.

Synthesising a full-length novel takes hours -- a 16-chapter Chinese book runs from
mid-afternoon into the night -- and for most of that time nobody is touching the keyboard.
An idle laptop suspends, the TTS loop and the final ffmpeg pass stop with it, and what
should have finished overnight is found half-done in the morning.

So for the duration of main() we hold the platform's "don't idle-sleep" assertion and drop
it as soon as the .m4b is written. Three things this deliberately does *not* do:

- keep the *display* awake. The screen may as well go dark; only the system suspending hurts.
- override a closed lid. Clamshell sleep is not something an assertion can veto, on any
  of these platforms.
- fail the run. A machine that will not hold the lock still converts books, it just needs
  to not be left alone, so an unavailable lock is a printed warning and nothing more.
"""
import os
import platform
import shutil
import subprocess
from contextlib import contextmanager

# SetThreadExecutionState flags (winbase.h). ES_CONTINUOUS makes the state stick until it is
# cleared, rather than applying to a single call.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake():
    """Hold off idle sleep inside the block. Usable as a decorator: @keep_awake()."""
    release = _acquire()
    try:
        yield
    finally:
        release()


def _acquire():
    """Take the wake lock and return a callable that releases it."""
    system = platform.system()
    try:
        if system == 'Darwin':
            return _acquire_with_helper(['caffeinate', '-i', '-m', '-s', '-w', str(os.getpid())])
        if system == 'Windows':
            return _acquire_windows()
        if system == 'Linux':
            return _acquire_with_helper([
                'systemd-inhibit', '--what=idle:sleep', '--who=epubcast',
                '--why=Converting an e-book to an audiobook', '--mode=block',
                'sleep', 'infinity'])
    except Exception as e:
        _warn(f'{e}')
        return _nothing
    _warn(f'no wake lock is implemented for {system}')
    return _nothing


def _acquire_with_helper(args):
    """Both Unix locks are held by a child process, and both die with us.

    caffeinate is passed our own pid to -w, and systemd-inhibit drops the inhibition when
    the process holding its file descriptor goes away, so a hard crash or a kill -9 cannot
    strand an assertion that outlives the run.
    """
    if shutil.which(args[0]) is None:
        _warn(f'{args[0]} not found')
        return _nothing
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _announce()

    def release():
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return release


def _acquire_windows():
    import ctypes
    set_state = ctypes.windll.kernel32.SetThreadExecutionState
    if not set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
        _warn('SetThreadExecutionState was refused')
        return _nothing
    _announce()
    # The state is per-thread and this runs on whichever thread called main(), which is the
    # same thread that will release it -- the GUI does its synthesis in a single CoreThread.
    return lambda: set_state(ES_CONTINUOUS)


def _nothing():
    pass


def _announce():
    print('Keeping the system awake until the audiobook is finished (the display may still sleep).')


def _warn(reason):
    print('\033[91m' + f'Warning: could not stop the system from sleeping ({reason}). '
          'A long run may be interrupted if the machine is left idle.' + '\033[0m')
