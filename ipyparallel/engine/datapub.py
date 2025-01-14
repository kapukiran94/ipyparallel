"""Publishing native (typically pickled) objects."""
# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
from ipykernel.jsonutil import json_clean
from jupyter_client.session import extract_header
from jupyter_client.session import Session
from traitlets import Any
from traitlets import CBytes
from traitlets import Dict
from traitlets import Instance
from traitlets.config import Configurable

from ipyparallel.serialize import serialize_object


class ZMQDataPublisher(Configurable):

    topic = topic = CBytes(b'datapub')
    session = Instance(Session, allow_none=True)
    pub_socket = Any(allow_none=True)
    parent_header = Dict({})

    def set_parent(self, parent):
        """Set the parent for outbound messages."""
        self.parent_header = extract_header(parent)

    def publish_data(self, data):
        """publish a data_message on the IOPub channel

        Parameters
        ----------

        data : dict
            The data to be published. Think of it as a namespace.
        """
        session = self.session
        buffers = serialize_object(
            data,
            buffer_threshold=session.buffer_threshold,
            item_threshold=session.item_threshold,
        )
        content = json_clean(dict(keys=list(data.keys())))
        session.send(
            self.pub_socket,
            'data_message',
            content=content,
            parent=self.parent_header,
            buffers=buffers,
            ident=self.topic,
        )


def publish_data(data):
    """publish a data_message on the IOPub channel

    Parameters
    ----------

    data : dict
        The data to be published. Think of it as a namespace.
    """
    from ipykernel.zmqshell import ZMQInteractiveShell

    ZMQInteractiveShell.instance().data_pub.publish_data(data)
