#!/usr/bin/env python
# encoding: utf-8
"""The ipcluster application."""
from __future__ import print_function

import errno
import logging
import os
import re
import signal
from subprocess import CalledProcessError
from subprocess import check_call
from subprocess import PIPE

import zmq
from IPython.core.application import BaseIPythonApplication
from IPython.core.profiledir import ProfileDir
from IPython.utils.sysinfo import num_cpus
from ipython_genutils.importstring import import_item
from ipython_genutils.py3compat import string_types
from traitlets import Any
from traitlets import Bool
from traitlets import CFloat
from traitlets import Dict
from traitlets import DottedObjectName
from traitlets import Integer
from traitlets import List
from traitlets import observe
from traitlets import Unicode
from traitlets.config.application import catch_config_error

from .._version import __version__
from .baseapp import base_aliases
from .baseapp import base_flags
from .baseapp import BaseParallelApplication
from .baseapp import PIDFileError
from .daemonize import daemonize


# -----------------------------------------------------------------------------
# Module level variables
# -----------------------------------------------------------------------------


_description = """Start an IPython cluster for parallel computing.

An IPython cluster consists of 1 controller and 1 or more engines.
This command automates the startup of these processes using a wide range of
startup methods (SSH, local processes, PBS, mpiexec, SGE, LSF, HTCondor,
Slurm, Windows HPC Server 2008). To start a cluster with 4 engines on your
local host simply do 'ipcluster start --n=4'. For more complex usage
you will typically do 'ipython profile create mycluster --parallel', then edit
configuration files, followed by 'ipcluster start --profile=mycluster --n=4'.
"""

_main_examples = """
ipcluster start --n=4 # start a 4 node cluster on localhost
ipcluster start -h    # show the help string for the start subcmd

ipcluster stop -h     # show the help string for the stop subcmd
ipcluster engines -h  # show the help string for the engines subcmd
"""

_start_examples = """
ipython profile create mycluster --parallel # create mycluster profile
ipcluster start --profile=mycluster --n=4   # start mycluster with 4 nodes

# args to `ipcluster start` after `--` are passed through to the controller
# see `ipcontroller -h` for options
ipcluster start -- --sqlitedb
"""

_stop_examples = """
ipcluster stop --profile=mycluster  # stop a running cluster by profile name
"""

_engines_examples = """
ipcluster engines --profile=mycluster --n=4  # start 4 engines only
"""


# Exit codes for ipcluster

# This will be the exit code if the ipcluster appears to be running because
# a .pid file exists
ALREADY_STARTED = 10


# This will be the exit code if ipcluster stop is run, but there is not .pid
# file to be found.
ALREADY_STOPPED = 11

# This will be the exit code if ipcluster engines is run, but there is not .pid
# file to be found.
NO_CLUSTER = 12


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------


def find_launcher_class(clsname, kind):
    """Return a launcher for a given clsname and kind.

    Parameters
    ==========
    clsname : str
        The full name of the launcher class, either with or without the
        module path, or an abbreviation (MPI, SSH, SGE, PBS, LSF, HTCondor
        Slurm, WindowsHPC).
    kind : str
        Either 'EngineSet' or 'Controller'.
    """
    if '.' not in clsname:
        # not a module, presume it's the raw name in apps.launcher
        if kind and kind not in clsname:
            # doesn't match necessary full class name, assume it's
            # just 'PBS' or 'MPI' etc prefix:
            clsname = clsname + kind + 'Launcher'
        clsname = 'ipyparallel.apps.launcher.' + clsname
    klass = import_item(clsname)
    return klass


# -----------------------------------------------------------------------------
# Main application
# -----------------------------------------------------------------------------

start_help = """Start an IPython cluster for parallel computing

Start an ipython cluster by its profile name or cluster
directory. Cluster directories contain configuration, log and
security related files and are named using the convention
'profile_<name>' and should be creating using the 'start'
subcommand of 'ipcluster'. If your cluster directory is in
the cwd or the ipython directory, you can simply refer to it
using its profile name, 'ipcluster start --n=4 --profile=<profile>`,
otherwise use the 'profile-dir' option.
"""
stop_help = """Stop a running IPython cluster

Stop a running ipython cluster by its profile name or cluster
directory. Cluster directories are named using the convention
'profile_<name>'. If your cluster directory is in
the cwd or the ipython directory, you can simply refer to it
using its profile name, 'ipcluster stop --profile=<profile>`, otherwise
use the '--profile-dir' option.
"""
engines_help = """Start engines connected to an existing IPython cluster

Start one or more engines to connect to an existing Cluster
by profile name or cluster directory.
Cluster directories contain configuration, log and
security related files and are named using the convention
'profile_<name>' and should be creating using the 'start'
subcommand of 'ipcluster'. If your cluster directory is in
the cwd or the ipython directory, you can simply refer to it
using its profile name, 'ipcluster engines --n=4 --profile=<profile>`,
otherwise use the 'profile-dir' option.
"""
stop_aliases = dict(
    signal='IPClusterStop.signal',
)
stop_aliases.update(base_aliases)


