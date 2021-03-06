import abc
import logging
import netifaces
import socket
import struct

from twisted.internet import reactor



class Endpoint(object):
    """
    Interface for sending messages over the Internet.
    """

    __metaclass__ = abc.ABCMeta

    def __init__(self):
        self._logger = logging.getLogger(self.__class__.__name__)
        self._listeners = []

    def add_listener(self, listener):
        """
        Add an EndpointListener to our listeners.

        :raises: IllegalEndpointListenerError if the provided listener is not an EndpointListener
        """
        if not isinstance(listener, EndpointListener):
            raise IllegalEndpointListenerError(listener)
        self._listeners.append(listener)

    def remove_listener(self, listener):
        """
        Remove a listener from our listeners, if it is registered.
        """
        self._listeners = [l for l in self._listeners if l != listener]

    def _deliver_later(self, listener, packet):
        """
        Ensure that the listener is still loaded when delivering the packet later.
        """
        if reactor.running and self.is_open() and listener in self._listeners:
            listener.on_packet(packet)

    def notify_listeners(self, packet):
        """
        Send data to all listeners.

        :param data: the data to send to all listeners.
        """
        for listener in self._listeners:
            reactor.callFromThread(self._deliver_later, listener, packet)

    @abc.abstractmethod
    def assert_open(self):
        pass

    @abc.abstractmethod
    def is_open(self):
        pass

    @abc.abstractmethod
    def get_address(self):
        pass

    @abc.abstractmethod
    def send(self, socket_address, packet):
        pass

    @abc.abstractmethod
    def open(self):
        pass

    @abc.abstractmethod
    def close(self, timeout=0.0):
        pass


