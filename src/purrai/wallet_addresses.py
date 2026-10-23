"""Recognize common on-chain address encodings without network lookups."""

# Address formats: bitcoin/bips BIP-173 and BIP-350; Solana account-structure;
# developers.tron.network/docs/encoding; docs.ton.org/foundations/addresses/formats.

import base64
import binascii
import hashlib
import re
import unicodedata

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
RIPPLE58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
TOKENS = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"0[xX][0-9a-fA-F]{40}|41[0-9a-fA-F]{40}|(?:-1|0):[0-9a-fA-F]{64}|"
    r"[A-Za-z0-9_+/\-]{48}|[A-Za-z0-9]{26,90})(?![A-Za-z0-9_])"
)


def decode58(value, alphabet=BASE58):
    number = 0
    for char in value:
        number = number * 58 + alphabet.index(char)
    return b"\0" * (len(value) - len(value.lstrip(alphabet[0]))) + number.to_bytes(
        (number.bit_length() + 7) // 8, "big"
    )


def base58_address(value, alphabet=BASE58):
    try:
        data = decode58(value, alphabet)
    except ValueError:
        return False
    if alphabet == BASE58 and len(data) == 32:
        # Solana account addresses carry no checksum. Transaction signatures
        # decode to 64 bytes and must not match this branch.
        return True
    versions = (0,) if alphabet == RIPPLE58 else (0, 5, 30, 22, 48, 50, 65, 111, 196)
    return bool(
        len(data) == 25 and data[0] in versions
        and hashlib.sha256(hashlib.sha256(data[:-4]).digest()).digest()[:4] == data[-4:]
    )


def bech32_address(value):
    if value.lower() != value and value.upper() != value:
        return False
    value = value.lower()
    prefix, separator, encoded = value.rpartition("1")
    if not separator or prefix not in ("bc", "tb", "bcrt", "ltc", "tltc", "cosmos", "osmo", "bnb", "inj", "sei"):
        return False
    try:
        data = [BECH32.index(char) for char in encoded]
    except ValueError:
        return False
    if len(data) < 7:
        return False
    checksum = 1
    expanded = [ord(char) >> 5 for char in prefix] + [0] + [ord(char) & 31 for char in prefix]
    for number in expanded + data:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ number
        for index, generator in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            if (top >> index) & 1:
                checksum ^= generator
    if prefix in ("cosmos", "osmo", "bnb", "inj", "sei"):
        return checksum == 1 and len(data) == 38  # 20-byte account + checksum
    version, payload = data[0], data[1:-6]
    if version > 16 or checksum != (1 if version == 0 else 0x2BC830A3):
        return False
    bits, accumulator, size = 0, 0, 0
    for number in payload:
        accumulator = (accumulator << 5) | number
        bits += 5
        if bits >= 8:
            bits -= 8
            size += 1
    return bool(
        bits < 5 and (accumulator & ((1 << bits) - 1)) == 0
        and 2 <= size <= 40 and (version != 0 or size in (20, 32))
    )


def ton_address(value):
    if len(value) != 48:
        return False
    try:
        data = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return False
    return bool(
        len(data) == 36 and data[0] & 0x7F in (0x11, 0x51)
        and data[1] in (0, 255)
        and binascii.crc_hqx(data[:-2], 0).to_bytes(2, "big") == data[-2:]
    )


def contains_wallet_address(text):
    text = "".join(char for char in unicodedata.normalize("NFKC", text) if unicodedata.category(char) != "Cf")
    for match in TOKENS.finditer(text):
        value = match.group()
        if (
            re.fullmatch(r"0[xX][0-9a-fA-F]{40}|41[0-9a-fA-F]{40}|(?:-1|0):[0-9a-fA-F]{64}", value)
            or ton_address(value) or bech32_address(value)
            or base58_address(value) or (value.startswith("r") and base58_address(value, RIPPLE58))
        ):
            return True
    return False
