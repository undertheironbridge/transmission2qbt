#!/usr/bin/env python3

from dataclasses import dataclass
from typing import Literal, NamedTuple, cast, get_origin, overload
from collections.abc import Generator
import sys
import os
import argparse
import logging
import re
import shutil
import binascii
import hashlib


import bencodepy
from humanize import naturalsize


class ConversionError(RuntimeError):
    pass


def rm_f(path: str) -> None:
    try:
        os.remove(path)
    except (FileNotFoundError, OSError):
        pass


class ReadBencodedError(RuntimeError):
    pass


class QbtUsesSqliteForResumeError(RuntimeError):
    pass


type BencodeType = bytes | int | list[BencodeType] | dict[bytes, BencodeType]


class BencodeNode[T: BencodeType](NamedTuple):
    # A class for convenience, which carries the full path of the node
    # e.g. value=xxx, path="torrent.info.length"
    value: T
    path: str


type BencodeDict = BencodeNode[dict[bytes, BencodeType]]


def build_node[T: BencodeType](
    type: type[T], value: BencodeType, path: str
) -> BencodeNode[T]:
    # origin is the parent class for generics (e.g. list for list[int])
    # origin is none for non-generics
    # isinstance does not work on generics but it works on the origin of a generic
    # cast works on the generic itself so type inference works
    typecheck = get_origin(type) or type
    if not isinstance(value, typecheck):
        raise ConversionError(f"f{path} is not of type {typecheck}")
    return BencodeNode(cast(T, value), path)


@overload
def get_child[T: BencodeType](
    parent: BencodeNode[dict[bytes, BencodeType]],
    child_name: bytes,
    child_type: type[T],
) -> BencodeNode[T]: ...
@overload
def get_child[T: BencodeType](
    parent: BencodeNode[dict[bytes, BencodeType]],
    child_name: bytes,
    child_type: type[T],
    *,
    default: T,
) -> BencodeNode[T]: ...
@overload
def get_child[T: BencodeType](
    parent: BencodeNode[dict[bytes, BencodeType]],
    child_name: bytes,
    child_type: type[T],
    *,
    optional: Literal[True],
) -> BencodeNode[T] | None: ...
def get_child[T: BencodeType](
    parent: BencodeNode[dict[bytes, BencodeType]],
    child_name: bytes,
    child_type: type[T],
    *,
    default: T | None = None,
    optional: bool = False,
) -> BencodeNode[T] | None:
    child_value = parent.value.get(child_name, default)
    child_path = f"{parent.path}.{child_name.decode()}"
    if child_value is None:
        if optional:
            return None
        raise ConversionError(f"{child_path} is missing")
    return build_node(child_type, child_value, child_path)


def bencode(data: BencodeType) -> bytes:
    return bencodepy.bencode(data)  # type: ignore


def bdecode(data: bytes) -> BencodeType:
    return bencodepy.bdecode(data)  # type: ignore


def check_for_qbt_sqlite_resume_db(qbt_bt_backup_dir: str) -> None:
    torrents_db_path = os.path.join(qbt_bt_backup_dir, "..", "torrents.db")
    if os.path.exists(torrents_db_path):
        raise QbtUsesSqliteForResumeError()


def read_bencoded(file_path: str, node_path: str) -> BencodeDict:
    with open(file_path, "rb") as f:
        try:
            decoded = bdecode(f.read())
        except (ValueError, bencodepy.BencodeDecodeError) as e:
            raise ReadBencodedError(file_path) from e
    return build_node(dict[bytes, BencodeType], decoded, node_path)


# FIXME BEP0003 says that clients must not perform a decode-encode
# roundtrip on invalid data. this is exactly what this does.
def calc_info_hash(torrent: BencodeDict) -> str:
    info, _ = get_child(torrent, b"info", dict[bytes, BencodeType])
    return hashlib.sha1(bencode(info)).hexdigest()