class IPClusterStop(BaseParallelApplication):
    name = u'ipcluster'
    description = stop_help
    examples = _stop_examples

    signal = Integer(
        signal.SIGINT, config=True, help="signal to use for stopping processes."
    )

    aliases = Dict(stop_aliases)

    def start(self):
        """Start the app for the stop subcommand."""
        try:
            pid = self.get_pid_from_file()
        except PIDFileError:
            self.log.critical(
                'Could not read pid file, cluster is probably not running.'
            )
            # Here I exit with a unusual exit status that other processes
            # can watch for to learn how I existed.
            self.remove_pid_file()
            self.exit(ALREADY_STOPPED)

        if not self.check_pid(pid):
            self.log.critical('Cluster [pid=%r] is not running.' % pid)
            self.remove_pid_file()
            # Here I exit with a unusual exit status that other processes
            # can watch for to learn how I existed.
            self.exit(ALREADY_STOPPED)

        elif os.name == 'posix':
            sig = self.signal
            self.log.info("Stopping cluster [pid=%r] with [signal=%r]" % (pid, sig))
            try:
                os.kill(pid, sig)
            except OSError:
                self.log.error(
                    "Stopping cluster failed, assuming already dead.", exc_info=True
                )
                self.remove_pid_file()
        elif os.name == 'nt':
            try:
                # kill the whole tree
                check_call(
                    ['taskkill', '-pid', str(pid), '-t', '-f'], stdout=PIPE, stderr=PIPE
                )
            except (CalledProcessError, OSError):
                self.log.error(
                    "Stopping cluster failed, assuming already dead.", exc_info=True
                )
            self.remove_pid_file()


engine_aliases = {}
engine_aliases.update(base_aliases)
engine_aliases.update(
    dict(
        n='IPClusterEngines.n',
        engines='IPClusterEngines.engine_launcher_class',
        daemonize='IPClusterEngines.daemonize',
    )
)
engine_flags = {}
engine_flags.update(base_flags)

engine_flags.update(
    dict(
        daemonize=(
            {'IPClusterEngines': {'daemonize': True}},
            """run the cluster into the background (not available on Windows)""",
        )
    )
)


