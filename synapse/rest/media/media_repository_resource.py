# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018-2021 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING

from synapse.config._base import ConfigError
from synapse.http.server import JsonResource, HttpServer

from .config_resource import MediaConfigResource
from .download_resource import DownloadResource
from .preview_url_resource import PreviewUrlResource
from .thumbnail_resource import ThumbnailResource
from .upload_resource import UploadResource

if TYPE_CHECKING:
    from synapse.server import HomeServer


class MediaRepositoryResource(JsonResource):
    """File uploading and downloading.

    Uploads are POSTed to a resource which returns a token which is used to GET
    the download::

        => POST /_matrix/media/r0/upload HTTP/1.1
           Content-Type: <media-type>
           Content-Length: <content-length>

           <media>

        <= HTTP/1.1 200 OK
           Content-Type: application/json

           { "content_uri": "mxc://<server-name>/<media-id>" }

        => GET /_matrix/media/r0/download/<server-name>/<media-id> HTTP/1.1

        <= HTTP/1.1 200 OK
           Content-Type: <media-type>
           Content-Disposition: attachment;filename=<upload-filename>

           <media>

    Clients can get thumbnails by supplying a desired width and height and
    thumbnailing method::

        => GET /_matrix/media/r0/thumbnail/<server_name>
                /<media-id>?width=<w>&height=<h>&method=<m> HTTP/1.1

        <= HTTP/1.1 200 OK
           Content-Type: image/jpeg or image/png

           <thumbnail>

    The thumbnail methods are "crop" and "scale". "scale" tries to return an
    image where either the width or the height is smaller than the requested
    size. The client should then scale and letterbox the image if it needs to
    fit within a given rectangle. "crop" tries to return an image where the
    width and height are close to the requested size and the aspect matches
    the requested size. The client should scale the image if it needs to fit
    within a given rectangle.

    This gets mounted at various points under /_matrix/media, including:
       * /_matrix/media/r0
       * /_matrix/media/v1
       * /_matrix/media/v3
    """

    def __init__(self, hs: "HomeServer"):
        # If we're not configured to use it, raise if we somehow got here.
        if not hs.config.media.can_load_media_repo:
            raise ConfigError("Synapse is not configured to use a media repo.")

        JsonResource.__init__(self, hs, canonical_json=False)
        self.register_servlets(self, hs)

    @staticmethod
    def register_servlets(http_server: HttpServer, hs: "HomeServer") -> None:
        media_repo = hs.get_media_repository()

        # Note that many of these should not exist as v1 endpoints, but empirically
        # a lot of traffic still goes to them.

        # TODO Convert the individual resources to use RestServlet and then call
        # FooResource(...).register(...).
        http_server.register_paths(
            "POST",
            [re.compile("/_matrix/media/(r0|v3|v1)/upload")],
            UploadResource(hs, media_repo)._async_render_POST,
            "UploadResource",
        )
        http_server.register_paths(
            "GET",
            [re.compile("/_matrix/media/(r0|v3|v1)/download")],
            DownloadResource(hs, media_repo)._async_render_GET,
            "DownloadResource",
        )
        http_server.register_paths(
            "GET",
            [re.compile("/_matrix/media/(r0|v3|v1)/thumbnail")],
            ThumbnailResource(
                hs, media_repo, media_repo.media_storage
            )._async_render_GET,
            "ThumbnailResource",
        )
        if hs.config.media.url_preview_enabled:
            http_server.register_paths(
                "GET",
                [re.compile("/_matrix/media/(r0|v3|v1)/preview_url")],
                PreviewUrlResource(
                    hs, media_repo, media_repo.media_storage
                )._async_render_GET,
                "PreviewUrlResource",
            )
        http_server.register_paths(
            "GET",
            [re.compile("/_matrix/media/(r0|v3|v1)/config")],
            MediaConfigResource(hs)._async_render_GET,
            "MediaConfigResource",
        )
