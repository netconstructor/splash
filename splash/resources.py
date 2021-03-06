import os, time, resource, json
from twisted.web.server import NOT_DONE_YET
from twisted.web.resource import Resource
from twisted.internet import reactor, defer
from twisted.python import log
from splash.qtrender2 import HtmlRender, PngRender, IframesRender, RenderError
from splash.utils import getarg, BadRequest, get_num_fds, get_leaks
from splash import sentry


class RenderHtml(Resource):

    isLeaf = True
    content_type = "text/html; charset=utf-8"

    def __init__(self, pool):
        Resource.__init__(self)
        self.pool = pool

    def _getRender(self, request):
        url = getarg(request, "url")
        baseurl = getarg(request, "baseurl", None)
        return self.pool.render(HtmlRender, url, baseurl)

    def render_GET(self, request):
        d = self._getRender(request)
        timeout = getarg(request, "timeout", 30, type=float, range=(0, 60))
        timer = reactor.callLater(timeout, d.cancel)
        d.addCallback(self._cancelTimer, timer)
        d.addCallback(self._writeOutput, request)
        d.addErrback(self._timeoutError, request)
        d.addErrback(self._renderError, request)
        d.addErrback(self._internalError, request)
        d.addBoth(self._finishRequest, request)
        request.starttime = time.time()
        return NOT_DONE_YET

    def render(self, request):
        try:
            return Resource.render(self, request)
        except BadRequest as e:
            request.setResponseCode(400)
            return str(e) + "\n"

    def _cancelTimer(self, _, timer):
        timer.cancel()
        return _

    def _writeOutput(self, html, request):
        stats = {
            "path": request.path,
            "args": request.args,
            "rendertime": time.time() - request.starttime,
            "maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "load": os.getloadavg(),
            "fds": get_num_fds(),
            "active": len(self.pool.active),
            "qsize": len(self.pool.queue.pending),
        }
        log.msg(json.dumps(stats), system="stats")
        request.setHeader("content-type", self.content_type)
        request.write(html)

    def _timeoutError(self, failure, request):
        failure.trap(defer.CancelledError)
        request.setResponseCode(504)
        request.write("Timeout exceeded rendering page\n")

    def _renderError(self, failure, request):
        failure.trap(RenderError)
        request.setResponseCode(502)
        request.write("Error rendering page\n")

    def _internalError(self, failure, request):
        request.setResponseCode(500)
        request.write(failure.getErrorMessage())
        log.err()
        sentry.capture(failure)

    def _finishRequest(self, _, request):
        if not request._disconnected:
            request.finish()


class RenderPng(RenderHtml):

    content_type = "image/png"

    def _getRender(self, request):
        url = getarg(request, "url")
        baseurl = getarg(request, "baseurl", None)
        width = getarg(request, "width", None, type=int, range=(0, 1920))
        height = getarg(request, "height", None, type=int, range=(0, 1080))
        vwidth = getarg(request, "vwidth", 1024, type=int, range=(0, 1920))
        vheight = getarg(request, "vheight", 768, type=int, range=(0, 1080))
        return self.pool.render(PngRender, url, baseurl, width, height, vwidth, vheight)


class RenderIframes(RenderHtml):

    content_type = "application/json"

    def _getRender(self, request):
        url = getarg(request, "url")
        baseurl = getarg(request, "baseurl", None)
        return self.pool.render(IframesRender, url, baseurl)


class Debug(Resource):

    isLeaf = True

    def __init__(self, pool):
        Resource.__init__(self)
        self.pool = pool

    def render_GET(self, request):
        return json.dumps({
            "leaks": get_leaks(),
            "active": [x.url for x in self.pool.active],
            "qsize": len(self.pool.queue.pending),
            "maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "fds": get_num_fds(),
        })


class Root(Resource):

    def __init__(self, pool):
        Resource.__init__(self)
        self.putChild("render.html", RenderHtml(pool))
        self.putChild("render.png", RenderPng(pool))
        self.putChild("iframes.json", RenderIframes(pool))
        self.putChild("debug", Debug(pool))

    def getChild(self, name, request):
        if name == "":
            return self
        return Resource.getChild(self, name, request)

    def render_GET(self, request):
        return ""
