"""Auto-updater for official python.org builds of python."""

import collections
from os import makedirs, rename, rmdir, unlink
from os.path import expanduser
from os.path import join as pathjoin
from platform import mac_ver
from plistlib import dumps as dumpplist
from plistlib import loads as loadplist
from re import compile as compile_re
from subprocess import PIPE, run  # noqa: S404
from sys import version_info
from tempfile import NamedTemporaryFile
from typing import Dict, Iterable, List, Match, Pattern, Tuple
from uuid import uuid4

import html5lib
import requests
from hyperlink import DecodedURL
from packaging.version import Version, parse
from rich.progress import Progress


def alllinksin(
    u: DecodedURL, e: Pattern[str]
) -> Iterable[Tuple[Match[str], DecodedURL]]:
    """Get all the links in the given URL whose text matches the given pattern."""
    for a in html5lib.parse(
        requests.get(u.to_text(), timeout=30).text, namespaceHTMLElements=False
    ).findall(".//a"):
        match = e.fullmatch(a.text or "")
        if match is not None:
            yield match, u.click(a.attrib["href"])


def choicechanges(pkgfile: str) -> str:
    """
    Compute the choice-changes XML for a given package based on what is
    currently installed.
    """

    all_installed = set(
        run(  # noqa: S603
            [
                "/usr/sbin/pkngutil",
                "--pkgs",
            ],
            stdout=PIPE,
        )
        .stdout.decode()
        .split("\n")
    )
    dicts = loadplist(
        run(  # noqa: S603
            [
                "/usr/sbin/installer",
                "-showChoiceChangesXML",
                "-pkg",
                pkgfile,
            ],
            stdout=PIPE,
        ).stdout
    )
    for each in dicts:
        if each["choiceAttribute"] == "selected":
            choice_id = each["choiceIdentifier"]
            setting = int(choice_id in all_installed)
            if setting:
                print("selecting choice", each["choiceIdentifier"])
                each["attributeSetting"] = setting
    return dumpplist(dicts).decode()


def main(interactive: bool, force: bool, minor_upgrade: bool, dry_run: bool) -> None:
    """Do an update."""
    this_mac_ver = tuple(map(int, mac_ver()[0].split(".")[:2]))
    ver = compile_re(r"(\d+)\.(\d+).(\d+)/")
    macpkg = compile_re(r"python-(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)-macosx?(\d+).pkg")

    thismajor, thisminor, thismicro, releaselevel, serial = version_info
    level = {
        "alpha": "a",
        "beta": "b",
        "candidate": "rc",
        "final": "",
    }[releaselevel]

    thispkgver = Version(
        f"{thismajor}.{thisminor}.{thismicro}" + (f".{level}{serial}" if level else "")
    )

    # {macos, major, minor: [(Version, URL)]}
    # major, minor, micro, macos: [(version, URL)]
    versions: Dict[
        int, Dict[int, Dict[int, Dict[str, List[Tuple[Version, DecodedURL]]]]]
    ] = collections.defaultdict(
        lambda: collections.defaultdict(
            lambda: collections.defaultdict(lambda: collections.defaultdict(list))
        )
    )

    baseurl = DecodedURL.from_text("https://www.python.org/ftp/python/")

    for eachver, suburl in alllinksin(baseurl, ver):
        major, minor, micro = map(int, eachver.groups())
        if major != thismajor:
            continue
        if minor != thisminor and not minor_upgrade:
            continue
        for eachmac, pkgdl in alllinksin(suburl, macpkg):
            pyver, macver = eachmac.groups()
            fullversion = parse(pyver)
            if fullversion.pre and not thispkgver.pre:
                continue
            if (
                fullversion.major,
                fullversion.minor,
                fullversion.micro,
            ) == (
                major,
                minor,
                micro,
            ):
                versions[major][minor][micro][macver].append((fullversion, pkgdl))

    newminor = max(versions[thismajor].keys())
    newmicro = max(versions[thismajor][newminor].keys())
    available_mac_vers = versions[thismajor][newminor][newmicro].keys()
    best_available_mac = max(
        available_mac_ver
        for available_mac_ver in available_mac_vers
        if this_mac_ver >= tuple(int(x) for x in available_mac_ver.split("."))
    )

    download_urls = versions[thismajor][newminor][newmicro][best_available_mac]

    best_ver, download_url = sorted(download_urls, reverse=True)[0]

    # TODO: somehow flake8 in pre-commit thinks that this semicolon is in the
    # *code* and not in a string.
    print(f"this version: {thispkgver}; new version: {best_ver}")  # noqa
    update_needed = best_ver > thispkgver

    print(
        "update",
        "needed" if update_needed else "not needed",
        "from",
        download_url,
    )

    if dry_run or not (update_needed or force):
        return

    finalname = do_download(download_url)
    with NamedTemporaryFile(mode="w", suffix=".plist") as tf:
        if interactive:
            argv = ["/usr/bin/open", "-b", "com.apple.installer", finalname]
        else:
            tf.write(choicechanges(finalname))
            tf.flush()
            print("Enter your administrative password to run the update:")
            argv = [
                "/usr/bin/sudo",
                "/usr/sbin/installer",
                "-applyChoiceChangesXML",
                tf.name,
                "-pkg",
                finalname,
                "-target",
                "/",
            ]
        run(argv)  # noqa: S603
    print("Complete.")


def do_download(download_url: DecodedURL) -> str:
    """
    Download the given URL into the downloads directory.

    Returning the path when successful.
    """
    basename = download_url.path[-1]
    partial = basename + ".mopup-partial"
    downloads_dir = expanduser("~/Downloads/")
    partialdir = pathjoin(downloads_dir, partial)
    contentname = pathjoin(partialdir, f"{uuid4()}.content")
    finalname = pathjoin(downloads_dir, basename)

    with requests.get(
        download_url.to_uri().to_text(), stream=True, timeout=30
    ) as response:
        response.raise_for_status()
        try:
            makedirs(partialdir, exist_ok=True)
            total_size = int(response.headers["content-length"])
            with open(contentname, "wb") as f:
                with Progress() as progress:
                    task = progress.add_task(
                        f"Downloading {basename}...", total=total_size
                    )
                    for chunk in response.iter_content(chunk_size=8192):
                        progress.update(task, advance=len(chunk))
                        f.write(chunk)
            print(".")
            rename(contentname, finalname)
        except BaseException:
            unlink(contentname)
            rmdir(partialdir)
            raise
        else:
            rmdir(partialdir)
            return finalname
