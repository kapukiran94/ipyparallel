"""Views of remote engines."""
# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
from __future__ import absolute_import
from __future__ import print_function

import imp
import threading
import warnings
from contextlib import contextmanager

from decorator import decorator
from IPython import get_ipython
from ipython_genutils.py3compat import iteritems
from ipython_genutils.py3compat import PY3
from ipython_genutils.py3compat import string_types
from traitlets import Any
from traitlets import Bool
from traitlets import CFloat
from traitlets import Dict
from traitlets import HasTraits
from traitlets import Instance
from traitlets import Integer
from traitlets import List
from traitlets import Set

from . import map as Map
from .. import serialize
from ..serialize import PrePickled
from .asyncresult import AsyncMapResult
from .asyncresult import AsyncResult
from .remotefunction import getname
from .remotefunction import parallel
from .remotefunction import ParallelFunction
from .remotefunction import remote
from ipyparallel import util
from ipyparallel.controller.dependency import Dependency
from ipyparallel.controller.dependency import dependent

# -----------------------------------------------------------------------------
# Decorators
# -----------------------------------------------------------------------------


@decorator
def save_ids(f, self, *args, **kwargs):
    """Keep our history and outstanding attributes up to date after a method call."""
    n_previous = len(self.client.history)
    try:
        ret = f(self, *args, **kwargs)
    finally:
        nmsgs = len(self.client.history) - n_previous
        msg_ids = self.client.history[-nmsgs:]
        self.history.extend(msg_ids)
        self.outstanding.update(msg_ids)
    return ret


@decorator
def sync_results(f, self, *args, **kwargs):
    """sync relevant results from self.client to our results attribute."""
    if self._in_sync_results:
        return f(self, *args, **kwargs)
    self._in_sync_results = True
    try:
        ret = f(self, *args, **kwargs)
    finally:
        self._in_sync_results = False
        self._sync_results()
    return ret


# -----------------------------------------------------------------------------
# Classes
# -----------------------------------------------------------------------------


