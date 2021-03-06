"""
Copyright 2018 6x68mx <6x68mx@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from pipeline import Pipeline, run_pipelines, PipelineError
import formats

import mutagen.flac
import mutagen.mp3
from mutagen.easyid3 import EasyID3

import subprocess
import os
import signal
import shutil

ALLOWED_EXTENSIONS = (
    ".cue",
    ".gif",
    ".jpeg",
    ".jpg",
    ".log",
    ".md5",
    ".nfo",
    ".pdf",
    ".png",
    ".sfv",
    ".txt",
)

REQUIRED_TAGS = (
    "title",
    "tracknumber",
    "artist",
    "album",
)

def check_tags(files):
    """
    Check if files containe all required tags.

    :param files: A `list` of `mutagen.FileType` objects.

    :returns: `True` if all files contain the required tags, `False` if not.
    """
    for f in files:
        if any(tag not in f or f[tag] == [""] for tag in REQUIRED_TAGS):
            return False
    return True

def check_flacs(flacs):
    """
    Check if the given flacs are suitable for transcoding.

    :param flacs: A `list` of `mutagen.flac.FLAC` objects.

    :returns: A string containing a description of the problem if there was
              one, or `None` if no problems were detected.
    """
    if any(flac.info.channels > 2 for flac in flacs):
        return "More than 2 channels are not supported."

    bits = flacs[0].info.bits_per_sample
    rate = flacs[0].info.sample_rate
    if any((flac.info.bits_per_sample != bits
            or flac.info.sample_rate != rate)
            for flac in flacs):
        return "Inconsistent sample rate or bit depth"

    try:
        compute_resample(flacs[0])
    except TranscodeError as e:
        return str(e)

    if not check_tags(flacs):
        return "One or more required tags are missing."

    return None

def compute_resample(flac):
    """
    Check if resampling is required and compute the target rate.

    Resampling is required if `flac` has a bit depth > 16 or
    a sample rate that's not ether 44.1 or 48kHz.

    :param flac: A `mutagen.flac.FLAC` object.
    :returns: The target rate or `None` in case no resampling is needed.
    :raises TranscodeError: If `flac` has a sample rate that is not a
                            multiple of ether 44.1 or 48kHz
    """
    bits = flac.info.bits_per_sample
    rate = flac.info.sample_rate
    if bits > 16 or not (rate == 44100 or rate == 48000):
        if rate % 44100 == 0:
            return 44100
        elif rate % 48000 == 0:
            return 48000
        else:
            raise TranscodeError("Unsupported Rate: {}Hz. Only multiples of 44.1 or 48 kHz are supported".format(rate))
    else:
        return None

def generate_transcode_cmds(src, dst, target_format, resample=None):
    cmds = []
    if resample is not None:
        cmds.append(["sox", src, "-G", "-b", "16", "-t", "wav", "-", "rate", "-v", "-L", str(resample), "dither"])
    else:
        cmds.append(["flac", "-dcs", "--", src])

    cmds.append(target_format.encode_cmd(dst))

    return cmds

def copy_files(src_dir, dst_dir, suffixes=None):
    """
    Recursively copy files from `src_dir` to `dst_dir`.

    :param src_dir: Path like object to the source directory.
    :param dst_dir: Path like object to the destination directory.
    :param suffixes: Ether `None`, in this case all files will be copied
                     or a `set` of suffixes in which case only files with one
                     of those suffixes will be copied.
    """
    if not dst_dir.is_dir():
        return

    dirs = [src_dir]
    while dirs:
        for x in dirs.pop().iterdir():
            if x.is_dir():
                dirs.append(x)
            elif suffixes is None or x.suffix in suffixes:
                d = dst_dir / x.relative_to(src_dir)
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(x, d)
                try:
                    shutil.copystat(x, d)
                except PermissionError:
                    # copystat sometimes failes even if copyfile worked
                    # happens mainly with some special filesystems (cifs/samba, ...)
                    # or strange permissions.
                    # Not really a big problem, let's just emit a warning.
                    print("Waring: No permission to write file metadata to {}".format(d))

def copy_tags(src, dst):
    """
    Copy all tags from `src` to `dst` and saves `dst`.

    Both `src` and `dst` must be `mutagen.FileType` objects.
    """
    if type(dst) == mutagen.mp3.EasyMP3:
        valid_tag_fn = lambda k: k in EasyID3.valid_keys.keys()
    else:
        valid_tag_fn = lambda k: True

    for tag in filter(valid_tag_fn, src):
        value = src[tag]
        if value != "":
            dst[tag] = value
    dst.save()

class TranscodeError(Exception):
    pass

def transcode(src, dst, target_format, njobs=None):
    """
    Transcode a release.

    Transcodes all FLAC files in a directory and copies other files.

    :param src: Path object to the source directory
    :param dst: Path object to the target directory.
                This directory must not yet exist but it's parent must exist.
    :param target_format: The format to which FLAC files in `src` should be
                          transcoded. See `formats.py`.
    :param njobs: Number of transcodes to run in parallel. If `None` it will
                  default to the number of available CPU cores.

    :raises TranscodeError:
    """
    if dst.exists():
        raise TranscodeError("Destination directory ({}) allready exists".format(dst))
    if not dst.parent.is_dir():
        raise TranscodeError("Parent of destination ({}) does not exist or isn't a directory".format(dst.parent))

    files = list(src.glob("**/*" + formats.FormatFlac.SUFFIX))
    transcoded_files = [dst / f.relative_to(src).with_suffix(target_format.SUFFIX) for f in files]
    
    flacs = [mutagen.flac.FLAC(f) for f in files]

    msg = check_flacs(flacs)
    if msg is not None:
        raise TranscodeError(msg)

    resample = compute_resample(flacs[0])

    try:
        dst.mkdir()
    except PermissionError:
        raise TranscodeError("You do not have permission to write to the destination directory ({})".format(dst))

    jobs = []
    for f_src, f_dst in zip(files, transcoded_files):
        f_dst.parent.mkdir(parents=True, exist_ok=True)
        cmds = generate_transcode_cmds(
            f_src,
            f_dst,
            target_format,
            resample)
        jobs.append(Pipeline(cmds))

    try:
        run_pipelines(jobs)

        for flac, transcode in zip(flacs, transcoded_files):
            copy_tags(flac, mutagen.mp3.EasyMP3(transcode))

        copy_files(src, dst, ALLOWED_EXTENSIONS)
    except PipelineError as e:
        shutil.rmtree(dst)
        raise TranscodeError("Transcode failed: " + str(e))
    except:
        shutil.rmtree(dst)
        raise



# EasyID3 extensions:

for key, frameid in {
            "albumartist": "TPE2",
            "album artist": "TPE2",
            "grouping": "TIT1",
            "content group": "TIT1",
        }.items():
    EasyID3.RegisterTextKey(key, frameid)

def comment_get(id3, _):
    return [comment.text for comment in id3["COMM"].text]

def comment_set(id3, _, value):
    id3.add(mutagen.id3.COMM(encoding=3, lang="eng", desc="", text=value))

def originaldate_get(id3, _):
    return [stamp.text for stamp in id3["TDOR"].text]

def originaldate_set(id3, _, value):
    id3.add(mutagen.id3.TDOR(encoding=3, text=value))

EasyID3.RegisterKey("comment", comment_get, comment_set)
EasyID3.RegisterKey("description", comment_get, comment_set)
EasyID3.RegisterKey("originaldate", originaldate_get, originaldate_set)
EasyID3.RegisterKey("original release date", originaldate_get, originaldate_set)
