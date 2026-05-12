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


@dataclass(frozen=True)
class BencodeNode[T: BencodeType](NamedTuple):
    # A class for convenience, which carries the full path of the node
    # e.g. value=xxx, path="torrent.info.length"
    value: T
    path: str


type BencodeDict = BencodeNode[dict[bytes, BencodeType]]


def build_bencode_node[T: BencodeType](
    type_dest: type[T], value: BencodeType, path: str
) -> BencodeNode[T]:
    # origin is the parent class for generics (e.g. list for list[int])
    # origin is none for non-generics
    # isinstance does not work on generics but it works on the origin of a generic
    # cast works on the generic itself so type inference works
    typecheck = get_origin(type_dest) or type_dest
    if not isinstance(value, typecheck):
        raise ConversionError(f"f{path} is not of type {typecheck}")
    return BencodeNode(cast(T, value), path)


@overload
def get[T: BencodeType](
    type_dest: type[T],
    data: BencodeNode[dict[bytes, BencodeType]],
    key: bytes,
    *,
    default: T | None = None,
) -> BencodeNode[T]: ...
@overload
def get[T: BencodeType](
    type_dest: type[T],
    data: BencodeNode[dict[bytes, BencodeType]],
    key: bytes,
    *,
    optional: Literal[True],
) -> BencodeNode[T] | None: ...
def get[T: BencodeType](
    type_dest: type[T],
    data: BencodeNode[dict[bytes, BencodeType]],
    key: bytes,
    *,
    default: T | None = None,
    optional: bool = False,
) -> BencodeNode[T] | None:
    value = data.value.get(key, default)
    path = f"{data.path}.{key.decode()}"
    if value is None:
        if optional:
            return None
        raise ConversionError(f"{path} is missing")
    return build_bencode_node(type_dest, value, path)


def bencode(data: BencodeType) -> bytes:
    return bencodepy.bencode(data)  # type: ignore


def bdecode(data: bytes) -> BencodeType:
    return bencodepy.bdecode(data)  # type: ignore


def check_for_qbt_sqlite_resume_db(qbt_bt_backup_dir: str) -> None:
    torrents_db_path = os.path.join(qbt_bt_backup_dir, "..", "torrents.db")
    if os.path.exists(torrents_db_path):
        raise QbtUsesSqliteForResumeError()


def get_data(root_path: str, file_path: str) -> BencodeDict:
    with open(file_path, "rb") as f:
        try:
            decoded = bdecode(f.read())
        except (ValueError, bencodepy.BencodeDecodeError) as e:
            raise ReadBencodedError(file_path) from e
    return build_bencode_node(dict[bytes, BencodeType], decoded, root_path)


# FIXME BEP0003 says that clients must not perform a decode-encode
# roundtrip on invalid data. this is exactly what this does.
def calc_info_hash(parsed_tor: BencodeDict) -> str:
    info, _ = get(dict[bytes, BencodeType], parsed_tor, b"info")
    return hashlib.sha1(bencode(info)).hexdigest()


def transmission_get_speed_limit(resume_data: BencodeDict, key: bytes) -> int:
    speed_limit_obj = get(dict[bytes, BencodeType], resume_data, key)
    use_speed_limit, _ = get(int, speed_limit_obj, b"use-speed-limit")
    if use_speed_limit != 0:
        return get(int, speed_limit_obj, b"speed-Bps").value

    return -1


def transmission_get_file_prorities(resume_data: BencodeDict) -> Generator[int]:
    priority_node = get(list[BencodeType], resume_data, b"priority", optional=True)
    dnd_node = get(list[BencodeType], resume_data, b"dnd", optional=True)

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
    for i, d in enumerate(src):
        d_path = f"{key}[{i}]"
        d_dict = build_bencode_node(dict[bytes, BencodeType], d, d_path)
        socket_address = get(bytes, d_dict, b"socket_address").value
        rv += socket_address
    return bytes(rv)