class View(HasTraits):
    """Base View class for more convenint apply(f,*args,**kwargs) syntax via attributes.

    Don't use this class, use subclasses.

    Methods
    -------

    spin
        flushes incoming results and registration state changes
        control methods spin, and requesting `ids` also ensures up to date

    wait
        wait on one or more msg_ids

    execution methods
        apply
        legacy: execute, run

    data movement
        push, pull, scatter, gather

    query methods
        get_result, queue_status, purge_results, result_status

    control methods
        abort, shutdown

    """

    # flags
    block = Bool(False)
    track = Bool(False)
    targets = Any()

    history = List()
    outstanding = Set()
    results = Dict()
    client = Instance('ipyparallel.Client', allow_none=True)

    _socket = Any()
    _flag_names = List(['targets', 'block', 'track'])
    _in_sync_results = Bool(False)
    _targets = Any()
    _idents = Any()

    def __init__(self, client=None, socket=None, **flags):
        super(View, self).__init__(client=client, _socket=socket)
        self.results = client.results
        self.block = client.block
        self.executor = ViewExecutor(self)

        self.set_flags(**flags)

        assert not self.__class__ is View, "Don't use base View objects, use subclasses"

    def __repr__(self):
        strtargets = str(self.targets)
        if len(strtargets) > 16:
            strtargets = strtargets[:12] + '...]'
        return "<%s %s>" % (self.__class__.__name__, strtargets)

    def __len__(self):
        if isinstance(self.targets, list):
            return len(self.targets)
        elif isinstance(self.targets, int):
            return 1
        else:
            return len(self.client)

    def set_flags(self, **kwargs):
        """set my attribute flags by keyword.

        Views determine behavior with a few attributes (`block`, `track`, etc.).
        These attributes can be set all at once by name with this method.

        Parameters
        ----------

        block : bool
            whether to wait for results
        track : bool
            whether to create a MessageTracker to allow the user to
            safely edit after arrays and buffers during non-copying
            sends.
        """
        for name, value in iteritems(kwargs):
            if name not in self._flag_names:
                raise KeyError("Invalid name: %r" % name)
            else:
                setattr(self, name, value)

    @contextmanager
    def temp_flags(self, **kwargs):
        """temporarily set flags, for use in `with` statements.

        See set_flags for permanent setting of flags

        Examples
        --------

        >>> view.track=False
        ...
        >>> with view.temp_flags(track=True):
        ...    ar = view.apply(dostuff, my_big_array)
        ...    ar.tracker.wait() # wait for send to finish
        >>> view.track
        False

        """
        # preflight: save flags, and set temporaries
        saved_flags = {}
        for f in self._flag_names:
            saved_flags[f] = getattr(self, f)
        self.set_flags(**kwargs)
        # yield to the with-statement block
        try:
            yield
        finally:
            # postflight: restore saved flags
            self.set_flags(**saved_flags)

    # ----------------------------------------------------------------
    # apply
    # ----------------------------------------------------------------

    def _sync_results(self):
        """to be called by @sync_results decorator

        after submitting any tasks.
        """
        delta = self.outstanding.difference(self.client.outstanding)
        completed = self.outstanding.intersection(delta)
        self.outstanding = self.outstanding.difference(completed)

    @sync_results
    @save_ids
    def _really_apply(self, f, args, kwargs, block=None, **options):
        """wrapper for client.send_apply_request"""
        raise NotImplementedError("Implement in subclasses")

    def apply(self, f, *args, **kwargs):
        """calls ``f(*args, **kwargs)`` on remote engines, returning the result.

        This method sets all apply flags via this View's attributes.

        Returns :class:`~ipyparallel.client.asyncresult.AsyncResult`
        instance if ``self.block`` is False, otherwise the return value of
        ``f(*args, **kwargs)``.
        """
        return self._really_apply(f, args, kwargs)

    def apply_async(self, f, *args, **kwargs):
        """calls ``f(*args, **kwargs)`` on remote engines in a nonblocking manner.

        Returns :class:`~ipyparallel.client.asyncresult.AsyncResult` instance.
        """
        return self._really_apply(f, args, kwargs, block=False)

    def apply_sync(self, f, *args, **kwargs):
        """calls ``f(*args, **kwargs)`` on remote engines in a blocking manner,
        returning the result.
        """
        return self._really_apply(f, args, kwargs, block=True)

    # ----------------------------------------------------------------
    # wrappers for client and control methods
    # ----------------------------------------------------------------
    @sync_results
    def spin(self):
        """spin the client, and sync"""
        self.client.spin()

    @sync_results
    def wait(self, jobs=None, timeout=-1):
        """waits on one or more `jobs`, for up to `timeout` seconds.

        Parameters
        ----------

        jobs : int, str, or list of ints and/or strs, or one or more AsyncResult objects
                ints are indices to self.history
                strs are msg_ids
                default: wait on all outstanding messages
        timeout : float
                a time in seconds, after which to give up.
                default is -1, which means no timeout

        Returns
        -------

        True : when all msg_ids are done
        False : timeout reached, some msg_ids still outstanding
        """
        if jobs is None:
            jobs = self.history
        return self.client.wait(jobs, timeout)

    def abort(self, jobs=None, targets=None, block=None):
        """Abort jobs on my engines.

        Parameters
        ----------

        jobs : None, str, list of strs, optional
            if None: abort all jobs.
            else: abort specific msg_id(s).
        """
        block = block if block is not None else self.block
        targets = targets if targets is not None else self.targets
        jobs = jobs if jobs is not None else list(self.outstanding)

        return self.client.abort(jobs=jobs, targets=targets, block=block)

    def queue_status(self, targets=None, verbose=False):
        """Fetch the Queue status of my engines"""
        targets = targets if targets is not None else self.targets
        return self.client.queue_status(targets=targets, verbose=verbose)

    def purge_results(self, jobs=[], targets=[]):
        """Instruct the controller to forget specific results."""
        if targets is None or targets == 'all':
            targets = self.targets
        return self.client.purge_results(jobs=jobs, targets=targets)

    def shutdown(self, targets=None, restart=False, hub=False, block=None):
        """Terminates one or more engine processes, optionally including the hub."""
        block = self.block if block is None else block
        if targets is None or targets == 'all':
            targets = self.targets
        return self.client.shutdown(
            targets=targets, restart=restart, hub=hub, block=block
        )

    def get_result(self, indices_or_msg_ids=None, block=None, owner=False):
        """return one or more results, specified by history index or msg_id.

        See :meth:`ipyparallel.client.client.Client.get_result` for details.
        """

        if indices_or_msg_ids is None:
            indices_or_msg_ids = -1
        if isinstance(indices_or_msg_ids, int):
            indices_or_msg_ids = self.history[indices_or_msg_ids]
        elif isinstance(indices_or_msg_ids, (list, tuple, set)):
            indices_or_msg_ids = list(indices_or_msg_ids)
            for i, index in enumerate(indices_or_msg_ids):
                if isinstance(index, int):
                    indices_or_msg_ids[i] = self.history[index]
        return self.client.get_result(indices_or_msg_ids, block=block, owner=owner)

    # -------------------------------------------------------------------
    # Map
    # -------------------------------------------------------------------

    @sync_results
    def map(self, f, *sequences, **kwargs):
        """override in subclasses"""
        raise NotImplementedError

    def map_async(self, f, *sequences, **kwargs):
        """Parallel version of builtin :func:`python:map`, using this view's engines.

        This is equivalent to ``map(...block=False)``.

        See `self.map` for details.
        """
        if 'block' in kwargs:
            raise TypeError("map_async doesn't take a `block` keyword argument.")
        kwargs['block'] = False
        return self.map(f, *sequences, **kwargs)

    def map_sync(self, f, *sequences, **kwargs):
        """Parallel version of builtin :func:`python:map`, using this view's engines.

        This is equivalent to ``map(...block=True)``.

        See `self.map` for details.
        """
        if 'block' in kwargs:
            raise TypeError("map_sync doesn't take a `block` keyword argument.")
        kwargs['block'] = True
        return self.map(f, *sequences, **kwargs)

    def imap(self, f, *sequences, **kwargs):
        """Parallel version of :func:`itertools.imap`.

        See `self.map` for details.

        """

        return iter(self.map_async(f, *sequences, **kwargs))

    # -------------------------------------------------------------------
    # Decorators
    # -------------------------------------------------------------------

    def remote(self, block=None, **flags):
        """Decorator for making a RemoteFunction"""
        block = self.block if block is None else block
        return remote(self, block=block, **flags)

    def parallel(self, dist='b', block=None, **flags):
        """Decorator for making a ParallelFunction"""
        block = self.block if block is None else block
        return parallel(self, dist=dist, block=block, **flags)


