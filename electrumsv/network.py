# ElectrumSV - lightweight Bitcoin SV client
# Copyright (C) 2019 The ElectrumSV Developers
# Copyright (c) 2011-2016 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from collections import defaultdict
from contextlib import suppress
from enum import IntEnum
from functools import partial
import os
import random
import re
import ssl
import stat
import threading
import time
from typing import List

import certifi
from aiorpcx import (
    connect_rs, RPCSession, Notification, BatchError, RPCError, CancelledError, SOCKSError,
    TaskTimeout, TaskGroup, handler_invocation, sleep, ignore_after, timeout_after,
    SOCKS4a, SOCKS5, SOCKSProxy, SOCKSUserAuth
)
from bitcoinx import (
    MissingHeader, IncorrectBits, InsufficientPoW, hex_str_to_hash, hash_to_hex_str,
    sha256, double_sha256
)

from .app_state import app_state
from .bitcoin import scripthash_hex
from .i18n import _
from .logs import logs
from .transaction import Transaction
from .util import JSON, protocol_tuple, version_string
from .networks import Net
from .version import PACKAGE_VERSION, PROTOCOL_MIN, PROTOCOL_MAX


logger = logs.get_logger("network")

HEADER_SIZE = 80
ONE_MINUTE = 60
ONE_DAY = 24 * 3600
HEADERS_SUBSCRIBE = 'blockchain.headers.subscribe'
REQUEST_MERKLE_PROOF = 'blockchain.transaction.get_merkle'
SCRIPTHASH_HISTORY = 'blockchain.scripthash.get_history'
SCRIPTHASH_SUBSCRIBE = 'blockchain.scripthash.subscribe'
SCRIPTHASH_UNSUBSCRIBE = 'blockchain.scripthash.unsubscribe'
BROADCAST_TX_MSG_LIST = (
    ('dust', _('very small "dust" payments')),
    (('Missing inputs', 'Inputs unavailable', 'bad-txns-inputs-spent'),
     _('missing, already-spent, or otherwise invalid coins')),
    ('insufficient priority', _('insufficient fees or priority')),
    ('bad-txns-premature-spend-of-coinbase', _('attempt to spend an unmatured coinbase')),
    (('txn-already-in-mempool', 'txn-already-known'),
     _("it already exists in the server's mempool")),
    ('txn-mempool-conflict', _("it conflicts with one already in the server's mempool")),
    ('bad-txns-nonstandard-inputs', _('use of non-standard input scripts')),
    ('absurdly-high-fee', _('fee is absurdly high')),
    ('non-mandatory-script-verify-flag', _('the script fails verification')),
    ('tx-size', _('transaction is too large')),
    ('scriptsig-size', _('it contains an oversized script')),
    ('scriptpubkey', _('it contains a non-standard signature')),
    ('bare-multisig', _('it contains a bare multisig input')),
    ('multi-op-return', _('it contains more than 1 OP_RETURN input')),
    ('scriptsig-not-pushonly', _('a scriptsig is not simply data')),
    ('bad-txns-nonfinal', _("transaction is not final"))
)


def broadcast_failure_reason(exception):
    if isinstance(exception, RPCError):
        msg = exception.message
        for in_msgs, out_msg in BROADCAST_TX_MSG_LIST:
            if isinstance(in_msgs, str):
                in_msgs = (in_msgs, )
            if any(in_msg in msg for in_msg in in_msgs):
                return out_msg
    return _('reason unknown')


class SwitchReason(IntEnum):
    '''The reason the main server was changed.'''
    disconnected = 0
    lagging = 1
    user_set = 2


def _require_list(obj):
    assert isinstance(obj, (tuple, list))
    return obj


def _require_number(obj):
    assert isinstance(obj, (int, float))
    return obj


def _require_string(obj):
    assert isinstance(obj, str)
    return obj


def _history_status(history):
    if not history:
        return None
    status = ''.join(f'{tx_hash}:{tx_height}:' for tx_hash, tx_height in history)
    return sha256(status.encode()).hex()


def _root_from_proof(hash, branch, index):
    '''From ElectrumX.'''
    for elt in branch:
        if index & 1:
            hash = double_sha256(elt + hash)
        else:
            hash = double_sha256(hash + elt)
        index >>= 1
    if index:
        raise ValueError(f'index {index} out of range for proof of length {len(branch)}')
    return hash


class DisconnectSessionError(Exception):

    def __init__(self, reason, *, blacklist=False):
        super().__init__(reason)
        self.blacklist = False


class SVServerState:
    '''The run-time state of an SVServer.'''

    def __init__(self):
        self.banner = ''
        self.donation_address = ''
        self.last_try = 0
        self.last_good = 0
        self.last_blacklisted = 0
        self.retry_delay = 0

    def can_retry(self, now):
        return not self.is_blacklisted(now) and self.last_try + self.retry_delay < now

    def is_blacklisted(self, now):
        return self.last_blacklisted > now - ONE_DAY

    def to_json(self):
        return {
            'last_try': int(self.last_try),
            'last_good': int(self.last_good),
            'last_blacklisted': int(self.last_blacklisted),
        }

    @classmethod
    def from_json(cls, dct):
        result = cls()
        for attr, value in dct.items():
            setattr(result, attr, value)
        return result

    def __str__(self):
        return str(self.to_json())


