#!/usr/bin/env python3

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


def rm_f(path):
    try:
        os.remove(path)
    except (FileNotFoundError, OSError):
        pass


class ReadBencodedError(RuntimeError):
    pass


def read_bencoded(path):
    with open(path, "rb") as f:
        try:
            return bencodepy.bdecode(f.read())
        except (ValueError, bencodepy.BencodeDecodeError) as e:
            raise ReadBencodedError(path) from e


# FIXME BEP0003 says that clients must not perform a decode-encode
# roundtrip on invalid data. this is exactly what this does.
def calc_info_hash(parsed_tor):
    return hashlib.sha1(bencodepy.bencode(parsed_tor[b"info"]))


def transmission_get_speed_limit(resume_data, key):
    speed_limit_obj = resume_data[key]
    if speed_limit_obj[b"use-speed-limit"] != 0:
        return speed_limit_obj[b"speed-Bps"]

    return -1


def transmission_get_file_prorities(resume_data):
    priority = resume_data.get(b"priority")
    dnd = resume_data.get(b"dnd")
    rv = []
    if len(priority) != len(dnd):
        raise ConversionError(
            f"priority and dnd lengths are not equal : {len(priority)} != {len(dnd)}"
        )

    for idx, prio in list(enumerate(priority)):
        if dnd[idx] == 1:
            rv.append(0)  # libtorrent::dont_download
        elif prio == -1:  # TR_PRI_LOW
            rv.append(1)  # libtorrent::low_priority
        elif prio == 0:  # TR_PRI_NORMAL
            rv.append(4)  # libtorrent::default_priority
        elif prio == 1:  # TR_PRI_HIGH
            rv.append(7)  # libtorrent::top_priority

    return rv


def transmission_get_peers(resume_data, addr_size, key):
    src = resume_data.get(key)
    if src is None:
        return b""

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


def transmission_get_limit(tr_resume, limit_kind):
    limit_key = f"{limit_kind}-limit".encode()
    mode_key = f"{limit_kind}-mode".encode()

    limit_obj = tr_resume[limit_key]
    limit_mode = limit_obj[mode_key]
    if limit_mode == 0:  # TR_*LIMIT_GLOBAL
        return "-2"  # BitTorrent::Torrent::USE_GLOBAL_*
    if limit_mode == 1:  # TR_*LIMIT_SINGLE
        return limit_obj[limit_key]
    if limit_mode == 2:  # TR_*LIMIT_UNLIMITED
        return "-1"  # BitTorrent::Torrent::NO_*_LIMIT

    raise ConversionError(f"unknown value for {mode_key} : {limit_mode}")


def map_resume_to_qbt(resume_data, info_hash):
    qbt_resume_data = {
        b"file-format": "libtorrent resume file",
        b"file-version": 1,
        b"info-hash": binascii.unhexlify(info_hash),
        b"name": resume_data[b"name"],
        b"total_uploaded": resume_data[b"uploaded"],
        b"total_downloaded": resume_data[b"downloaded"],
        b"added_time": resume_data[b"added-date"],
        b"completed_time": resume_data[b"done-date"],
        b"active_time": resume_data[b"downloading-time-seconds"]
        + resume_data[b"seeding-time-seconds"],
        b"finished_time": resume_data[b"downloading-time-seconds"],
        b"seeding_time": resume_data[b"seeding-time-seconds"],
        b"max_connections": resume_data[b"max-peers"],
        b"upload_rate_limit": transmission_get_speed_limit(
            resume_data, b"speed-limit-up"
        ),
        b"download_rate_limit": transmission_get_speed_limit(
            resume_data, b"speed-limit-down"
        ),
        b"save_path": resume_data[b"destination"],
        b"paused": resume_data[b"paused"],
        b"sequential_download": resume_data.get(b"sequentialDownload", 0),
        b"file_priority": transmission_get_file_prorities(resume_data),
        b"peers": transmission_get_peers(resume_data, 4, b"peers2"),
        b"peers6": transmission_get_peers(resume_data, 16, b"peers2-6"),
        b"qBt-name": resume_data[b"name"],
        b"qBt-ratioLimit": transmission_get_limit(resume_data, "ratio"),
        b"qBt-inactiveSeedingTimeLimit": int(
            transmission_get_limit(resume_data, "idle")
        ),
        b"qBt-savePath": resume_data[b"destination"],
    }

    if b"group" in resume_data:
        qbt_resume_data[b"qBt-category"] = resume_data[b"group"]

    if b"labels" in resume_data:
        qbt_resume_data[b"qBt-tags"] = (resume_data[b"labels"],)

    if b"files" in resume_data:
        qbt_resume_data[b"mapped_files"] = resume_data[b"files"]

    if b"incomplete-dir" in resume_data:
        qbt_resume_data[b"qBt-downloadPath"] = resume_data[b"incomplete-dir"]

    if resume_data[b"paused"] == 1:
        qbt_resume_data[b"auto_managed"] = 0

    return qbt_resume_data