class DirectView(View):
    """Direct Multiplexer View of one or more engines.

    These are created via indexed access to a client:

    >>> dv_1 = client[1]
    >>> dv_all = client[:]
    >>> dv_even = client[::2]
    >>> dv_some = client[1:3]

    This object provides dictionary access to engine namespaces:

    # push a=5:
    >>> dv['a'] = 5
    # pull 'foo':
    >>> dv['foo']

    """

    def __init__(self, client=None, socket=None, targets=None):
        super(DirectView, self).__init__(client=client, socket=socket, targets=targets)

    @property
    def importer(self):
        """sync_imports(local=True) as a property.

        See sync_imports for details.

        """
        return self.sync_imports(True)

    @contextmanager
    def sync_imports(self, local=True, quiet=False):
        """Context Manager for performing simultaneous local and remote imports.

        'import x as y' will *not* work.  The 'as y' part will simply be ignored.

        If `local=True`, then the package will also be imported locally.

        If `quiet=True`, no output will be produced when attempting remote
        imports.

        Note that remote-only (`local=False`) imports have not been implemented.

        >>> with view.sync_imports():
        ...    from numpy import recarray
        importing recarray from numpy on engine(s)

        """
        from ipython_genutils.py3compat import builtin_mod

        local_import = builtin_mod.__import__
        modules = set()
        results = []

        @util.interactive
        def remote_import(name, fromlist, level):
            """the function to be passed to apply, that actually performs the import
            on the engine, and loads up the user namespace.
            """
            import sys

            user_ns = globals()
            mod = __import__(name, fromlist=fromlist, level=level)
            if fromlist:
                for key in fromlist:
                    user_ns[key] = getattr(mod, key)
            else:
                user_ns[name] = sys.modules[name]

        def view_import(name, globals={}, locals={}, fromlist=[], level=0):
            """the drop-in replacement for __import__, that optionally imports
            locally as well.
            """
            # don't override nested imports
            save_import = builtin_mod.__import__
            builtin_mod.__import__ = local_import

            if imp.lock_held():
                # this is a side-effect import, don't do it remotely, or even
                # ignore the local effects
                return local_import(name, globals, locals, fromlist, level)

            imp.acquire_lock()
            if local:
                mod = local_import(name, globals, locals, fromlist, level)
            else:
                raise NotImplementedError("remote-only imports not yet implemented")
            imp.release_lock()

            key = name + ':' + ','.join(fromlist or [])
            if level <= 0 and key not in modules:
                modules.add(key)
                if not quiet:
                    if fromlist:
                        print(
                            "importing %s from %s on engine(s)"
                            % (','.join(fromlist), name)
                        )
                    else:
                        print("importing %s on engine(s)" % name)
                results.append(self.apply_async(remote_import, name, fromlist, level))
            # restore override
            builtin_mod.__import__ = save_import

            return mod

        # override __import__
        builtin_mod.__import__ = view_import
        try:
            # enter the block
            yield
        except ImportError:
            if local:
                raise
            else:
                # ignore import errors if not doing local imports
                pass
        finally:
            # always restore __import__
            builtin_mod.__import__ = local_import

        for r in results:
            # raise possible remote ImportErrors here
            r.get()

    def use_dill(self):
        """Expand serialization support with dill

        adds support for closures, etc.

        This calls ipyparallel.serialize.use_dill() here and on each engine.
        """
        serialize.use_dill()
        return self.apply(serialize.use_dill)

    def use_cloudpickle(self):
        """Expand serialization support with cloudpickle.

        This calls ipyparallel.serialize.use_cloudpickle() here and on each engine.
        """
        serialize.use_cloudpickle()
        return self.apply(serialize.use_cloudpickle)

    def use_pickle(self):
        """Restore

        This reverts changes to serialization caused by `use_dill|.cloudpickle`.
        """
        serialize.use_pickle()
        return self.apply(serialize.use_pickle)

    @sync_results
    @save_ids
    def _really_apply(
        self, f, args=None, kwargs=None, targets=None, block=None, track=None
    ):
        """calls f(*args, **kwargs) on remote engines, returning the result.

        This method sets all of `apply`'s flags via this View's attributes.

        Parameters
        ----------

        f : callable

        args : list [default: empty]

        kwargs : dict [default: empty]

        targets : target list [default: self.targets]
            where to run
        block : bool [default: self.block]
            whether to block
        track : bool [default: self.track]
            whether to ask zmq to track the message, for safe non-copying sends

        Returns
        -------

        if self.block is False:
            returns AsyncResult
        else:
            returns actual result of f(*args, **kwargs) on the engine(s)
            This will be a list of self.targets is also a list (even length 1), or
            the single result if self.targets is an integer engine id
        """
        args = [] if args is None else args
        kwargs = {} if kwargs is None else kwargs
        block = self.block if block is None else block
        track = self.track if track is None else track
        targets = self.targets if targets is None else targets

        _idents, _targets = self.client._build_targets(targets)
        futures = []

        pf = PrePickled(f)
        pargs = [PrePickled(arg) for arg in args]
        pkwargs = {k: PrePickled(v) for k, v in kwargs.items()}

        for ident in _idents:
            future = self.client.send_apply_request(
                self._socket, pf, pargs, pkwargs, track=track, ident=ident
            )
            futures.append(future)
        if track:
            trackers = [_.tracker for _ in futures]
        else:
            trackers = []
        if isinstance(targets, int):
            futures = futures[0]
        ar = AsyncResult(
            self.client, futures, fname=getname(f), targets=_targets, owner=True
        )
        if block:
            try:
                return ar.get()
            except KeyboardInterrupt:
                pass
        return ar

    @sync_results
    def map(self, f, *sequences, **kwargs):
        """``view.map(f, *sequences, block=self.block)`` => list|AsyncMapResult

        Parallel version of builtin `map`, using this View's `targets`.

        There will be one task per target, so work will be chunked
        if the sequences are longer than `targets`.

        Results can be iterated as they are ready, but will become available in chunks.

        Parameters
        ----------

        f : callable
            function to be mapped
        *sequences: one or more sequences of matching length
            the sequences to be distributed and passed to `f`
        block : bool
            whether to wait for the result or not [default self.block]

        Returns
        -------


        If block=False
          An :class:`~ipyparallel.client.asyncresult.AsyncMapResult` instance.
          An object like AsyncResult, but which reassembles the sequence of results
          into a single list. AsyncMapResults can be iterated through before all
          results are complete.
        else
            A list, the result of ``map(f,*sequences)``
        """

        block = kwargs.pop('block', self.block)
        for k in kwargs.keys():
            if k not in ['block', 'track']:
                raise TypeError("invalid keyword arg, %r" % k)

        assert len(sequences) > 0, "must have some sequences to map onto!"
        pf = ParallelFunction(self, f, block=block, **kwargs)
        return pf.map(*sequences)

    @sync_results
    @save_ids
    def execute(self, code, silent=True, targets=None, block=None):
        """Executes `code` on `targets` in blocking or nonblocking manner.

        ``execute`` is always `bound` (affects engine namespace)

        Parameters
        ----------

        code : str
                the code string to be executed
        block : bool
                whether or not to wait until done to return
                default: self.block
        """
        block = self.block if block is None else block
        targets = self.targets if targets is None else targets

        _idents, _targets = self.client._build_targets(targets)
        futures = []
        for ident in _idents:
            future = self.client.send_execute_request(
                self._socket, code, silent=silent, ident=ident
            )
            futures.append(future)
        if isinstance(targets, int):
            futures = futures[0]
        ar = AsyncResult(
            self.client, futures, fname='execute', targets=_targets, owner=True
        )
        if block:
            try:
                ar.get()
                ar.wait_for_output()
            except KeyboardInterrupt:
                pass
        return ar

    def run(self, filename, targets=None, block=None):
        """Execute contents of `filename` on my engine(s).

        This simply reads the contents of the file and calls `execute`.

        Parameters
        ----------

        filename : str
                The path to the file
        targets : int/str/list of ints/strs
                the engines on which to execute
                default : all
        block : bool
                whether or not to wait until done
                default: self.block

        """
        with open(filename, 'r') as f:
            # add newline in case of trailing indented whitespace
            # which will cause SyntaxError
            code = f.read() + '\n'
        return self.execute(code, block=block, targets=targets)

    def update(self, ns):
        """update remote namespace with dict `ns`

        See `push` for details.
        """
        return self.push(ns, block=self.block, track=self.track)

    def push(self, ns, targets=None, block=None, track=None):
        """update remote namespace with dict `ns`

        Parameters
        ----------

        ns : dict
            dict of keys with which to update engine namespace(s)
        block : bool [default : self.block]
            whether to wait to be notified of engine receipt

        """

        block = block if block is not None else self.block
        track = track if track is not None else self.track
        targets = targets if targets is not None else self.targets
        # applier = self.apply_sync if block else self.apply_async
        if not isinstance(ns, dict):
            raise TypeError("Must be a dict, not %s" % type(ns))
        return self._really_apply(
            util._push, kwargs=ns, block=block, track=track, targets=targets
        )

    def get(self, key_s):
        """get object(s) by `key_s` from remote namespace

        see `pull` for details.
        """
        # block = block if block is not None else self.block
        return self.pull(key_s, block=True)

    def pull(self, names, targets=None, block=None):
        """get object(s) by `name` from remote namespace

        will return one object if it is a key.
        can also take a list of keys, in which case it will return a list of objects.
        """
        block = block if block is not None else self.block
        targets = targets if targets is not None else self.targets
        if isinstance(names, string_types):
            pass
        elif isinstance(names, (list, tuple, set)):
            for key in names:
                if not isinstance(key, string_types):
                    raise TypeError("keys must be str, not type %r" % type(key))
        else:
            raise TypeError("names must be strs, not %r" % names)
        return self._really_apply(util._pull, (names,), block=block, targets=targets)

    def scatter(
        self, key, seq, dist='b', flatten=False, targets=None, block=None, track=None
    ):
        """
        Partition a Python sequence and send the partitions to a set of engines.
        """
        block = block if block is not None else self.block
        track = track if track is not None else self.track
        targets = targets if targets is not None else self.targets

        # construct integer ID list:
        targets = self.client._build_targets(targets)[1]

        mapObject = Map.dists[dist]()
        nparts = len(targets)
        futures = []
        trackers = []
        for index, engineid in enumerate(targets):
            partition = mapObject.getPartition(seq, index, nparts)
            if flatten and len(partition) == 1:
                ns = {key: partition[0]}
            else:
                ns = {key: partition}
            r = self.push(ns, block=False, track=track, targets=engineid)
            r.owner = False
            futures.extend(r._children)

        r = AsyncResult(
            self.client, futures, fname='scatter', targets=targets, owner=True
        )
        if block:
            r.wait()
        else:
            return r

    @sync_results
    @save_ids
    def gather(self, key, dist='b', targets=None, block=None):
        """
        Gather a partitioned sequence on a set of engines as a single local seq.
        """
        block = block if block is not None else self.block
        targets = targets if targets is not None else self.targets
        mapObject = Map.dists[dist]()
        msg_ids = []

        # construct integer ID list:
        targets = self.client._build_targets(targets)[1]

        futures = []
        for index, engineid in enumerate(targets):
            ar = self.pull(key, block=False, targets=engineid)
            ar.owner = False
            futures.extend(ar._children)

        r = AsyncMapResult(self.client, futures, mapObject, fname='gather')

        if block:
            try:
                return r.get()
            except KeyboardInterrupt:
                pass
        return r

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        self.update({key: value})

    def clear(self, targets=None, block=None):
        """Clear the remote namespaces on my engines."""
        block = block if block is not None else self.block
        targets = targets if targets is not None else self.targets
        return self.client.clear(targets=targets, block=block)

    # ----------------------------------------
    # activate for %px, %autopx, etc. magics
    # ----------------------------------------

    def activate(self, suffix=''):
        """Activate IPython magics associated with this View

        Defines the magics `%px, %autopx, %pxresult, %%px, %pxconfig`

        Parameters
        ----------


        suffix: str [default: '']
            The suffix, if any, for the magics.  This allows you to have
            multiple views associated with parallel magics at the same time.

            e.g. ``rc[::2].activate(suffix='_even')`` will give you
            the magics ``%px_even``, ``%pxresult_even``, etc. for running magics
            on the even engines.
        """

        from ipyparallel.client.magics import ParallelMagics

        ip = get_ipython()
        if ip is None:
            warnings.warn(
                "The IPython parallel magics (%px, etc.) only work within IPython."
            )
            return

        M = ParallelMagics(ip, self, suffix)
        ip.magics_manager.register(M)