class SVServer:
    '''A smart wrapper around a (host, port, protocol) tuple.'''

    all_servers = {}

    def __init__(self, host, port, protocol):
        if not isinstance(host, str) or not host:
            raise ValueError(f'bad host: {host}')
        if not isinstance(port, int):
            raise ValueError(f'bad port: {port}')
        if protocol not in 'st':
            raise ValueError(f'unknown protocol: {protocol}')
        key = (host, port, protocol)
        assert key not in SVServer.all_servers
        SVServer.all_servers[key] = self
        # API attributes
        self.host = host
        self.port = port
        self.protocol = protocol
        self.state = SVServerState()

    @classmethod
    def unique(cls, host, port, protocol):
        if isinstance(port, str):
            with suppress(ValueError):
                port = int(port)
        key = (host, port, protocol)
        obj = cls.all_servers.get(key)
        if not obj:
            obj = cls(host, port, protocol)
        return obj

    def _sslc(self):
        if self.protocol != 's':
            return None
        # FIXME: implement certificate pinning like Electrum?
        return ssl.SSLContext(ssl.PROTOCOL_TLS)

    def _connector(self, session_factory, proxy):
        return connect_rs(self.host, self.port, proxy=proxy, session_factory=session_factory,
                          ssl=self._sslc())

    def _logger(self, n):
        logger_name = f'[{self.host}:{self.port} {self.protocol_text()} #{n}]'
        return logs.get_logger(logger_name)

    def to_json(self):
        return (self.host, self.port, self.protocol, self.state)

    @classmethod
    def from_string(cls, s):
        parts = s.split(':', 3)
        return cls.unique(*parts)

    @classmethod
    def from_json(cls, hpps):
        host, port, protocol, state = hpps
        result = cls.unique(host, port, protocol)
        result.state = state
        return result

    async def connect(self, network, n):
        '''Raises: OSError'''
        await sleep(self.state.retry_delay)
        self.state.retry_delay = max(10, min(self.state.retry_delay * 2 + 1, 600))
        logger = self._logger(n)
        logger.info('connecting...')

        self.state.last_try = time.time()
        session_factory = partial(SVSession, network, self, logger)
        async with self._connector(session_factory, proxy=network.proxy) as session:
            try:
                await session.run()
            except DisconnectSessionError as error:
                await session.disconnect(str(error), blacklist=error.blacklist)
            except (RPCError, BatchError, TaskTimeout) as error:
                await session.disconnect(str(error))
        logger.info('disconnected')

    def protocol_text(self):
        if self.protocol == 's':
            return 'SSL'
        return 'TCP'

    def __repr__(self):
        return f'SVServer("{self.host}", {self.port}, "{self.protocol}")'

    def __str__(self):
        return str(self.to_json()[:3])


class SVUserAuth(SOCKSUserAuth):

    def to_json(self):
        return (self.username, self.password)

    @classmethod
    def from_json(cls, item):
        username, password = item
        return cls(username, password)

    def __repr__(self):
        # So its safe in logs, etc.  Also used in proxy comparisons.
        hash_str = sha256(str(self.to_json()).encode())[:8].hex()
        return f'{self.__class__.__name__}({hash_str})'


class SVProxy(SOCKSProxy):
    '''Encapsulates a SOCKS proxy.'''

    kinds = {'SOCKS4' : SOCKS4a, 'SOCKS5': SOCKS5}

    def __init__(self, address, kind, auth):
        protocol = self.kinds.get(kind.upper())
        if not protocol:
            raise ValueError(f'invalid proxy kind: {kind}')
        super().__init__(address, protocol, auth)

    def to_json(self):
        return (str(self.address), self.kind(), self.auth)

    @classmethod
    def from_json(cls, obj):
        return cls(*obj)

    @classmethod
    def from_string(cls, obj):
        # Backwards compatibility
        try:
            kind, host, port, username, password = obj.split(':', 5)
            return cls((host, port), kind, SVUserAuth(username, password))
        except Exception:
            return None

    def kind(self):
        return 'SOCKS4' if self.protocol is SOCKS4a else 'SOCKS5'

    def host(self):
        return self.address.host

    def port(self):
        return self.address.port

    def username(self):
        return self.auth.username if self.auth else ''

    def password(self):
        return self.auth.password if self.auth else ''

    def __str__(self):
        return ', '.join((repr(self.address), self.kind(), repr(self.auth)))


