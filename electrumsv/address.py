# ElectrumSV - lightweight Bitcoin client
# Copyright (C) 2018 The ElectrumSV Developers
# Copyright (C) 2017 The Electron Cash Developers
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

# Many of the functions in this file are copied from ElectrumX

from collections import namedtuple
import struct

from bitcoinx import Ops, PrivateKey

from . import cashaddr
from .bitcoin import is_minikey, minikey_to_private_key
from .crypto import hash_160, sha256, sha256d
from .networks import Net
from .util import cachedproperty


hex_to_bytes = bytes.fromhex


class AddressError(Exception):
    '''Exception used for Address errors.'''

class ScriptError(Exception):
    '''Exception used for Script errors.'''


# Utility functions

def to_bytes(x):
    '''Convert to bytes which is hashable.'''
    if isinstance(x, bytes):
        return x
    if isinstance(x, bytearray):
        return bytes(x)
    raise TypeError('{} is not bytes ({})'.format(x, type(x)))

def hash_to_hex_str(x):
    '''Convert a big-endian binary hash to displayed hex string.

    Display form of a binary hash is reversed and converted to hex.
    '''
    return bytes(reversed(x)).hex()

def bytes_to_int(be_bytes):
    '''Interprets a big-endian sequence of bytes as an integer'''
    return int.from_bytes(be_bytes, 'big')

