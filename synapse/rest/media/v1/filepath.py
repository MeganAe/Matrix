# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2020-2021 The Matrix.org Foundation C.I.C.
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

import functools
import os
import re
from typing import Callable, List

NEW_FORMAT_ID_RE = re.compile(r"^\d\d\d\d-\d\d-\d\d")


def _wrap_in_base_path(func: Callable[..., str]) -> Callable[..., str]:
    """Takes a function that returns a relative path and turns it into an
    absolute path based on the location of the primary media store
    """

    @functools.wraps(func)
    def _wrapped(self, *args, **kwargs):
        path = func(self, *args, **kwargs)
        return os.path.join(self.base_path, path)

    return _wrapped


class MediaFilePaths:
    """Describes where files are stored on disk.

    Most of the functions have a `*_rel` variant which returns a file path that
    is relative to the base media store path. This is mainly used when we want
    to write to the backup media store (when one is configured)
    """

    def __init__(self, primary_base_path: str):
        self.base_path = primary_base_path

    def default_thumbnail_rel(
        self,
        default_top_level: str,
        default_sub_type: str,
        width: int,
        height: int,
        content_type: str,
        method: str,
    ) -> str:
        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s-%s" % (width, height, top_level_type, sub_type, method)
        return os.path.join(
            "default_thumbnails", default_top_level, default_sub_type, file_name
        )

    default_thumbnail = _wrap_in_base_path(default_thumbnail_rel)

    def local_media_filepath_rel(self, media_id: str) -> str:
        return os.path.join("local_content", media_id[0:2], media_id[2:4], media_id[4:])

    local_media_filepath = _wrap_in_base_path(local_media_filepath_rel)

    def local_media_thumbnail_rel(
        self, media_id: str, width: int, height: int, content_type: str, method: str
    ) -> str:
        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s-%s" % (width, height, top_level_type, sub_type, method)
        return os.path.join(
            "local_thumbnails", media_id[0:2], media_id[2:4], media_id[4:], file_name
        )

    local_media_thumbnail = _wrap_in_base_path(local_media_thumbnail_rel)

    def local_media_thumbnail_dir(self, media_id: str) -> str:
        """
        Retrieve the local store path of thumbnails of a given media_id

        Args:
            media_id: The media ID to query.
        Returns:
            Path of local_thumbnails from media_id
        """
        return os.path.join(
            self.base_path,
            "local_thumbnails",
            media_id[0:2],
            media_id[2:4],
            media_id[4:],
        )

    def remote_media_filepath_rel(self, server_name: str, file_id: str) -> str:
        return os.path.join(
            "remote_content", server_name, file_id[0:2], file_id[2:4], file_id[4:]
        )

    remote_media_filepath = _wrap_in_base_path(remote_media_filepath_rel)

    def remote_media_thumbnail_rel(
        self,
        server_name: str,
        file_id: str,
        width: int,
        height: int,
        content_type: str,
        method: str,
    ) -> str:
        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s-%s" % (width, height, top_level_type, sub_type, method)
        return os.path.join(
            "remote_thumbnail",
            server_name,
            file_id[0:2],
            file_id[2:4],
            file_id[4:],
            file_name,
        )

    remote_media_thumbnail = _wrap_in_base_path(remote_media_thumbnail_rel)

    # Legacy path that was used to store thumbnails previously.
    # Should be removed after some time, when most of the thumbnails are stored
    # using the new path.
    def remote_media_thumbnail_rel_legacy(
        self, server_name: str, file_id: str, width: int, height: int, content_type: str
    ):
        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s" % (width, height, top_level_type, sub_type)
        return os.path.join(
            "remote_thumbnail",
            server_name,
            file_id[0:2],
            file_id[2:4],
            file_id[4:],
            file_name,
        )

    def remote_media_thumbnail_dir(self, server_name: str, file_id: str) -> str:
        return os.path.join(
            self.base_path,
            "remote_thumbnail",
            server_name,
            file_id[0:2],
            file_id[2:4],
            file_id[4:],
        )

    def url_cache_filepath_rel(self, media_id: str) -> str:
        if NEW_FORMAT_ID_RE.match(media_id):
            # Media id is of the form <DATE><RANDOM_STRING>
            # E.g.: 2017-09-28-fsdRDt24DS234dsf
            return os.path.join("url_cache", media_id[:10], media_id[11:])
        else:
            return os.path.join("url_cache", media_id[0:2], media_id[2:4], media_id[4:])

    url_cache_filepath = _wrap_in_base_path(url_cache_filepath_rel)

    def url_cache_filepath_dirs_to_delete(self, media_id: str) -> List[str]:
        "The dirs to try and remove if we delete the media_id file"
        if NEW_FORMAT_ID_RE.match(media_id):
            return [os.path.join(self.base_path, "url_cache", media_id[:10])]
        else:
            return [
                os.path.join(self.base_path, "url_cache", media_id[0:2], media_id[2:4]),
                os.path.join(self.base_path, "url_cache", media_id[0:2]),
            ]

    def url_cache_thumbnail_rel(
        self, media_id: str, width: int, height: int, content_type: str, method: str
    ) -> str:
        # Media id is of the form <DATE><RANDOM_STRING>
        # E.g.: 2017-09-28-fsdRDt24DS234dsf

        top_level_type, sub_type = content_type.split("/")
        file_name = "%i-%i-%s-%s-%s" % (width, height, top_level_type, sub_type, method)

        if NEW_FORMAT_ID_RE.match(media_id):
            return os.path.join(
                "url_cache_thumbnails", media_id[:10], media_id[11:], file_name
            )
        else:
            return os.path.join(
                "url_cache_thumbnails",
                media_id[0:2],
                media_id[2:4],
                media_id[4:],
                file_name,
            )

    url_cache_thumbnail = _wrap_in_base_path(url_cache_thumbnail_rel)

    def url_cache_thumbnail_directory(self, media_id: str) -> str:
        # Media id is of the form <DATE><RANDOM_STRING>
        # E.g.: 2017-09-28-fsdRDt24DS234dsf

        if NEW_FORMAT_ID_RE.match(media_id):
            return os.path.join(
                self.base_path, "url_cache_thumbnails", media_id[:10], media_id[11:]
            )
        else:
            return os.path.join(
                self.base_path,
                "url_cache_thumbnails",
                media_id[0:2],
                media_id[2:4],
                media_id[4:],
            )

    def url_cache_thumbnail_dirs_to_delete(self, media_id: str) -> List[str]:
        "The dirs to try and remove if we delete the media_id thumbnails"
        # Media id is of the form <DATE><RANDOM_STRING>
        # E.g.: 2017-09-28-fsdRDt24DS234dsf
        if NEW_FORMAT_ID_RE.match(media_id):
            return [
                os.path.join(
                    self.base_path, "url_cache_thumbnails", media_id[:10], media_id[11:]
                ),
                os.path.join(self.base_path, "url_cache_thumbnails", media_id[:10]),
            ]
        else:
            return [
                os.path.join(
                    self.base_path,
                    "url_cache_thumbnails",
                    media_id[0:2],
                    media_id[2:4],
                    media_id[4:],
                ),
                os.path.join(
                    self.base_path, "url_cache_thumbnails", media_id[0:2], media_id[2:4]
                ),
                os.path.join(self.base_path, "url_cache_thumbnails", media_id[0:2]),
            ]
