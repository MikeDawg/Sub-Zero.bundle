# coding=utf-8
import certifi
import ssl
import os
import socket
import logging
import requests
import xmlrpclib

from xmlrpclib import SafeTransport, ProtocolError, Fault, Transport
from requests import Session, exceptions
from retry.api import retry_call

from subzero.lib.io import get_viable_encoding

logger = logging.getLogger(__name__)
pem_file = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(unicode(__file__, get_viable_encoding()))), "..", certifi.where()))
try:
    default_ssl_context = ssl.create_default_context(cafile=pem_file)
except AttributeError:
    # < Python 2.7.9
    default_ssl_context = None


class RetryingSession(Session):
    proxied_functions = ("get", "post")

    def __init__(self):
        super(RetryingSession, self).__init__()
        self.verify = pem_file

        proxy = os.environ.get('SZ_HTTP_PROXY')
        if proxy:
            self.proxies = {
                "http": proxy,
                "https": proxy
            }

    def retry_method(self, method, *args, **kwargs):
        if self.proxies:
            # fixme: may be a little loud
            logger.debug("Using proxy %s for: %s", self.proxies["http"], args[0])

        return retry_call(getattr(super(RetryingSession, self), method), fargs=args, fkwargs=kwargs, tries=3, delay=5,
                          exceptions=(exceptions.ConnectionError,
                                      exceptions.ProxyError,
                                      exceptions.SSLError,
                                      exceptions.Timeout,
                                      exceptions.ConnectTimeout,
                                      exceptions.ReadTimeout,
                                      socket.timeout))

    def get(self, *args, **kwargs):
        if self.proxies and "timeout" in kwargs and kwargs["timeout"]:
            kwargs["timeout"] = kwargs["timeout"] * 3
        return self.retry_method("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        if self.proxies and "timeout" in kwargs and kwargs["timeout"]:
            kwargs["timeout"] = kwargs["timeout"] * 3
        return self.retry_method("post", *args, **kwargs)


class TimeoutTransport(Transport):
    """Timeout support for ``xmlrpc.client.SafeTransport``."""
    def __init__(self, timeout, *args, **kwargs):
        Transport.__init__(self, *args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        c = Transport.make_connection(self, host)
        c.timeout = self.timeout

        return c


class SubZeroTransport(SafeTransport):
    """
    Timeout and proxy support for ``xmlrpc.client.(Safe)Transport``
    """
    def __init__(self, timeout, url, *args, **kwargs):
        SafeTransport.__init__(self, *args, **kwargs)
        self.timeout = timeout
        self.host = None
        self.proxy = None
        self.scheme = url.split('://', 1)[0]
        self.https = url.startswith('https')
        self.proxy = os.environ.get('SZ_HTTP_PROXY')

        if self.https:
            self.context = default_ssl_context

        if self.proxy:
            logger.debug("Using proxy %s for: %s", self.proxy, url)
            self.https = self.proxy.startswith('https')

            if self.timeout:
                self.timeout = self.timeout * 3

    def make_connection(self, host):
        self.host = host
        if self.proxy:
            host = self.proxy.split('://', 1)[-1]
        if self.https:
            c = SafeTransport.make_connection(self, host)
        else:
            c = Transport.make_connection(self, host)

        c.timeout = self.timeout

        return c

    def send_request(self, connection, handler, request_body):
        handler = '%s://%s%s' % (self.scheme, self.host, handler)
        Transport.send_request(self, connection, handler, request_body)


class SubZeroRequestsTransport(xmlrpclib.SafeTransport):
    """
    Drop in Transport for xmlrpclib that uses Requests instead of httplib

    Based on: https://gist.github.com/chrisguitarguy/2354951#gistcomment-2388906

    """
    # change our user agent to reflect Requests
    user_agent = "Python XMLRPC with Requests (python-requests.org)"
    proxies = None

    def __init__(self, use_https=True, verify=None, user_agent=None, timeout=10, *args, **kwargs):
        self.verify = pem_file if verify is None else verify
        self.use_https = use_https
        self.user_agent = user_agent if user_agent is not None else self.user_agent
        self.timeout = timeout
        proxy = os.environ.get('SZ_HTTP_PROXY')
        if proxy:
            self.proxies = {
                "http": proxy,
                "https": proxy
            }

        xmlrpclib.SafeTransport.__init__(self, *args, **kwargs)

    def request(self, host, handler, request_body, verbose=0):
        """
        Make an xmlrpc request.
        """
        headers = {'User-Agent': self.user_agent}
        url = self._build_url(host, handler)
        try:
            resp = requests.post(url, data=request_body, headers=headers,
                                 stream=True, timeout=self.timeout, proxies=self.proxies,
                                 verify=self.verify)
        except ValueError:
            raise
        except Exception:
            raise  # something went wrong
        else:
            try:
                resp.raise_for_status()
            except requests.RequestException as e:
                raise xmlrpclib.ProtocolError(url, resp.status_code,
                                              str(e), resp.headers)
            else:
                self.verbose = verbose
                return self.parse_response(resp.raw)

    def _build_url(self, host, handler):
        """
        Build a url for our request based on the host, handler and use_http
        property
        """
        scheme = 'https' if self.use_https else 'http'
        handler = handler[1:] if handler and handler[0] == "/" else handler
        return '%s://%s/%s' % (scheme, host, handler)