def transmission_get_peers(
    resume_data: BencodeDict, addr_size: int, key: bytes
) -> bytes:
    src = resume_data.value.get(key)
    if src is None:
        return b""

    if isinstance(src, list):
        return peers_convert_from_bencoded(src, key)

    if isinstance(src, bytes):
        return peers_convert_from_raw_bytes(src, addr_size)

    raise ConversionError(f"{key} is not a list or bytes")


def transmission_get_limit(tr_resume: BencodeDict, limit_kind: str) -> int:
    limit_key = f"{limit_kind}-limit".encode()
    limit_obj = get(dict[bytes, BencodeType], tr_resume, limit_key)

    mode_key = f"{limit_kind}-mode".encode()
    limit_mode, _ = get(int, limit_obj, mode_key)

    match limit_mode:
        case 0:  # TR_*LIMIT_GLOBAL
            return -2  # BitTorrent::Torrent::USE_GLOBAL_*
        case 1:  # TR_*LIMIT_SINGLE
            return get(int, limit_obj, limit_key).value
        case 2:  # TR_*LIMIT_UNLIMITED
            return -1  # BitTorrent::Torrent::NO_*_LIMIT
        case _:
            raise ConversionError(f"unknown value for {mode_key} : {limit_mode}")


BLOCK_SIZE = 1 << 14  # 16KiB, standard across torrents