def transmission_get_speed_limit(resume: BencodeDict, key: bytes) -> int:
    speed_limit_obj = get_child(resume, key, dict[bytes, BencodeType])
    use_speed_limit, _ = get_child(speed_limit_obj, b"use-speed-limit", int)
    if use_speed_limit != 0:
        return get_child(speed_limit_obj, b"speed-Bps", int).value

    return -1


def transmission_get_file_priorities(resume: BencodeDict) -> Generator[int]:
    priority_node = get_child(resume, b"priority", list[BencodeType], optional=True)
    dnd_node = get_child(resume, b"dnd", list[BencodeType], optional=True)

    # Return empty list if priority data is not available
    if priority_node is None or dnd_node is None:
        return

    priority = priority_node.value
    dnd = dnd_node.value
    if len(priority) != len(dnd):
        raise ConversionError(
            f"priority and dnd lengths are not equal : {len(priority)} != {len(dnd)}"
        )

    for i, (p, d) in enumerate(zip(priority, dnd, strict=True)):
        if d == 1:
            yield 0  # libtorrent::dont_download
        else:
            match p:
                case -1:  # TR_PRI_LOW
                    yield 1  # libtorrent::low_priority
                case 0:  # TR_PRI_NORMAL
                    yield 4  # libtorrent::default_priority
                case 1:  # TR_PRI_HIGH
                    yield 7  # libtorrent::top_priority
                case _:
                    raise ConversionError(f"Unknown priority[{i}]: {p}")


def peers_convert_from_raw_bytes(src: bytes, addr_size: int) -> bytes:
    rv = bytearray()
    i = 0
    while i < len(src):
        i += 4  # type
        rv += src[i : (i + addr_size)]  # addr
        i += max(addr_size, 16)
        rv += src[i : (i + 2)]  # port
        i += 2
        i += 2  # flags
    return bytes(rv)


def peers_convert_from_bencoded(src: list[BencodeType], key: bytes) -> bytes:
    # used since Transmission commit 1054ba4 (earliest release - 4.1.0)
    rv = bytearray()
    for i, x in enumerate(src):
        d = build_node(dict[bytes, BencodeType], x, f"{key}[{i}]")
        socket_address, _ = get_child(d, b"socket_address", bytes)
        rv += socket_address
    return bytes(rv)


def transmission_get_peers(resume: BencodeDict, addr_size: int, key: bytes) -> bytes:
    src = resume.value.get(key)
    if src is None:
        return b""

    if isinstance(src, list):
        return peers_convert_from_bencoded(src, key)

    if isinstance(src, bytes):
        return peers_convert_from_raw_bytes(src, addr_size)

    raise ConversionError(f"{key} is not a list or bytes")


def transmission_get_limit(resume: BencodeDict, limit_kind: str) -> int:
    limit_key = f"{limit_kind}-limit".encode()
    limit_obj = get_child(resume, limit_key, dict[bytes, BencodeType])

    mode_key = f"{limit_kind}-mode".encode()
    limit_mode, _ = get_child(limit_obj, mode_key, int)

    match limit_mode:
        case 0:  # TR_*LIMIT_GLOBAL
            return -2  # BitTorrent::Torrent::USE_GLOBAL_*
        case 1:  # TR_*LIMIT_SINGLE
            return get_child(limit_obj, limit_key, int).value
        case 2:  # TR_*LIMIT_UNLIMITED
            return -1  # BitTorrent::Torrent::NO_*_LIMIT
        case _:
            raise ConversionError(f"unknown value for {mode_key} : {limit_mode}")