class BroadcastView(DirectView):
    is_coalescing = Bool(False)

    @sync_results
    @save_ids
    def _really_apply(
        self, f, args=None, kwargs=None, block=None, track=None, targets=None
    ):
        args = [] if args is None else args
        kwargs = {} if kwargs is None else kwargs
        block = self.block if block is None else block
        track = self.track if track is None else track
        targets = self.targets if targets is None else targets
        idents, _targets = self.client._build_targets(targets)
        futures = []

        pf = PrePickled(f)
        pargs = [PrePickled(arg) for arg in args]
        pkwargs = {k: PrePickled(v) for k, v in kwargs.items()}

        s_idents = [ident.decode("utf8") for ident in idents]

        metadata = dict(
            targets=s_idents, is_broadcast=True, is_coalescing=self.is_coalescing
        )
        if not self.is_coalescing:
            original_future = self.client.send_apply_request(
                self._socket, pf, pargs, pkwargs, track=track, metadata=metadata
            )
            original_msg_id = original_future.msg_id

            for ident in s_idents:
                msg_and_target_id = f'{original_msg_id}_{ident}'
                future = self.client.create_message_futures(
                    msg_and_target_id, async_result=True, track=True
                )
                self.client.outstanding.add(msg_and_target_id)
                self.outstanding.add(msg_and_target_id)
                futures.append(future[0])
            if original_msg_id in self.outstanding:
                self.outstanding.remove(original_msg_id)
        else:
            message_future = self.client.send_apply_request(
                self._socket, pf, pargs, pkwargs, track=track, metadata=metadata
            )
            self.client.outstanding.add(message_future.msg_id)
            futures = message_future

        ar = AsyncResult(
            self.client, futures, fname=getname(f), targets=_targets, owner=True
        )
        if block:
            try:
                return ar.get()
            except KeyboardInterrupt:
                pass
        return ar

    def map(self, f, *sequences, **kwargs):
        pass


