#!/usr/bin/env python
# encoding: utf-8
"""
The IPython controller application.
"""
# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
from __future__ import with_statement

import json
import os
import socket
import stat
import sys
from multiprocessing import Process
from signal import SIGABRT
from signal import SIGINT
from signal import signal
from signal import SIGTERM

import zmq
from IPython.core.profiledir import ProfileDir
from ipython_genutils.importstring import import_item
from jupyter_client.session import Session
from jupyter_client.session import session_aliases
from jupyter_client.session import session_flags
from traitlets import Bool
from traitlets import Dict
from traitlets import List
from traitlets import observe
from traitlets import TraitError
from traitlets import Unicode
from zmq.devices import ProcessMonitoredQueue
from zmq.log.handlers import PUBHandler

from ipyparallel.apps.baseapp import base_aliases
from ipyparallel.apps.baseapp import base_flags
from ipyparallel.apps.baseapp import BaseParallelApplication
from ipyparallel.apps.baseapp import catch_config_error
from ipyparallel.controller.broadcast_scheduler import BroadcastScheduler
from ipyparallel.controller.broadcast_scheduler import launch_broadcast_scheduler
from ipyparallel.controller.dictdb import DictDB
from ipyparallel.controller.heartmonitor import HeartMonitor
from ipyparallel.controller.hub import HubFactory
from ipyparallel.controller.scheduler import launch_scheduler
from ipyparallel.controller.task_scheduler import TaskScheduler
from ipyparallel.util import disambiguate_url

# conditional import of SQLiteDB / MongoDB backend class
real_dbs = []

try:
    from ipyparallel.controller.sqlitedb import SQLiteDB
except ImportError:
    pass
else:
    real_dbs.append(SQLiteDB)

try:
    from ipyparallel.controller.mongodb import MongoDB
except ImportError:
    pass
else:
    real_dbs.append(MongoDB)


# -----------------------------------------------------------------------------
# Module level variables
# -----------------------------------------------------------------------------


_description = """Start the IPython controller for parallel computing.

The IPython controller provides a gateway between the IPython engines and
clients. The controller needs to be started before the engines and can be
configured using command line options or using a cluster directory. Cluster
directories contain config, log and security files and are usually located in
your ipython directory and named as "profile_name". See the `profile`
and `profile-dir` options for details.
"""

_examples = """
ipcontroller --ip=192.168.0.1 --port=1000  # listen on ip, port for engines
ipcontroller --scheme=pure  # use the pure zeromq scheduler
"""


# -----------------------------------------------------------------------------
# The main application
# -----------------------------------------------------------------------------
flags = {}
flags.update(base_flags)
flags.update(
    {
        'usethreads': (
            {'IPControllerApp': {'use_threads': True}},
            'Use threads instead of processes for the schedulers',
        ),
        'sqlitedb': (
            {'HubFactory': {'db_class': 'ipyparallel.controller.sqlitedb.SQLiteDB'}},
            'use the SQLiteDB backend',
        ),
        'mongodb': (
            {'HubFactory': {'db_class': 'ipyparallel.controller.mongodb.MongoDB'}},
            'use the MongoDB backend',
        ),
        'dictdb': (
            {'HubFactory': {'db_class': 'ipyparallel.controller.dictdb.DictDB'}},
            'use the in-memory DictDB backend',
        ),
        'nodb': (
            {'HubFactory': {'db_class': 'ipyparallel.controller.dictdb.NoDB'}},
            """use dummy DB backend, which doesn't store any information.
                    
                    This is the default as of IPython 0.13.
                    
                    To enable delayed or repeated retrieval of results from the Hub,
                    select one of the true db backends.
                    """,
        ),
        'reuse': (
            {'IPControllerApp': {'reuse_files': True}},
            'reuse existing json connection files',
        ),
        'restore': (
            {'IPControllerApp': {'restore_engines': True, 'reuse_files': True}},
            'Attempt to restore engines from a JSON file.  '
            'For use when resuming a crashed controller',
        ),
    }
)

flags.update(session_flags)