class IPClusterEngines(BaseParallelApplication):

    name = u'ipcluster'
    description = engines_help
    examples = _engines_examples
    usage = None
    default_log_level = logging.INFO
    classes = List()

    def _classes_default(self):
        from ipyparallel.apps import launcher

        launchers = launcher.all_launchers
        eslaunchers = [l for l in launchers if 'EngineSet' in l.__name__]
        return [ProfileDir] + eslaunchers

    n = Integer(
        num_cpus(),
        config=True,
        help="""The number of engines to start. The default is to use one for each
        CPU on your machine""",
    )

    engine_launcher = Any(config=True, help="Deprecated, use engine_launcher_class")

    @observe('engine_launcher')
    def _engine_launcher_changed(self, change):
        if isinstance(change['new'], string_types):
            self.log.warn(
                "WARNING: %s.engine_launcher is deprecated as of 0.12,"
                " use engine_launcher_class" % self.__class__.__name__
            )
            self.engine_launcher_class = change['new']

    engine_launcher_class = DottedObjectName(
        'LocalEngineSetLauncher',
        config=True,
        help="""The class for launching a set of Engines. Change this value
        to use various batch systems to launch your engines, such as PBS,SGE,MPI,etc.
        Each launcher class has its own set of configuration options, for making sure
        it will work in your environment.

        You can also write your own launcher, and specify it's absolute import path,
        as in 'mymodule.launcher.FTLEnginesLauncher`.

        IPython's bundled examples include:

            Local : start engines locally as subprocesses [default]
            MPI : use mpiexec to launch engines in an MPI environment
            PBS : use PBS (qsub) to submit engines to a batch queue
            SGE : use SGE (qsub) to submit engines to a batch queue
            LSF : use LSF (bsub) to submit engines to a batch queue
            SSH : use SSH to start the controller
                        Note that SSH does *not* move the connection files
                        around, so you will likely have to do this manually
                        unless the machines are on a shared file system.
            HTCondor : use HTCondor to submit engines to a batch queue
            Slurm : use Slurm to submit engines to a batch queue
            WindowsHPC : use Windows HPC

        If you are using one of IPython's builtin launchers, you can specify just the
        prefix, e.g:

            c.IPClusterEngines.engine_launcher_class = 'SSH'

        or:

            ipcluster start --engines=MPI

        """,
    )
    daemonize = Bool(
        False,
        config=True,
        help="""Daemonize the ipcluster program. This implies --log-to-file.
        Not available on Windows.
        """,
    )

    @observe('daemonize')
    def _daemonize_changed(self, change):
        if change['new']:
            self.log_to_file = True

    early_shutdown = Integer(30, config=True, help="The timeout (in seconds)")
    _stopping = False

    aliases = Dict(engine_aliases)
    flags = Dict(engine_flags)

    @catch_config_error
    def initialize(self, argv=None):
        super(IPClusterEngines, self).initialize(argv)
        self.init_signal()
        self.init_launchers()

    def init_launchers(self):
        self.engine_launcher = self.build_launcher(
            self.engine_launcher_class, 'EngineSet'
        )

    def init_signal(self):
        # Setup signals
        signal.signal(signal.SIGINT, self.sigint_handler)

    def build_launcher(self, clsname, kind=None):
        """import and instantiate a Launcher based on importstring"""
        try:
            klass = find_launcher_class(clsname, kind)
        except (ImportError, KeyError):
            self.log.fatal("Could not import launcher class: %r" % clsname)
            self.exit(1)

        launcher = klass(
            work_dir=u'.',
            parent=self,
            log=self.log,
            profile_dir=self.profile_dir.location,
            cluster_id=self.cluster_id,
        )
        return launcher

    def engines_started_ok(self):
        if self.engine_launcher.running:
            self.log.info("Engines appear to have started successfully")
            self.early_shutdown = 0

    def start_engines(self):
        # Some EngineSetLaunchers ignore `n` and use their own engine count, such as SSH:
        n = getattr(self.engine_launcher, 'engine_count', self.n)
        self.log.info("Starting %s Engines with %s", n, self.engine_launcher_class)
        try:
            self.engine_launcher.start(self.n)
        except:
            self.log.exception("Engine start failed")
            raise
        self.engine_launcher.on_stop(self.engines_stopped_early)
        if self.early_shutdown:
            self.loop.add_timeout(
                self.loop.time() + self.early_shutdown, self.engines_started_ok
            )

    def engines_stopped_early(self, r):
        if self.early_shutdown and not self._stopping:
            self.log.error(
                """
            Engines shutdown early, they probably failed to connect.

            Check the engine log files for output.

            If your controller and engines are not on the same machine, you probably
            have to instruct the controller to listen on an interface other than localhost.

            You can set this by adding "--ip='*'" to your ControllerLauncher.controller_args.

            Be sure to read our security docs before instructing your controller to listen on
            a public interface.
            """
            )
            self.stop_launchers()

        return self.engines_stopped(r)

    def engines_stopped(self, r):
        return self.loop.stop()

    def stop_engines(self):
        if self.engine_launcher.running:
            self.log.info("Stopping Engines...")
            d = self.engine_launcher.stop()
            return d
        else:
            return None

    def stop_launchers(self, r=None):
        if not self._stopping:
            self._stopping = True
            self.log.error("IPython cluster: stopping")
            self.stop_engines()
            # Wait a few seconds to let things shut down.
            self.loop.add_timeout(self.loop.time() + 3, self.loop.stop)

    def sigint_handler(self, signum, frame):
        self.log.debug("SIGINT received, stopping launchers...")
        self.stop_launchers()

    def start_logging(self):
        # Remove old log files of the controller and engine
        if self.clean_logs:
            log_dir = self.profile_dir.log_dir
            for f in os.listdir(log_dir):
                if re.match(r'ip(engine|controller)-.+\.(log|err|out)', f):
                    os.remove(os.path.join(log_dir, f))

    def start(self):
        """Start the app for the engines subcommand."""
        self.log.info("IPython cluster: started")
        # First see if the cluster is already running

        # Now log and daemonize
        self.log.info('Starting engines with [daemon=%r]' % self.daemonize)
        # TODO: Get daemonize working on Windows or as a Windows Server.
        if self.daemonize:
            if os.name == 'posix':
                daemonize()

        self.loop.add_callback(self.start_engines)
        # Now write the new pid file AFTER our new forked pid is active.
        # self.write_pid_file()
        try:
            self.loop.start()
        except KeyboardInterrupt:
            pass
        except zmq.ZMQError as e:
            if e.errno == errno.EINTR:
                pass
            else:
                raise


