import datetime
import hashlib
import io
import random
from typing import List, Dict, Tuple

import bitarray
import trio

import bencode
import peer
import torrent as state
import tracker


class Engine(object):
    def __init__(self, torrent: state.Torrent) -> None:
        self._state = torrent
        # interact with self
        self._peers_without_connection = trio.Queue(100)
        # interact with FileManager
        self._complete_pieces_to_write = trio.Queue(100) # TODO remove magic number
        self._write_confirmation       = trio.Queue(100) # TODO remove magic number
        # interact with peer connections 
        self._msg_from_peer            = trio.Queue(100) # TODO remove magic number
        # queues for sending TO peers are initialized on a per-peer basis
        #self._queues_for_peers: Dict[state.Peer,trio.Queue] = dict()
        self._peers: Dict[state.PeerAddress, state.PeerState] = dict()
        # data received but not written to disk
        self._received_blocks: Dict[int, List[Tuple[int,bytes]]] = {}

    @property
    def msg_from_peer(self) -> trio.Queue:
        return self._msg_from_peer

    async def run(self):
        async with trio.open_nursery() as nursery:
            nursery.start_soon(self.peer_clients_loop)
            nursery.start_soon(self.peer_server_loop)
            nursery.start_soon(self.tracker_loop)
            nursery.start_soon(self.peer_messages_loop)

    async def tracker_loop(self):
        new = True
        while True:
            start_time = trio.current_time()
            event = b'started' if new else None
            raw_tracker_info = await tracker.query(self._state, event)
            tracker_info = bencode.parse_value(io.BytesIO(raw_tracker_info))
            # update peers
            # TODO we could recieve peers in a different format
            peer_ips_and_ports = bencode.parse_peers(tracker_info[b'peers']) 
            peers = [state.PeerAddress(ip, port) for ip, port, _ in peer_ips_and_ports]
            print('Found peers: {}'.format(peers))
            await self.update_peers(peers)
            # update other info: 
            #self._state.complete_peers = tracker_info['complete']
            #self._state.incomplete_peers = tracker_info['incomplete']
            #self._state.interval = int(tracker_info['interval'])
            # tell tracker the new interval
            await trio.sleep_until(start_time + self._state.interval)
            new = False

    async def peer_server_loop(self):
        await trio.serve_tcp(peer.make_handler(self), self._state.listening_port)

    async def peer_clients_loop(self):
        '''
        Start up clients for new peers that are not from the serve.
        '''
        print('Peer client loop!!!')
        async with trio.open_nursery() as nursery:
            while True:
                (peer_id, peer_state) = await self._peers_without_connection.get()
                nursery.start_soon(peer.make_standalone, self, peer_id, peer_state)

    async def update_peers(self, peers: List[state.PeerAddress]) -> None:
        for p in peers:
            await self.get_or_add_peer(p, peer.PeerType.CLIENT)

    async def get_or_add_peer(self, address: state.PeerAddress, peer_type=peer.PeerType, peer_id=None) -> state.PeerState:
        # 1. get or create PeerState
        if address in self._peers:
            return self._peers[address]
        else:
            now = datetime.datetime.now()
            pieces = bitarray.bitarray(self._state._num_pieces)
            pieces.setall(False)
            peer_state = state.PeerState(pieces, now, peer_id=peer_id)
            self._peers[address] = peer_state
        # 2. start connection if needed
        # If server, then connection already exists
        if peer_type == peer.PeerType.SERVER:
            return peer_state
        elif peer_type == peer.PeerType.CLIENT:
            await self._peers_without_connection.put((address, peer_state))
            return peer_state
        else:
            assert(False)

    def _blocks_from_index(self, index):
        piece_length = self._state.piece_length
        block_length = min(piece_length, 1024 * 8)
        begin_indexes = list(range(0, piece_length, block_length))
        return [ (index, begin, min(block_length, piece_length-begin))
                for begin in begin_indexes ]

    async def update_peer_requests(self):
        # Look at what the client has, what the peers have
        # and update the requested pieces for each peer.
        for peer_state in self._peers.values():
            # TODO don't read private field of another object
            targets = (~self._state._complete) & peer_state._pieces
            indexes = [i for i, b in enumerate(targets) if b]
            random.shuffle(indexes)
            if indexes:
                blocks_to_request = self._blocks_from_index(indexes[0])
                print('Blocks to request: {}'.format(blocks_to_request))
                await peer_state.to_send_queue.put(("blocks_to_request", blocks_to_request))

    async def handle_peer_message(self, peer_state, msg_type, msg_payload):
        if msg_type == peer.PeerMsg.CHOKE:
            print('Got CHOKE') # TODO
        elif msg_type == peer.PeerMsg.UNCHOKE:
            print('Got UNCHOKE') # TODO
        elif msg_type == peer.PeerMsg.INTERESTED:
            print('Got INTERESTED') # TODO
        elif msg_type == peer.PeerMsg.NOT_INTERESTED:
            print('Got NOT_INTERESTED') # TODO
        elif msg_type == peer.PeerMsg.HAVE:
            print('Got HAVE')
            index: int = peer.parse_have(msg_payload)
            peer_state.get_pieces()[index] = True
        elif msg_type == peer.PeerMsg.BITFIELD:
            print('Got BITFIELD')
            bitfield = peer.parse_bitfield(msg_payload)
            peer_state.set_pieces(bitfield)
        elif msg_type == peer.PeerMsg.REQUEST:
            print('Got REQUEST') # TODO
            reqest_info = peer.parse_request_or_cancel(msg_payload)
            #self._peer_state.add_request(request_info)
        elif msg_type == peer.peer.PeerMsg.PIECE:
            print('Got PIECE')
            (index, begin, data) = peer.parse_piece(msg_payload)
            self.handle_block_received(index, begin, data)
            #self._torrent.add_piece(index, begin, data)
        elif msg_type == peer.PeerMsg.CANCEL:
            print('Got CANCEL') # TODO
            request_info = peer.parse_request_or_cancel(msg_payload)
            #self._peer_state.cancel_request(request_info)
        else:
            # TODO - Exceptions are bad here!
            print('Bad message: length = {}'.format(length))
            print('Bad message: data = {}'.format(data))
            raise Exception('bad peer message')

    async def handle_block_received(self, index: int, begin: int, data: bytes) -> None:
        if index not in self._received_blocks:
            self._received_blocks[index] = []
        blocks = self._received_blocks[index]
        blocks.append((begin, data))
        piece_data = b''
        for offset, block_data in blocks:
            if offset == len(piece_data):
                piece_data = piece_data + block_data
            else:
                break
        piece_info = self._state.piece_info(index)
        if len(piece_data) == self._state._piece_length:
            if hashlib.sha1(piece_data).digest() == piece_info.sha1:
                await self._complete_pieces_to_write.put((index, piece_data))
                self._received_blocks.pop(index)
            else:
                raise Exception('sha1hash does not match for index {}'.format(index))

    async def peer_messages_loop(self):
        peer_state, msg_type, msg_payload = await self._msg_from_peer.get() 
        #
        print('Engine: recieved peer message')
        await self.handle_peer_message(peer_state, msg_type, msg_payload)
        await self.update_peer_requests()


#async def main_loop(torrent):
#    raw_tracker_info = await tracker.query(torrent)
#    tracker_info = bencode.parse_value(io.BytesIO(raw_tracker_info))
#    # TODO we could recieve peers in a different format
#    peers = bencode.parse_compact_peers(tracker_info[b'peers']) 
#    print(tracker_info)
#    print(peers)
#    for ip, port in peers:
#        peer = tstate.Peer(ip, port)
#        torrent.add_peer(peer)

def run(torrent):
    engine = Engine(torrent)
    trio.run(engine.run)
