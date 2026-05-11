#!/usr/bin/env python3

from dataclasses import dataclass
from typing import Literal, cast, overload
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


def rm_f(path: str):
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
class BencodeData:
    path: str
    data: dict[bytes, BencodeType]

    def _get[T](
        self, key: bytes, type: type[T], optional: bool, default: T | None
    ) -> T | None:
        result = self.data.get(key)
        if result is None:
            if optional or default is not None:
                return default
            raise ConversionError(f"{self.path}.{key.decode()} missing")

        if not isinstance(result, type):
            raise ConversionError(f"{self.path}.{key.decode()} is not {type}")
        return cast(T, result)

    @overload
    def get_bytes(self, key: bytes, *, optional: Literal[False] = False) -> bytes: ...
    @overload
    def get_bytes(self, key: bytes, *, optional: Literal[True]) -> bytes | None: ...
    @overload
    def get_bytes(self, key: bytes, *, default: bytes) -> bytes: ...
    def get_bytes(
        self, key: bytes, *, optional: bool = False, default: bytes | None = None
    ) -> bytes | None:
        return self._get(key, bytes, optional, default)

    @overload
    def get_int(self, key: bytes, *, optional: Literal[False] = False) -> int: ...
    @overload
    def get_int(self, key: bytes, *, optional: Literal[True]) -> int | None: ...
    @overload
    def get_int(self, key: bytes, *, default: int) -> int: ...
    def get_int(
        self, key: bytes, *, optional: bool = False, default: int | None = None
    ) -> int | None:
        return self._get(key, int, optional, default)

    @overload
    def get_list(
        self, key: bytes, *, optional: Literal[False] = False
    ) -> list[BencodeType]: ...
    @overload
    def get_list(
        self, key: bytes, *, optional: Literal[True]
    ) -> list[BencodeType] | None: ...
    def get_list(
        self, key: bytes, *, optional: bool = False
    ) -> list[BencodeType] | None:
        result = self.data.get(key)
        if result is None:
            if optional:
                return None
            raise ConversionError(f"{self.path}.{key.decode()} missing")

        if not isinstance(result, list):
            raise ConversionError(f"{self.path}.{key.decode()} is not {type}")

        return result

    @overload
    def get_dict(
        self, key: bytes, *, optional: Literal[False] = False
    ) -> "BencodeData": ...
    @overload
    def get_dict(
        self, key: bytes, *, optional: Literal[True]
    ) -> "BencodeData | None": ...
    def get_dict(self, key: bytes, *, optional: bool = False) -> "BencodeData | None":
        result = self.data.get(key)
        if result is None:
            if optional:
                return None
            raise ConversionError(f"{self.path}.{key.decode()} missing")

        if not isinstance(result, dict):
            raise ConversionError(f"{self.path}.{key.decode()} is not {type}")

        return BencodeData(f"{self.path}.{key.decode()}", result)


def bencode(data: BencodeType):
    return bencodepy.bencode(data)  # type: ignore


def bdecode(data: bytes) -> BencodeType:
    return bencodepy.bdecode(data)  # type: ignore


def check_for_qbt_sqlite_resume_db(qbt_bt_backup_dir: str):
    torrents_db_path = os.path.join(qbt_bt_backup_dir, "..", "torrents.db")
    if os.path.exists(torrents_db_path):
        raise QbtUsesSqliteForResumeError()


def get_data(root: str, path: str):
    with open(path, "rb") as f:
        try:
            decoded = bdecode(f.read())
        except (ValueError, bencodepy.BencodeDecodeError) as e:
            raise ReadBencodedError(path) from e
    if not isinstance(decoded, dict):
        raise ConversionError(f"{root} is not a dict")
    return BencodeData(root, decoded)


# FIXME BEP0003 says that clients must not perform a decode-encode
# roundtrip on invalid data. this is exactly what this does.
def calc_info_hash(parsed_tor: BencodeData):
    info = parsed_tor.get_dict(b"info")
    return hashlib.sha1(bencode(info.data))


def transmission_get_speed_limit(resume_data: BencodeData, key: bytes):
    speed_limit_obj = resume_data.get_dict(key)
    if speed_limit_obj.get_int(b"use-speed-limit") != 0:
        return speed_limit_obj.get_int(b"speed-Bps")

    return -1


def transmission_get_file_prorities(resume_data: BencodeData):
    priority = resume_data.get_list(b"priority", optional=True)
    dnd = resume_data.get_list(b"dnd", optional=True)

    # Return empty list if priority data is not available
    if priority is None or dnd is None:
        return

    if len(priority) != len(dnd):
        raise ConversionError(
            f"priority and dnd lengths are not equal : {len(priority)} != {len(dnd)}"
        )

    for i, (p, d) in enumerate(zip(priority, dnd, strict=True)):
        if not isinstance(p, int):
            raise ConversionError(f"priority[{i}] is not an int")
        if not isinstance(d, int):
            raise ConversionError(f"dnd[{i}] is not an int")
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


def peers_convert_from_raw_bytes(src: bytes, addr_size: int):
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


def peers_convert_from_bencoded(src: list[BencodeType], key: bytes):
    # used since Transmission commit 1054ba4 (earliest release - 4.1.0)
    rv = bytearray()
    for i, d in enumerate(src):
        if not isinstance(d, dict):
            raise ConversionError(f"{key}[{i}] is not a dict")
        socket_address = d[b"socket_address"]
        if not isinstance(socket_address, bytes):
            raise ConversionError(f"{key}[{i}].socket_address is not a bytes")
        rv += socket_address
    return bytes(rv)