def get_last_piece_mask_for_block_checking(torrent_size: int, piece_size: int) -> bytes:
    # We use masks to determine if torrent pieces are on disk, by extracting progress info
    # from the Transmission resume for the pieces and comparing them to the mask.
    # This method creates the mask for the last piece of the torrent, which is not as trivial
    # as for the other pieces (where the mask is just 1s)
    # This method is used by transmission_get_pieces

    # Size of the last piece
    # Note that in the special case where the torrent size is a multiple of the piece size,
    # the last piece has the same size as all the other pieces
    last_piece_size = torrent_size % piece_size or piece_size

    # Number of blocks in the last piece
    # Ceiling integer division
    blocks_in_last_piece = -(-last_piece_size // BLOCK_SIZE)

    # Get the start of the mask, made of 1s in groups of 8
    mask_full_bytes = blocks_in_last_piece // 8
    piece_mask = b"\xff" * mask_full_bytes

    # Append last byte of the mask: 1s followed by 0s
    mask_extra_bits = blocks_in_last_piece % 8
    if mask_extra_bits:
        # Create a byte made of n 1s followed by (8-n) 0s
        piece_mask += bytes([0xFF << 8 - mask_extra_bits & 0xFF])

    return piece_mask


def transmission_get_pieces(parsed_tor: BencodeDict, tr_resume: BencodeDict) -> bytes:
    info = get(dict[bytes, BencodeType], parsed_tor, b"info")
    torrent_size, _ = get(int, info, b"length")
    piece_size, _ = get(int, info, b"piece length")

    # Sanity check the piece length
    # Only accept piece size in powers of 2
    if piece_size & -piece_size != piece_size:  # This is only true for powers of 2
        raise ConversionError(f"Piece size {piece_size} is not a power of 2")
    # The progress.blocks bytes in the Transmission resume contain 1 bit per data block (where bit=1 means the block is on disk)
    # So each byte in progress.blocks represents 8 blocks
    # The pieces bytes in the qBittorrent resume contains a full byte per piece, where 0x01 means the piece is on disk
    # The algorithm in this method determines if a piece is complete by extracting progress.blocks bytes in groups of n
    # (where n=piece_size//BLOCK_SIZE//8), and checking that they are all 0xff
    # For this to work without having to do bitwise operations, the piece size must be at least 8x the block size
    # The block size is 16KiB so the minimum piece size is 128KiB
    if piece_size < 1 << 17:  # 128KiB
        raise ConversionError(f"Piece size {piece_size} is lower than 128KiB, aborting")

    progress = get(dict[bytes, BencodeType], tr_resume, b"progress")
    blocks, _ = get(bytes, progress, b"blocks")

    # Sanity check the block bytes length
    # ceiling integer division
    expected_num_blocks = -(-torrent_size // BLOCK_SIZE // 8)
    if len(blocks) != expected_num_blocks:
        raise ConversionError(
            f"resume block length was expected to be {expected_num_blocks} but was {len(blocks)}"
        )

    # ceiling integer division
    num_pieces = -(-torrent_size // piece_size)

    # Initialise the return object with 0s
    qbit_pieces = bytearray(num_pieces)

    blocks_per_piece = piece_size // BLOCK_SIZE
    block_bytes_per_piece = blocks_per_piece // 8

    # What a full piece looks like in progress.blocks
    full_piece_mask = b"\xff" * block_bytes_per_piece

    # Go through progress.blocks, grouping them into pieces
    for piece_index in range(num_pieces):
        piece_start = piece_index * block_bytes_per_piece
        piece_blocks = blocks[piece_start : piece_start + block_bytes_per_piece]

        # The last piece is special: if it is not exactly 128KiB then progress.blocks does not end with 0xff even when complete
        # So we need to calculate the expected mask, then compare it to the last byte(s) of the blocks object
        if piece_index == num_pieces - 1:
            full_piece_mask = get_last_piece_mask_for_block_checking(
                torrent_size, piece_size
            )

        if piece_blocks == full_piece_mask:
            qbit_pieces[piece_index] = 1

    return bytes(qbit_pieces)


def map_resume_to_qbt(
    info_hash: str, parsed_tor: BencodeDict, resume_data: BencodeDict
) -> dict[bytes, BencodeType]:
    downloading_time_seconds, _ = get(int, resume_data, b"downloading-time-seconds")
    seeding_time_seconds, _ = get(int, resume_data, b"seeding-time-second")
    name, _ = get(bytes, resume_data, b"name")
    paused, __ = get(int, resume_data, b"paused")

    qbt_resume_data: BencodeType = {
        b"file-format": b"libtorrent resume file",
        b"file-version": 1,
        b"info-hash": binascii.unhexlify(info_hash),
        b"name": name,
        b"total_uploaded": get(int, resume_data, b"uploaded").value,
        b"total_downloaded": get(int, resume_data, b"downloaded").value,
        b"added_time": get(int, resume_data, b"added-date").value,
        b"completed_time": get(int, resume_data, b"done-date").value,
        b"active_time": downloading_time_seconds + seeding_time_seconds,
        b"finished_time": downloading_time_seconds,
        b"seeding_time": seeding_time_seconds,
        b"max_connections": get(int, resume_data, b"max-peers").value,
        b"upload_rate_limit": transmission_get_speed_limit(
            resume_data, b"speed-limit-up"
        ),
        b"download_rate_limit": transmission_get_speed_limit(
            resume_data, b"speed-limit-down"
        ),
        b"save_path": get(bytes, resume_data, b"destination").value,
        b"paused": paused,
        b"sequential_download": get(
            int, resume_data, b"sequentialDownload", default=0
        ).value,
        b"file_priority": list(transmission_get_file_prorities(resume_data)),
        b"peers": transmission_get_peers(resume_data, 4, b"peers2"),
        b"peers6": transmission_get_peers(resume_data, 16, b"peers2-6"),
        b"qBt-name": name,
        b"qBt-ratioLimit": transmission_get_limit(resume_data, "ratio"),
        b"qBt-inactiveSeedingTimeLimit": int(
            transmission_get_limit(resume_data, "idle")
        ),
        b"qBt-savePath": get(bytes, resume_data, b"destination").value,
        b"pieces": transmission_get_pieces(parsed_tor, resume_data),
    }

    group = get(bytes, resume_data, b"group", optional=True)
    if group is not None:
        qbt_resume_data[b"qBt-category"] = group.value

    labels = get(list[BencodeType], resume_data, b"labels", optional=True)
    if labels is not None:
        qbt_resume_data[b"qBt-tags"] = labels.value

    files = get(list[BencodeType], resume_data, b"files", optional=True)
    if files is not None:
        qbt_resume_data[b"mapped_files"] = files.value

    incomplete_dir = get(bytes, resume_data, b"incomplete_dir", optional=True)
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


class TransmissionQbtImporter:
    def __init__(self, args: Args) -> None:
        check_for_qbt_sqlite_resume_db(args.qbt_bt_backup_dir)
        self.source_torrents_dir = os.path.join(
            args.transmission_config_dir, "torrents"
        )
        self.source_resume_dir = os.path.join(args.transmission_config_dir, "resume")
        self.target_dir = args.qbt_bt_backup_dir
        self.predicate = args.predicate
        self.torrent_file_300_rgx = re.compile("([0-9a-f]{40})\\.torrent")
        self.torrent_file_294_rgx = re.compile("\\.[0-9a-f]{16}\\.torrent$")

    def copy_to_target(
        self,
        source_tor_abs_path: str,
        info_hash: str,
        parsed_tor: BencodeDict,
        resume_data: BencodeDict,
    ) -> None:
        qbt_resume_data = map_resume_to_qbt(info_hash, parsed_tor, resume_data)
        qbt_resume_path = os.path.join(self.target_dir, info_hash + ".fastresume")
        qbt_torrent_path = os.path.join(self.target_dir, info_hash + ".torrent")
        try:
            with open(qbt_resume_path, "wb") as resumf:
                resumf.write(bencode(qbt_resume_data))
            shutil.copy(source_tor_abs_path, qbt_torrent_path)
            logging.info(
                f"Successfully imported {os.path.basename(source_tor_abs_path)} ({info_hash})"
            )

        except:
            logging.warning(
                f"Could not copy files for {os.path.basename(source_tor_abs_path)} ({info_hash}) into {self.target_dir}"
            )
            rm_f(qbt_resume_path)
            rm_f(qbt_torrent_path)

    def copy_if_wanted(
        self,
        source_tor_abs_path: str,
        source_res_abs_path: str,
        info_hash: str | None,
    ) -> None:
        parsed_tor = get_data("torrent", source_tor_abs_path)

        resume_data = get_data("resume", source_res_abs_path)

        if info_hash is None:
            info_hash = calc_info_hash(parsed_tor)

        if self.predicate is None:
            predicate_rv = True
        else:
            try:
                predicate_rv = eval(self.predicate)
            except Exception as e:
                logging.info(
                    f"Predicate threw {type(e).__name__} with {e} for torrent {info_hash}, skipping"
                )
                return

        if predicate_rv is True:
            self.copy_to_target(source_tor_abs_path, info_hash, parsed_tor, resume_data)
        else:
            logging.info(
                f"Predicate returned {predicate_rv} for torrent {info_hash}, skipping"
            )

    def import_one(self, torf: str) -> None:
        match = self.torrent_file_300_rgx.fullmatch(torf)
        if match:
            info_hash = match[1]
            self.copy_if_wanted(
                os.path.join(self.source_torrents_dir, torf),
                os.path.join(self.source_resume_dir, info_hash + ".resume"),
                match[1],
            )
            return

        match = self.torrent_file_294_rgx.search(torf)
        if match:
            self.copy_if_wanted(
                os.path.join(self.source_torrents_dir, torf),
                os.path.join(
                    self.source_resume_dir, os.path.splitext(torf)[0] + ".resume"
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
                        f"Error while converting resume data for {torf} : {str(e)}"
                    )
                except OSError as e:
                    logging.warning(
                        f"Failed to read {e.filename} ({e.strerror}), skipping"
                    )
                except ReadBencodedError as e:
                    logging.warning(f"Failed to decode {str(e)}, skipping")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")
    parser = argparse.ArgumentParser(
        prog="transmission2qbt",
        description="Imports all your torrents from Transmission to qBittorrent while trying to preserve as much metadata as possible",
        epilog="See https://github.com/undertheironbridge/transmission2qbt for updates",
    )
    parser.add_argument(
        "transmission_config_dir",
        action="store",
        help="The root configuration directory of the Transmission instance whose torrents to import",
    )
    parser.add_argument(
        "qbt_bt_backup_dir",
        action="store",
        help="The BT_backup directory inside target qBittorrent instance's data directory",
    )
    parser.add_argument(
        "--predicate",
        action="store",
        help="A Python expression for filtering source torrents",
    )

    args = cast(Args, parser.parse_args())
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