class SVSession(RPCSession):

    ca_path = certifi.where()
    _connecting_tips = {}
    _need_checkpoint_headers = True
    # wallet -> list of script hashes.  Also acts as a list of registered wallets
    _subs_by_wallet = {}
    # script_hash -> address
    _address_map = {}

    def __init__(self, network, server, logger, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._handlers = {}
        self._network = network
        self._closed_event = app_state.async_.event()
        # These attributes are intended to part of the external API
        self.chain = None
        self.logger = logger
        self.server = server
        self.tip = None
        self.ptuple = (0, )

    # async def send_request(self, method, args=()):
    #     t0 = time.time()
    #     logger.debug(f"send_request({method}, {args}) at {t0}")
    #     try:
    #         return await super().send_request(method, args)
    #     finally:
    #         td = time.time() - t0
    #         if td > 0.5:
    #             logger.debug(f"send_request({method}, {args}) at {t0} took {td}")
    #             traceback.print_stack()

    @classmethod
    def _required_checkpoint_headers(cls):
        '''Returns (start_height, count).  The range of headers needed for the DAA so that all
        post-checkpoint headers can have their difficulty verified.
        '''
        if cls._need_checkpoint_headers:
            headers_obj = app_state.headers
            chain = headers_obj.longest_chain()
            cp_height = headers_obj.checkpoint.height
            if cp_height == 0:
                cls._need_checkpoint_headers = False
            else:
                try:
                    for height in range(cp_height - 146, cp_height):
                        headers_obj.header_at_height(chain, height)
                    cls._need_checkpoint_headers = False
                except MissingHeader:
                    return height, cp_height - height
        return 0, 0

    @classmethod
    def _connect_header(cls, height, raw_header):
        '''It is assumed that if height is <= the checkpoint height then the header has
        been checked for validity.
        '''
        headers_obj = app_state.headers
        checkpoint = headers_obj.checkpoint

        if height <= checkpoint.height:
            headers_obj.set_one(height, raw_header)
            headers_obj.flush()
            header = Net.COIN.deserialized_header(raw_header, height)
            return header, headers_obj.longest_chain()
        else:
            return app_state.headers.connect(raw_header)

    @classmethod
    def _connect_chunk(cls, start_height, raw_chunk):
        '''It is assumed that if the last header of the raw chunk is before the checkpoint height
        then it has been checked for validity.
        '''
        headers_obj = app_state.headers
        checkpoint = headers_obj.checkpoint
        coin = headers_obj.coin
        end_height = start_height + len(raw_chunk) // HEADER_SIZE

        def extract_header(height):
            start = (height - start_height) * HEADER_SIZE
            return raw_chunk[start: start + HEADER_SIZE]

        def verify_chunk_contiguous_and_set(next_raw_header, to_height):
            # Set headers backwards from a proven header, verifying the prev_hash links.
            for height in reversed(range(start_height, to_height)):
                raw_header = extract_header(height)
                if coin.header_prev_hash(next_raw_header) != coin.header_hash(raw_header):
                    raise MissingHeader('prev_hash does not connect')
                headers_obj.set_one(height, raw_header)
                next_raw_header = raw_header

        try:
            # For pre-checkpoint headers with a verified proof, just set the headers after
            # verifying the prev_hash links
            if end_height < checkpoint.height:
                # Set the last proven header
                last_header = extract_header(end_height - 1)
                headers_obj.set_one(end_height - 1, last_header)
                verify_chunk_contiguous_and_set(last_header, end_height - 1)
                return headers_obj.longest_chain()

            # For chunks prior to but connecting to the checkpoint, no proof is required
            verify_chunk_contiguous_and_set(checkpoint.raw_header, checkpoint.height)

            # Process any remaining headers forwards from the checkpoint
            chain = None
            for height in range(max(checkpoint.height + 1, start_height), end_height):
                _header, chain = headers_obj.connect(extract_header(height))

            return chain or headers_obj.longest_chain()
        finally:
            headers_obj.flush()

    async def _negotiate_protocol(self):
        '''Raises: RPCError, TaskTimeout'''
        method = 'server.version'
        args = (PACKAGE_VERSION, [ version_string(PROTOCOL_MIN), version_string(PROTOCOL_MAX) ])
        try:
            server_string, protocol_string = await self.send_request(method, args)
            self.logger.debug(f'server string: {server_string}')
            self.logger.debug(f'negotiated protocol: {protocol_string}')
            self.ptuple = protocol_tuple(protocol_string)
            assert PROTOCOL_MIN <= self.ptuple <= PROTOCOL_MAX
        except (AssertionError, ValueError) as e:
            raise DisconnectSessionError(f'{method} failed: {e}', blacklist=True)

    async def _get_checkpoint_headers(self):
        '''Raises: RPCError, TaskTimeout'''
        while True:
            start_height, count = self._required_checkpoint_headers()
            if not count:
                break
            logger.info(f'{count:,d} checkpoint headers needed')
            await self._request_chunk(start_height, count)

    async def _request_chunk(self, height, count):
        '''Returns the greatest height successfully connected (might be lower than expected
        because of a small server response).

        Raises: RPCError, TaskTimeout, DisconnectSessionError'''
        self.logger.info(f'requesting {count:,d} headers from height {height:,d}')
        method = 'blockchain.block.headers'
        cp_height = app_state.headers.checkpoint.height
        if height + count >= cp_height:
            cp_height = 0

        try:
            result = await self.send_request(method, (height, count, cp_height))

            rec_count = result['count']
            last_height = height + rec_count - 1
            if count != rec_count:
                self.logger.info(f'received just {rec_count:,d} headers')

            raw_chunk = bytes.fromhex(result['hex'])
            assert len(raw_chunk) == HEADER_SIZE * rec_count
            if cp_height:
                hex_root = result['root']
                branch = [hex_str_to_hash(item) for item in result['branch']]
                self._check_header_proof(hex_root, branch, raw_chunk[-HEADER_SIZE:], last_height)

            self.chain = self._connect_chunk(height, raw_chunk)
        except (AssertionError, KeyError, TypeError, ValueError,
                IncorrectBits, InsufficientPoW, MissingHeader) as e:
            raise DisconnectSessionError(f'{method} failed: {e}', blacklist=True)

        self.logger.info(f'connected {rec_count:,d} headers up to height {last_height:,d}')
        return last_height

    async def _subscribe_headers(self):
        '''Raises: RPCError, TaskTimeout, DisconnectSessionError'''
        self._handlers[HEADERS_SUBSCRIBE] = self._on_new_tip
        tip = await self.send_request(HEADERS_SUBSCRIBE)
        await self._on_new_tip(tip)

    def _secs_to_next_ping(self):
        return self.last_send + 300 - time.time()

    async def _ping_loop(self):
        '''Raises: RPCError, TaskTimeout'''
        method = 'server.ping'
        while True:
            await sleep(self._secs_to_next_ping())
            if self._secs_to_next_ping() < 1:
                self.logger.debug(f'sending {method}')
                await self.send_request(method)

    def _check_header_proof(self, hex_root, branch, raw_header, height):
        '''Raises: DisconnectSessionError'''
        expected_root = Net.VERIFICATION_BLOCK_MERKLE_ROOT
        if hex_root != expected_root:
            raise DisconnectSessionError(f'bad header merkle root {hex_root} expected '
                                         f'{expected_root}', blacklist=True)
        header = Net.COIN.deserialized_header(raw_header, height)
        proven_root = hash_to_hex_str(_root_from_proof(header.hash, branch, height))
        if proven_root != expected_root:
            raise DisconnectSessionError(f'invalid header proof {proven_root} expected '
                                         f'{expected_root}', blacklist=True)
        self.logger.debug(f'good header proof for height {height}')

    async def _on_new_tip(self, json_tip):
        '''Raises: RPCError, TaskTimeout, DisconnectSessionError'''
        try:
            raw_header = bytes.fromhex(json_tip['hex'])
            height = json_tip['height']
            assert isinstance(height, int), "height must be an integer"
        except Exception as e:
            raise DisconnectSessionError(f'error connecting tip: {e} {json_tip}')

        if height < Net.CHECKPOINT.height:
            raise DisconnectSessionError(f'server tip height {height:,d} below checkpoint')

        self.chain = None
        self.tip = None
        tip = Net.COIN.deserialized_header(raw_header, height)

        while True:
            try:
                self.tip, self.chain = self._connect_header(tip.height, tip.raw)
                self.logger.debug(f'connected tip at height {height:,d}')
                self._network.check_main_chain_event.set()
                return
            except (IncorrectBits, InsufficientPoW) as e:
                raise DisconnectSessionError(f'bad header provided: {e}', blacklist=True)
            except MissingHeader:
                pass
            # Try to connect and then re-check.  Note self.tip might have changed.
            await self._catch_up_to_tip_throttled(tip)

    async def _catch_up_to_tip_throttled(self, tip):
        '''Raises: DisconnectSessionError, BatchError, TaskTimeout'''
        # Avoid thundering herd effect by having one session catch up per tip
        done_event = SVSession._connecting_tips.get(tip.raw)
        if done_event:
            self.logger.debug(f'another session is connecting my tip {tip.hex_str()}')
            await done_event.wait()
        else:
            self.logger.debug(f'connecting my own tip {tip.hex_str()}')
            SVSession._connecting_tips[tip.raw] = app_state.async_.event()
            try:
                await self._catch_up_to_tip(tip)
            finally:
                SVSession._connecting_tips.pop(tip.raw).set()

    async def _catch_up_to_tip(self, tip):
        '''Raises: DisconnectSessionError, BatchError, TaskTimeout'''
        headers_obj = app_state.headers
        cp_height = headers_obj.checkpoint.height
        max_height = max(chain.height for chain in headers_obj.chains())
        heights = [cp_height + 1]
        step = 1
        height = min(tip.height, max_height)
        while height > cp_height:
            heights.append(height)
            height -= step
            step += step

        height = await self._request_headers_at_heights(heights)
        # Catch up
        while height < tip.height:
            height = await self._request_chunk(height + 1, 2016)

    async def _subscribe_to_script_hash(self, script_hash: str) -> None:
        '''Raises: RPCError, TaskTimeout'''
        status = await self.send_request(SCRIPTHASH_SUBSCRIBE, [script_hash])
        await self._on_status_changed(script_hash, status)

    async def _unsubscribe_from_script_hash(self, script_hash: str) -> bool:
        return await self.send_request(SCRIPTHASH_UNSUBSCRIBE, [script_hash])

    async def _on_status_changed(self, script_hash, status):
        address = self._address_map.get(script_hash)
        if not address:
            self.logger.error(f'received status notification for unsubscribed {script_hash}')
            return

        # Wallets needing a notification
        wallets = [wallet for wallet, subs in self._subs_by_wallet.items()
                   if script_hash in subs and
                   _history_status(wallet.get_address_history(address)) != status]
        if not wallets:
            return

        # Status has changed; get history
        result = await self.request_history(script_hash)
        self.logger.debug(f'received history of {address} length {len(result)}')
        try:
            history = [(item['tx_hash'], item['height']) for item in result]
            tx_fees = {item['tx_hash']: item['fee'] for item in result if 'fee' in item}
            # Check that txids are unique
            assert len(set(tx_hash for tx_hash, tx_height in history)) == len(history), \
                f'server history for {address} has duplicate transactions'
        except (AssertionError, KeyError) as e:
            raise DisconnectSessionError(f'bad history returned: {e}')

        # Check the status; it can change legitimately between initial notification and
        # history request
        hstatus = _history_status(history)
        if hstatus != status:
            self.logger.warning(f'history status mismatch {hstatus} vs {status} for {address}')

        for wallet in wallets:
            await wallet.set_address_history(address, history, tx_fees)

    async def _main_server_batch(self):
        '''Raises: DisconnectSessionError, BatchError, TaskTimeout'''
        async with timeout_after(10):
            async with self.send_batch(raise_errors=True) as batch:
                batch.add_request('server.banner')
                batch.add_request('server.donation_address')
                batch.add_request('server.peers.subscribe')
        server = self.server
        try:
            server.state.banner = _require_string(batch.results[0])
            server.state.donation_address = _require_string(batch.results[1])
            server.state.peers = self._parse_peers_subscribe(batch.results[2])
            self._network.trigger_callback('banner')
        except AssertionError as e:
            raise DisconnectSessionError(f'main server requests bad batch response: {e}')

    def _parse_peers_subscribe(self, result):
        peers = []
        for host_details in _require_list(result):
            host_details = _require_list(host_details)
            host = _require_string(host_details[1])
            for v in host_details[2]:
                if re.match(r"[st]\d*", _require_string(v)):
                    protocol, port = v[0], v[1:]
                    try:
                        peers.append(SVServer.unique(host, port, protocol))
                    except ValueError:
                        pass
        self.logger.info(f'{len(peers)} servers returned from server.peers.subscribe')
        return peers

    async def _request_headers_at_heights(self, heights):
        '''Requests the headers as a batch and connects them, lowest height first.

        Return the greatest connected height (-1 if none connected).
        Raises: DisconnectSessionError, BatchError, TaskTimeout
        '''
        heights = sorted(set(heights))
        self.logger.debug(f'requesting headers at heights {heights}')
        cp_height = Net.CHECKPOINT.height
        method = 'blockchain.block.header'

        async with timeout_after(10):
            async with self.send_batch(raise_errors=True) as batch:
                for height in heights:
                    batch.add_request(method, (height, cp_height if height <= cp_height else 0))

        min_good_height = max((height for height in heights if height <= cp_height), default=-1)
        good_height = -1
        try:
            for result, height in zip(batch.results, heights):
                if height <= cp_height:
                    hex_root = result['root']
                    branch = [hex_str_to_hash(item) for item in result['branch']]
                    raw_header = bytes.fromhex(result['header'])
                    self._check_header_proof(hex_root, branch, raw_header, height)
                else:
                    raw_header = bytes.fromhex(result)
                _header, self.chain = self._connect_header(height, raw_header)
                good_height = height
        except MissingHeader:
            hex_str = hash_to_hex_str(Net.COIN.header_hash(raw_header))
            self.logger.info(f'failed to connect at height {height:,d}, '
                             f'hash {hex_str} last good {good_height:,d}')
        except (AssertionError, KeyError, TypeError, ValueError) as e:
            raise DisconnectSessionError(f'bad {method} response: {e}')

        if good_height < min_good_height:
            raise DisconnectSessionError(f'cannot connect to checkpoint', blacklist=True)
        return good_height

    async def handle_request(self, request):
        if isinstance(request, Notification):
            handler = self._handlers.get(request.method)
        else:
            handler = None
        coro = handler_invocation(handler, request)()
        return await coro

    async def connection_lost(self):
        await super().connection_lost()
        self._closed_event.set()

    #
    # API exposed to the rest of this file
    #

    async def disconnect(self, reason, *, blacklist=False):
        if blacklist:
            self.server.state.last_blacklisted = time.time()
            self.logger.error(f'disconnecting and blacklisting: {reason}')
        else:
            self.logger.error(f'disconnecting: {reason}')
        await self.close()

    async def run(self):
        '''Called when a connection is established to manage the connection.

        Raises: RPCError, BatchError, TaskTimeout, DisconnectSessionError
        '''
        # Negotiate the protocol before doing anything else
        await self._negotiate_protocol()
        # Checkpoint headers are essential to attempting tip connection
        await self._get_checkpoint_headers()
        # Then subscribe headers and connect the server's tip
        await self._subscribe_headers()
        # Only once the tip is connected to our set of chains do we consider the
        # session good and add it to the network's session list.  The network and
        # other client code can assume a session 'tip' and 'chain' set.
        is_main_server = await self._network.session_established(self)
        try:
            self.server.state.retry_delay = 0
            async with TaskGroup() as group:
                if is_main_server:
                    self.logger.info('using as main server')
                    await group.spawn(self.subscribe_wallets)
                    await group.spawn(self._main_server_batch)
                await group.spawn(self._ping_loop)
                await self._closed_event.wait()
                await group.cancel_remaining()
        finally:
            await self._network.session_closed(self)

    async def subscribe_wallet(self, wallet, pairs=None):
        if pairs is None:
            pairs = [(address, scripthash_hex(address))
                for address in wallet.get_observed_addresses()]
            self.logger.info(f'subscribing to {len(pairs):,d} observed addresses for {wallet}')
        else:
            self.logger.info(f'subscribing to {len(pairs):,d} addresses for {wallet}')
            # If wallet was unsubscribed in the meantime keep it that way
            if wallet not in self._subs_by_wallet:
                return
        await self.subscribe_to_pairs(wallet, pairs)

    async def subscribe_wallets(self):
        '''When switching main server or when initially connected to the main server, send script
        hash subs to the new main session.

        Raises: RPCError, TaskTimeout
        '''
        self.logger.debug("subscribe_wallets")
        subs_by_wallet = self._subs_by_wallet
        address_map = self._address_map
        SVSession._address_map = {}
        SVSession._subs_by_wallet = {wallet: [] for wallet in subs_by_wallet}

        async with TaskGroup() as group:
            for wallet in list(subs_by_wallet):
                pairs = [(address_map[sh], sh) for sh in subs_by_wallet[wallet]]
                await group.spawn(self.subscribe_wallet, wallet, pairs)

    async def headers_at_heights(self, heights):
        '''Raises: MissingHeader, DisconnectSessionError, BatchError, TaskTimeout'''
        result = {}
        missing = []
        header_at_height = app_state.headers.header_at_height
        for height in set(heights):
            try:
                result[height] = header_at_height(self.chain, height)
            except MissingHeader:
                missing.append(height)
        if missing:
            await self._request_headers_at_heights(missing)
            for height in missing:
                result[height] = header_at_height(self.chain, height)
        return result

    async def request_tx(self, tx_hash):
        '''Raises: RPCError, TaskTimeout'''
        return await self.send_request('blockchain.transaction.get', [tx_hash])

    async def request_proof(self, *args):
        '''Raises: RPCError, TaskTimeout'''
        return await self.send_request(REQUEST_MERKLE_PROOF, args)

    async def request_history(self, script_hash):
        '''Raises: RPCError, TaskTimeout'''
        return await self.send_request(SCRIPTHASH_HISTORY, [script_hash])

    async def subscribe_to_pairs(self, wallet, pairs) -> None:
        '''pairs is an iterable of (address, script_hash) pairs.

        Raises: RPCError, TaskTimeout'''
        # Set notification handler
        self._handlers[SCRIPTHASH_SUBSCRIBE] = self._on_status_changed
        if wallet not in self._subs_by_wallet:
            self._subs_by_wallet[wallet] = []
        # Take reference so wallet can be unsubscribed asynchronously without conflict
        subs = self._subs_by_wallet[wallet]
        async with TaskGroup() as group:
            wallet.request_count += len(pairs)
            wallet.progress_event.set()
            for address, script_hash in pairs:
                subs.append(script_hash)
                # Send request even if already subscribed, as our user expects a response
                # to trigger other actions and won't get one if we swallow it.
                self._address_map[script_hash] = address
                await group.spawn(self._subscribe_to_script_hash(script_hash))

            while await group.next_done():
                wallet.response_count += 1
                wallet.progress_event.set()
        # A wallet shouldn't be subscribing the same address twice
        assert len(set(subs)) == len(subs)

    async def unsubscribe_from_pairs(self, wallet, pairs) -> None:
        '''pairs is an iterable of (address, script_hash) pairs.

        Raises: RPCError, TaskTimeout'''
        subs = self._subs_by_wallet[wallet]
        exclusive_subs = self._get_exclusive_set(wallet, subs)
        async with TaskGroup() as group:
            for address, script_hash in pairs:
                if script_hash not in exclusive_subs:
                    continue
                # Blocking on each removal allows for race conditions.
                if script_hash not in subs:
                    continue
                subs.remove(script_hash)
                del self._address_map[script_hash]
                await group.spawn(self._unsubscribe_from_script_hash(script_hash))

    @classmethod
    def _get_exclusive_set(cls, wallet, subs: List[str]) -> set:
        # This returns the script hashes the given wallet is subscribed to, that no other
        # wallet is also subscribed to. This ensures that when we unsubscribe script hashes for
        # the given wallet, as the server subscription is shared between wallets, we only
        # unsubscribe if the script hash will no longer be needed for any wallet.
        subs_set = set(subs)
        for other_wallet, other_subs in cls._subs_by_wallet.items():
            if other_wallet == wallet:
                continue
            subs_set -= set(other_subs)
        return subs_set

    @classmethod
    async def unsubscribe_wallet(cls, wallet, session):
        subs = cls._subs_by_wallet.pop(wallet, None)
        if subs is None:
            return
        if not session:
            return
        exclusive_subs = cls._get_exclusive_set(wallet, subs)
        if not exclusive_subs:
            return

        if session.ptuple < (1, 4, 2):
            logger.debug("negotiated protocol does not support unsubscribing")
            return
        logger.debug(f"unsubscribing {len(exclusive_subs)} subscriptions for {wallet}")
        async with TaskGroup() as group:
            for script_hash in exclusive_subs:
                await group.spawn(session._unsubscribe_from_script_hash(script_hash))
        logger.debug(f"unsubscribed {len(exclusive_subs)} subscriptions for {wallet}")


class Network:
    '''Manages a set of connections to remote ElectrumX servers.  All operations are
    asynchronous.
    '''

    def __init__(self):
        app_state.read_headers()

        # Sessions
        self.sessions = []
        self.chosen_servers = set()
        self.main_server = None
        self.proxy = None

        # Events
        self.sessions_changed_event = app_state.async_.event()
        self.check_main_chain_event = app_state.async_.event()
        self.stop_network_event = app_state.async_.event()
        self.shutdown_complete_event = app_state.async_.event()

        # Add a wallet, remove a wallet, or redo all wallet verifications
        self.wallet_jobs = app_state.async_.queue()

        # Callbacks and their lock
        self.callbacks = defaultdict(list)
        self.lock = threading.Lock()

        dir_path = app_state.config.file_path('certs')
        if not os.path.exists(dir_path):
            os.mkdir(dir_path)
            os.chmod(dir_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        self.future = app_state.async_.spawn(self._main_task)

    async def _main_task(self):
        try:
            async with TaskGroup() as group:
                await group.spawn(self._start_network, group)
                await group.spawn(self._monitor_lagging_sessions)
                await group.spawn(self._monitor_main_chain)
                await group.spawn(self._monitor_wallets, group)
        finally:
            self.shutdown_complete_event.set()
            app_state.config.set_key('servers', list(SVServer.all_servers.values()), True)

    async def _restart_network(self):
        self.stop_network_event.set()

    async def _start_network(self, group):
        while True:
            # Treat all servers as not used so connections are not delayed
            for server in SVServer.all_servers.values():
                server.state.retry_delay = 0

            if self.main_server is None:
                self.main_server, self.proxy = self._read_config()

            logger.debug('starting...')
            connections_task = await group.spawn(self._maintain_connections)
            await self.stop_network_event.wait()
            self.stop_network_event.clear()
            with suppress(CancelledError):
                await connections_task

    async def _maintain_connections(self):
        count = 1 if app_state.config.get('oneserver') else 10
        async with TaskGroup() as group:
            for n in range(0, count):
                await group.spawn(self._maintain_connection, n)

    async def _maintain_connection(self, n):
        # Connection 0 initially connects to the main_server.  main_server can change if
        # auto_connect is true, or the user specifies a new one in the network dialog.
        server = self.main_server if n == 0 else None
        while True:
            if server is self.main_server:
                self.trigger_callback('status')
            else:
                server = await self._random_server(self.main_server.protocol)

            self.chosen_servers.add(server)
            try:
                await server.connect(self, n)
            except (OSError, SOCKSError) as e:
                logger.error(f'{server} connection error: {e}')
            finally:
                self.chosen_servers.remove(server)

            if server is self.main_server:
                await self._maybe_switch_main_server(SwitchReason.disconnected)

    async def _maybe_switch_main_server(self, reason):
        now = time.time()
        max_height = max((session.tip.height for session in self.sessions), default=0)
        for session in self.sessions:
            if session.tip.height > max_height - 2:
                session.server.state.last_good = now
        # Give a 60-second breather for a lagging server to catch up
        good_servers = [session.server for session in self.sessions
                        if session.server.state.last_good > now - 60]
        if not good_servers:
            logger.warning(f'no good servers available')
        elif self.main_server not in good_servers:
            if self.auto_connect():
                await self._set_main_server(random.choice(good_servers), reason)
            else:
                logger.warning(f'main server {self.main_server} is not good, but '
                               f'retaining it because auto-connect is off')

    async def _monitor_lagging_sessions(self):
        '''Monitor which sessions are lagging.

        If the main server is lagging switch the main server if auto_connect.
        '''
        while True:
            async with ignore_after(20):
                await self.sessions_changed_event.wait()
            await self._maybe_switch_main_server(SwitchReason.lagging)

    async def _monitor_wallets(self, group):
        wallet_tasks = {}
        while True:
            job, wallet = await self.wallet_jobs.get()
            if job == 'add':
                if wallet not in wallet_tasks:
                    wallet_tasks[wallet] = await group.spawn(self._maintain_wallet(wallet))
            elif job == 'remove':
                if wallet in wallet_tasks:
                    wallet_tasks.pop(wallet).cancel()
            elif job == 'undo_verifications':
                above_height = wallet
                for wallet in wallet_tasks:
                    wallet.undo_verifications(above_height)
            else:
                logger.error(f'unknown wallet job {job}')

    async def _monitor_main_chain(self):
        main_chain = None
        while True:
            await self.check_main_chain_event.wait()
            self.check_main_chain_event.clear()
            main_session = await self._main_session()
            new_main_chain = main_session.chain
            if main_chain != new_main_chain and main_chain:
                _chain, above_height = main_chain.common_chain_and_height(new_main_chain)
                logger.info(f'main chain updated; undoing wallet verifications '
                            f'above height {above_height:,d}')
                await self.wallet_jobs.put(('undo_verifications', above_height))
            main_chain = new_main_chain
            self.trigger_callback('updated')
            self.trigger_callback('main_chain', main_chain, new_main_chain)

    async def _set_main_server(self, server, reason):
        '''Set the main server to something new.'''
        assert isinstance(server, SVServer), f"got invalid server value: {server}"
        logger.info(f'switching main server to {server}: {reason.name}')
        old_main_session = self.main_session()
        self.main_server = server
        self.check_main_chain_event.set()
        main_session = self.main_session()
        if main_session:
            await main_session.subscribe_wallets()
        # Disconnect the old main session, if any, in order to lose scripthash
        # subscriptions.
        if old_main_session:
            if reason == SwitchReason.user_set:
                old_main_session.server.retry_delay = 0
            await old_main_session.close()
        self.trigger_callback('status')

    def _read_config(self):
        # Remove obsolete key
        app_state.config.set_key('server_blacklist', None)
        count = len(SVServer.all_servers)
        logger.info(f'read {count:,d} servers from config file')
        if count < 5:
            # Add default servers if not present.   FIXME: an awful dict.  Make it a list!
            for host, data in Net.DEFAULT_SERVERS.items():
                for protocol in 'st':
                    if protocol in data:
                        SVServer.unique(host, data[protocol], protocol)
        main_server = app_state.config.get('server', None)
        if isinstance(main_server, str):
            try:
                main_server = SVServer.from_string(main_server)
                app_state.config.set_key('server', main_server, True)
            except Exception:
                pass
        if not isinstance(main_server, SVServer):
            logger.info('choosing an SSL server randomly; none in config')
            main_server = self._random_server_nowait('s')
            if not main_server:
                raise RuntimeError('no servers available')
        proxy = app_state.config.get('proxy', None)
        if isinstance(proxy, str):
            proxy = SVProxy.from_string(proxy)
        logger.info(f'main server: {main_server}; proxy: {proxy}')
        return main_server, proxy

    async def _request_transactions(self, wallet):
        missing_hashes = wallet.missing_transactions()
        if not missing_hashes:
            return False
        wallet.request_count += len(missing_hashes)
        wallet.progress_event.set()
        had_timeout = False
        session = await self._main_session()
        session.logger.debug(f'requesting {len(missing_hashes)} missing transactions')
        async with TaskGroup() as group:
            tasks = {}
            for tx_hash in missing_hashes:
                tasks[await group.spawn(session.request_tx(tx_hash))] = tx_hash

            while tasks:
                task = await group.next_done()
                wallet.response_count += 1
                wallet.progress_event.set()
                tx_hash = tasks.pop(task)
                try:
                    tx_hex = task.result()
                    tx = Transaction.from_hex(tx_hex)
                    session.logger.debug(f'received tx {tx_hash} bytes: {len(tx_hex)//2}')
                except CancelledError:
                    had_timeout = True
                except Exception as e:
                    logger.exception(e)
                    logger.error(f'fetching transaction {tx_hash}: {e}')
                else:
                    wallet.add_transaction(tx_hash, tx)
                    self.trigger_callback('new_transaction', tx, wallet)
        return had_timeout

    def _available_servers(self, protocol):
        now = time.time()
        unchosen = set(SVServer.all_servers.values()).difference(self.chosen_servers)
        return [server for server in unchosen
                if server.protocol == protocol and server.state.can_retry(now)]

    def _random_server_nowait(self, protocol):
        servers = self._available_servers(protocol)
        return random.choice(servers) if servers else None

    async def _random_server(self, protocol):
        while True:
            server = self._random_server_nowait(protocol)
            if server:
                return server
            await sleep(10)

    async def _request_proofs(self, wallet):
        wanted_map = wallet.unverified_transactions()
        if not wanted_map:
            return False
        had_timeout = False
        session = await self._main_session()
        session.logger.debug(f'requesting {len(wanted_map)} proofs')
        async with TaskGroup() as group:
            tasks = {}
            for tx_hash, tx_height in wanted_map.items():
                tasks[await group.spawn(session.request_proof(tx_hash, tx_height))] = tx_hash
            headers = await session.headers_at_heights(wanted_map.values())

            while tasks:
                task = await group.next_done()
                tx_hash = tasks.pop(task)
                tx_height = wanted_map[tx_hash]
                try:
                    result = task.result()
                    branch = [hex_str_to_hash(item) for item in result['merkle']]
                    tx_pos = result['pos']
                    proven_root = _root_from_proof(hex_str_to_hash(tx_hash), branch, tx_pos)
                    header = headers[wanted_map[tx_hash]]
                except CancelledError:
                    had_timeout = True
                except Exception as e:
                    logger.error(f'getting proof for {tx_hash}: {e}')
                else:
                    if header.merkle_root == proven_root:
                        logger.debug(f'received valid proof for {tx_hash}')
                        wallet.add_verified_tx(tx_hash,
                            tx_height, header.timestamp, tx_pos, tx_pos, branch)
                    else:
                        hhts = hash_to_hex_str
                        logger.error(f'invalid proof for tx {tx_hash} in block '
                                     f'{hhts(header.hash)}; got {hhts(proven_root)} expected '
                                     f'{hhts(header.merkle_root)}')
        return had_timeout

    async def _monitor_txs(self, wallet):
        '''Raises: RPCError, BatchError, TaskTimeout, DisconnectSessionError'''
        while True:
            async with TaskGroup() as group:
                tasks = (
                    await group.spawn(self._request_transactions(wallet)),
                    await group.spawn(self._request_proofs(wallet)),
                )
            # Try again if a request timed out
            if any(task.result() for task in tasks):
                continue
            await wallet.txs_changed_event.wait()
            wallet.txs_changed_event.clear()

    async def _monitor_new_addresses(self, wallet):
        '''Raises: RPCError, TaskTimeout'''
        addresses = wallet.get_observed_addresses()
        while True:
            session = await self._main_session()
            session.logger.info(f'subscribing to {len(addresses):,d} new addresses for {wallet}')
            # Do in reverse to require fewer wallet re-sync loops
            pairs = [(address, scripthash_hex(address)) for address in addresses]
            pairs.reverse()
            await session.subscribe_to_pairs(wallet, pairs)
            addresses = await wallet.new_addresses()

    async def _monitor_used_addresses(self, wallet):
        '''Raises: RPCError, TaskTimeout'''
        while True:
            addresses = await wallet.used_addresses()
            session = await self._main_session()
            if len(addresses) < 5:
                address_strings = [a.to_string(coin=Net.COIN) for a in addresses]
                session.logger.info(
                    f'unsubscribing from used addresses for {wallet}: {address_strings}')
            else:
                session.logger.info(f'unsubscribing from {len(addresses):,d} '+
                    f'used addresses for {wallet}')
            pairs = [(address, scripthash_hex(address)) for address in addresses]
            await session.unsubscribe_from_pairs(wallet, pairs)

    async def _maintain_wallet(self, wallet):
        '''Put all tasks for a single wallet in a group so they can be cancelled together.'''
        logger.info(f'maintaining wallet {wallet}')
        try:
            while True:
                try:
                    async with TaskGroup() as group:
                        await group.spawn(self._monitor_txs, wallet)
                        await group.spawn(self._monitor_new_addresses, wallet)
                        await group.spawn(self._monitor_used_addresses, wallet)
                        await group.spawn(wallet.synchronize_loop)
                except (RPCError, BatchError, DisconnectSessionError, TaskTimeout) as error:
                    blacklist = isinstance(error, DisconnectSessionError) and error.blacklist
                    session = self.main_session()
                    if session:
                        await session.disconnect(str(error), blacklist=blacklist)
        finally:
            await SVSession.unsubscribe_wallet(wallet, self.main_session())
            logger.info(f'stopped maintaining wallet {wallet}')

    async def _main_session(self):
        while True:
            session = self.main_session()
            if session:
                return session
            await self.sessions_changed_event.wait()

    async def _random_session(self):
        while not self.sessions:
            logger.info('waiting for new session')
            await self.sessions_changed_event.wait()
        return random.choice(self.sessions)

    #
    # API exposed to SVSession
    #

    async def session_established(self, session):
        self.sessions.append(session)
        self.sessions_changed_event.set()
        self.sessions_changed_event.clear()
        self.trigger_callback('sessions')
        if session.server is self.main_server:
            self.trigger_callback('status')
            return True
        return False

    async def session_closed(self, session):
        self.sessions.remove(session)
        self.sessions_changed_event.set()
        self.sessions_changed_event.clear()
        if session.server is self.main_server:
            self.trigger_callback('status')
        self.trigger_callback('sessions')

    #
    # External API
    #

    async def shutdown_wait(self):
        self.future.cancel()
        await self.shutdown_complete_event.wait()
        assert not self.sessions
        logger.warning('stopped')

    def auto_connect(self):
        return app_state.config.get('auto_connect', True)

    def is_connected(self):
        return self.main_session() is not None

    def main_session(self):
        '''Returns the session, if any, connected to main_server.'''
        for session in self.sessions:
            if session.server is self.main_server:
                return session
        return None

    def get_servers(self):
        return SVServer.all_servers.values()

    def add_wallet(self, wallet):
        app_state.async_.spawn(self.wallet_jobs.put, ('add', wallet))

    def remove_wallet(self, wallet):
        app_state.async_.spawn(self.wallet_jobs.put, ('remove', wallet))

    def register_callback(self, callback, events):
        with self.lock:
            for event in events:
                self.callbacks[event].append(callback)

    def unregister_callback(self, callback):
        with self.lock:
            for callbacks in self.callbacks.values():
                if callback in callbacks:
                    callbacks.remove(callback)

    def trigger_callback(self, event, *args):
        with self.lock:
            callbacks = self.callbacks[event][:]
        [callback(event, *args) for callback in callbacks]

    def chain(self):
        main_session = self.main_session()
        if main_session:
            return main_session.chain
        return app_state.headers.longest_chain()

    def get_local_height(self):
        chain = self.chain()
        # This can be called from network_dialog.py when there is no chain
        return chain.height if chain else 0

    def get_server_height(self):
        main_session = self.main_session()
        if main_session and main_session.tip:
            return main_session.tip.height
        return 0

    def set_server(self, server, auto_connect):
        config = app_state.config
        config.set_key('server', server, True)
        if config.get('server') is server:
            app_state.config.set_key('auto_connect', auto_connect, False)
            app_state.async_.spawn(self._set_main_server, server, SwitchReason.user_set)

    def set_proxy(self, proxy):
        if str(proxy) == str(self.proxy):
            return
        app_state.config.set_key("proxy", proxy, False)
        # See if config accepted the update
        if str(app_state.config.get('proxy')) == str(proxy):
            self.proxy = proxy
            logger.info(f"Set proxy to {proxy}")
            app_state.async_.spawn(self._restart_network)

    def sessions_by_chain(self):
        '''Return a map {chain: sessions} for each chain being followed by any session.'''
        result = defaultdict(list)
        for session in self.sessions:
            if session.chain:
                result[session.chain].append(session)
        return result

    def status(self):
        return {
            'server': str(self.main_server),
            'blockchain_height': self.get_local_height(),
            'server_height': self.get_server_height(),
            'spv_nodes': len(self.sessions),
            'connected': self.is_connected(),
            'auto_connect': self.auto_connect(),
        }

    # FIXME: this should be removed; its callers need to be fixed
    def request_and_wait(self, method, args):
        async def send_request():
            session = await self._main_session()
            return await session.send_request(method, args)

        return app_state.async_.spawn_and_wait(send_request)

    def get_utxos(self, script_hash):
        return self.request_and_wait('blockchain.scripthash.listunspent', [script_hash])

    def broadcast_transaction_and_wait(self, transaction: Transaction) -> str:
        return self.request_and_wait('blockchain.transaction.broadcast', [str(transaction)])


JSON.register(SVServerState, SVServer, SVProxy)