# On transmission_get_pieces & is_piece_complete
#
# The blocks data the in Transmission resume (tr_blocks) is a bitmask in the form of a bytes object (an immutable array of bytes, aka a
# binary string).
# Each byte represents 8 blocks: each bit in that byte represents a single block, i.e. a 16KiB chunk of torrent data.
# Each bit is equal to 1 if the block is on disk, 0 if not.
#
# The pieces data in the qBittorrent/libtorrent resume (qb_pieces) is also a bytes object but not a bitmask: each byte represent a piece,
# and is equal to 0x01 if the piece is on disk, 0x00 if not (only one of the 8 bits is used in each byte, the first 7 bits are always 0).
#
# Blocks are the atomic amount of data used for storage. Pieces are used in the hash calculation (in Bittorrent v1), the protocol, and in
# the resume files, such as qb_pieces here.
# There is an additional field (unfinished) in the qBittorrent resume to store block info for incomplete pieces, but this field is ignored
# here, see README.md.
#
# The piece size is a multiple of the block size, but not always a multiple of 8*16KiB (the data chunk represented by a tr_blocks byte).
# Which means that a tr_blocks byte might represent more than one qb_pieces byte, and vice versa; it also means that the first block of each
# piece will not necessarily be aligned with the leftmost bit of a tr_blocks byte.
#
# So to understand which pieces are fully on disk (which is what we need to build qb_pieces), we need to extract piece_blocks for each piece
# in tr_blocks, and check if they are all 1.
# We start by finding what tr_block byte the piece starts and ends with. It might start midway through a byte (i.e. not the leftmost bit of
# the start byte) and likewise it might end midway through a byte (not the rightmost bit of the end byte). For this reason the first and last
# byte are checked using a bitwise masking operation, with a precalculated dict of masks indexed with the order of the first and last bit.
# For instance, if the start bit order is 3, then the mask is 00011111 (0x1f). We mask the byte coming from tr_blocks against this
# i.e. tr_blocks[piece_start] & 0x1f == 0x1f. The masks for the end bit order are reversed i.e. if the bit order is 3, the mask is
# 11110000 (0xf0) so we will check if tr_blocks[piece_end] & 0xf0 == 0xf0.
# All the tr_blocks bytes between the first and the last are fully part of the piece, so if the piece is complete the block bytes are all
# 0xff. No need to mask for these, we can just compare them all to 0xff.
#
# There is a special case when a piece is fully contained within a block byte (which only happens when the piece length is <= 8*16KiB).
# In this case there is only one block byte to check and we need to mask it against both the start and end mask.
#
# We also need to take in account that the total number of blocks might not align with the end of a piece, i.e. the last piece can be smaller.
#
# In another special case, tr_blocks might contain fewer bytes than expected, because Transmission does not write the end of tr_blocks when the
# end of the torrent is not on disk (it will only write up to the last block that is on disk). For that reason when retrieving a piece we might
# get fewer bytes than expected. In that case we need to return False since it means that the piece is not on disk.
#
# Finally, Transmission sets the whole tr_blocks to b"all" for complete torrents and b"none" for empty torrents, so these special cases must
# be dealt with separately.

BLOCK_SIZE = 1 << 14  # 16KiB, standard across torrents
FIRST_BYTE_MASK_BY_BIT_ORDER = {i: 0xFF >> i for i in range(0, 8)}
LAST_BYTE_MASK_BY_BIT_ORDER = {i: 0xFF << 7 - i & 0xFF for i in range(0, 8)}