class TransmissionQbtImporter:
    def __init__(self, args):
        self.source_torrents_dir = os.path.join(
            args.transmission_config_dir, "torrents"
        )
        self.source_resume_dir = os.path.join(args.transmission_config_dir, "resume")
        self.target_dir = args.qbt_bt_backup_dir
        self.predicate = args.predicate
        self.torrent_file_300_rgx = re.compile("([0-9a-f]{40})\\.torrent")
        self.torrent_file_294_rgx = re.compile("\\.[0-9a-f]{16}\\.torrent$")

    def copy_to_target(self, source_tor_abs_path, info_hash, resume_data):
        qbt_resume_data = map_resume_to_qbt(resume_data, info_hash)
        qbt_resume_path = os.path.join(self.target_dir, info_hash + ".fastresume")
        qbt_torrent_path = os.path.join(self.target_dir, info_hash + ".torrent")
        try:
            with open(qbt_resume_path, "wb") as resumf:
                resumf.write(bencodepy.bencode(qbt_resume_data))
            shutil.copy(source_tor_abs_path, qbt_torrent_path)
            logging.info(
                f"Successfully imported {os.path.basename(source_tor_abs_path)} ({info_hash})"
            )

        except:
            logging.warning(
                f"Could not copy files for {os.path.basename(source_tor_abs_path)} ({info_hash}) into {qbt_bt_backup_dir}"
            )
            rm_f(qbt_resume_path)
            rm_f(qbt_torrent_path)

    def copy_if_wanted(self, source_tor_abs_path, parsed_tor, info_hash, resume_data):
        if self.predicate is None:
            self.copy_to_target(source_tor_abs_path, info_hash, resume_data)
            return

        predicate_rv = None
        parsed_tor = (
            read_bencoded(source_tor_abs_path) if parsed_tor is None else parsed_tor
        )
        try:
            predicate_rv = eval(self.predicate)
        except Exception as e:
            logging.info(
                f"Predicate threw {type(e).__name__} with {e} for torrent {info_hash}, skipping"
            )
            return

        if predicate_rv is True:
            self.copy_to_target(source_tor_abs_path, info_hash, resume_data)
        else:
            logging.info(
                f"Predicate returned {predicate_rv} for torrent {info_hash}, skipping"
            )

    def import_one(self, torf):
        match = self.torrent_file_300_rgx.fullmatch(torf)
        if match:
            info_hash = match[1]
            resume_data = read_bencoded(
                os.path.join(self.source_resume_dir, info_hash + ".resume")
            )
            self.copy_if_wanted(
                os.path.join(self.source_torrents_dir, torf),
                None,
                info_hash,
                resume_data,
            )
            return

        match = self.torrent_file_294_rgx.search(torf)
        if match:
            resume_data = read_bencoded(
                os.path.join(
                    self.source_resume_dir, os.path.splitext(torf)[0] + ".resume"
                )
            )
            source_tor_abs_path = os.path.join(self.source_torrents_dir, torf)
            parsed_tor = read_bencoded(source_tor_abs_path)
            info_hash = calc_info_hash(parsed_tor).hexdigest()
            self.copy_if_wanted(
                source_tor_abs_path,
                parsed_tor,
                info_hash,
                resume_data,
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
    args = parser.parse_args()
    importer = TransmissionQbtImporter(args)
    importer.scan()

    return 0


if __name__ == "__main__":
    sys.exit(main())