def int_to_bytes(value):
    '''Converts an integer to a big-endian sequence of bytes'''
    return value.to_bytes((value.bit_length() + 7) // 8, 'big')


class UnknownAddress(object):

    def to_string(self):
        return '<UnknownAddress>'

    def __str__(self):
        return self.to_string()

    def __repr__(self):
        return '<UnknownAddress>'


class PublicKey(namedtuple("PublicKeyTuple", "pubkey")):

    @classmethod
    def from_pubkey(cls, pubkey):
        '''Create from a public key expressed as binary bytes.'''
        if isinstance(pubkey, str):
            pubkey = hex_to_bytes(pubkey)
        cls.validate(pubkey)
        return cls(to_bytes(pubkey))

    @classmethod
    def privkey_from_WIF_privkey(cls, WIF_privkey):
        '''Given a WIF private key (or minikey), return the private key as
        binary and a boolean indicating whether it was encoded to
        indicate a compressed public key or not.
        '''
        if is_minikey(WIF_privkey):
            # The Casascius coins were uncompressed
            return minikey_to_private_key(WIF_privkey), False
        raw = Base58.decode_check(WIF_privkey)
        if not raw or raw[0] != Net.WIF_PREFIX:
            raise ValueError('private key has invalid WIF prefix')
        if len(raw) == 34 and raw[-1] == 1:
            return raw[1:33], True
        if len(raw) == 33:
            return raw[1:], False
        raise ValueError('invalid private key')

    @classmethod
    def from_WIF_privkey(cls, WIF_privkey):
        '''Create a compressed or uncompressed public key from a private
        key.'''
        privkey, compressed = cls.privkey_from_WIF_privkey(WIF_privkey)
        return cls.from_pubkey(PrivateKey(privkey).public_key.to_bytes(compressed=compressed))

    @classmethod
    def from_string(cls, string):
        '''Create from a hex string.'''
        return cls.from_pubkey(hex_to_bytes(string))

    @classmethod
    def validate(cls, pubkey):
        if not isinstance(pubkey, (bytes, bytearray)):
            raise TypeError('pubkey must be of bytes type, not {}'
                            .format(type(pubkey)))
        if len(pubkey) == 33 and pubkey[0] in (2, 3):
            return  # Compressed
        if len(pubkey) == 65 and pubkey[0] == 4:
            return  # Uncompressed
        raise AddressError('invalid pubkey {}'.format(pubkey))

    @cachedproperty
    def address(self):
        '''Convert to an Address object.'''
        return Address(hash_160(self.pubkey), Address.ADDR_P2PKH)

    def is_compressed(self):
        '''Returns True if the pubkey is compressed.'''
        return len(self.pubkey) == 33

    def to_string(self):
        '''Convert to a hexadecimal string.'''
        return self.pubkey.hex()

    def to_script(self):
        '''Note this returns the P2PK script.'''
        return Script.P2PK_script(self.pubkey)

    def to_script_hex(self):
        '''Return a script to pay to the address as a hex string.'''
        return self.to_script().hex()

    def to_scripthash(self):
        '''Returns the hash of the script in binary.'''
        return sha256(self.to_script())

    def to_scripthash_hex(self):
        '''Like other bitcoin hashes this is reversed when written in hex.'''
        return hash_to_hex_str(self.to_scripthash())

    def to_P2PKH_script(self):
        '''Return a P2PKH script.'''
        return self.address.to_script()

    def __str__(self):
        return self.to_string()

    def __repr__(self):
        return '<PubKey {}>'.format(self.__str__())


class ScriptOutput(namedtuple("ScriptAddressTuple", "script")):

    @classmethod
    def from_string(self, string):
        '''Instantiate from a mixture of opcodes and raw data.'''
        script = bytearray()
        for word in string.split():
            if word.startswith('OP_'):
                try:
                    opcode = Ops[word]
                except KeyError:
                    raise AddressError(f'unknown opcode "{word}"') from None
                script.append(opcode)
            else:
                script.extend(Script.push_data(bytes.fromhex(word)))
        return ScriptOutput(bytes(script))

    def to_string(self):
        '''Convert to user-readable OP-codes (plus pushdata as text if possible)
        eg OP_RETURN (12) "Hello there!"
        '''
        try:
            ops = Script.get_ops(self.script)
        except ScriptError:
            # Truncated script -- so just default to hex string.
            return self.script.hex()

        def lookup(n):
            try:
                return Ops(n).name
            except ValueError:
                return f'({n})'

        parts = []
        for op in ops:
            if isinstance(op, tuple):
                op, data = op
                if data is None:
                    data = b''

                # Attempt to make a friendly string, or fail to hex
                try:
                    astext = data.decode('utf8')

                    friendlystring = repr(astext)

                    # if too many escaped characters, it's too ugly!
                    if friendlystring.count('\\')*3 > len(astext):
                        friendlystring = None
                except Exception:
                    friendlystring = None

                if not friendlystring:
                    friendlystring = data.hex()

                parts.append(lookup(op) + " " + friendlystring)
            else:
                parts.append(lookup(op))
        return ', '.join(parts)

    def to_script(self):
        return self.script

    def __str__(self):
        return self.to_string()

    def __repr__(self):
        return '<ScriptOutput {}>'.format(self.__str__())

    @classmethod
    def as_op_return(self, data_chunks):
        script = bytearray()
        script.append(Ops.OP_RETURN)
        for data_bytes in data_chunks:
            script.extend(Script.push_data(data_bytes))
        return ScriptOutput(bytes(script))


# A namedtuple for easy comparison and unique hashing
class Address(namedtuple("AddressTuple", "hash160 kind")):

    # Address kinds
    ADDR_P2PKH = 0
    ADDR_P2SH = 1

    def __new__(cls, hash160value, kind):
        assert kind in (cls.ADDR_P2PKH, cls.ADDR_P2SH)
        hash160value = to_bytes(hash160value)
        assert len(hash160value) == 20
        return super().__new__(cls, hash160value, kind)

    @classmethod
    def from_cashaddr_string(cls, string):
        '''Construct from a cashaddress string.'''
        prefix = Net.CASHADDR_PREFIX
        if string.upper() == string:
            prefix = prefix.upper()
        if not string.startswith(prefix + ':'):
            string = ':'.join([prefix, string])
        addr_prefix, kind, addr_hash = cashaddr.decode(string)
        if addr_prefix != prefix:
            raise AddressError('address has unexpected prefix {}'
                               .format(addr_prefix))
        if kind == cashaddr.PUBKEY_TYPE:
            return cls(addr_hash, cls.ADDR_P2PKH)
        elif kind == cashaddr.SCRIPT_TYPE:
            return cls(addr_hash, cls.ADDR_P2SH)
        else:
            raise AddressError('address has unexpected kind {}'.format(kind))

    @classmethod
    def from_string(cls, string, net=Net):
        '''Construct from an address string.'''
        if len(string) > 35:
            try:
                return cls.from_cashaddr_string(string)
            except ValueError as e:
                raise AddressError(str(e))

        try:
            raw = Base58.decode_check(string)
        except Base58Error as e:
            raise AddressError(str(e))

        # Require version byte(s) plus hash160.
        if len(raw) != 21:
            raise AddressError('invalid address: {}'.format(string))

        verbyte, hash160_ = raw[0], raw[1:]
        if verbyte == net.ADDRTYPE_P2PKH:
            kind = cls.ADDR_P2PKH
        elif verbyte == net.ADDRTYPE_P2SH:
            kind = cls.ADDR_P2SH
        else:
            raise AddressError('unknown version byte: {}'.format(verbyte))

        return cls(hash160_, kind)

    @classmethod
    def is_valid(cls, string):
        try:
            cls.from_string(string)
            return True
        except Exception:
            return False

    @classmethod
    def from_strings(cls, strings):
        '''Construct a list from an iterable of strings.'''
        return [cls.from_string(string) for string in strings]

    @classmethod
    def from_pubkey(cls, pubkey):
        '''Returns a P2PKH address from a public key.  The public key can
        be bytes or a hex string.'''
        if isinstance(pubkey, str):
            pubkey = hex_to_bytes(pubkey)
        PublicKey.validate(pubkey)
        return cls(hash_160(pubkey), cls.ADDR_P2PKH)

    @classmethod
    def from_P2PKH_hash(cls, hash160value):
        '''Construct from a P2PKH hash160.'''
        return cls(hash160value, cls.ADDR_P2PKH)

    @classmethod
    def from_P2SH_hash(cls, hash160value):
        '''Construct from a P2PKH hash160.'''
        return cls(hash160value, cls.ADDR_P2SH)

    @classmethod
    def from_multisig_script(cls, script):
        return cls(hash_160(script), cls.ADDR_P2SH)

    def to_string(self):
        '''Converts to a string of the given format.'''
        if self.kind == self.ADDR_P2PKH:
            verbyte = Net.ADDRTYPE_P2PKH
        else:
            verbyte = Net.ADDRTYPE_P2SH

        return Base58.encode_check(bytes([verbyte]) + self.hash160)

    def to_script(self):
        '''Return a binary script to pay to the address.'''
        if self.kind == self.ADDR_P2PKH:
            return Script.P2PKH_script(self.hash160)
        else:
            return Script.P2SH_script(self.hash160)

    def to_script_hex(self):
        '''Return a script to pay to the address as a hex string.'''
        return self.to_script().hex()

    def to_scripthash(self):
        '''Returns the hash of the script in binary.'''
        return sha256(self.to_script())

    def to_scripthash_hex(self):
        '''Like other bitcoin hashes this is reversed when written in hex.'''
        return hash_to_hex_str(self.to_scripthash())

    def __str__(self):
        return self.to_string()

    def __repr__(self):
        return '<Address {}>'.format(self.__str__())


def _match_ops(ops, pattern):
    if len(ops) != len(pattern):
        return False
    for op, pop in zip(ops, pattern):
        if pop != op:
            # -1 means 'data push', whose op is an (op, data) tuple
            if pop == -1 and isinstance(op, tuple):
                continue
            return False

    return True


class Script(object):

    @classmethod
    def P2SH_script(cls, hash160value):
        return (bytes([Ops.OP_HASH160])
                + cls.push_data(hash160value)
                + bytes([Ops.OP_EQUAL]))

    @classmethod
    def P2PKH_script(cls, hash160value):
        return (bytes([Ops.OP_DUP, Ops.OP_HASH160])
                + cls.push_data(hash160value)
                + bytes([Ops.OP_EQUALVERIFY, Ops.OP_CHECKSIG]))

    @classmethod
    def P2PK_script(cls, pubkey):
        return cls.push_data(pubkey) + bytes([Ops.OP_CHECKSIG])

    @classmethod
    def multisig_script(cls, m, pubkeys):
        '''Returns the script for a pay-to-multisig transaction.'''
        n = len(pubkeys)
        if not 1 <= m <= n <= 15:
            raise ScriptError('{:d} of {:d} multisig script not possible'
                              .format(m, n))
        for pubkey in pubkeys:
            PublicKey.validate(pubkey)   # Can be compressed or not
        # See https://bitcoin.org/en/developer-guide
        # 2 of 3 is: OP_2 pubkey1 pubkey2 pubkey3 OP_3 OP_CHECKMULTISIG
        return (bytes([Ops.OP_1 + m - 1])
                + b''.join(cls.push_data(pubkey) for pubkey in pubkeys)
                + bytes([Ops.OP_1 + n - 1, Ops.OP_CHECKMULTISIG]))

    @classmethod
    def push_data(cls, data):
        '''Returns the Ops to push the data on the stack.'''
        assert isinstance(data, (bytes, bytearray))

        n = len(data)
        if n < Ops.OP_PUSHDATA1:
            return bytes([n]) + data
        if n < 256:
            return bytes([Ops.OP_PUSHDATA1, n]) + data
        if n < 65536:
            return bytes([Ops.OP_PUSHDATA2]) + struct.pack('<H', n) + data
        return bytes([Ops.OP_PUSHDATA4]) + struct.pack('<I', n) + data

    @classmethod
    def get_ops(cls, script):
        ops = []

        # The unpacks or script[n] below throw on truncated scripts
        try:
            n = 0
            while n < len(script):
                op = script[n]
                n += 1

                if op <= Ops.OP_PUSHDATA4:
                    # Raw bytes follow
                    if op < Ops.OP_PUSHDATA1:
                        dlen = op
                    elif op == Ops.OP_PUSHDATA1:
                        dlen = script[n]
                        n += 1
                    elif op == Ops.OP_PUSHDATA2:
                        dlen, = struct.unpack('<H', script[n: n + 2])
                        n += 2
                    else:
                        dlen, = struct.unpack('<I', script[n: n + 4])
                        n += 4
                    if n + dlen > len(script):
                        raise IndexError
                    op = (op, script[n:n + dlen])
                    n += dlen

                ops.append(op)
        except Exception:
            # Truncated script; e.g. tx_hash
            # ebc9fa1196a59e192352d76c0f6e73167046b9d37b8302b6bb6968dfd279b767
            raise ScriptError('truncated script')

        return ops


class Base58Error(Exception):
    '''Exception used for Base58 errors.'''


class Base58(object):
    '''Class providing base 58 functionality.'''

    chars = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    assert len(chars) == 58
    cmap = {c: n for n, c in enumerate(chars)}

    @staticmethod
    def char_value(c):
        val = Base58.cmap.get(c)
        if val is None:
            raise Base58Error('invalid base 58 character "{}"'.format(c))
        return val

    @staticmethod
    def decode(txt):
        """Decodes txt into a big-endian bytearray."""
        if not isinstance(txt, str):
            raise TypeError('a string is required')

        if not txt:
            raise Base58Error('string cannot be empty')

        value = 0
        for c in txt:
            value = value * 58 + Base58.char_value(c)

        result = int_to_bytes(value)

        # Prepend leading zero bytes if necessary
        count = 0
        for c in txt:
            if c != '1':
                break
            count += 1
        if count:
            result = bytes(count) + result

        return result

    @staticmethod
    def encode(be_bytes):
        """Converts a big-endian bytearray into a base58 string."""
        value = bytes_to_int(be_bytes)

        txt = ''
        while value:
            value, mod = divmod(value, 58)
            txt += Base58.chars[mod]

        for byte in be_bytes:
            if byte != 0:
                break
            txt += '1'

        return txt[::-1]

    @staticmethod
    def decode_check(txt):
        '''Decodes a Base58Check-encoded string to a payload.  The version
        prefixes it.'''
        be_bytes = Base58.decode(txt)
        result, check = be_bytes[:-4], be_bytes[-4:]
        if check != sha256d(result)[:4]:
            raise Base58Error('invalid base 58 checksum for {}'.format(txt))
        return result

    @staticmethod
    def encode_check(payload):
        """Encodes a payload bytearray (which includes the version byte(s))
        into a Base58Check string."""
        be_bytes = payload + sha256d(payload)[:4]
        return Base58.encode(be_bytes)