def is_piece_complete(
    tr_blocks: bytes, piece_index: int, blocks_per_piece: int, num_blocks: int
):
    # Indexes of the first block of the piece
    # In total blocks
    piece_start_in_overall_blocks = piece_index * blocks_per_piece
    # In tr_blocks
    piece_start_in_block_bytes = piece_start_in_overall_blocks // 8
    # in the specific tr_blocks byte
    piece_start_bit_order = piece_start_in_overall_blocks % 8

    # Indexes of the last block of the piece
    # In total blocks
    piece_end_in_overall_blocks = (
        min(piece_start_in_overall_blocks + blocks_per_piece, num_blocks) - 1
    )
    # In tr_blocks
    piece_end_in_block_bytes = piece_end_in_overall_blocks // 8
    # in the specific tr_blocks byte
    piece_end_bit_order = piece_end_in_overall_blocks % 8

    num_piece_bytes = piece_end_in_block_bytes - piece_start_in_block_bytes + 1
    piece_bytes = tr_blocks[
        piece_start_in_block_bytes : piece_start_in_block_bytes + num_piece_bytes
    ]
    if len(piece_bytes) < num_piece_bytes:
        # tr_blocks is shorter than expected, meaning the piece is not complete
        return False
    first_byte_mask = FIRST_BYTE_MASK_BY_BIT_ORDER[piece_start_bit_order]
    last_byte_mask = LAST_BYTE_MASK_BY_BIT_ORDER[piece_end_bit_order]
    if num_piece_bytes == 1:
        # If there is only one byte we mask as first and last byte
        byte_mask = first_byte_mask & last_byte_mask
        return piece_bytes[0] & byte_mask == byte_mask
    if piece_bytes[0] & first_byte_mask != first_byte_mask:
        # If the piece fails the mask for the first byte
        return False
    if piece_bytes[-1] & last_byte_mask != last_byte_mask:
        # If the piece fails the mask for the last byte
        return False
    # Test all bytes except the first and the last (should all be 0xff)
    return piece_bytes[1:-1] == b"\xff" * (num_piece_bytes - 2)


