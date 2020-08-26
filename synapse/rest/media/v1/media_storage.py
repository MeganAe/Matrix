# -*- coding: utf-8 -*-
# Copyright 2018 New Vecotr Ltd
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
import contextlib
import logging
import os
import shutil
from typing import IO, TYPE_CHECKING, Any, Optional, Sequence

from twisted.protocols.basic import FileSender

from synapse.logging.context import defer_to_thread, make_deferred_yieldable
from synapse.util.file_consumer import BackgroundFileConsumer

from ._base import FileInfo, Responder
from .filepath import MediaFilePaths

if TYPE_CHECKING:
    from synapse.server import HomeServer

    from .storage_provider import StorageProviderWrapper

logger = logging.getLogger(__name__)


class MediaStorage(object):
    """Responsible for storing/fetching files from local sources.

    Args:
        hs
        local_media_directory: Base path where we store media on disk
        filepaths
        storage_providers: List of StorageProvider that are used to fetch and store files.
    """

    def __init__(
        self,
        hs: "HomeServer",
        local_media_directory: str,
        filepaths: MediaFilePaths,
        storage_providers: Sequence["StorageProviderWrapper"],
    ):
        self.hs = hs
        self.local_media_directory = local_media_directory
        self.filepaths = filepaths
        self.storage_providers = storage_providers

    async def store_file(self, source: IO, file_info: FileInfo) -> str:
        """Write `source` to the on disk media store, and also any other
        configured storage providers

        Args:
            source: A file like object that should be written
            file_info: Info about the file to store

        Returns:
            the file path written to in the primary media store
        """

        with self.store_into_file(file_info) as (f, fname, finish_cb):
            # Write to the main repository
            await defer_to_thread(
                self.hs.get_reactor(), _write_file_synchronously, source, f
            )
            await finish_cb()

        return fname

    @contextlib.contextmanager
    def store_into_file(self, file_info: FileInfo):
        """Context manager used to get a file like object to write into, as
        described by file_info.

        Actually yields a 3-tuple (file, fname, finish_cb), where file is a file
        like object that can be written to, fname is the absolute path of file
        on disk, and finish_cb is a function that returns an awaitable.

        fname can be used to read the contents from after upload, e.g. to
        generate thumbnails.

        finish_cb must be called and waited on after the file has been
        successfully been written to. Should not be called if there was an
        error.

        Args:
            file_info: Info about the file to store

        Example:

            with media_storage.store_into_file(info) as (f, fname, finish_cb):
                # .. write into f ...
                await finish_cb()
        """

        path = self._file_info_to_path(file_info)
        fname = os.path.join(self.local_media_directory, path)

        dirname = os.path.dirname(fname)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        finished_called = [False]

        async def finish():
            for provider in self.storage_providers:
                await provider.store_file(path, file_info)

            finished_called[0] = True

        try:
            with open(fname, "wb") as f:
                yield f, fname, finish
        except Exception:
            try:
                os.remove(fname)
            except Exception:
                pass
            raise

        if not finished_called:
            raise Exception("Finished callback not called")

    async def fetch_media(self, file_info: FileInfo) -> Optional[Responder]:
        """Attempts to fetch media described by file_info from the local cache
        and configured storage providers.

        Args:
            file_info

        Returns:
            Returns a Responder if the file was found, otherwise None.
        """

        path = self._file_info_to_path(file_info)
        local_path = os.path.join(self.local_media_directory, path)
        if os.path.exists(local_path):
            return FileResponder(open(local_path, "rb"))

        for provider in self.storage_providers:
            res = await provider.fetch(path, file_info)  # type: Any
            if res:
                logger.debug("Streaming %s from %s", path, provider)
                return res

        return None

    async def ensure_media_is_in_local_cache(self, file_info: FileInfo) -> str:
        """Ensures that the given file is in the local cache. Attempts to
        download it from storage providers if it isn't.

        Args:
            file_info

        Returns:
            Full path to local file
        """
        path = self._file_info_to_path(file_info)
        local_path = os.path.join(self.local_media_directory, path)
        if os.path.exists(local_path):
            return local_path

        dirname = os.path.dirname(local_path)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        for provider in self.storage_providers:
            res = await provider.fetch(path, file_info)  # type: Any
            if res:
                with res:
                    consumer = BackgroundFileConsumer(
                        open(local_path, "wb"), self.hs.get_reactor()
                    )
                    await res.write_to_consumer(consumer)
                    await consumer.wait()
                return local_path

        raise Exception("file could not be found")

    def _file_info_to_path(self, file_info: FileInfo) -> str:
        """Converts file_info into a relative path.

        The path is suitable for storing files under a directory, e.g. used to
        store files on local FS under the base media repository directory.
        """
        if file_info.url_cache:
            if file_info.thumbnail:
                return self.filepaths.url_cache_thumbnail_rel(
                    media_id=file_info.file_id,
                    width=file_info.thumbnail_width,
                    height=file_info.thumbnail_height,
                    content_type=file_info.thumbnail_type,
                    method=file_info.thumbnail_method,
                )
            return self.filepaths.url_cache_filepath_rel(file_info.file_id)

        if file_info.server_name:
            if file_info.thumbnail:
                return self.filepaths.remote_media_thumbnail_rel(
                    server_name=file_info.server_name,
                    file_id=file_info.file_id,
                    width=file_info.thumbnail_width,
                    height=file_info.thumbnail_height,
                    content_type=file_info.thumbnail_type,
                    method=file_info.thumbnail_method,
                )
            return self.filepaths.remote_media_filepath_rel(
                file_info.server_name, file_info.file_id
            )

        if file_info.thumbnail:
            return self.filepaths.local_media_thumbnail_rel(
                media_id=file_info.file_id,
                width=file_info.thumbnail_width,
                height=file_info.thumbnail_height,
                content_type=file_info.thumbnail_type,
                method=file_info.thumbnail_method,
            )
        return self.filepaths.local_media_filepath_rel(file_info.file_id)


def _write_file_synchronously(source, dest):
    """Write `source` to the file like `dest` synchronously. Should be called
    from a thread.

    Args:
        source: A file like object that's to be written
        dest: A file like object to be written to
    """
    source.seek(0)  # Ensure we read from the start of the file
    shutil.copyfileobj(source, dest)


class FileResponder(Responder):
    """Wraps an open file that can be sent to a request.

    Args:
        open_file (file): A file like object to be streamed ot the client,
            is closed when finished streaming.
    """

    def __init__(self, open_file):
        self.open_file = open_file

    def write_to_consumer(self, consumer):
        return make_deferred_yieldable(
            FileSender().beginFileTransfer(self.open_file, consumer)
        )

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.open_file.close()
