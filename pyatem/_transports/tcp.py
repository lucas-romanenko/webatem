"""
TCP transport — talks to an OpenSwitcher proxy that fronts one or more
upstream ATEMs over TCP. AV Server's production deployments don't use
this; ``AtemProtocol.__init__`` lazy-imports it when the caller passes
``ip='tcp://...'``.
"""

import logging
import socket
import struct
from urllib.parse import urlparse

from pyatem._transfer import TransferTask
from pyatem.transport import BaseProtocol, Packet


class TcpProtocol(BaseProtocol):
    STATE_INIT = 0
    STATE_AUTH = 1
    STATE_CONNECTED = 2

    STRUCT_HEADER = struct.Struct('!H')
    STRUCT_FIELD = struct.Struct('!H2x 4s')

    def __init__(self, url=None, host=None, port=None, username=None, password=None, device=None):
        super().__init__()
        if url is not None:
            part = urlparse(url)
            host = part.hostname
            port = part.port or 4532
            username = part.username
            password = part.password
            device = part.path[1:]
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.device = device

        self.sock = None
        self.state = TcpProtocol.STATE_INIT

        self.log = logging.getLogger('TcpTransport')

    def _send_packet(self, data):
        header = self.STRUCT_HEADER.pack(len(data))
        self.sock.sendall(header + data)

    def _receive_packet(self):
        try:
            header = self.sock.recv(2)
            datalength, = self.STRUCT_HEADER.unpack(header)
            data_left = datalength
            data = b''
            while data_left > 0:
                block = self.sock.recv(data_left)
                if len(block) == 0:
                    self.log.error("Connection closed")
                    return
                data_left -= len(block)
                data += block
        except:
            return None

        packet = Packet()
        packet.data = data
        return packet

    def decode_packet(self, data):
        offset = 0
        if len(data) < 8:
            raise ValueError("Packet too short")
        while offset < len(data):
            datalen, cmd = self.STRUCT_FIELD.unpack_from(data, offset)
            raw = data[offset + 8:offset + datalen]
            yield (cmd, raw)
            offset += datalen

    def list_to_packets(self, data):
        result = b''
        for key, value in data:
            result += self.STRUCT_FIELD.pack(len(value) + 8, key)
            result += value
        return result

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))

        # Send magic packet to init the connection
        self._send_packet(self.list_to_packets([(b'*SW*', b'')]))

    def send_auth(self):
        if self.username is None or self.password is None:
            raise ValueError("Proxy requests AUTH but username or password is not set")
        self._send_packet(self.list_to_packets([
            (b'*USR', self.username.encode()),
            (b'*PWD', self.password.encode()),
        ]))

    def connect_device(self):
        self._send_packet(self.list_to_packets([
            (b'*DEV', self.device.encode()),
        ]))

    def receive_packet(self):
        while True:
            packet = self._receive_packet()
            if packet is None:
                continue
            if self.state == TcpProtocol.STATE_INIT:
                fields = list(self.decode_packet(packet.data))
                if fields[0][0] == b'AUTH':
                    self.send_auth()
                    continue
                elif fields[0][0] == b'*HW*':
                    self.connect_device()
                    self.state = TcpProtocol.STATE_CONNECTED
            elif self.state == TcpProtocol.STATE_CONNECTED:
                if packet is not None:
                    return packet

    def send_packet(self, packet):
        self._send_packet(packet.data)

    def upload(self, task):
        if not isinstance(task, TransferTask):
            raise ValueError()
        for packet in task.to_tcp():
            self._send_packet(self.list_to_packets([packet]))

    def download(self, task):
        pass
