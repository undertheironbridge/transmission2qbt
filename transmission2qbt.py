#!/usr/bin/env python3

import sys
import os
import bencodepy
import argparse
import logging
import re
import shutil
import binascii


class ConversionError(RuntimeError):
    pass


def rm_f(path):
    try:
        os.remove(path)
    except (FileNotFoundError, OSError):
        pass


def transmission_get_speed_limit(resume_data, key):
    speed_limit_obj = resume_data[key]
    if speed_limit_obj[b"use-speed-limit"] != 0:
        return speed_limit_obj[b"speed-Bps"]
    else:
        return -1


def transmission_get_file_prorities(resume_data):
    priority = resume_data.get(b"priority")
    dnd = resume_data.get(b"dnd")
    rv = []
    if len(priority) != len(dnd):
        raise ConversionError(
            f"priority and dnd lengths are not equal : {len(priority)} != {len(dnd)}"
        )

    for i in range(0, len(priority)):
        if dnd[i] == 1:
            rv.append(0)  # libtorrent::dont_download
        elif priority[i] == -1:  # TR_PRI_LOW
            rv.append(1)  # libtorrent::low_priority
        elif priority[i] == 0:  # TR_PRI_NORMAL
            rv.append(4)  # libtorrent::default_priority
        elif priority[i] == 1:  # TR_PRI_HIGH
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


def transmission_get_limit(tr_resume, type):
    limit_key = f"{type}-limit".encode()
    mode_key = f"{type}-mode".encode()

    limit_obj = tr_resume[limit_key]
    limit_mode = limit_obj[mode_key]
    if limit_mode == 0:  # TR_*LIMIT_GLOBAL
        return "-2"  # BitTorrent::Torrent::USE_GLOBAL_*
    elif limit_mode == 1:  # TR_*LIMIT_SINGLE
        return limit_obj[limit_key]
    elif limit_mode == 2:  # TR_*LIMIT_UNLIMITED
        return "-1"  # BitTorrent::Torrent::NO_*_LIMIT
    else:
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
        b"mapped_files": resume_data[b"files"],
        b"peers": transmission_get_peers(resume_data, 4, b"peers2"),
        b"peers6": transmission_get_peers(resume_data, 16, b"peers2-6"),
        b"qBt-category": resume_data[b"group"],
        b"qBt-name": resume_data[b"name"],
        b"qBt-tags": resume_data[b"labels"],
        b"qBt-ratioLimit": transmission_get_limit(resume_data, "ratio"),
        b"qBt-inactiveSeedingTimeLimit": int(
            transmission_get_limit(resume_data, "idle")
        ),
        b"qBt-savePath": resume_data[b"destination"],
    }

    if resume_data.get(b"incomplete-dir", b"") != b"":
        qbt_resume_data = resume_data[b"incomplete-dir"]

    if resume_data[b"paused"] == 1:
        qbt_resume_data[b"auto_managed"] = 0

    return qbt_resume_data


def copy_to_target(source_torrent_abs_path, qbt_bt_backup_dir, info_hash, resume_data):
    qbt_resume_data = map_resume_to_qbt(resume_data, info_hash)
    qbt_resume_path = os.path.join(qbt_bt_backup_dir, info_hash + ".fastresume")
    qbt_torrent_path = os.path.join(qbt_bt_backup_dir, info_hash + ".torrent")
    try:
        with open(qbt_resume_path, "wb") as resumf:
            resumf.write(bencodepy.bencode(qbt_resume_data))
        shutil.copy(source_torrent_abs_path, qbt_torrent_path)
        logging.info(f"Successfully imported torrent {info_hash}")

    except:
        logging.warning(
            f"Could not copy files for {info_hash} into {qbt_bt_backup_dir}"
        )
        rm_f(qbt_resume_path)
        rm_f(qbt_torrent_path)


def do_scan(transmission_config_dir, qbt_bt_backup_dir):
    transmission_resume_dir = os.path.join(transmission_config_dir, "resume")
    transmission_torrents_dir = os.path.join(transmission_config_dir, "torrents")
    torrent_file_rgx = re.compile("([0-9a-f]{40})\\.torrent")

    for root, dirs, files in os.walk(transmission_torrents_dir):
        for torf in files:
            match = torrent_file_rgx.fullmatch(torf)
            if match:
                info_hash = match[1]
                try:
                    with open(
                        os.path.join(transmission_resume_dir, info_hash + ".resume"),
                        "rb",
                    ) as resumf:
                        resume_data = bencodepy.bdecode(resumf.read())
                        copy_to_target(
                            os.path.join(transmission_torrents_dir, torf),
                            qbt_bt_backup_dir,
                            info_hash,
                            resume_data,
                        )

                except ConversionError as e:
                    logging.warning(
                        f"Error while converting resume data for {info_hash} : {str(e)}"
                    )
                except OSError:
                    logging.warning(
                        f"Failed to read resume file for {info_hash}, skipping"
                    )
                except (ValueError, bencodepy.BencodeDecodeError):
                    logging.warning(
                        f"Failed to parse resume file for {info_hash}, skipping"
                    )
            else:
                logging.warning(
                    f"Unknown file {torf} found in torrents directory, skipping"
                )


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
    args = parser.parse_args()
    do_scan(args.transmission_config_dir, args.qbt_bt_backup_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
