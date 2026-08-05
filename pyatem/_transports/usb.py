"""
USB transport for the Atem Mini line.

Implements the same surface as ``UdpProtocol`` — ``connect()``,
``receive_packet()``, ``send_packet()``, etc. — over libusb via pyusb.
AV Server's production deployments don't use this; the module is
imported lazily by ``AtemProtocol.__init__`` only when the caller
passes ``usb=<port>``.
"""

import logging
import struct
import threading
import time
from queue import Empty, Queue

import usb.core
import usb.util

from pyatem.transport import BaseProtocol, ConnectionReady, Packet


class UsbProtocol(BaseProtocol):
    STATE_INIT = 0
    STATE_CONNECTED = 1

    PRODUCTS = {
        0xbe49: "Atem Mini",
        0xbe55: "Atem Mini Pro",
        0xbe56: "Atem Mini Pro ISO",
        0xbe7c: "Atem Mini Extreme",
    }

    def __init__(self, port=None):
        super().__init__()
        port = port or "auto"
        self.port = port
        self.queue = Queue()

        self.log = logging.getLogger('USBTransport')
        self.thread = threading.Thread(None, self._rx_thread, "atem-usb", daemon=True)
        self.rx_queue = Queue(maxsize=1024)

        self.handle = UsbProtocol.find_device()
        self._detach_kernel()
        self.handle.set_configuration()

        self.rev = None
        self.init_done = False
        self.state = UsbProtocol.STATE_INIT
        self.buffer_use = 0

    @classmethod
    def device_exists(cls):
        return cls.find_device() is not None

    @classmethod
    def find_device(cls):
        for prod in UsbProtocol.PRODUCTS.keys():
            device = usb.core.find(idVendor=0x1edb, idProduct=prod)
            if device is not None:
                return device
        return None

    def _rx_thread(self):
        timer = 0
        while True:
            # Send buffer pressure info if more than a kilobyte of data is buffered
            diff = (time.time_ns() - timer) / (10 ** 9)
            if self.buffer_use > 1024 or diff < 0.01:
                buffer_blocks = min(1, self.buffer_use // 1024)
                if self.rev == 8.6:
                    self.write(0x03, struct.pack('>6xH', buffer_blocks))

            try:
                data = self.handle.read(0x82, 8192 * 4, timeout=0)
                timer = time.time_ns()
            except usb.USBError as e:
                if e.errno == 32:
                    self.log.fatal(f"USB failure on reading new packet: {e}")

                if e.errno == 110:
                    # Timeout happened, start reading again
                    continue
                break
            self.buffer_use += len(data)
            self.rx_queue.put(data)
        self.rx_queue.put(None)

    def _detach_kernel(self):
        try:
            for config in self.handle:
                for i in range(config.bNumInterfaces):
                    if self.handle.is_kernel_driver_active(i):
                        try:
                            self.handle.detach_kernel_driver(i)
                            self.log.debug('kernel driver detached')
                        except usb.core.USBError as e:
                            self.log.error('Could not detach kernel driver: ' + str(e))
        except usb.core.USBError as e:
            if e.errno == 13:
                raise PermissionError(e)

    def _send_packet(self, packet):
        raw = packet.to_usb()
        if self.rev == 8.6:
            raw = raw[4:]
            self.write(0x04, struct.pack('>6xH', len(raw)))
        self.handle.write(0x02, raw)

    def write(self, ptype, data, pid=1):
        raw = struct.pack('BBBB', 0, ptype, 0, 1) + data
        self.handle.write(0x02, raw)

    def read(self):
        result = self.handle.read(0x82, 16 * 1024)
        payload = bytes(result)
        header = struct.unpack_from('BBBB', payload, 0)
        return header[0], payload[4:]

    def connect(self):
        try:
            # Try the initial control transfer for firmware 8.5 and older
            self.handle.ctrl_transfer(0x21, 0, 0x0000, 2, [])
            # Start RX queue
            self.thread.start()
            self.rev = 8.5
            self.log.info("Using USB protocol version 8.5")
        except usb.core.USBError:
            # 8.6+ firmware raises a pipe error on the previous URB, try the newer handshake
            self.log.info('Using USB protocol version 8.6+')
            self.handle.reset()
            self.handle.clear_halt(0x02)
            self.handle.clear_halt(0x82)
            self.state = UsbProtocol.STATE_INIT

            # Start RX queue
            self.thread.start()

            # Init packet
            self.log.debug('Sending handshake packet 0x01')
            self.write(0x01, b'\x00\x01\x10\x92')
            self.rev = 8.6

    def receive_packet(self):
        if self.mark_next_connected:
            self.mark_next_connected = False
            return ConnectionReady()

        timeout = 0.5
        if self.rev < 8.6:
            timeout = 0.2

        while True:
            try:
                packet = self.rx_queue.get(timeout=timeout)
                time.sleep(0.01)
            except Empty:
                self.keep_alive()
                time.sleep(0.01)
                continue
            if packet is None:
                # Cannot read from USB anymore, device has disconnected
                return None
            self.buffer_use -= len(packet)

            if self.state == UsbProtocol.STATE_INIT:
                self.state = UsbProtocol.STATE_CONNECTED
                if self.rev == 8.6:
                    header = struct.unpack_from('BBBB', packet, 0)
                    self.log.debug(f'RX packet 0x{header[0]:x}')
                    self.log.debug(f'Send handshake packet 0x03')
                    self.write(0x03, b'x00\x00\x00\x00\x00\x00\x10\x00\x00\x00')
                    continue

            if self.rev == 8.6:
                header = struct.unpack_from('BBBB', packet, 0)
                packet_type = header[0]
                if packet_type == 0x03:
                    payload = struct.unpack('>BBBBH6x', packet)
                    self.log.warning(f"RX Buffer change received: {payload[0]}")
                elif packet_type == 0x04:
                    # Packet is ATEM protocol data.
                    payload_length, = struct.unpack_from('<H', packet, 4)

                    # Receive the actual ATEM data in the next packet
                    packet = b''
                    while len(packet) < payload_length:
                        chunk = self.rx_queue.get()
                        packet += chunk
                        self.buffer_use -= len(chunk)

                    if not self.mark_next_connected and not self.init_done:
                        self.mark_next_connected = True
                        self.init_done = True
                        self.log.debug("Mark connection ready after this")
                else:
                    self.log.warning(f"Unknown packet type received: {packet_type:#X}")
                    continue

            raw = bytes(packet)
            if self.rev == 8.6:
                packet = Packet()
                packet.data = raw
                return packet

            if len(raw) == 0:
                continue

            chunks = []
            while True:
                length, = Packet.STRUCT_USB.unpack_from(raw)
                chunks.append(raw[4:length + 4])
                raw = raw[length + 4:]
                if len(raw) == 0:
                    break

            packet = Packet()
            packet.data = b''.join(chunks)
            return packet

    def send_packet(self, packet):
        self._send_packet(packet)
        time.sleep(0.004)

    def keep_alive(self):
        try:
            self.handle.write(0x02, b'')
        except:
            pass
