#!/usr/bin/env python
# encoding: utf-8
"""
The IPython engine application
"""
# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
import json
import os
import sys
import time

import zmq
from ipykernel.ipkernel import IPythonKernel as Kernel
from ipykernel.kernelapp import IPKernelApp
from ipykernel.zmqshell import ZMQInteractiveShell
from IPython.core.profiledir import ProfileDir
from ipython_genutils.py3compat import cast_bytes
from jupyter_client.session import Session
from jupyter_client.session import session_aliases
from jupyter_client.session import session_flags
from traitlets import Dict
from traitlets import Float
from traitlets import Instance
from traitlets import List
from traitlets import observe
from traitlets import Unicode

from ipyparallel.apps.baseapp import base_aliases
from ipyparallel.apps.baseapp import base_flags
from ipyparallel.apps.baseapp import BaseParallelApplication
from ipyparallel.apps.baseapp import catch_config_error
from ipyparallel.engine.engine import EngineFactory
from ipyparallel.engine.log import EnginePUBHandler
from ipyparallel.util import disambiguate_ip_address

# -----------------------------------------------------------------------------
# Module level variables
# -----------------------------------------------------------------------------

_description = """Start an IPython engine for parallel computing.

IPython engines run in parallel and perform computations on behalf of a client
and controller. A controller needs to be started before the engines. The
engine can be configured using command line options or using a cluster
directory. Cluster directories contain config, log and security files and are
usually located in your ipython directory and named as "profile_name".
See the `profile` and `profile-dir` options for details.
"""

_examples = """
ipengine --ip=192.168.0.1 --port=1000     # connect to hub at ip and port
ipengine --log-to-file --log-level=DEBUG  # log to a file with DEBUG verbosity
"""


# -----------------------------------------------------------------------------
# Main application
# -----------------------------------------------------------------------------
aliases = dict(
    file='IPEngineApp.url_file',
    c='IPEngineApp.startup_command',
    s='IPEngineApp.startup_script',
    url='EngineFactory.url',
    ssh='EngineFactory.sshserver',
    sshkey='EngineFactory.sshkey',
    ip='EngineFactory.ip',
    transport='EngineFactory.transport',
    port='EngineFactory.regport',
    location='EngineFactory.location',
    timeout='EngineFactory.timeout',
)
aliases.update(base_aliases)
aliases.update(session_aliases)
flags = {
    'mpi': (
        {
            'EngineFactory': {'use_mpi': True},
        },
        "enable MPI integration",
    ),
}
flags.update(base_flags)
flags.update(session_flags)


