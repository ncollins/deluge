import hashlib
import logging
import os
from typing import Any

logger = logging.getLogger("file_manager")

import trio

import torrent as tstate


def _create_empty_file(path, torrent):
    with open(path, "wb") as f:
        for i in range(torrent._num_pieces):  # TODO remove private property access
            b = bytes(torrent.piece_length(i))
            f.write(b)


class FileWrapper(object):
    def __init__(self, *, torrent: tstate.Torrent, file_suffix: str = "") -> None:
        self._torrent = torrent
        self._tmp_path = torrent.file_path + file_suffix + ".part"
        self._final_path = torrent.file_path + file_suffix
        self._file_path = None
        self._file: Any = None

    def create_file_or_return_hashes(self):
        if os.path.exists(self._final_path):
            self._file_path = self._final_path
        else:
            self._file_path = self._tmp_path
        try:
            self._file = open(self._file_path, "rb")
            hashes = []
            for i, _ in enumerate(self._torrent._complete):
                l = self._torrent.piece_length(i)
                p = self.read_block(i, 0, l)
                h = hashlib.sha1(p).digest()
                hashes.append(h)
            self._file.close()
        except FileNotFoundError:
            _create_empty_file(self._file_path, self._torrent)  # TODO don't read private property
            hashes = None
        self._file = open(self._file_path, "rb+")
        return hashes

    def write_piece(self, index: int, piece: bytes) -> None:
        start = index * self._torrent._piece_length  # TODO
        self._file.seek(start)
        self._file.write(piece)
        self._file.flush()

    def read_block(self, index: int, begin: int, length: int) -> bytes:
        start = index * self._torrent._piece_length + begin
        self._file.seek(start)
        block = self._file.read(length)
        return block

    def move_file_to_final_location(self):
        if self._file_path != self._final_path:
            self._file.close()
            os.rename(self._file_path, self._final_path)
            logger.info("Moved {} to {}".format(self._file_path, self._final_path))
            self._file_path = self._final_path
            self._file = open(self._file_path, "rb+")


class FileManager(object):
    def __init__(
        self,
        *,
        file_wrapper: FileWrapper,
        pieces_to_write: trio.MemoryReceiveChannel,
        write_confirmations: trio.MemorySendChannel,
        blocks_to_read: trio.MemoryReceiveChannel,
        blocks_for_peers: trio.MemorySendChannel,
    ) -> None:
        self._file_wrapper = file_wrapper
        self._pieces_to_write = pieces_to_write
        self._write_confirmations = write_confirmations
        self._blocks_to_read = blocks_to_read
        self._blocks_for_peers = blocks_for_peers

    # async def move_file_to_final_location(self):
    #    self._file_wrapper.move_file_to_final_location()

    async def run(self):
        async with trio.open_nursery() as nursery:
            nursery.start_soon(self.piece_writing_loop)
            nursery.start_soon(self.block_reading_loop)

    async def piece_writing_loop(self):
        while True:
            index, piece = await self._pieces_to_write.receive()
            if (index is None) and (piece is None):  # TODO better msg types
                self._file_wrapper.move_file_to_final_location()
            else:
                self._file_wrapper.write_piece(index, piece)
                logger.info("Wrote #{} to disk".format(index))
                await self._write_confirmations.send(index)

    async def block_reading_loop(self):
        while True:
            who, (index, begin, length) = await self._blocks_to_read.receive()
            block = self._file_wrapper.read_block(index, begin, length)
            await self._blocks_for_peers.send((who, (index, begin, length), block))