class LoadBalancedView(View):
    """An load-balancing View that only executes via the Task scheduler.

    Load-balanced views can be created with the client's `view` method:

    >>> v = client.load_balanced_view()

    or targets can be specified, to restrict the potential destinations:

    >>> v = client.load_balanced_view([1,3])

    which would restrict loadbalancing to between engines 1 and 3.

    """

    follow = Any()
    after = Any()
    timeout = CFloat()
    retries = Integer(0)

    _task_scheme = Any()
    _flag_names = List(
        ['targets', 'block', 'track', 'follow', 'after', 'timeout', 'retries']
    )

    def __init__(self, client=None, socket=None, **flags):
        super(LoadBalancedView, self).__init__(client=client, socket=socket, **flags)
        self._task_scheme = client._task_scheme

    def _validate_dependency(self, dep):
        """validate a dependency.

        For use in `set_flags`.
        """
        if dep is None or isinstance(dep, string_types + (AsyncResult, Dependency)):
            return True
        elif isinstance(dep, (list, set, tuple)):
            for d in dep:
                if not isinstance(d, string_types + (AsyncResult,)):
                    return False
        elif isinstance(dep, dict):
            if set(dep.keys()) != set(Dependency().as_dict().keys()):
                return False
            if not isinstance(dep['msg_ids'], list):
                return False
            for d in dep['msg_ids']:
                if not isinstance(d, string_types):
                    return False
        else:
            return False

        return True

    def _render_dependency(self, dep):
        """helper for building jsonable dependencies from various input forms."""
        if isinstance(dep, Dependency):
            return dep.as_dict()
        elif isinstance(dep, AsyncResult):
            return dep.msg_ids
        elif dep is None:
            return []
        else:
            # pass to Dependency constructor
            return list(Dependency(dep))

    def set_flags(self, **kwargs):
        """set my attribute flags by keyword.

        A View is a wrapper for the Client's apply method, but with attributes
        that specify keyword arguments, those attributes can be set by keyword
        argument with this method.

        Parameters
        ----------

        block : bool
            whether to wait for results
        track : bool
            whether to create a MessageTracker to allow the user to
            safely edit after arrays and buffers during non-copying
            sends.

        after : Dependency or collection of msg_ids
            Only for load-balanced execution (targets=None)
            Specify a list of msg_ids as a time-based dependency.
            This job will only be run *after* the dependencies
            have been met.

        follow : Dependency or collection of msg_ids
            Only for load-balanced execution (targets=None)
            Specify a list of msg_ids as a location-based dependency.
            This job will only be run on an engine where this dependency
            is met.

        timeout : float/int or None
            Only for load-balanced execution (targets=None)
            Specify an amount of time (in seconds) for the scheduler to
            wait for dependencies to be met before failing with a
            DependencyTimeout.

        retries : int
            Number of times a task will be retried on failure.
        """

        super(LoadBalancedView, self).set_flags(**kwargs)
        for name in ('follow', 'after'):
            if name in kwargs:
                value = kwargs[name]
                if self._validate_dependency(value):
                    setattr(self, name, value)
                else:
                    raise ValueError("Invalid dependency: %r" % value)
        if 'timeout' in kwargs:
            t = kwargs['timeout']
            if not isinstance(t, (int, float, type(None))):
                raise TypeError("Invalid type for timeout: %r" % type(t))
            if t is not None:
                if t < 0:
                    raise ValueError("Invalid timeout: %s" % t)

            self.timeout = t

    @sync_results
    @save_ids
    def _really_apply(
        self,
        f,
        args=None,
        kwargs=None,
        block=None,
        track=None,
        after=None,
        follow=None,
        timeout=None,
        targets=None,
        retries=None,
    ):
        """calls f(*args, **kwargs) on a remote engine, returning the result.

        This method temporarily sets all of `apply`'s flags for a single call.

        Parameters
        ----------

        f : callable

        args : list [default: empty]

        kwargs : dict [default: empty]

        block : bool [default: self.block]
            whether to block
        track : bool [default: self.track]
            whether to ask zmq to track the message, for safe non-copying sends

        !!!!!! TODO: THE REST HERE  !!!!

        Returns
        -------

        if self.block is False:
            returns AsyncResult
        else:
            returns actual result of f(*args, **kwargs) on the engine(s)
            This will be a list of self.targets is also a list (even length 1), or
            the single result if self.targets is an integer engine id
        """

        # validate whether we can run
        if self._socket.closed():
            msg = "Task farming is disabled"
            if self._task_scheme == 'pure':
                msg += " because the pure ZMQ scheduler cannot handle"
                msg += " disappearing engines."
            raise RuntimeError(msg)

        if self._task_scheme == 'pure':
            # pure zmq scheme doesn't support extra features
            msg = "Pure ZMQ scheduler doesn't support the following flags:"
            "follow, after, retries, targets, timeout"
            if follow or after or retries or targets or timeout:
                # hard fail on Scheduler flags
                raise RuntimeError(msg)
            if isinstance(f, dependent):
                # soft warn on functional dependencies
                warnings.warn(msg, RuntimeWarning)

        # build args
        args = [] if args is None else args
        kwargs = {} if kwargs is None else kwargs
        block = self.block if block is None else block
        track = self.track if track is None else track
        after = self.after if after is None else after
        retries = self.retries if retries is None else retries
        follow = self.follow if follow is None else follow
        timeout = self.timeout if timeout is None else timeout
        targets = self.targets if targets is None else targets

        if not isinstance(retries, int):
            raise TypeError('retries must be int, not %r' % type(retries))

        if targets is None:
            idents = []
        else:
            idents = self.client._build_targets(targets)[0]
            # ensure *not* bytes
            idents = [ident.decode() for ident in idents]

        after = self._render_dependency(after)
        follow = self._render_dependency(follow)
        metadata = dict(
            after=after, follow=follow, timeout=timeout, targets=idents, retries=retries
        )

        future = self.client.send_apply_request(
            self._socket, f, args, kwargs, track=track, metadata=metadata
        )

        ar = AsyncResult(
            self.client,
            future,
            fname=getname(f),
            targets=None,
            owner=True,
        )
        if block:
            try:
                return ar.get()
            except KeyboardInterrupt:
                pass
        return ar

    @sync_results
    @save_ids
    def map(self, f, *sequences, **kwargs):
        """``view.map(f, *sequences, block=self.block, chunksize=1, ordered=True)`` => list|AsyncMapResult
        Parallel version of builtin `map`, load-balanced by this View.

        `block`, and `chunksize` can be specified by keyword only.

        Each `chunksize` elements will be a separate task, and will be
        load-balanced. This lets individual elements be available for iteration
        as soon as they arrive.

        Parameters
        ----------

        f : callable
            function to be mapped
        *sequences: one or more sequences of matching length
            the sequences to be distributed and passed to `f`
        block : bool [default self.block]
            whether to wait for the result or not
        track : bool
            whether to create a MessageTracker to allow the user to
            safely edit after arrays and buffers during non-copying
            sends.
        chunksize : int [default 1]
            how many elements should be in each task.
        ordered : bool [default True]
            Whether the results should be gathered as they arrive, or enforce
            the order of submission.

            Only applies when iterating through AsyncMapResult as results arrive.
            Has no effect when block=True.

        Returns
        -------

        if block=False
          An :class:`~ipyparallel.client.asyncresult.AsyncMapResult` instance.
          An object like AsyncResult, but which reassembles the sequence of results
          into a single list. AsyncMapResults can be iterated through before all
          results are complete.
        else
            A list, the result of ``map(f,*sequences)``
        """

        # default
        block = kwargs.get('block', self.block)
        chunksize = kwargs.get('chunksize', 1)
        ordered = kwargs.get('ordered', True)

        keyset = set(kwargs.keys())
        extra_keys = keyset.difference_update(set(['block', 'chunksize']))
        if extra_keys:
            raise TypeError("Invalid kwargs: %s" % list(extra_keys))

        assert len(sequences) > 0, "must have some sequences to map onto!"

        pf = ParallelFunction(
            self, f, block=block, chunksize=chunksize, ordered=ordered
        )
        return pf.map(*sequences)

    def register_joblib_backend(self, name='ipyparallel', make_default=False):
        """Register this View as a joblib parallel backend

        To make this the default backend, set make_default=True.

        Use with::

            p = Parallel(backend='ipyparallel')
            ...

        See joblib docs for details

        Requires joblib >= 0.10

        .. versionadded:: 5.1
        """
        from joblib.parallel import register_parallel_backend
        from ._joblib import IPythonParallelBackend

        register_parallel_backend(
            name,
            lambda **kwargs: IPythonParallelBackend(view=self, **kwargs),
            make_default=make_default,
        )


from concurrent.futures import Executor


class ViewExecutor(Executor):
    """A PEP-3148 Executor API for Views

    Access as view.executor
    """

    def __init__(self, view):
        self.view = view

    def submit(self, fn, *args, **kwargs):
        """Same as View.apply_async"""
        return self.view.apply_async(fn, *args, **kwargs)

    def map(self, func, *iterables, **kwargs):
        """Return generator for View.map_async"""
        if 'timeout' in kwargs:
            warnings.warn("timeout unsupported in ViewExecutor.map")
            kwargs.pop('timeout')
        for r in self.view.map_async(func, *iterables, **kwargs):
            yield r

    def shutdown(self, wait=True):
        """ViewExecutor does *not* shutdown engines

        results are awaited if wait=True, but engines are *not* shutdown.
        """
        if wait:
            self.view.wait()


__all__ = ['LoadBalancedView', 'DirectView', 'ViewExecutor', 'BroadcastView']