class IPEngineApp(BaseParallelApplication):

    name = 'ipengine'
    description = _description
    examples = _examples
    classes = List([ZMQInteractiveShell, ProfileDir, Session, EngineFactory, Kernel])

    startup_script = Unicode(
        u'', config=True, help='specify a script to be run at startup'
    )
    startup_command = Unicode(
        '', config=True, help='specify a command to be run at startup'
    )

    url_file = Unicode(
        u'',
        config=True,
        help="""The full location of the file containing the connection information for
        the controller. If this is not given, the file must be in the
        security directory of the cluster directory.  This location is
        resolved using the `profile` or `profile_dir` options.""",
    )
    wait_for_url_file = Float(
        10,
        config=True,
        help="""The maximum number of seconds to wait for url_file to exist.
        This is useful for batch-systems and shared-filesystems where the
        controller and engine are started at the same time and it
        may take a moment for the controller to write the connector files.""",
    )

    url_file_name = Unicode(u'ipcontroller-engine.json', config=True)

    @observe('cluster_id')
    def _cluster_id_changed(self, change):
        if change['new']:
            base = 'ipcontroller-{}'.format(change['new'])
        else:
            base = 'ipcontroller'
        self.url_file_name = "%s-engine.json" % base

    log_url = Unicode(
        '',
        config=True,
        help="""The URL for the iploggerapp instance, for forwarding
        logging to a central location.""",
    )

    # an IPKernelApp instance, used to setup listening for shell frontends
    kernel_app = Instance(IPKernelApp, allow_none=True)

    aliases = Dict(aliases)
    flags = Dict(flags)

    @property
    def kernel(self):
        """allow access to the Kernel object, so I look like IPKernelApp"""
        return self.engine.kernel

    def find_url_file(self):
        """Set the url file.

        Here we don't try to actually see if it exists for is valid as that
        is hadled by the connection logic.
        """
        # Find the actual controller key file
        if not self.url_file:
            self.url_file = os.path.join(
                self.profile_dir.security_dir, self.url_file_name
            )

    def load_connector_file(self):
        """load config from a JSON connector file,
        at a *lower* priority than command-line/config files.
        """

        self.log.info("Loading url_file %r", self.url_file)
        config = self.config

        with open(self.url_file) as f:
            num_tries = 0
            max_tries = 5
            d = ""
            while not d:
                try:
                    d = json.loads(f.read())
                except ValueError:
                    if num_tries > max_tries:
                        raise
                    num_tries += 1
                    time.sleep(0.5)

        # allow hand-override of location for disambiguation
        # and ssh-server
        if 'EngineFactory.location' not in config:
            config.EngineFactory.location = d['location']
        if 'EngineFactory.sshserver' not in config:
            config.EngineFactory.sshserver = d.get('ssh')

        location = config.EngineFactory.location

        proto, ip = d['interface'].split('://')
        ip = disambiguate_ip_address(ip, location)
        d['interface'] = '%s://%s' % (proto, ip)

        # DO NOT allow override of basic URLs, serialization, or key
        # JSON file takes top priority there
        config.Session.key = cast_bytes(d['key'])
        config.Session.signature_scheme = d['signature_scheme']

        config.EngineFactory.url = d['interface'] + ':%i' % d['registration']

        config.Session.packer = d['pack']
        config.Session.unpacker = d['unpack']

        self.log.debug("Config changed:")
        self.log.debug("%r", config)
        self.connection_info = d

    def bind_kernel(self, **kwargs):
        """Promote engine to listening kernel, accessible to frontends."""
        if self.kernel_app is not None:
            return

        self.log.info("Opening ports for direct connections as an IPython kernel")

        kernel = self.kernel

        kwargs.setdefault('config', self.config)
        kwargs.setdefault('log', self.log)
        kwargs.setdefault('profile_dir', self.profile_dir)
        kwargs.setdefault('session', self.engine.session)

        app = self.kernel_app = IPKernelApp(**kwargs)

        # allow IPKernelApp.instance():
        IPKernelApp._instance = app

        app.init_connection_file()
        # relevant contents of init_sockets:

        app.shell_port = app._bind_socket(kernel.shell_streams[0], app.shell_port)
        app.log.debug("shell ROUTER Channel on port: %i", app.shell_port)

        iopub_socket = kernel.iopub_socket
        # ipykernel 4.3 iopub_socket is an IOThread wrapper:
        if hasattr(iopub_socket, 'socket'):
            iopub_socket = iopub_socket.socket

        app.iopub_port = app._bind_socket(iopub_socket, app.iopub_port)
        app.log.debug("iopub PUB Channel on port: %i", app.iopub_port)

        kernel.stdin_socket = self.engine.context.socket(zmq.ROUTER)
        app.stdin_port = app._bind_socket(kernel.stdin_socket, app.stdin_port)
        app.log.debug("stdin ROUTER Channel on port: %i", app.stdin_port)

        # start the heartbeat, and log connection info:

        app.init_heartbeat()

        app.log_connection_info()
        app.connection_dir = self.profile_dir.security_dir
        app.write_connection_file()

    def init_engine(self):
        # This is the working dir by now.
        sys.path.insert(0, '')
        config = self.config
        # print config
        self.find_url_file()

        # was the url manually specified?
        keys = set(self.config.EngineFactory.keys())
        keys = keys.union(set(self.config.RegistrationFactory.keys()))

        if self.wait_for_url_file and not os.path.exists(self.url_file):
            self.log.warn("url_file %r not found", self.url_file)
            self.log.warn(
                "Waiting up to %.1f seconds for it to arrive.", self.wait_for_url_file
            )
            tic = time.time()
            while not os.path.exists(self.url_file) and (
                time.time() - tic < self.wait_for_url_file
            ):
                # wait for url_file to exist, or until time limit
                time.sleep(0.1)

        if os.path.exists(self.url_file):
            self.load_connector_file()
        else:
            self.log.fatal("Fatal: url file never arrived: %s", self.url_file)
            self.exit(1)

        exec_lines = []
        for app in ('IPKernelApp', 'InteractiveShellApp'):
            if '%s.exec_lines' % app in config:
                exec_lines = config[app].exec_lines
                break

        exec_files = []
        for app in ('IPKernelApp', 'InteractiveShellApp'):
            if '%s.exec_files' % app in config:
                exec_files = config[app].exec_files
                break

        config.IPKernelApp.exec_lines = exec_lines
        config.IPKernelApp.exec_files = exec_files

        if self.startup_script:
            exec_files.append(self.startup_script)
        if self.startup_command:
            exec_lines.append(self.startup_command)

        # Create the underlying shell class and Engine
        # shell_class = import_item(self.master_config.Global.shell_class)
        # print self.config
        try:
            self.engine = EngineFactory(
                config=config,
                log=self.log,
                connection_info=self.connection_info,
            )
        except:
            self.log.error("Couldn't start the Engine", exc_info=True)
            self.exit(1)

    def forward_logging(self):
        if self.log_url:
            self.log.info("Forwarding logging to %s", self.log_url)
            context = self.engine.context
            lsock = context.socket(zmq.PUB)
            lsock.connect(self.log_url)
            handler = EnginePUBHandler(self.engine, lsock)
            handler.setLevel(self.log_level)
            self.log.addHandler(handler)

    @catch_config_error
    def initialize(self, argv=None):
        super(IPEngineApp, self).initialize(argv)
        self.init_engine()
        self.forward_logging()

    def start(self):
        self.engine.start()
        try:
            self.engine.loop.start()
        except KeyboardInterrupt:
            self.log.critical("Engine Interrupted, shutting down...\n")


launch_new_instance = IPEngineApp.launch_instance


if __name__ == '__main__':
    launch_new_instance()