class EndpointListener(object):
    """
    Handler for messages coming in through an Endpoint.
    """

    __metaclass__ = abc.ABCMeta

    def __init__(self, endpoint):
        """
        Create a new listener.

        """

        self.endpoint = endpoint

        self._netifaces_failed = False
        self.my_estimated_lan = (self._get_lan_address(True)[0], self.endpoint._port)
        self.my_estimated_wan = self.my_estimated_lan

    @abc.abstractmethod
    def on_packet(self, packet):
        """
        Callback for when data is received on this endpoint.

        :param packet: the received packet, in (source, binary string) format.
        """
        pass

    @staticmethod
    def _get_interface_addresses():
        """
        Yields Interface instances for each available AF_INET interface found.

        An Interface instance has the following properties:
        - name          (i.e. "eth0")
        - address       (i.e. "10.148.3.254")
        - netmask       (i.e. "255.255.255.0")
        - broadcast     (i.e. "10.148.3.255")
        """

        class Interface(object):

            def __init__(self, name, address, netmask, broadcast):
                self.name = name
                self.address = address
                self.netmask = netmask
                self.broadcast = broadcast
                self._l_address, = struct.unpack_from(">L", socket.inet_aton(address))
                self._l_netmask, = struct.unpack_from(">L", socket.inet_aton(netmask))

            def __contains__(self, address):
                assert isinstance(address, str), type(address)
                l_address, = struct.unpack_from(">L", socket.inet_aton(address))
                return (l_address & self._l_netmask) == (self._l_address & self._l_netmask)

            def __str__(self):
                return "<{self.__class__.__name__} \"{self.name}\" addr:{self.address} mask:{self.netmask}>".format(
                    self=self)

            def __repr__(self):
                return "<{self.__class__.__name__} \"{self.name}\" addr:{self.address} mask:{self.netmask}>".format(
                    self=self)

        try:
            for interface in netifaces.interfaces():
                try:
                    addresses = netifaces.ifaddresses(interface)

                except ValueError:
                    # some interfaces are given that are invalid, we encountered one called ppp0
                    pass

                else:
                    for option in addresses.get(netifaces.AF_INET, []):
                        try:
                            # On Windows netifaces currently returns IP addresses as unicode,
                            # and on *nix it returns str. So, we convert any unicode objects to str.
                            unicode_to_str = lambda s: s.encode('utf-8') if isinstance(s, unicode) else s
                            yield Interface(interface,
                                            unicode_to_str(option.get("addr")),
                                            unicode_to_str(option.get("netmask")),
                                            unicode_to_str(option.get("broadcast")))

                        except TypeError:
                            # some interfaces have no netmask configured, causing a TypeError when
                            # trying to unpack _l_netmask
                            pass
        except OSError, e:
            logger = logging.getLogger("dispersy")
            logger.warning("failed to check network interfaces, error was: %r", e)

    def _address_in_subnet(self, address, subnet):
        """
        Checks whether a given address is in a given subnet
        :param address: an ip v4 address as a string formatted as four pairs of decimals separated by dots
        :param subnet: a tuple consisting of the main address of the subnet formatted as above, and the subnet formatted as
        an int with the number of significant bits in the address.
        :return: True if the address is in the subnet, False otherwise
        """
        address = struct.unpack_from(">L", socket.inet_aton(address))[0]
        (subnet_main, netmask) = subnet
        subnet_main = struct.unpack_from(">L", socket.inet_aton(subnet_main))[0]
        address >>= 32 - netmask
        subnet_main >>= 32 - netmask
        return address == subnet_main

    def _address_is_lan_without_netifaces(self, address):
        """
        Checks if the given ip address is either our own address or in one of the subnet defined for local network usage
        :param address: ip v4 address to be checked
        :return: True if the adrress is a lan address, False otherwise
        """
        if address == self.get_lan_address_without_netifaces():
            return True
        else:
            lan_subnets = (("192.168.0.0", 16),
                           ("172.16.0.0", 12),
                           ("10.0.0.0", 8))
            return any(self._address_in_subnet(address, subnet) for subnet in lan_subnets)

    def address_is_lan(self, address):
        if self._netifaces_failed:
            return self._address_is_lan_without_netifaces(address)
        else:
            return any(address in interface for interface in self._local_interfaces)

    def get_lan_address_without_netifaces(self):
        """
        # Get the local ip address by creating a socket for a (random) internet ip
        :return: the local ip address
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.0", 80))  # TEST-NET-1, guaranteed to not be connected => no callbacks
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except socket.error as exception:
            return "0.0.0.0"

    def _get_lan_address(self, bootstrap=False):
        """
        Attempt to get the newest lan ip of this machine, preferably with netifaces, but use the fallback if it fails
        :return: lan address
        """
        if self._netifaces_failed:
            return (self.get_lan_address_without_netifaces(), self.endpoint._port)
        else:
            self._local_interfaces = list(self._get_interface_addresses())
            interface = self._guess_lan_address(self._local_interfaces)
            return (interface.address if interface else self.get_lan_address_without_netifaces()), \
                   (0 if bootstrap else self.endpoint._port)

    def _guess_lan_address(self, interfaces, default=None):
        """
        Chooses the most likely Interface instance out of INTERFACES to use as our LAN address.

        INTERFACES can be obtained from _get_interface_addresses()
        DEFAULT is used when no appropriate Interface can be found
        """
        assert isinstance(interfaces, list), type(interfaces)
        blacklist = ["127.0.0.1", "0.0.0.0", "255.255.255.255"]

        # prefer interfaces where we have a broadcast address
        for interface in interfaces:
            if interface.broadcast and interface.address and not interface.address in blacklist:
                return interface

        # Exception for virtual machines/containers
        for interface in interfaces:
            if interface.address and not interface.address in blacklist:
                return interface

        self._netifaces_failed = True
        return default


class IllegalEndpointListenerError(RuntimeError):
    """
    Exception raised when an EndpointListener instance was expected, but not supplied.
    """

    def __init__(self, other):
        message = '%s is not an instance of %s' % (type(other), str(EndpointListener.__name__))
        super(IllegalEndpointListenerError, self).__init__(message)


class EndpointClosedException(Exception):
    """
    Exception raised when an endpoint is expected to be open, but is closed.
    """

    def __init__(self, endpoint):
        super(EndpointClosedException, self).__init__('%s is unexpectedly closed' % type(endpoint))

class DataTooBigException(Exception):
    """
    Exception raised when the data being sent exceeds the maximum size.
    """

    def __init__(self, size, max_size):
        super(DataTooBigException, self).__init__('Tried to send packet of size %s > MAX_SIZE(%d)' % (size, max_size))

class IllegalDestination(Exception):
    """
    Exception raised when trying to send to the 0 address.
    """

    def __init__(self):
        super(IllegalDestination, self).__init__('Attempted to send a message to 0.0.0.0:0.')