def transmission_get_peers(resume_data: BencodeData, addr_size: int, key: bytes):
    src = resume_data.data.get(key)
    if src is None:
        return b""

    if isinstance(src, list):
        return peers_convert_from_bencoded(src, key)

    if isinstance(src, bytes):
        return peers_convert_from_raw_bytes(src, addr_size)

    raise ConversionError(f"{key} is not a list or a bytes")


def transmission_get_limit(tr_resume: BencodeData, limit_kind: str):
    limit_key = f"{limit_kind}-limit".encode()
    mode_key = f"{limit_kind}-mode".encode()

    limit_obj = tr_resume.get_dict(limit_key)
    limit_mode = limit_obj.get_int(mode_key)

    match limit_mode:
        case 0:  # TR_*LIMIT_GLOBAL
            return -2  # BitTorrent::Torrent::USE_GLOBAL_*
        case 1:  # TR_*LIMIT_SINGLE
            return limit_obj.get_int(limit_key)
        case 2:  # TR_*LIMIT_UNLIMITED
            return -1  # BitTorrent::Torrent::NO_*_LIMIT
        case _:
            raise ConversionError(f"unknown value for {mode_key} : {limit_mode}")


BIT_LOOKUP = [bytes(n >> i & 1 for i in range(7, -1, -1)) for n in range(256)]


def transmission_get_pieces(tr_resume: BencodeData, num_pieces: int):
    tr_piece_bytes = tr_resume.get_dict(b"progress").get_bytes(b"pieces")
    match tr_piece_bytes:
        case b"all":
            return b"\1" * num_pieces
        case b"none":
            return b"\0" * num_pieces
        case _:
            qb_pieces = bytearray()
            for tr_piece_byte in tr_piece_bytes:
                qb_pieces += BIT_LOOKUP[tr_piece_byte]
            return bytes(qb_pieces[:num_pieces].ljust(num_pieces, b"\0"))


def map_resume_to_qbt(resume_data: BencodeData, info_hash: str, num_pieces: int):
    downloading_time_seconds = resume_data.get_int(b"downloading-time-seconds")
    seeding_time_seconds = resume_data.get_int(b"seeding-time-seconds")
    name = resume_data.get_bytes(b"name")

    qbt_resume_data: BencodeType = {
        b"file-format": b"libtorrent resume file",
        b"file-version": 1,
        b"info-hash": binascii.unhexlify(info_hash),
        b"name": name,
        b"total_uploaded": resume_data.get_int(b"uploaded"),
        b"total_downloaded": resume_data.get_int(b"downloaded"),
        b"added_time": resume_data.get_int(b"added-date"),
        b"completed_time": resume_data.get_int(b"done-date"),
        b"active_time": downloading_time_seconds + seeding_time_seconds,
        b"finished_time": downloading_time_seconds,
        b"seeding_time": seeding_time_seconds,
        b"max_connections": resume_data.get_int(b"max-peers"),
        b"upload_rate_limit": transmission_get_speed_limit(
            resume_data, b"speed-limit-up"
        ),
        b"download_rate_limit": transmission_get_speed_limit(
            resume_data, b"speed-limit-down"
        ),
        b"save_path": resume_data.get_bytes(b"destination"),
        b"paused": resume_data.get_int(b"paused"),
        b"sequential_download": resume_data.get_int(b"sequentialDownload", default=0),
        b"file_priority": list(transmission_get_file_prorities(resume_data)),
        b"peers": transmission_get_peers(resume_data, 4, b"peers2"),
        b"peers6": transmission_get_peers(resume_data, 16, b"peers2-6"),
        b"qBt-name": name,
        b"qBt-ratioLimit": transmission_get_limit(resume_data, "ratio"),
        b"qBt-inactiveSeedingTimeLimit": int(
            transmission_get_limit(resume_data, "idle")
        ),
        b"qBt-savePath": resume_data.get_bytes(b"destination"),
    }

    group = resume_data.get_bytes(b"group", optional=True)
    if group is not None:
        qbt_resume_data[b"qBt-category"] = group

    labels = resume_data.get_list(b"labels", optional=True)
    if labels is not None:
        qbt_resume_data[b"qBt-tags"] = labels

    files = resume_data.get_list(b"files", optional=True)
    if files is not None:
        qbt_resume_data[b"mapped_files"] = files

    incomplete_dir = resume_data.get_bytes(b"incomplete_dir", optional=True)
    if incomplete_dir is not None:
        qbt_resume_data[b"qBt-downloadPath"] = incomplete_dir

    paused = resume_data.get_int(b"paused")
    if paused == 1:
        qbt_resume_data[b"auto_managed"] = 0

    qbt_resume_data[b"pieces"] = transmission_get_pieces(resume_data, num_pieces)

    return qbt_resume_data


@dataclass(frozen=True)
class Args:
    qbt_bt_backup_dir: str
    transmission_config_dir: str
    predicate: str | None


class TransmissionQbtImporter:
    def __init__(self, args: Args):
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
        num_pieces: int,
        resume_data: BencodeData,
    ):
        qbt_resume_data = map_resume_to_qbt(resume_data, info_hash, num_pieces)
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
    ):
        parsed_tor = get_data("torrent", source_tor_abs_path)

        resume_data = get_data("resume", source_res_abs_path)

        if info_hash is None:
            info_hash = calc_info_hash(parsed_tor).hexdigest()

        num_pieces = len(parsed_tor.get_dict(b"info").get_list(b"pieces")) // 20

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
            self.copy_to_target(source_tor_abs_path, info_hash, num_pieces, resume_data)
        else:
            logging.info(
                f"Predicate returned {predicate_rv} for torrent {info_hash}, skipping"
            )

    def import_one(self, torf: str):
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

    def scan(self):
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


def main():
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