start_aliases = {}
start_aliases.update(engine_aliases)
start_aliases.update(
    dict(
        delay='IPClusterStart.delay',
        controller='IPClusterStart.controller_launcher_class',
        ip='IPClusterStart.controller_ip',
        location='IPClusterStart.controller_location',
    )
)
start_aliases['clean-logs'] = 'IPClusterStart.clean_logs'


class IPClusterStart(IPClusterEngines):

    name = u'ipcluster'
    description = start_help
    examples = _start_examples
    default_log_level = logging.INFO
    auto_create = Bool(
        True, config=True, help="whether to create the profile_dir if it doesn't exist"
    )
    classes = List()

    def _classes_default(
        self,
    ):
        from ipyparallel.apps import launcher

        return [ProfileDir] + [IPClusterEngines] + launcher.all_launchers

    clean_logs = Bool(
        True, config=True, help="whether to cleanup old logs before starting"
    )

    delay = CFloat(
        1.0,
        config=True,
        help="delay (in s) between starting the controller and the engines",
    )

    controller_ip = Unicode(config=True, help="Set the IP address of the controller.")
    controller_location = Unicode(
        config=True,
        help="""Set the location (hostname or ip) of the controller.
        
        This is used by engines and clients to locate the controller
        when the controller listens on all interfaces
        """,
    )
    controller_launcher = Any(
        config=True, help="Deprecated, use controller_launcher_class"
    )

    @observe('controller_launcher')
    def _controller_launcher_changed(self, change):
        if isinstance(change['new'], string_types):
            # old 0.11-style config
            self.log.warn(
                "WARNING: %s.controller_launcher is deprecated as of 0.12,"
                " use controller_launcher_class" % self.__class__.__name__
            )
            self.controller_launcher_class = change['new']

    controller_launcher_class = DottedObjectName(
        'LocalControllerLauncher',
        config=True,
        help="""The class for launching a Controller. Change this value if you want
        your controller to also be launched by a batch system, such as PBS,SGE,MPI,etc.

        Each launcher class has its own set of configuration options, for making sure
        it will work in your environment.

        Note that using a batch launcher for the controller *does not* put it
        in the same batch job as the engines, so they will still start separately.

        IPython's bundled examples include:

            Local : start engines locally as subprocesses
            MPI : use mpiexec to launch the controller in an MPI universe
            PBS : use PBS (qsub) to submit the controller to a batch queue
            SGE : use SGE (qsub) to submit the controller to a batch queue
            LSF : use LSF (bsub) to submit the controller to a batch queue
            HTCondor : use HTCondor to submit the controller to a batch queue
            Slurm : use Slurm to submit engines to a batch queue
            SSH : use SSH to start the controller
            WindowsHPC : use Windows HPC

        If you are using one of IPython's builtin launchers, you can specify just the
        prefix, e.g:

            c.IPClusterStart.controller_launcher_class = 'SSH'

        or:

            ipcluster start --controller=MPI

        """,
    )
    reset = Bool(
        False, config=True, help="Whether to reset config files as part of '--create'."
    )

    # flags = Dict(flags)
    aliases = Dict(start_aliases)

    def init_launchers(self):
        self.controller_launcher = self.build_launcher(
            self.controller_launcher_class, 'Controller'
        )

        controller_args = getattr(self.controller_launcher, 'controller_args', None)
        if controller_args is None:

            def add_args(args):
                # only some Launchers support modifying controller args
                self.log.warning(
                    "Not adding controller args %s. "
                    "controller_args passthrough is not supported by %s",
                    args,
                    self.controller_launcher.__class__.__name__,
                )

        else:
            add_args = controller_args.extend
        if self.controller_ip:
            add_args(['--ip=%s' % self.controller_ip])
        if self.controller_location:
            add_args(['--location=%s' % self.controller_location])
        if self.extra_args:
            add_args(self.extra_args)
        self.engine_launcher = self.build_launcher(
            self.engine_launcher_class, 'EngineSet'
        )

    def engines_stopped(self, r):
        """prevent parent.engines_stopped from stopping everything on engine shutdown"""
        pass

    def start_controller(self):
        self.log.info("Starting Controller with %s", self.controller_launcher_class)
        self.controller_launcher.on_stop(self.stop_launchers)
        try:
            self.controller_launcher.start()
        except:
            self.log.exception("Controller start failed")
            raise

    def stop_controller(self):
        # self.log.info("In stop_controller")
        if self.controller_launcher and self.controller_launcher.running:
            return self.controller_launcher.stop()

    def stop_launchers(self, r=None):
        if not self._stopping:
            self.stop_controller()
            super(IPClusterStart, self).stop_launchers()

    def start(self):
        """Start the app for the start subcommand."""
        # First see if the cluster is already running
        try:
            pid = self.get_pid_from_file()
        except PIDFileError:
            pass
        else:
            if self.check_pid(pid):
                self.log.critical(
                    'Cluster is already running with [pid=%s]. '
                    'use "ipcluster stop" to stop the cluster.' % pid
                )
                # Here I exit with a unusual exit status that other processes
                # can watch for to learn how I existed.
                self.exit(ALREADY_STARTED)
            else:
                self.remove_pid_file()

        # Now log and daemonize
        self.log.info('Starting ipcluster with [daemon=%r]' % self.daemonize)
        # TODO: Get daemonize working on Windows or as a Windows Server.
        if self.daemonize:
            if os.name == 'posix':
                daemonize()

        def start():
            self.start_controller()
            self.loop.add_timeout(self.loop.time() + self.delay, self.start_engines)

        self.loop.add_callback(start)
        # Now write the new pid file AFTER our new forked pid is active.
        self.write_pid_file()
        try:
            self.loop.start()
        except KeyboardInterrupt:
            pass
        except zmq.ZMQError as e:
            if e.errno == errno.EINTR:
                pass
            else:
                raise
        finally:
            self.remove_pid_file()


