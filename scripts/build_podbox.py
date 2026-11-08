# SPDX-License-Identifier: GPL-2.0-only
"""Build pinned PodBox engines and host codecs, with an enforced local overlay.

Only operates on an explicitly supplied, disposable checkout. Never a player.
Requires make, Perl, a C compiler and SDL2's configure metadata (no SDL runtime).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = "f0850c4647e7c1cfde11ffc89ab40b55f8aa5635"
CODECS = ("flac", "wav", "mpa", "aac", "vorbis", "opus", "alac", "aiff", "wavpack")


def run(args, cwd):
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)


def replace(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"Pinned source patch does not match: {path.name}: {old[:65]}")
    path.write_text(text.replace(old, new))


def patch_source(source):
    filesystem = source / "uisimulator/common/filesystem-sim.c"
    replace(filesystem, "int sim_get_os_path(char *buffer, const char *path, size_t bufsize)",
            "static int rlm_original_os_path(char *buffer, const char *path, size_t bufsize)")
    replace(filesystem, "/** File functions **/",
            (ROOT / "scripts/native/podbox_overlay.c").read_text() + "\n/** File functions **/")
    replace(filesystem, "filestr->osfd = os_open(ospath, oflag | O_BINARY __OPEN_MODE_ARG);",
            "if ((oflag & (O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND)) && !rlm_can_write(ospath))\n"
            "        return -1;\n    filestr->osfd = os_open(ospath, oflag | O_BINARY __OPEN_MODE_ARG);")
    for call, guard in (("os_remove(ospath)", "rlm_can_write(ospath)"),
                        ("os_rename(osold, osnew)", "rlm_can_write(osold) && rlm_can_write(osnew)"),
                        ("os_mkdir(ospath __MKDIR_MODE_ARG)", "rlm_can_write(ospath)"),
                        ("os_rmdir(ospath)", "rlm_can_write(ospath)")):
        replace(filesystem, f"int rc = {call};", f"if (!({guard})) return -1;\n    int rc = {call};")
    replace(filesystem, "return os_modtime(ospath, modtime);",
            "if (!rlm_can_write(ospath)) return -1;\n    return os_modtime(ospath, modtime);")
    replace(filesystem, "    OS_DIRENT_T *osdirent = os_readdir(dirstr->osdirp);",
            "rlm_read_next: ;\n    OS_DIRENT_T *osdirent = os_readdir(dirstr->osdirp);")
    replace(filesystem, "    entry->info.osdirent = osdirent;",
            '''    /* Host OS metadata and this application's private transaction
     * directories are not player media. Preserve ordinary hidden music. */
    if (!strcasecmp(entry->d_name, ".Trashes") ||
        !strcasecmp(entry->d_name, ".Spotlight-V100") ||
        !strcasecmp(entry->d_name, ".fseventsd") ||
        !strcasecmp(entry->d_name, "System Volume Information") ||
        !strcasecmp(entry->d_name, "$RECYCLE.BIN") ||
        !strncmp(entry->d_name, ".rlm-library-", 13))
        goto rlm_read_next;
    entry->info.osdirent = osdirent;''')
    replace(filesystem, "info.mtime = mktime(&tm);",
            '/* RLM: explicit FAT mount offset, frozen by the preview. */\n'
            '    const char *offset = getenv("RLM_PODBOX_FAT_OFFSET_SECONDS");\n'
            '    info.mtime = offset ? s.st_mtime + strtol(offset, NULL, 10) : mktime(&tm);')
    # The two implementations are identical. Keep the metadata copy for soundscan,
    # rename only strutil's copy and its local callers for database/artcache.
    p = source / "apps-ipod/system/strutil.c"
    p.write_text(p.read_text().replace("string_option(", "rlm_strutil_string_option("))
    # Exercise the actual linked filesystem adapter on temporary fixtures in CI.
    probe = '''
    if (argc == 3 && !strcmp(argv[1], "--rlm-overlay-check"))
    {
        sim_root_dir = argv[2];
        int fd = open("/Music/sentinel", O_RDONLY);
        if (fd < 0) return 80;
        close(fd);
        if (open("/Music/forbidden", O_WRONLY|O_CREAT|O_TRUNC, 0666) >= 0) return 81;
        if (remove("/Music/sentinel") == 0) return 82;
        if (rename("/Music/sentinel", "/Music/renamed") == 0) return 83;
        if (mkdir("/Music/forbidden-dir") == 0) return 84;
        if (rmdir("/Music/empty") == 0) return 85;
        if (open("/.rockbox/library/../../Music/forbidden", O_WRONLY|O_CREAT, 0666) >= 0) return 86;
        if (open("/<HDD1>/forbidden", O_WRONLY|O_CREAT, 0666) >= 0) return 87;
        fd = open(LIB_DIR "/overlay-probe", O_WRONLY|O_CREAT|O_TRUNC, 0666);
        if (fd < 0) return 88;
        if (write(fd, "ok", 2) != 2) return 89;
        close(fd);
        return 0;
    }
'''
    replace(source / "tools/database_pb/database_pb.c", "    init_volume_names();", "    init_volume_names();\n" + probe)


def build(source: Path, destination: Path, *, prepared=False):
    if not prepared:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        if revision != REVISION:
            raise RuntimeError(f"Expected PodBox {REVISION}, found {revision}.")
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).strip():
            raise RuntimeError("Use a clean, disposable pinned checkout; build applies host adapters to it.")
    windows = os.name == "nt" or platform.system().startswith(("MINGW", "MSYS"))
    mac = sys.platform == "darwin"
    if mac:
        os.environ.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0" if platform.machine() == "arm64" else "10.13")
    builddir = source / "build-sim-ipodvideo"
    builddir.mkdir(exist_ok=prepared)
    run(["bash", "../tools/configure", "--target=ipodvideo", "--type=s", "--appsdir=apps-ipod"], builddir)
    if not prepared:
        patch_source(source)
    autoconf = builddir / "autoconf.h"
    s = autoconf.read_text()
    # No simulator threads are run; SDL's read/write scheduling shim is unwanted.
    import re
    s = re.sub(r"^#define HAVE_(?:SDL_THREADS|UNIXCONTEXT|WIN32_FIBER).*$", "", s, flags=re.M)
    s = re.sub(r"^#define GCCNUM\s*$", "#define GCCNUM 0", s, flags=re.M)
    autoconf.write_text(s)
    makefile = builddir / "Makefile"
    s = makefile.read_text()
    if mac:
        s = s.replace("export CC=gcc-16", "export CC=clang").replace("export CPP=cpp-16", "export CPP=clang -E")
        s = s.replace("export AR=gcc-ar-16", "export AR=ar").replace("export RANLIB=gcc-ranlib", "export RANLIB=ranlib")
        s = s.replace("-funit-at-a-time", "-D_FORTIFY_SOURCE=0 -Wno-implicit-function-declaration")
        p = source / "lib/rbcodec/codecs/codecs.make"
        p.write_text(p.read_text().replace("-lgcc ", ""))
    if windows:
        # Host codecs and soundscan must share a static CRT, as upstream requires.
        s = s.replace("export GLOBAL_LDOPTS=", "export GLOBAL_LDOPTS=-static ")
    s += ("\n.PHONY: rlm-headers rlm-codecs\n"
          "rlm-headers: $(BUILDDIR)/lang/lang.h $(APPSBUILDDIR)/bitmaps/native/podboxnoart.c\n"
          "rlm-codecs: " + " ".join(f"$(RBCODEC_BLD)/codecs/{c}.codec" for c in CODECS) + "\n")
    makefile.write_text(s)
    run(["make", "-j4", "rlm-headers", "rlm-codecs"], builddir)
    destination.mkdir(parents=True, exist_ok=True)
    compiler = "clang" if mac else "gcc"
    opt = "-O2 -DDEFAULT_STACK_SIZE=16384" + (" -D_FORTIFY_SOURCE=0" if mac else "")
    for name in ("database_pb", "artcache_pb", "soundscan"):
        args = ["make", "-C", f"tools/{name}", "win" if windows else "native", f"OPT={opt}"]
        if windows:
            args += ["WINCC=gcc", "SIMDIR=../../build-sim-ipodvideo"]
        else:
            args += [f"CC={compiler}"]
        if mac and name != "soundscan":
            args += ["LDFLAGS="]
        run(args, source)
        binary = source / "tools" / name / (name + (".exe" if windows else ""))
        shutil.copy2(binary, destination / binary.name)
    codecs = destination / "codecs"
    codecs.mkdir(exist_ok=True)
    for name in CODECS:
        shutil.copy2(builddir / f"lib/rbcodec/codecs/{name}.codec", codecs / f"{name}.codec")
    (destination / "engine.json").write_text(json.dumps({"schema": 1, "revision": REVISION,
                "adapter": 1, "library_format": 5, "art_format": 8, "codecs": CODECS}, indent=2))
    # Corresponding source accompanies every binary distribution: original
    # notices, our exact modified C, generated headers and reproducible recipe.
    trees = {"apps-ipod", "firmware", "lib", "tools", "uisimulator"}
    if prepared and not (source / ".git").exists():
        names = [p.relative_to(source).as_posix() for tree in trees for p in (source / tree).rglob("*") if p.is_file()]
    else:
        names = subprocess.check_output(["git", "ls-files", "-z"], cwd=source).decode().split("\0")
    with tarfile.open(destination / "source.tar.gz", "w:gz") as archive:
        for name in names:
            if name and (name.split("/")[0] in trees or name in {"docs/COPYING", "docs/CREDITS", "README.md"}):
                if prepared:
                    if Path(name).suffix in (".o", ".a", ".codec", ".elf", ".exe", ".map", ".d"):
                        continue
                    with (source / name).open("rb") as fp:
                        if fp.read(4) in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                            continue
                archive.add(source / name, arcname="podbox/" + name, recursive=False)
        for directory in (builddir / "lang", builddir / "apps-ipod/bitmaps"):
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix in (".h", ".c"):
                    archive.add(path, arcname="podbox/" + path.relative_to(source).as_posix())
        archive.add(autoconf, arcname="podbox/build-sim-ipodvideo/autoconf.h")
        archive.add(makefile, arcname="podbox/build-sim-ipodvideo/Makefile.reference")
        archive.add(Path(__file__), arcname="rlm/scripts/build_podbox.py")
        archive.add(ROOT / "scripts/native/podbox_overlay.c", arcname="rlm/scripts/native/podbox_overlay.c")
    print(f"Built PodBox engines and {len(CODECS)} matching host codecs in {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Clean disposable PodBox checkout at the pinned revision")
    parser.add_argument("--destination", type=Path, default=ROOT / "build/podbox")
    parser.add_argument("--prepared", action="store_true", help="Rebuild the already adapted corresponding-source archive")
    args = parser.parse_args()
    build(args.source.resolve(), args.destination.resolve(), prepared=args.prepared)
