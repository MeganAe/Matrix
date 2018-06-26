from io import BytesIO

import attr
import json
from six import text_type

from twisted.python.failure import Failure
from twisted.internet.defer import Deferred
from twisted.test.proto_helpers import MemoryReactorClock

from synapse.http.site import SynapseRequest
from twisted.internet import threads
from tests.utils import setup_test_homeserver as _sth


@attr.s
class FakeChannel(object):

    result = attr.ib(factory=dict)

    @property
    def json_body(self):
        if not self.result:
            raise Exception("No result yet.")
        return json.loads(self.result["body"])

    def writeHeaders(self, version, code, reason, headers):
        self.result["version"] = version
        self.result["code"] = code
        self.result["reason"] = reason
        self.result["headers"] = headers

    def write(self, content):
        if "body" not in self.result:
            self.result["body"] = b""

        self.result["body"] += content

    def requestDone(self, _self):
        self.result["done"] = True

    def getPeer(self):
        return None

    def getHost(self):
        return None

    @property
    def transport(self):
        return self


class FakeSite:

    server_version_string = b"1"
    site_tag = "test"

    @property
    def access_logger(self):
        class FakeLogger:
            def info(self, *args, **kwargs):
                pass

        return FakeLogger()


def make_request(method, path, content=b""):

    if isinstance(content, text_type):
        content = content.encode('utf8')

    site = FakeSite()
    channel = FakeChannel()

    req = SynapseRequest(site, channel)
    req.process = lambda: b""
    req.content = BytesIO(content)
    req.requestReceived(method, path, b"1.1")

    return req, channel


def wait_until_result(clock, channel, timeout=100):
    """
    Wait until the channel has a result.
    """
    clock.run()
    x = 0

    while not channel.result:
        x += 1

        if x > timeout:
            raise Exception("Timed out waiting for request to finish.")

        clock.advance(0.1)


class ThreadedMemoryReactorClock(MemoryReactorClock):
    def callFromThread(self, callback, *args, **kwargs):
        d = Deferred()
        d.addCallback(lambda x: callback(*args, **kwargs))
        self.callLater(0, d.callback, True)
        return d


def setup_test_homeserver(*args, **kwargs):

    d = _sth(*args, **kwargs).result

    # Make the pool synchronous.
    clock = d.get_clock()
    pool = d.get_db_pool()

    def runWithConnection(func, *args, **kwargs):
        return threads.deferToThreadPool(
            pool._reactor,
            pool.threadpool,
            pool._runWithConnection,
            func,
            *args,
            **kwargs
        )

    def runInteraction(interaction, *args, **kwargs):
        return threads.deferToThreadPool(
            pool._reactor,
            pool.threadpool,
            pool._runInteraction,
            interaction,
            *args,
            **kwargs
        )

    pool.runWithConnection = runWithConnection
    pool.runInteraction = runInteraction

    class ThreadPool:
        def start(self):
            pass

        def callInThreadWithCallback(self, onResult, function, *args, **kwargs):
            def _(res):
                if isinstance(res, Failure):
                    onResult(False, res)
                else:
                    onResult(True, res)

            d = Deferred()
            d.addCallback(lambda x: function(*args, **kwargs))
            d.addBoth(_)
            clock._reactor.callLater(0, d.callback, True)
            return d

    clock.threadpool = ThreadPool()
    pool.threadpool = ThreadPool()
    return d