class IPClusterNBExtension(BaseIPythonApplication):
    """Enable/disable ipcluster tab extension in Jupyter notebook"""

    name = 'ipcluster-nbextension'

    description = """Enable/disable IPython clusters tab in Jupyter notebook

    for Jupyter Notebook >= 4.2, you can use the new nbextension API:

    jupyter serverextension enable --py ipyparallel
    jupyter nbextension install --py ipyparallel
    jupyter nbextension enable --py ipyparallel
    """

    examples = """
    ipcluster nbextension enable
    ipcluster nbextension disable
    """
    version = __version__
    user = Bool(False, help="Apply the operation only for the given user").tag(
        config=True
    )
    flags = Dict(
        {
            'user': (
                {'IPClusterNBExtension': {'user': True}},
                'Apply the operation only for the given user',
            )
        }
    )

    def start(self):
        from ipyparallel.nbextension.install import install_extensions

        if len(self.extra_args) != 1:
            self.exit("Must specify 'enable' or 'disable'")
        action = self.extra_args[0].lower()
        if action == 'enable':
            print("Enabling IPython clusters tab")
            install_extensions(enable=True, user=self.user)
        elif action == 'disable':
            print("Disabling IPython clusters tab")
            install_extensions(enable=False, user=self.user)
        else:
            self.exit("Must specify 'enable' or 'disable', not '%s'" % action)


base = 'ipyparallel.apps.ipclusterapp.IPCluster'


class IPClusterApp(BaseIPythonApplication):
    name = u'ipcluster'
    description = _description
    examples = _main_examples
    version = __version__

    subcommands = {
        'start': (base + 'Start', start_help),
        'stop': (base + 'Stop', stop_help),
        'engines': (base + 'Engines', engines_help),
        'nbextension': (base + 'NBExtension', IPClusterNBExtension.description),
    }

    # no aliases or flags for parent App
    aliases = Dict()
    flags = Dict()

    def start(self):
        if self.subapp is None:
            keys = ', '.join("'{}'".format(key) for key in self.subcommands.keys())
            print("No subcommand specified. Must specify one of: %s" % keys)
            print()
            self.print_description()
            self.print_subcommands()
            self.exit(1)
        else:
            return self.subapp.start()


launch_new_instance = IPClusterApp.launch_instance

if __name__ == '__main__':
    launch_new_instance()