aliases = dict(
    ssh='IPControllerApp.ssh_server',
    enginessh='IPControllerApp.engine_ssh_server',
    location='IPControllerApp.location',
    url='HubFactory.url',
    ip='HubFactory.ip',
    transport='HubFactory.transport',
    port='HubFactory.regport',
    ping='HeartMonitor.period',
    scheme='TaskScheduler.scheme_name',
    hwm='TaskScheduler.hwm',
)
aliases.update(base_aliases)
aliases.update(session_aliases)


class IPControllerApp(BaseParallelApplication):

    name = u'ipcontroller'
    description = _description
    examples = _examples
    classes = [
        ProfileDir,
        Session,
        HubFactory,
        TaskScheduler,
        HeartMonitor,
        DictDB,
    ] + real_dbs

    # change default to True
    auto_create = Bool(
        True, config=True, help="""Whether to create profile dir if it doesn't exist."""
    )

    reuse_files = Bool(
        False,
        config=True,
        help="""Whether to reuse existing json connection files.
        If False, connection files will be removed on a clean exit.
        """,
    )
    restore_engines = Bool(
        False,
        config=True,
        help="""Reload engine state from JSON file
        """,
    )
    ssh_server = Unicode(
        u'',
        config=True,
        help="""ssh url for clients to use when connecting to the Controller
        processes. It should be of the form: [user@]server[:port]. The
        Controller's listening addresses must be accessible from the ssh server""",
    )
    engine_ssh_server = Unicode(
        u'',
        config=True,
        help="""ssh url for engines to use when connecting to the Controller
        processes. It should be of the form: [user@]server[:port]. The
        Controller's listening addresses must be accessible from the ssh server""",
    )
    location = Unicode(
        socket.gethostname(),
        config=True,
        help="""The external IP or domain name of the Controller, used for disambiguating
        engine and client connections.""",
    )
    import_statements = List(
        [],
        config=True,
        help="import statements to be run at startup.  Necessary in some environments",
    )

    use_threads = Bool(
        False, config=True, help='Use threads instead of processes for the schedulers'
    )

    engine_json_file = Unicode(
        'ipcontroller-engine.json',
        config=True,
        help="JSON filename where engine connection info will be stored.",
    )
    client_json_file = Unicode(
        'ipcontroller-client.json',
        config=True,
        help="JSON filename where client connection info will be stored.",
    )

    @observe('cluster_id')
    def _cluster_id_changed(self, change):
        super(IPControllerApp, self)._cluster_id_changed(change)
        self.engine_json_file = "%s-engine.json" % self.name
        self.client_json_file = "%s-client.json" % self.name

    # internal
    children = List()
    mq_class = Unicode('zmq.devices.ProcessMonitoredQueue')

    @observe('use_threads')
    def _use_threads_changed(self, change):
        self.mq_class = 'zmq.devices.{}MonitoredQueue'.format(
            'Thread' if change['new'] else 'Process'
        )

    write_connection_files = Bool(
        True,
        help="""Whether to write connection files to disk.
        True in all cases other than runs with `reuse_files=True` *after the first*
        """,
    )

    aliases = Dict(aliases)
    flags = Dict(flags)

    def save_connection_dict(self, fname, cdict):
        """save a connection dict to json file."""
        fname = os.path.join(self.profile_dir.security_dir, fname)
        self.log.info("writing connection info to %s", fname)
        with open(fname, 'w') as f:
            f.write(json.dumps(cdict, indent=2))
        os.chmod(fname, stat.S_IRUSR | stat.S_IWUSR)

    def load_config_from_json(self):
        """load config from existing json connector files."""
        c = self.config
        self.log.debug("loading config from JSON")

        # load engine config

        fname = os.path.join(self.profile_dir.security_dir, self.engine_json_file)
        self.log.info("loading connection info from %s", fname)
        with open(fname) as f:
            ecfg = json.loads(f.read())

        # json gives unicode, Session.key wants bytes
        c.Session.key = ecfg['key'].encode('ascii')

        xport, ip = ecfg['interface'].split('://')

        c.HubFactory.engine_ip = ip
        c.HubFactory.engine_transport = xport

        self.location = ecfg['location']
        if not self.engine_ssh_server:
            self.engine_ssh_server = ecfg['ssh']

        # load client config

        fname = os.path.join(self.profile_dir.security_dir, self.client_json_file)
        self.log.info("loading connection info from %s", fname)
        with open(fname) as f:
            ccfg = json.loads(f.read())

        for key in ('key', 'registration', 'pack', 'unpack', 'signature_scheme'):
            assert ccfg[key] == ecfg[key], (
                "mismatch between engine and client info: %r" % key
            )

        xport, ip = ccfg['interface'].split('://')

        c.HubFactory.client_transport = xport
        c.HubFactory.client_ip = ip
        if not self.ssh_server:
            self.ssh_server = ccfg['ssh']

        # load port config:
        c.HubFactory.regport = ecfg['registration']
        c.HubFactory.hb = (ecfg['hb_ping'], ecfg['hb_pong'])
        c.HubFactory.control = (ccfg['control'], ecfg['control'])
        c.HubFactory.mux = (ccfg['mux'], ecfg['mux'])
        c.HubFactory.task = (ccfg['task'], ecfg['task'])
        c.HubFactory.iopub = (ccfg['iopub'], ecfg['iopub'])
        c.HubFactory.notifier_port = ccfg['notification']

    def cleanup_connection_files(self):
        if self.reuse_files:
            self.log.debug("leaving JSON connection files for reuse")
            return
        self.log.debug("cleaning up JSON connection files")
        for f in (self.client_json_file, self.engine_json_file):
            f = os.path.join(self.profile_dir.security_dir, f)
            try:
                os.remove(f)
            except Exception as e:
                self.log.error("Failed to cleanup connection file: %s", e)
            else:
                self.log.debug(u"removed %s", f)

    def load_secondary_config(self):
        """secondary config, loading from JSON and setting defaults"""
        if self.reuse_files:
            try:
                self.load_config_from_json()
            except (AssertionError, IOError) as e:
                self.log.error("Could not load config from JSON: %s" % e)
            else:
                # successfully loaded config from JSON, and reuse=True
                # no need to wite back the same file
                self.write_connection_files = False

        self.log.debug("Config changed")
        self.log.debug(repr(self.config))

    def init_hub(self):
        c = self.config

        self.do_import_statements()

        try:
            self.factory = HubFactory(
                config=c,
                log=self.log,
            )
            # self.start_logging()
            self.factory.init_hub()
        except TraitError:
            raise
        except Exception:
            self.log.error("Couldn't construct the Controller", exc_info=True)
            self.exit(1)

        if self.write_connection_files:
            # save to new json config files
            f = self.factory
            base = {
                'key': f.session.key.decode('ascii'),
                'location': self.location,
                'pack': f.session.packer,
                'unpack': f.session.unpacker,
                'signature_scheme': f.session.signature_scheme,
            }

            cdict = {'ssh': self.ssh_server}
            cdict.update(f.client_info)
            cdict.update(base)
            self.save_connection_dict(self.client_json_file, cdict)

            edict = {'ssh': self.engine_ssh_server}
            edict.update(f.engine_info)
            edict.update(base)
            self.save_connection_dict(self.engine_json_file, edict)

        fname = "engines%s.json" % self.cluster_id
        self.factory.hub.engine_state_file = os.path.join(
            self.profile_dir.log_dir, fname
        )
        if self.restore_engines:
            self.factory.hub._load_engine_state()
        # load key into config so other sessions in this process (TaskScheduler)
        # have the same value
        self.config.Session.key = self.factory.session.key

    def launch_python_scheduler(self, scheduler_args, children):
        if 'Process' in self.mq_class:
            # run the Python scheduler in a Process
            q = Process(target=launch_scheduler, kwargs=scheduler_args)
            q.daemon = True
            children.append(q)
        else:
            # single-threaded Controller
            scheduler_args['in_thread'] = True
            launch_scheduler(**scheduler_args)

    def init_schedulers(self):
        children = self.children
        mq = import_item(str(self.mq_class))

        f = self.factory
        ident = f.session.bsession
        # disambiguate url, in case of *
        monitor_url = disambiguate_url(f.monitor_url)
        # maybe_inproc = 'inproc://monitor' if self.use_threads else monitor_url
        # IOPub relay (in a Process)
        q = mq(zmq.PUB, zmq.SUB, zmq.PUB, b'N/A', b'iopub')
        q.bind_in(f.client_url('iopub'))
        q.setsockopt_in(zmq.IDENTITY, ident + b"_iopub")
        q.bind_out(f.engine_url('iopub'))
        q.setsockopt_out(zmq.SUBSCRIBE, b'')
        q.connect_mon(monitor_url)
        q.daemon = True
        children.append(q)

        # Multiplexer Queue (in a Process)
        q = mq(zmq.ROUTER, zmq.ROUTER, zmq.PUB, b'in', b'out')

        q.bind_in(f.client_url('mux'))
        q.setsockopt_in(zmq.IDENTITY, b'mux_in')
        q.bind_out(f.engine_url('mux'))
        q.setsockopt_out(zmq.IDENTITY, b'mux_out')
        q.connect_mon(monitor_url)
        q.daemon = True
        children.append(q)

        # Control Queue (in a Process)
        q = mq(zmq.ROUTER, zmq.ROUTER, zmq.PUB, b'incontrol', b'outcontrol')
        q.bind_in(f.client_url('control'))
        q.setsockopt_in(zmq.IDENTITY, b'control_in')
        q.bind_out(f.engine_url('control'))
        q.setsockopt_out(zmq.IDENTITY, b'control_out')
        q.connect_mon(monitor_url)
        q.daemon = True
        children.append(q)
        if 'TaskScheduler.scheme_name' in self.config:
            scheme = self.config.TaskScheduler.scheme_name
        else:
            scheme = TaskScheduler.scheme_name.default_value
        # Task Queue (in a Process)
        if scheme == 'pure':
            self.log.warn("task::using pure DEALER Task scheduler")
            q = mq(zmq.ROUTER, zmq.DEALER, zmq.PUB, b'intask', b'outtask')
            # q.setsockopt_out(zmq.HWM, hub.hwm)
            q.bind_in(f.client_url('task'))
            q.setsockopt_in(zmq.IDENTITY, b'task_in')
            q.bind_out(f.engine_url('task'))
            q.setsockopt_out(zmq.IDENTITY, b'task_out')
            q.connect_mon(monitor_url)
            q.daemon = True
            children.append(q)
        elif scheme == 'none':
            self.log.warn("task::using no Task scheduler")

        else:
            self.log.info("task::using Python %s Task scheduler" % scheme)
            self.launch_python_scheduler(
                self.get_python_scheduler_args('task', f, TaskScheduler, monitor_url),
                children,
            )

        self.launch_broadcast_schedulers(f, monitor_url, children)

        # set unlimited HWM for all relay devices
        if hasattr(zmq, 'SNDHWM'):
            q = children[0]
            q.setsockopt_in(zmq.RCVHWM, 0)
            q.setsockopt_out(zmq.SNDHWM, 0)

            for q in children[1:]:
                if not hasattr(q, 'setsockopt_in'):
                    continue
                q.setsockopt_in(zmq.SNDHWM, 0)
                q.setsockopt_in(zmq.RCVHWM, 0)
                q.setsockopt_out(zmq.SNDHWM, 0)
                q.setsockopt_out(zmq.RCVHWM, 0)
                q.setsockopt_mon(zmq.SNDHWM, 0)

    def terminate_children(self):
        child_procs = []
        for child in self.children:
            if isinstance(child, ProcessMonitoredQueue):
                child_procs.append(child.launcher)
            elif isinstance(child, Process):
                child_procs.append(child)
        if child_procs:
            self.log.critical("terminating children...")
            for child in child_procs:
                try:
                    child.terminate()
                except OSError:
                    # already dead
                    pass

    def handle_signal(self, sig, frame):
        self.log.critical("Received signal %i, shutting down", sig)
        self.terminate_children()
        self.loop.stop()

    def init_signal(self):
        for sig in (SIGINT, SIGABRT, SIGTERM):
            signal(sig, self.handle_signal)

    def do_import_statements(self):
        statements = self.import_statements
        for s in statements:
            try:
                self.log.msg("Executing statement: '%s'" % s)
                exec(s, globals(), locals())
            except:
                self.log.msg("Error running statement: %s" % s)

    def forward_logging(self):
        if self.log_url:
            self.log.info("Forwarding logging to %s" % self.log_url)
            context = zmq.Context.instance()
            lsock = context.socket(zmq.PUB)
            lsock.connect(self.log_url)
            handler = PUBHandler(lsock)
            handler.root_topic = 'controller'
            handler.setLevel(self.log_level)
            self.log.addHandler(handler)

    @catch_config_error
    def initialize(self, argv=None):
        super(IPControllerApp, self).initialize(argv)
        self.forward_logging()
        self.load_secondary_config()
        self.init_hub()
        self.init_schedulers()

    def start(self):
        # Start the subprocesses:
        self.factory.start()
        # children must be started before signals are setup,
        # otherwise signal-handling will fire multiple times
        for child in self.children:
            child.start()
        self.init_signal()

        self.write_pid_file(overwrite=True)

        try:
            self.factory.loop.start()
        except KeyboardInterrupt:
            self.log.critical("Interrupted, Exiting...\n")
        finally:
            self.cleanup_connection_files()

    def get_python_scheduler_args(
        self, scheduler_name, factory, scheduler_class, monitor_url, identity=None
    ):
        return {
            'scheduler_class': scheduler_class,
            'in_addr': factory.client_url(scheduler_name),
            'out_addr': factory.engine_url(scheduler_name),
            'mon_addr': monitor_url,
            'not_addr': disambiguate_url(factory.client_url('notification')),
            'reg_addr': disambiguate_url(factory.client_url('registration')),
            'identity': identity if identity else bytes(scheduler_name, 'utf8'),
            'logname': 'scheduler',
            'loglevel': self.log_level,
            'log_url': self.log_url,
            'config': dict(self.config),
        }

    def launch_broadcast_schedulers(self, factory, monitor_url, children):
        def launch_in_thread_or_process(scheduler_args):

            if 'Process' in self.mq_class:
                # run the Python scheduler in a Process
                q = Process(target=launch_broadcast_scheduler, kwargs=scheduler_args)
                q.daemon = True
                children.append(q)
            else:
                # single-threaded Controller
                scheduler_args['in_thread'] = True
                launch_broadcast_scheduler(**scheduler_args)

        def recursively_start_schedulers(identity, depth):
            outgoing_id1 = identity * 2 + 1
            outgoing_id2 = outgoing_id1 + 1
            is_leaf = depth == self.factory.broadcast_scheduler_depth

            scheduler_args = dict(
                in_addr=factory.client_url(BroadcastScheduler.port_name, identity),
                mon_addr=monitor_url,
                not_addr=disambiguate_url(factory.client_url('notification')),
                reg_addr=disambiguate_url(factory.client_url('registration')),
                identity=identity,
                config=dict(self.config),
                loglevel=self.log_level,
                log_url=self.log_url,
                outgoing_ids=[outgoing_id1, outgoing_id2],
                depth=depth,
                is_leaf=is_leaf,
            )
            if is_leaf:
                scheduler_args.update(
                    out_addrs=[
                        factory.engine_url(
                            BroadcastScheduler.port_name,
                            identity - factory.number_of_non_leaf_schedulers,
                        )
                    ],
                )
            else:
                scheduler_args.update(
                    out_addrs=[
                        factory.client_url(BroadcastScheduler.port_name, outgoing_id1),
                        factory.client_url(BroadcastScheduler.port_name, outgoing_id2),
                    ]
                )
            launch_in_thread_or_process(scheduler_args)
            if not is_leaf:
                recursively_start_schedulers(outgoing_id1, depth + 1)
                recursively_start_schedulers(outgoing_id2, depth + 1)

        recursively_start_schedulers(0, 0)


def launch_new_instance(*args, **kwargs):
    """Create and run the IPython controller"""
    if sys.platform == 'win32':
        # make sure we don't get called from a multiprocessing subprocess
        # this can result in infinite Controllers being started on Windows
        # which doesn't have a proper fork, so multiprocessing is wonky

        # this only comes up when IPython has been installed using vanilla
        # setuptools, and *not* distribute.
        import multiprocessing

        p = multiprocessing.current_process()
        # the main process has name 'MainProcess'
        # subprocesses will have names like 'Process-1'
        if p.name != 'MainProcess':
            # we are a subprocess, don't start another Controller!
            return
    return IPControllerApp.launch_instance(*args, **kwargs)


if __name__ == '__main__':
    launch_new_instance()
