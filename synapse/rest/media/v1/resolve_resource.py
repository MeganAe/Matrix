# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import logging
import cgi

from twisted.web.server import NOT_DONE_YET
from twisted.internet import defer
from twisted.web.resource import Resource

import requests

from synapse.http.server import (
    respond_with_json,
    wrap_json_request_handler
)
from synapse.api.errors import (
    SynapseError, Codes,
)
from synapse.http.servlet import parse_json_object_from_request
from synapse.rest.media.v1._base import validate_url_blacklist, \
    parse_content_disposition_filename


logger = logging.getLogger(__name__)


class ResolveResource(Resource):
    isLeaf = True

    def __init__(self, hs, media_repo):
        Resource.__init__(self)

        self.media_repo = media_repo
        self.max_upload_size = hs.config.max_upload_size
        self.url_blacklist = hs.config.url_blacklist
        self.server_name = hs.hostname
        self.auth = hs.get_auth()
        self.clock = hs.get_clock()
        self.version_string = hs.version_string

    def render_POST(self, request):
        self._async_render_POST(request)
        return NOT_DONE_YET

    def render_OPTIONS(self, request):
        respond_with_json(request, 200, {}, send_cors=True)
        return NOT_DONE_YET

    @wrap_json_request_handler
    @defer.inlineCallbacks
    def _async_render_POST(self, request):
        requester = yield self.auth.get_user_by_req(request)

        url = request.args.get("url")[0]
        # should respond with errors in case of the issues
        # on validating resource.
        yield self._validate_resource(url)

        # should receive downloadable resource uri from the media repo
        json_object = yield self._upload_and_preview_url(url, requester)

        respond_with_json(
            request, 200, json_object, send_cors=True
        )

    @defer.inlineCallbacks
    def _upload_and_preview_url(self, url, user):
        response = requests.get(url, allow_redirects=True, stream=True)
        headers = response.headers
        upload_name = parse_content_disposition_filename(headers)
        content_length = headers.get('Content-Length')
        media_type = headers.get("Content-Type")

        print(self.media_repo.__dict__)
        content_uri = yield self.media_repo.create_content(
            media_type, upload_name, response.raw,
            content_length, user
        )

        logger.info("Uploaded content with URI %r", content_uri)

        defer.returnValue({
            "content_uri": content_uri,
            "media_info": {
                "media_type": media_type,
            }
        })

    def _validate_resource(self, url):
        head_response = requests.head(url, allow_redirects=True)
        self._should_have_url(url)
        self._should_be_downloadable(url, head_response)
        self._should_have_allowed_max_upload_size(url, head_response)
        self._should_have_allowed_urls(url)
        return True

    def _should_have_url(self, url):
        if not url:
            raise SynapseError(
                msg="Missing url parameter that should be passed as body of request",
                code=404,
            )

    def _should_have_allowed_urls(self, url):
        if not validate_url_blacklist(self.url_blacklist, url):
            raise SynapseError(
                403, "URL blocked by url pattern blacklist entry",
                Codes.UNKNOWN
            )

    def _should_be_downloadable(self, url, head_response):
        if head_response.status_code is not requests.codes.ok:
            raise SynapseError(
                msg="Not found resource to resolve %r" % (url),
                code=404,
            )

    def _should_have_allowed_max_upload_size(self, url, head_response):
        content_length = head_response.headers.get("Content-Length")
        if content_length is None:
            raise SynapseError(
                msg="Request must specify a Content-Length for %r" % (url),
                code=400
            )

        if int(content_length) > self.max_upload_size:
            raise SynapseError(
                msg="Upload request body is too large for %r" % (url),
                code=413,
            )

        return True
