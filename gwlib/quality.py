# -*- coding: utf-8 -*-
"""音质策略：设备只解 MP3，所以播放档位映射为 MP3 码率；下载保留原始文件。"""
import re

DEFAULT_QUALITY = "standard"

# name -> 播放档位。lossless 只影响下载：播放仍转 320k MP3，下载保留源文件。
PROFILES = {
    "standard": {"name": "standard", "label": "标准", "bitrate": "128k", "sample_rate": 44100,
                 "channels": 2, "ext_quality": "mp3", "download_original": False},
    "high": {"name": "high", "label": "高", "bitrate": "192k", "sample_rate": 44100,
             "channels": 2, "ext_quality": "mp3", "download_original": False},
    "exhigh": {"name": "exhigh", "label": "极高", "bitrate": "320k", "sample_rate": 48000,
               "channels": 2, "ext_quality": "mp3", "download_original": False},
    "lossless": {"name": "lossless", "label": "无损（下载优先）", "bitrate": "320k",
                 "sample_rate": 48000, "channels": 2, "ext_quality": "lossless",
                 "download_original": True},
}

ALIASES = {
    "": DEFAULT_QUALITY, "default": DEFAULT_QUALITY, "auto": DEFAULT_QUALITY,
    "standard": "standard", "normal": "standard", "128": "standard", "128k": "standard",
    "标准": "standard", "普通": "standard", "低": "standard",
    "high": "high", "192": "high", "192k": "high", "高": "high", "较高": "high",
    "exhigh": "exhigh", "320": "exhigh", "320k": "exhigh", "极高": "exhigh",
    "超高": "exhigh", "最高": "exhigh",
    "lossless": "lossless", "flac": "lossless", "无损": "lossless",
    "hires": "lossless", "hi-res": "lossless",
}


def normalize_quality(value):
    raw = str(value or "").strip().lower()
    if raw in ALIASES:
        return ALIASES[raw]
    if re.fullmatch(r"\d{2,4}k?", raw):
        n = int(raw.rstrip("k"))
        if n <= 128:
            return "standard"
        if n <= 192:
            return "high"
        return "exhigh"
    return DEFAULT_QUALITY


def get_profile(value=None):
    name = normalize_quality(value)
    return dict(PROFILES[name])


def describe():
    return [dict(v) for v in PROFILES.values()]