def transmission_get_pieces(torrent: BencodeDict, resume: BencodeDict) -> bytes:
    info = get_child(torrent, b"info", dict[bytes, BencodeType])
    torrent_size_node = get_child(info, b"length", int, optional=True)
    if torrent_size_node is None:
        files, files_path = get_child(info, b"files", list[BencodeType])
        torrent_size = 0
        for i, f in enumerate(files):
            file_dict = build_node(dict[bytes, BencodeType], f, f"{files_path}[{i}]")
            torrent_size += get_child(file_dict, b"length", int).value
    else:
        torrent_size, _ = torrent_size_node
    piece_size, _ = get_child(info, b"piece length", int)

    # Sanity check the piece size
    if piece_size % BLOCK_SIZE > 0:
        raise ConversionError(
            f"Piece size {piece_size} is not a multiple of {naturalsize(BLOCK_SIZE, binary=True)}, aborting"
        )

    blocks_per_piece = piece_size // BLOCK_SIZE
    # ceiling integer division
    num_pieces = -(-torrent_size // piece_size)
    num_blocks = -(-torrent_size // BLOCK_SIZE)

    progress = get_child(resume, b"progress", dict[bytes, BencodeType])
    tr_blocks, _ = get_child(progress, b"blocks", bytes)

    # Shorthand for complete torrents
    if tr_blocks == b"all":
        return b"\x01" * num_pieces
    # Shorthand for empty torrents
    if tr_blocks == b"none":
        return b"\x00" * num_pieces

    # Sanity check the block bytes length
    # ceiling integer division
    expected_block_bytes = -(-num_blocks // 8)
    if len(tr_blocks) > expected_block_bytes:
        raise ConversionError(
            f"the resume block length was expected to be {expected_block_bytes} but was {len(tr_blocks)}"
        )

    # Initialise the return object with 0s
    qb_pieces = bytearray(num_pieces)

    # Go through progress.blocks, grouping them into pieces
    for piece_index in range(num_pieces):
        if is_piece_complete(tr_blocks, piece_index, blocks_per_piece, num_blocks):
            qb_pieces[piece_index] = 0x01

    return bytes(qb_pieces)


def map_resume_to_qbt(
    info_hash: str, torrent: BencodeDict, resume: BencodeDict
) -> dict[bytes, BencodeType]:
    downloading_time_seconds, _ = get_child(resume, b"downloading-time-seconds", int)
    seeding_time_seconds, _ = get_child(resume, b"seeding-time-seconds", int)
    name, _ = get_child(resume, b"name", bytes)
    paused, _ = get_child(resume, b"paused", int)

    qbt_resume_data: BencodeType = {
        b"file-format": b"libtorrent resume file",
        b"file-version": 1,
        b"info-hash": binascii.unhexlify(info_hash),
        b"name": name,
        b"total_uploaded": get_child(resume, b"uploaded", int).value,
        b"total_downloaded": get_child(resume, b"downloaded", int).value,
        b"added_time": get_child(resume, b"added-date", int).value,
        b"completed_time": get_child(resume, b"done-date", int).value,
        b"active_time": downloading_time_seconds + seeding_time_seconds,
        b"finished_time": downloading_time_seconds,
        b"seeding_time": seeding_time_seconds,
        b"max_connections": get_child(resume, b"max-peers", int).value,
        b"upload_rate_limit": transmission_get_speed_limit(resume, b"speed-limit-up"),
        b"download_rate_limit": transmission_get_speed_limit(
            resume, b"speed-limit-down"
        ),
        b"save_path": get_child(resume, b"destination", bytes).value,
        b"paused": paused,
        b"sequential_download": get_child(
            resume, b"sequentialDownload", int, default=0
        ).value,
        b"file_priority": list(transmission_get_file_priorities(resume)),
        b"peers": transmission_get_peers(resume, 4, b"peers2"),
        b"peers6": transmission_get_peers(resume, 16, b"peers2-6"),
        b"qBt-name": name,
        b"qBt-ratioLimit": transmission_get_limit(resume, "ratio"),
        b"qBt-inactiveSeedingTimeLimit": int(transmission_get_limit(resume, "idle")),
        b"qBt-savePath": get_child(resume, b"destination", bytes).value,
        b"pieces": transmission_get_pieces(torrent, resume),
    }

    group = get_child(resume, b"group", bytes, optional=True)
    if group is not None:
        qbt_resume_data[b"qBt-category"] = group.value

    labels = get_child(resume, b"labels", list[BencodeType], optional=True)
    if labels is not None:
        qbt_resume_data[b"qBt-tags"] = labels.value

    files = get_child(resume, b"files", list[BencodeType], optional=True)
    if files is not None:
        qbt_resume_data[b"mapped_files"] = files.value

    incomplete_dir = get_child(resume, b"incomplete_dir", bytes, optional=True)
    if incomplete_dir is not None:
        qbt_resume_data[b"qBt-downloadPath"] = incomplete_dir.value

    if paused == 1:
        qbt_resume_data[b"auto_managed"] = 0

    return qbt_resume_data


@dataclass(frozen=True)
class Args:
    qbt_bt_backup_dir: str
    transmission_config_dir: str
    predicate: str | None
    dry_run: bool
    log_level: int


class TransmissionQbtImporter:
    def __init__(self, args: Args) -> None:
        check_for_qbt_sqlite_resume_db(args.qbt_bt_backup_dir)
        self.source_torrents_dir = os.path.join(
            args.transmission_config_dir, "torrents"
        )
        self._source_resume_dir = os.path.join(args.transmission_config_dir, "resume")
        self._target_dir = args.qbt_bt_backup_dir
        self._predicate = args.predicate
        self._dry_run = args.dry_run
        self._torrent_file_300_rgx = re.compile("([0-9a-f]{40})\\.torrent")
        self._torrent_file_294_rgx = re.compile("\\.[0-9a-f]{16}\\.torrent$")

    def copy_to_target(
        self,
        source_tor_abs_path: str,
        info_hash: str,
        torrent: BencodeDict,
        resume: BencodeDict,
    ) -> None:
        qbt_resume_data = map_resume_to_qbt(info_hash, torrent, resume)
        qbt_resume_enc = bencode(qbt_resume_data)
        qbt_resume_path = os.path.join(self._target_dir, info_hash + ".fastresume")
        qbt_torrent_path = os.path.join(self._target_dir, info_hash + ".torrent")
        if self._dry_run:
            logging.info(
                f"dry run: would save {naturalsize(len(qbt_resume_enc), binary=True)} to {qbt_resume_path}"
            )
            logging.info(
                f"dry run: would copy {source_tor_abs_path} to {qbt_torrent_path}"
            )
        else:
            try:
                with open(qbt_resume_path, "wb") as resumf:
                    resumf.write(qbt_resume_enc)
                shutil.copy(source_tor_abs_path, qbt_torrent_path)
                logging.info(
                    f"Successfully imported {os.path.basename(source_tor_abs_path)} ({info_hash})"
                )

            except:
                logging.warning(
                    f"Could not copy files for {os.path.basename(source_tor_abs_path)} ({info_hash}) into {self._target_dir}"
                )
                rm_f(qbt_resume_path)
                rm_f(qbt_torrent_path)

    def copy_if_wanted(
        self,
        source_tor_abs_path: str,
        source_res_abs_path: str,
        info_hash: str | None,
    ) -> None:
        torrent = read_bencoded(source_tor_abs_path, "torrent")

        resume = read_bencoded(source_res_abs_path, "resume")

        if info_hash is None:
            info_hash = calc_info_hash(torrent)

        if self._predicate is None:
            predicate_rv = True
        else:
            try:
                predicate_rv = eval(self._predicate)
            except Exception as e:
                logging.info(
                    f"Predicate threw {type(e).__name__} with {e} for torrent {info_hash}, skipping"
                )
                return

        if predicate_rv:
            self.copy_to_target(source_tor_abs_path, info_hash, torrent, resume)
        else:
            logging.info(
                f"Predicate returned {predicate_rv} for torrent {info_hash}, skipping"
            )

    def import_one(self, torf: str) -> None:
        match = self._torrent_file_300_rgx.fullmatch(torf)
        if match:
            info_hash = match[1]
            self.copy_if_wanted(
                os.path.join(self.source_torrents_dir, torf),
                os.path.join(self._source_resume_dir, info_hash + ".resume"),
                match[1],
            )
            return

        match = self._torrent_file_294_rgx.search(torf)
        if match:
            self.copy_if_wanted(
                os.path.join(self.source_torrents_dir, torf),
                os.path.join(
                    self._source_resume_dir, os.path.splitext(torf)[0] + ".resume"
                ),
                None,
            )
            return

        logging.warning(f"Unknown file {torf} found in torrents directory, skipping")

    def scan(self) -> None:
        for _, _, files in os.walk(self.source_torrents_dir):
            for torf in files:
                try:
                    self.import_one(torf)

                except ConversionError as e:
                    logging.warning(
                        f"Error while converting resume data for {torf} : {e}"
                    )
                except OSError as e:
                    logging.warning(
                        f"Failed to read {e.filename} ({e.strerror}), skipping"
                    )
                except ReadBencodedError as e:
                    logging.warning(f"Failed to decode {e}, skipping")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="transmission2qbt",
        description="Imports all your torrents from Transmission to qBittorrent while trying to preserve as much metadata as possible",
        epilog="See https://github.com/undertheironbridge/transmission2qbt for updates",
    )
    parser.add_argument(
        "transmission_config_dir",
        help="The root configuration directory of the Transmission instance whose torrents to import",
    )
    parser.add_argument(
        "qbt_bt_backup_dir",
        help="The BT_backup directory inside target qBittorrent instance's data directory",
    )
    parser.add_argument(
        "--predicate",
        "-p",
        help="A Python expression for filtering source torrents",
    )
    parser.add_argument(
        "--dry-run",
        "-d",
        action="store_true",
        help="Optional flag to not write any data to disk",
    )
    parser.add_argument(
        "--log-level",
        "-l",
        type=int,
        default=logging.INFO,
        help="Optional flag to set log level, see https://docs.python.org/3/library/logging.html#logging-levels",
    )

    args = cast(Args, parser.parse_args())
    logging.basicConfig(level=args.log_level, format="%(levelname)s: %(message)s")

    try:
        TransmissionQbtImporter(args).scan()
    except QbtUsesSqliteForResumeError:
        logging.error(
            """It looks like your qBittorrent instance uses the experimental SQLite-based
implementation for resume data storage. This is not supported. If you want to
use this script, go to Tools > Preferences > Advanced and change "Resume data
storage type" to "Fastresume files". Then, restart qBittorrent, close it, and
try running this script again."""
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
